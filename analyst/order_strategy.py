"""
analyst/order_strategy.py — 注文方法 (order_type / limit_price / expiry) だけを
最新の市場価格に基づいて軽量に再評価するモジュール。

全 AI 総合分析 (run_analysis) は ~5 分かかるが、こちらは:
  - 既存 ai_portfolio_analysis.json の priority_actions を読み込み
  - 各 ticker の現在価格 / VIX / ATR を yfinance で取得 (並列)
  - Sonnet を 1 回呼んで全 actions の order_type / limit_price / expiry_minutes / execution_reason を更新
  - synthesis.priority_actions を in-place 更新して保存

ユースケース: 相場が朝/昼/夜で動いたとき、AI の order_strategy だけを最新化したい。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = Path(__file__).parent.parent
CACHE_PATH = BASE_DIR / "ai_portfolio_analysis.json"

_running = False


def is_running() -> bool:
    return _running


def _get_current_price_atr(ticker: str) -> dict:
    """yfinance で現在価格と ATR(14d) を取得。失敗時は空 dict。"""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="30d")
        if hist.empty:
            return {}
        price = float(hist["Close"].iloc[-1])
        # ATR(14d) 近似: (High-Low) の 14 日 EMA
        hl = (hist["High"] - hist["Low"]).tail(14)
        atr14 = float(hl.mean()) if not hl.empty else 0.0
        # Bid-Ask spread bps 近似 (取得困難なので tier 別固定)
        info = getattr(t, "fast_info", {}) or {}
        last_volume = float(info.get("last_volume") or 0)
        avg_volume = float(info.get("ten_day_average_volume") or 0)
        bid = float(info.get("bid") or 0) or None
        ask = float(info.get("ask") or 0) or None
        spread_bps = None
        if bid and ask and ask >= bid:
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000 if mid > 0 else None
        return {
            "current_price": round(price, 2),
            "atr_14d": round(atr14, 2),
            "atr_pct": round(atr14 / price * 100, 2) if price else 0.0,
            "last_volume": last_volume,
            "avg_volume": avg_volume,
            "bid": bid,
            "ask": ask,
            "spread_bps": round(spread_bps, 1) if spread_bps is not None else None,
        }
    except Exception:
        return {}


def _get_market_meta() -> dict:
    """vix_state.json / macro_state.json から最新スナップショット。"""
    out: dict = {}
    try:
        v = json.loads((BASE_DIR / "vix_state.json").read_text(encoding="utf-8"))
        vb = v.get("vix") if isinstance(v.get("vix"), dict) else None
        if vb:
            out["vix"] = vb.get("level")
            out["vix_classification"] = vb.get("classification")
        else:
            out["vix"] = v.get("vix") or v.get("level")
    except Exception:
        pass
    try:
        m = json.loads((BASE_DIR / "macro_state.json").read_text(encoding="utf-8"))
        out["us10y"] = (m.get("yield_10y") or {}).get("value") if isinstance(m.get("yield_10y"), dict) else m.get("yield_10y")
        out["yield_inverted"] = m.get("yield_inverted")
    except Exception:
        pass
    return out


def _build_prompt(actions: list[dict], price_map: dict[str, dict], mm: dict) -> tuple[str, str]:
    system = (
        "あなたは執行アナリストです。\n"
        "下記の各アクションについて、現在の市場価格・VIX・ATR を踏まえて、"
        "実際にユーザーが broker に発注する際の **最適な注文方式（成行/指値/逆指値）と価格**を返してください。\n\n"
        "判断ロジック:\n"
        "- VIX < 20 で板厚い銘柄（メガキャップ） → 指値推奨 (現値 ± 0.3〜0.5 × ATR_pct)\n"
        "- VIX 20-30 中ボラ → 指値 (現値 ± 0.5〜0.8 × ATR_pct)\n"
        "- VIX > 30、低流動性、spread>30bps、quote欠落は成行理由にしない → 指値または見送り\n"
        "- urgency=high の緊急リスク削減でも、成行はspread<=30bpsかつbid/ask確認済みに限定\n"
        "- urgency=low → 指値で攻める (やや有利な価格)\n"
        "- 日本株 (.T) の通常単元注文は100株単位で指値推奨が基本。execution_channel が rakuten_kabu_mini_* の現物買いは1株単位可で、原則は寄付取引を優先\n"
        "- 投信 (SLIM_*, IFREE_*, MNXACT, NOMURA_*) は約定価格指定不可 → order_type=\"market\" 固定\n"
        "- 米株: 楽天証券は端株不可なので必ず整数株\n\n"
        "**stop_loss / 逆指値の推奨は禁止** (システムで除去対象なので order_type=\"stop_limit\" は出さない)。\n\n"
        "出力は JSON 1 オブジェクト:\n"
        "{\n"
        '  "orders": [\n'
        '    {"ticker": "META", "order_type": "limit", "limit_price": 605.0, "expiry_minutes": 240, "decision_price": 608.5, "execution_reason": "VIX18 強気でATR 1.4%、現値$608.5から-0.6%下の指値$605で攻める"},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "アクションリストの順番と同じ順で orders を返してください。"
    )

    lines = ["## 現在の市場環境"]
    if mm.get("vix") is not None:
        lines.append(f"VIX: {mm['vix']} ({mm.get('vix_classification','?')})")
    if mm.get("us10y") is not None:
        lines.append(f"米10年金利: {mm['us10y']}%")
    if mm.get("yield_inverted"):
        lines.append("⚠️ 逆イールド継続中")
    lines.append("")
    lines.append("## 注文方法を決めるアクション一覧")
    for i, a in enumerate(actions):
        tk = a.get("ticker", "?")
        info = price_map.get(tk, {})
        lines.append(
            f"{i+1}. [{a.get('tier','?')}] {a.get('type','?')} {tk} "
            f"urgency={a.get('urgency','?')} amount={a.get('amount_hint','?')}\n"
            f"   action: {(a.get('action') or '')[:100]}\n"
            f"   current_price={info.get('current_price','?')} ATR%={info.get('atr_pct','?')} "
            f"spread_bps={info.get('spread_bps','?')} bid={info.get('bid','?')} ask={info.get('ask','?')}"
        )
    user = "\n".join(lines)
    return system, user


def re_evaluate(send_telegram: bool = False) -> dict:
    """既存 priority_actions の order_type/limit_price/expiry/decision_price/execution_reason を更新。
    Returns: {"updated": N, "skipped": [..], "as_of": "..."}
    """
    global _running
    _running = True
    try:
        if not CACHE_PATH.exists():
            return {"error": "ai_portfolio_analysis.json が存在しません。先に AI 総合分析を実行してください。"}
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        syn = data.get("synthesis") or {}
        actions: list = syn.get("priority_actions") or []
        if not actions:
            # filtered_actions に何件あるかも教える（post-filter で除去された場合）
            n_filtered = len(syn.get("_filtered_actions") or [])
            if n_filtered > 0:
                return {
                    "status": "no_actions",
                    "message": (
                        f"現在実行すべきアクションはありません（priority_actions=0件）。"
                        f"AI 提案 {n_filtered} 件は全て post-filter で除去されました "
                        f"（cooldown / 既に実行済み / 金額過小 / 積立対象 等）。"
                        f"新しいアクションが出るのを待つか、「AI 総合分析」を再実行してください。"
                    ),
                    "updated": 0,
                    "filtered_count": n_filtered,
                }
            return {
                "status": "no_cache",
                "message": "priority_actions が空です。先に「AI 総合分析」を実行してください。",
                "updated": 0,
            }

        # 価格情報を並列取得
        tickers = [a.get("ticker") for a in actions if a.get("ticker")]
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(tickers)))) as ex:
            results = list(ex.map(_get_current_price_atr, tickers))
        price_map: dict[str, dict] = {tk: r for tk, r in zip(tickers, results) if r}

        mm = _get_market_meta()

        # 投信は自動で market 固定 (LLM 呼び出し対象から外す)
        FUND_PREFIXES = ("SLIM_", "IFREE_", "MNXACT", "NOMURA_")
        skipped: list = []
        llm_actions: list = []
        llm_indices: list = []
        for i, a in enumerate(actions):
            tk = a.get("ticker") or ""
            if any(tk.startswith(p) for p in FUND_PREFIXES) or tk in ("MNXACT",):
                a["order_type"] = "market"
                a.pop("limit_price", None)
                a["execution_reason"] = "投信は約定価格指定不可 → 成行固定"
                a["expiry_minutes"] = None
                skipped.append({"ticker": tk, "reason": "fund_auto_market"})
                continue
            llm_actions.append(a)
            llm_indices.append(i)

        if llm_actions:
            try:
                from analyst.llm_client import call_claude
                system, user = _build_prompt(llm_actions, price_map, mm)
                resp = call_claude(
                    system=system, user=user,
                    model="claude-sonnet-5",
                    max_tokens=3000, temperature=0.2, use_tool=False,
                )
                raw = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
                import re
                m = re.search(r"\{[\s\S]*\}", raw)
                if m:
                    parsed = json.loads(m.group(0))
                    orders = parsed.get("orders", [])
                    for j, order in enumerate(orders):
                        if j >= len(llm_indices):
                            break
                        idx = llm_indices[j]
                        a = actions[idx]
                        # 上書き対象フィールド
                        if order.get("order_type") in ("market", "limit"):
                            a["order_type"] = order["order_type"]
                        if "limit_price" in order:
                            lp = order["limit_price"]
                            if lp is None or a.get("order_type") == "market":
                                a.pop("limit_price", None)
                            else:
                                try:
                                    a["limit_price"] = float(lp)
                                except Exception:
                                    pass
                        if "decision_price" in order:
                            try:
                                a["decision_price"] = float(order["decision_price"])
                            except Exception:
                                pass
                        if "expiry_minutes" in order:
                            try:
                                a["expiry_minutes"] = int(order["expiry_minutes"])
                            except Exception:
                                pass
                        if order.get("execution_reason"):
                            a["execution_reason"] = order["execution_reason"]
            except Exception as e:
                return {
                    "error": f"LLM 呼び出し失敗: {type(e).__name__}: {e}",
                    "updated": 0,
                    "skipped": skipped,
                }

        # Deterministic safety pass.  LLM output may explain a decision but it
        # cannot waive spread/urgency/quote requirements.
        for action in actions:
            ticker = str(action.get("ticker") or "")
            if any(ticker.startswith(p) for p in FUND_PREFIXES):
                continue
            info = price_map.get(ticker) or {}
            for key in ("spread_bps", "bid", "ask"):
                if info.get(key) is not None:
                    action[{"bid": "quote_bid", "ask": "quote_ask"}.get(key, key)] = info.get(key)
            if action.get("decision_price") is None and info.get("current_price") is not None:
                action["decision_price"] = info.get("current_price")
            if str(action.get("order_type") or "").lower() != "market":
                continue
            urgency = str(action.get("urgency") or "medium").lower()
            spread = info.get("spread_bps")
            valid_market = (
                urgency == "high"
                and spread is not None
                and float(spread) <= 30
                and info.get("bid") is not None
                and info.get("ask") is not None
            )
            if valid_market:
                continue
            current = info.get("current_price")
            if current is not None:
                action["order_type"] = "limit"
                action["limit_price"] = float(current)
                action["execution_reason"] = (
                    f"安全ゲート: urgency={urgency}, spread={spread}bps のため成行を禁止し現値基準の指値へ変更。"
                )
            else:
                action["no_trade_zone"] = True
                action["skip_reason"] = "成行に必要なcurrent price/bid/ask/spreadを確認できない"
                action.pop("order_type", None)
                action.pop("limit_price", None)

        # Order quality changed, so the execution gate must be recomputed in
        # the same write.  Otherwise a newly created no-trade zone could retain
        # an old `ready` flag and reappear on Today's execution board.
        try:
            from execution_readiness import apply_execution_readiness
            apply_execution_readiness(actions, base_dir=CACHE_PATH.parent)
        except Exception as exc:
            for action in actions:
                action["execution_readiness"] = "review"
                action.setdefault("execution_block_reasons", []).append({
                    "code": "execution_readiness_refresh_error",
                    "message": f"注文方式更新後の実行可否判定に失敗: {type(exc).__name__}: {str(exc)[:160]}",
                })

        # synthesis の order_strategy_refreshed_at を更新
        syn["order_strategy_refreshed_at"] = datetime.now().isoformat(timespec="minutes")
        syn["priority_actions"] = actions
        data["synthesis"] = syn

        # atomic write
        try:
            from utils import atomic_write_json
            atomic_write_json(CACHE_PATH, data)
        except Exception:
            CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Telegram 通知 (任意)
        # ALMANAC: telegram disabled — ai_analysis only
        if False and send_telegram and len(llm_actions) > 0:
            try:
                _notify_telegram(actions[:10], mm)
            except Exception:
                pass

        return {
            "status":            "ok",
            "updated":           len(llm_actions),
            "skipped":           skipped,
            "market_context":    mm,
            "refreshed_at":      syn["order_strategy_refreshed_at"],
            "actions_preview":   [
                {"ticker": a.get("ticker"), "order_type": a.get("order_type"),
                 "limit_price": a.get("limit_price"), "decision_price": a.get("decision_price"),
                 "execution_reason": a.get("execution_reason")}
                for a in actions[:10]
            ],
        }
    finally:
        _running = False


def _notify_telegram(actions: list[dict], mm: dict) -> bool:
    import os
    import requests
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    def _fmt_price(value, ticker: str = "") -> str:
        if value in (None, ""):
            return ""
        try:
            price = float(value)
        except (TypeError, ValueError):
            return str(value)
        if ticker.endswith(".T"):
            return f"¥{price:,.0f}"
        if abs(price) >= 100:
            return f"${price:,.2f}"
        return f"${price:.2f}"

    def _fmt_order(a: dict) -> str:
        tk = a.get("ticker", "?")
        ot = str(a.get("order_type") or "?").lower()
        label = {"market": "成行", "limit": "指値", "stop": "逆指値", "stop_limit": "逆指値"}.get(ot, ot)
        parts = [label]
        if ot != "market" and a.get("limit_price") is not None:
            parts.append(_fmt_price(a.get("limit_price"), tk))
        if a.get("expiry_minutes") not in (None, ""):
            parts.append(f"有効{a.get('expiry_minutes')}分")
        if a.get("decision_price") not in (None, ""):
            parts.append(f"判断値{_fmt_price(a.get('decision_price'), tk)}")
        return " / ".join(p for p in parts if p)

    lines = [f"📋 *ALMANAC 注文方法更新* ({datetime.now().strftime('%m/%d %H:%M')})"]
    if mm.get("vix"):
        lines.append(f"市場: VIX {mm['vix']} ({mm.get('vix_classification','?')})")
    lines.append("")
    for a in actions[:8]:
        tk = a.get("ticker", "?")
        ot = str(a.get("order_type") or "").lower()
        emoji = "🟢" if ot == "market" else "🔵"
        line = f"{emoji} {tk}: {_fmt_order(a)}"
        reason = str(a.get("execution_reason") or "").strip()
        if reason:
            line += f"\n   {reason[:120]}"
        lines.append(line)
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"},
            timeout=10,
        )
        return True
    except Exception:
        return False
