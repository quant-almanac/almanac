"""
drawdown_dca_engine.py — ALMANAC Bottom-Fishing Ladder DCA

「弱気で下落し切る少し前のタイミングから買い増しておかないと
 反転急上昇時に利益を取り逃す」をシステム化するエンジン。

設計:
    - 3 つの Tranche (T1/T2/T3) を複合シグナルで評価
    - VIX ピーク減衰 + Portfolio DD + Fear&Greed + HY OAS + Put/Call の AND 条件
    - 誤発動防止:
        * セクター breadth: 7/11 以上が 20D MA 下回り
        * 出来高 capitulation: 直近 3 日が 90 日平均比 1.5x+
        * 連続発動クールダウン: 同 tranche 5 営業日以内は再発動しない
        * 年間予算上限: 3 tranche 合計で総資産 15%
    - 発動時の出力: active_tranche + recommended_buys（銘柄 + 投入目安金額）

関連:
    - macro_fetcher.classify_panic()
    - vix_tracker.get_vix_context() → vix.decay_from_peak_5d_pct
    - behavioral_guard: allow_dca_tranche は DCA ラダー限定の deterministic 例外フラグ。
      policy_engine は type="dca" source="dca_ladder" だけを DD stage 下で半量通過させる。
      stage_3 / daily_block、または trading_allowed が True と確認できない場合は例外も停止する。
    - analyst/__init__.py: bottom_fishing_signals.json を読んで Opus に注入
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
SIGNALS_FILE = BASE_DIR / "bottom_fishing_signals.json"
STATE_FILE   = BASE_DIR / "dca_ladder_state.json"   # クールダウン / 年間予算トラッキング
DB_FILE      = resolve_db_path(BASE_DIR)

# ─────────────────────────────────────────────────────────────
# Tranche 定義
# ─────────────────────────────────────────────────────────────
TRANCHES = {
    "T1": {
        "name":             "様子見エントリー",
        "cash_pct":         0.15,   # 現金の 15%
        "dd_threshold":     -0.08,  # Portfolio DD ≤ -8%
        "vix_min":          25.0,
        "vix_decay_pct":    -10.0,  # 5 日ピーク比 -10% 以上減衰
        "fg_max":           None,   # T1 は F&G 条件なし（VIX 主導）
        "hy_oas_min":       None,
        "default_targets":  ["SLIM_SP500", "1489.T", "GLD"],
    },
    "T2": {
        "name":             "本格買い下がり",
        "cash_pct":         0.25,
        "dd_threshold":     -0.12,
        "vix_min":          25.0,
        "vix_decay_pct":    None,   # T2 は F&G + HY OAS 主導
        "fg_max":           25,
        "hy_oas_min":       500.0,
        "default_targets":  ["AVGO", "NVDA", "META", "SMH"],
    },
    "T3": {
        "name":             "キャピチュレーション反転",
        "cash_pct":         0.40,
        "dd_threshold":     -0.18,
        "vix_min":          40.0,
        "vix_decay_pct":    None,
        "pc_or_vix40":      True,   # Put/Call > 1.2 OR VIX > 40（either-or）
        "rsi_reversal":     True,   # RSI(14) < 30 2 日連続後 +5pt 反転
        "default_targets":  ["CRWV", "LIT", "SOXL"],  # 高ベータ + レバレッジ
    },
}

ANNUAL_BUDGET_CAP_PCT = 0.15   # 年間総資産の 15%
COOLDOWN_DAYS         = 5      # 同一 tranche の再発動クールダウン
SECTOR_BREADTH_MIN    = 7      # 20D MA 下回りセクター数（11 中）


# ─────────────────────────────────────────────────────────────
# ポートフォリオ / ドローダウン計算
# ─────────────────────────────────────────────────────────────

def compute_drawdown_state(db_file: Path = DB_FILE, lookback_days: int = 252) -> dict:
    """
    DB の daily_performance から portfolio DD を計算。
    Returns:
        {
          "current_value_jpy": float,
          "peak_value_jpy":   float,
          "peak_date":        str (YYYY-MM-DD),
          "dd_from_peak":     float (-0.132 = -13.2%),
          "dd_mtd_pct":       float,
          "dd_3m_pct":        float,
          "dd_6m_pct":        float,
          "history_days":     int,
        }
    """
    default = {
        "current_value_jpy": None, "peak_value_jpy": None, "peak_date": None,
        "dd_from_peak": None, "dd_mtd_pct": None, "dd_3m_pct": None, "dd_6m_pct": None,
        "history_days": 0,
    }
    if not db_file.exists():
        return default
    try:
        con = sqlite3.connect(db_file, timeout=30.0)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA busy_timeout=30000')
        except sqlite3.Error:
            pass
        cur = con.cursor()
        since = (date.today() - timedelta(days=lookback_days)).isoformat()
        # Codex P1 #4: 推定 (nav_backfill, estimated=1) 行は DD 計算から除外する。
        # 列が無い旧 DB では推定行も存在しないので unfiltered に fallback。
        try:
            cur.execute(
                "SELECT date, portfolio_value FROM daily_performance "
                "WHERE date >= ? AND portfolio_value IS NOT NULL "
                "AND COALESCE(estimated, 0) = 0 ORDER BY date",
                (since,),
            )
        except sqlite3.OperationalError:
            cur.execute(
                "SELECT date, portfolio_value FROM daily_performance "
                "WHERE date >= ? AND portfolio_value IS NOT NULL ORDER BY date",
                (since,),
            )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        print(f"[dca] DB read error: {e}")
        return default

    if not rows:
        return default

    values = [(d, float(v)) for d, v in rows if v is not None]
    if not values:
        return default

    current_date, current = values[-1]
    # 全期間ピーク
    peak_date, peak = max(values, key=lambda x: x[1])
    dd_from_peak = (current / peak - 1.0) if peak > 0 else 0.0

    # MTD / 3M / 6M 絶対 DD（ピーク基準ではなく期間初比）
    def _dd_over(days: int) -> float | None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        window = [v for d, v in values if d >= cutoff]
        if not window:
            return None
        w_peak = max(window)
        return round((current / w_peak - 1.0), 4) if w_peak > 0 else None

    return {
        "current_value_jpy": round(current, 0),
        "peak_value_jpy":   round(peak, 0),
        "peak_date":        peak_date,
        "dd_from_peak":     round(dd_from_peak, 4),
        "dd_mtd_pct":       _dd_over(30),
        "dd_3m_pct":        _dd_over(90),
        "dd_6m_pct":        _dd_over(180),
        "history_days":     len(values),
    }


# ─────────────────────────────────────────────────────────────
# セクター breadth / 出来高 capitulation
# ─────────────────────────────────────────────────────────────

def evaluate_sector_breadth() -> dict:
    """
    sector_strength.json から「20D MA 下回りセクター数」を算出。
    Returns: {"sectors_below_ma20": int, "total": int, "breadth_score": float,
              "broad_selloff": bool}
    """
    f = BASE_DIR / "sector_strength.json"
    if not f.exists():
        return {"sectors_below_ma20": 0, "total": 0, "breadth_score": None, "broad_selloff": False}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"sectors_below_ma20": 0, "total": 0, "breadth_score": None, "broad_selloff": False}

    sectors = data.get("sectors", data) if isinstance(data, dict) else {}
    below = 0
    total = 0
    for _, sec in (sectors.items() if isinstance(sectors, dict) else []):
        if not isinstance(sec, dict):
            continue
        total += 1
        # sector_strength.json のキー例: "below_ma20" / "vs_ma20_pct" / "above_ma20"
        if sec.get("below_ma20") is True:
            below += 1
        elif isinstance(sec.get("vs_ma20_pct"), (int, float)) and sec["vs_ma20_pct"] < 0:
            below += 1
        elif sec.get("above_ma20") is False:
            below += 1
    breadth_score = round(below / total, 3) if total > 0 else None
    return {
        "sectors_below_ma20": below,
        "total":              total,
        "breadth_score":      breadth_score,
        "broad_selloff":      below >= SECTOR_BREADTH_MIN,
    }


def check_volume_capitulation(tickers: list[str] | None = None) -> bool:
    """
    直近 3 日の SPY/QQQ 合計出来高が 90 日平均比 1.5x 以上なら capitulation とみなす。
    yfinance 1日遅延許容。失敗時は False。
    """
    tickers = tickers or ["SPY", "QQQ"]
    try:
        import yfinance as yf  # type: ignore
        ratios: list[float] = []
        for t in tickers:
            hist = yf.Ticker(t).history(period="6mo")
            if hist.empty or "Volume" not in hist.columns:
                continue
            v = hist["Volume"].dropna()
            if len(v) < 95:
                continue
            recent = float(v.iloc[-3:].mean())
            baseline = float(v.iloc[-93:-3].mean())
            if baseline > 0:
                ratios.append(recent / baseline)
        if not ratios:
            return False
        return (sum(ratios) / len(ratios)) >= 1.5
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# RSI 反転（T3 の微シグナル）
# ─────────────────────────────────────────────────────────────

def evaluate_rsi_reversal(ticker: str = "SPY") -> dict:
    """
    RSI(14) が < 30 を 2 日連続した後、直近で +5pt 以上反転しているかを判定。
    Returns: {"reversed": bool, "rsi_latest": float|None, "trough": float|None}
    """
    try:
        import yfinance as yf  # type: ignore
        hist = yf.Ticker(ticker).history(period="2mo")
        if hist.empty:
            return {"reversed": False, "rsi_latest": None, "trough": None}
        close = hist["Close"].dropna()
        if len(close) < 20:
            return {"reversed": False, "rsi_latest": None, "trough": None}
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.dropna()
        if len(rsi) < 5:
            return {"reversed": False, "rsi_latest": None, "trough": None}
        latest = float(rsi.iloc[-1])
        # 直近 10 日内で <30 が 2 日連続あったか
        recent = rsi.iloc[-10:]
        pairs_below = any((recent.iloc[i] < 30) and (recent.iloc[i + 1] < 30)
                          for i in range(len(recent) - 1))
        trough = float(recent.min()) if len(recent) > 0 else None
        reversed_ok = pairs_below and (trough is not None) and (latest - trough >= 5.0)
        return {"reversed": bool(reversed_ok), "rsi_latest": round(latest, 2),
                "trough": round(trough, 2) if trough is not None else None}
    except Exception:
        return {"reversed": False, "rsi_latest": None, "trough": None}


# ─────────────────────────────────────────────────────────────
# クールダウン / 年間予算
# ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_fired": {}, "annual_spent_pct": 0.0, "year": date.today().year}


def _save_state(state: dict) -> None:
    # Codex P1 #10: クラッシュ時の部分書き込みを防ぐため atomic write。
    try:
        from utils import atomic_write_json
        atomic_write_json(STATE_FILE, state, ensure_ascii=False, indent=2)
    except Exception:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _within_cooldown(state: dict, tranche: str) -> bool:
    last = state.get("last_fired", {}).get(tranche)
    if not last:
        return False
    try:
        last_date = date.fromisoformat(last)
    except Exception:
        return False
    return (date.today() - last_date).days < COOLDOWN_DAYS


def _annual_budget_remaining_pct(state: dict) -> float:
    # 年替わりでリセット
    if state.get("year") != date.today().year:
        return ANNUAL_BUDGET_CAP_PCT
    return max(0.0, ANNUAL_BUDGET_CAP_PCT - float(state.get("annual_spent_pct", 0.0)))


# ─────────────────────────────────────────────────────────────
# Tranche 評価
# ─────────────────────────────────────────────────────────────

def _tranche_conditions_met(tranche_id: str, dd: dict, panic: dict, vix_ctx: dict,
                             rsi_state: dict) -> tuple[bool, list[str]]:
    """tranche の発動条件を AND 評価。reasons を返す。"""
    spec = TRANCHES[tranche_id]
    reasons: list[str] = []
    ok = True

    # Portfolio DD
    dd_val = dd.get("dd_from_peak")
    if dd_val is None or dd_val > spec["dd_threshold"]:
        ok = False
        reasons.append(f"DD={dd_val} not <= {spec['dd_threshold']}")
    else:
        reasons.append(f"DD={dd_val:.3f} ≤ {spec['dd_threshold']}")

    # VIX lower bound
    vix_level = (vix_ctx.get("vix", {}) or {}).get("level")
    if spec.get("vix_min") is not None:
        if vix_level is None or vix_level < spec["vix_min"]:
            ok = False
            reasons.append(f"VIX={vix_level} < {spec['vix_min']}")
        else:
            reasons.append(f"VIX={vix_level:.1f} ≥ {spec['vix_min']}")

    # VIX decay (T1 only)
    if spec.get("vix_decay_pct") is not None:
        decay = (vix_ctx.get("vix", {}) or {}).get("decay_from_peak_5d_pct")
        if decay is None or decay > spec["vix_decay_pct"]:
            ok = False
            reasons.append(f"VIX decay={decay} > {spec['vix_decay_pct']}")
        else:
            reasons.append(f"VIX decay={decay:.1f}% ≤ {spec['vix_decay_pct']}%")

    # Fear&Greed
    if spec.get("fg_max") is not None:
        fg = panic.get("fear_greed")
        if fg is None or fg > spec["fg_max"]:
            ok = False
            reasons.append(f"F&G={fg} > {spec['fg_max']}")
        else:
            reasons.append(f"F&G={fg} ≤ {spec['fg_max']}")

    # HY OAS
    if spec.get("hy_oas_min") is not None:
        hy = panic.get("hy_oas_bps")
        if hy is None or hy < spec["hy_oas_min"]:
            ok = False
            reasons.append(f"HY OAS={hy} < {spec['hy_oas_min']}")
        else:
            reasons.append(f"HY OAS={hy:.0f}bps ≥ {spec['hy_oas_min']}")

    # T3 either-or: PutCall > 1.2 OR VIX > 40
    if spec.get("pc_or_vix40"):
        pc = panic.get("put_call")
        vix = panic.get("vix")
        pass_pc = pc is not None and pc > 1.2
        pass_vix40 = vix is not None and vix > 40
        if not (pass_pc or pass_vix40):
            ok = False
            reasons.append(f"P/C={pc} ≤ 1.2 AND VIX={vix} ≤ 40")
        else:
            reasons.append(f"P/C>1.2 or VIX>40 satisfied")

    # T3 RSI reversal
    if spec.get("rsi_reversal"):
        if not rsi_state.get("reversed"):
            ok = False
            reasons.append("RSI reversal not confirmed")
        else:
            reasons.append(f"RSI reversed (latest={rsi_state.get('rsi_latest')})")

    return ok, reasons


def _ticker_currency(ticker: str) -> str:
    """ticker の決済通貨を判定。JP 株/ETF・国内投信は JPY、それ以外は USD。"""
    t = str(ticker or "")
    if t.endswith(".T"):
        return "JPY"
    # 国内投信の擬似ティッカー
    if t.startswith(("SLIM_", "IFREE_", "NOMURA_", "MNXACT")):
        return "JPY"
    return "USD"


def _build_recommended_buys(tranche_id: str, cash_jpy: float,
                             target_tickers: list[str] | None = None,
                             deploy_jpy: float | None = None,
                             cash_breakdown: dict | None = None) -> list[dict]:
    """
    tranche の投入額を default_targets へ等額配分して buy list を返す。
    実運用では Opus (dca_tranche_selector role) で tickers を選定し直す想定。

    Codex P1 #10: deploy_jpy が渡された場合は、残予算で clip 済みの投入額を使う
    (None なら従来どおり cash_jpy × cash_pct)。

    Codex re-review F5: 各 buy に決済通貨と「その通貨で実際に投入可能か」を注記する。
    total_cash は JPY+USD 換算合計なので、JPY 建て target (1489.T 等) を total で
    サイズすると JPY 残高 (balance) を超えて発注不能になり得る。通貨別 cash で
    currency_cash_sufficient を立て、不足分は呼出側/プロンプトが縮小判断できる。
    """
    spec = TRANCHES[tranche_id]
    tickers = target_tickers or spec["default_targets"]
    if not tickers:
        return []
    budget = deploy_jpy if deploy_jpy is not None else cash_jpy * spec["cash_pct"]
    if budget <= 0:
        return []
    per_ticker = budget / len(tickers)

    cb = cash_breakdown or {}
    avail_by_ccy = {"JPY": cb.get("jpy"), "USD": cb.get("usd_jpy")}

    # 通貨別の所要額を集計
    by_ccy_required: dict[str, float] = {}
    by_ccy_tickers: dict[str, list] = {}
    for t in tickers:
        ccy = _ticker_currency(t)
        by_ccy_required[ccy] = by_ccy_required.get(ccy, 0.0) + per_ticker
        by_ccy_tickers.setdefault(ccy, []).append(t)

    # Codex re-review #3 (B) + round3 #6 + round4 #6: 通貨別 clip。
    #   total_jpy はトランシェ予算の上限として維持するが、最終 target_jpy は
    #   通貨別残高で clip する。暗黙 FX 振替はしない。
    #   round3 #6: 通貨バケット総額を整数化し floor + 余りを決定配分し合計 ≤ 残高。
    #   round4 #6: requested も整数で先に決定配分し、target ≤ requested を保証して
    #   deferred = requested - target が常に >= 0 で会計恒等式を満たすようにする。
    def _int_distribute(total: float, n: int) -> list[int]:
        """total を floor 整数化し n 個へ base + 先頭余り +1 で決定配分する。"""
        ti = int(math.floor(max(0.0, total)))
        base = ti // n
        rem = ti - base * n
        return [base + (1 if i < rem else 0) for i in range(n)]

    requested_alloc: dict[str, int] = {}  # ticker -> 整数 requested_jpy
    int_alloc: dict[str, int] = {}        # ticker -> clip 後整数 target_jpy
    for ccy, ts in by_ccy_tickers.items():
        required = by_ccy_required.get(ccy, 0.0)
        avail = avail_by_ccy.get(ccy)
        n = len(ts)
        bucket = required if avail is None else min(float(avail), required)
        req_dist = _int_distribute(required, n)   # clip 前の整数要求額
        tgt_dist = _int_distribute(bucket, n)      # clip 後の整数投入額 (bucket ≤ required)
        for i, t in enumerate(ts):
            requested_alloc[t] = req_dist[i]
            # 同順 (先頭優先) で配分するので tgt_dist[i] ≤ req_dist[i] が保証される
            int_alloc[t] = min(tgt_dist[i], req_dist[i])

    out = []
    for t in tickers:
        ccy = _ticker_currency(t)
        avail = avail_by_ccy.get(ccy)
        required = by_ccy_required.get(ccy)
        clipped = int_alloc.get(t, 0)
        requested = requested_alloc.get(t, 0)
        deferred = max(0, requested - clipped)   # 会計恒等式: requested = target + deferred
        sufficient = None
        if avail is not None and required is not None:
            sufficient = avail >= required
        out.append({
            "ticker":          t,
            "target_jpy":      clipped,                     # 通貨別残高で clip 済み (整数)
            "requested_jpy":   requested,                   # clip 前の整数要求額
            "deferred_jpy":    deferred,                    # = requested - target (>=0)
            "currency":        ccy,
            "currency_cash_jpy_available": (round(avail, 0) if avail is not None else None),
            "currency_cash_sufficient": sufficient,
            "urgency":         "high",
            "source":          "dca_ladder",
            "tranche":         tranche_id,
            "rationale":       f"DCA {tranche_id}: {spec['name']}",
        })
    return out


# ─────────────────────────────────────────────────────────────
# エンジン本体
# ─────────────────────────────────────────────────────────────

def generate_ladder_signals(cash_jpy: float | None = None,
                              dry_run: bool = True,
                              cash_breakdown: dict | None = None) -> dict:
    """
    ラダーシグナル評価のメインエントリ。
    dry_run=True: state の更新なし（評価のみ）。analyzer から呼ぶ際は True。
    dry_run=False: 発動した tranche の cooldown / budget を state に記録する。

    cash_breakdown: 通貨別 cash (None なら自動取得)。recommended_buys の
    通貨別充足判定 (currency_cash_sufficient) に使う。
    """
    if cash_breakdown is None:
        cash_breakdown = _estimate_cash_breakdown()
    # 依存モジュールは遅延 import（テスト容易化）
    try:
        from macro_fetcher import get_macro_context, classify_panic
    except Exception:
        get_macro_context = None  # type: ignore
        classify_panic = None  # type: ignore
    try:
        from vix_tracker import get_vix_context
    except Exception:
        get_vix_context = None  # type: ignore

    dd = compute_drawdown_state()
    macro = get_macro_context() if get_macro_context else {}
    panic = classify_panic(macro) if classify_panic else {}
    vix_ctx = get_vix_context() if get_vix_context else {}
    breadth = evaluate_sector_breadth()
    volume_cap = check_volume_capitulation()
    rsi_state = evaluate_rsi_reversal("SPY")

    def _decide(state: dict) -> dict:
        annual_remaining = _annual_budget_remaining_pct(state)

        evaluations: dict[str, dict] = {}
        active_tranche: str | None = None
        reasons_fire: list[str] = []

        # 下位から評価（T3 が発動したら T1/T2 は skip）
        for t_id in ("T3", "T2", "T1"):
            ok, reasons = _tranche_conditions_met(t_id, dd, panic, vix_ctx, rsi_state)
            evaluations[t_id] = {
                "met":     ok,
                "reasons": reasons,
                "spec":    TRANCHES[t_id],
            }
            if ok and active_tranche is None:
                # 追加ガード
                guards_ok = True
                if not breadth.get("broad_selloff", False):
                    reasons.append(f"[guard fail] sector breadth {breadth.get('sectors_below_ma20')}/11 < {SECTOR_BREADTH_MIN}")
                    guards_ok = False
                if not volume_cap:
                    reasons.append("[guard fail] volume capitulation not confirmed")
                    # T1 は volume ガード緩和（様子見なので）
                    if t_id != "T1":
                        guards_ok = False
                if _within_cooldown(state, t_id):
                    reasons.append(f"[guard fail] within {COOLDOWN_DAYS}-day cooldown")
                    guards_ok = False
                required_pct = TRANCHES[t_id]["cash_pct"] * (cash_jpy or 0) / max(1.0, dd.get("current_value_jpy") or 1.0)
                # Codex P1 #10: 残予算が実質ゼロのときだけ発動不可とし、残っていれば後段で投入額を clip。
                if annual_remaining <= 1e-6:
                    reasons.append(f"[guard fail] annual budget exhausted (remaining {annual_remaining:.3f}, required ~{required_pct:.3f})")
                    guards_ok = False

                if guards_ok:
                    active_tranche = t_id
                    reasons_fire = reasons

        recommended: list[dict] = []
        deployed_pct = 0.0
        actual_deploy_jpy = 0.0
        if active_tranche and cash_jpy and cash_jpy > 0:
            _spec = TRANCHES[active_tranche]
            _total_value = dd.get("current_value_jpy") or 0.0
            # Codex P1 #10: 投入額を残予算 (annual_remaining × 総資産) で clip。
            _want_jpy = cash_jpy * _spec["cash_pct"]
            _budget_cap_jpy = annual_remaining * _total_value if _total_value > 0 else _want_jpy
            _deploy_jpy = max(0.0, min(_want_jpy, _budget_cap_jpy, cash_jpy))
            recommended = _build_recommended_buys(active_tranche, cash_jpy, deploy_jpy=_deploy_jpy,
                                                  cash_breakdown=cash_breakdown)
            # Codex re-review round3 #2: state 消費は通貨 clip 後の実投入額で行う。
            # _deploy_jpy (clip 前) ではなく recommended_buys の target_jpy 合計を使う。
            # 通貨別 cash が 0 で実投入 0 のとき、annual budget / cooldown を消費しない。
            actual_deploy_jpy = sum(float(b.get("target_jpy") or 0) for b in recommended)
            deployed_pct = (actual_deploy_jpy / _total_value) if _total_value > 0 else 0.0

        # Codex round4 #3: 通貨 clip で全 target が 0 (= 投入可能な通貨 cash 無し) のとき、
        # 条件は満たすが「発火」させない。active_tranche を None にし would_be_tranche に
        # 退避することで、downstream (guard / UI / analyst) が active 発火として扱うのを防ぐ。
        would_be_tranche = None
        non_executable_reason = None
        if active_tranche and actual_deploy_jpy <= 0:
            would_be_tranche = active_tranche
            non_executable_reason = (
                f"{active_tranche} 条件成立だが通貨別 cash 不足で実投入額 0 — 非発火"
            )
            active_tranche = None
            recommended = []

        payload = {
            "evaluated_at":      datetime.now().isoformat(),
            "freshness_date":    date.today().isoformat(),
            "cash_breakdown":    cash_breakdown,
            "active_tranche":    active_tranche,
            "would_be_tranche":  would_be_tranche,
            "non_executable_reason": non_executable_reason,
            "actual_deploy_jpy": actual_deploy_jpy,
            "tranche_reasons":   reasons_fire,
            "dd":                dd,
            "panic":             panic,
            "vix_extract": {
                "level":                 (vix_ctx.get("vix", {}) or {}).get("level"),
                "classification":        (vix_ctx.get("vix", {}) or {}).get("classification"),
                "decay_from_peak_5d_pct": (vix_ctx.get("vix", {}) or {}).get("decay_from_peak_5d_pct"),
            },
            "breadth":           breadth,
            "volume_capitulation": volume_cap,
            "rsi_state":         rsi_state,
            "evaluations":       evaluations,
            "recommended_buys":  recommended,
            "state": {
                "annual_remaining_pct": annual_remaining,
                "cooldown_active":      {t: _within_cooldown(state, t) for t in TRANCHES},
            },
            "dry_run":           dry_run,
        }

        # Codex re-review #10: 判定に使った state をそのまま消費する (別 _load_state を挟まない)。
        # 消費は clip 済み deployed_pct のみ・年間上限 (ANNUAL_BUDGET_CAP_PCT) で cap。
        # round3 #2: 通貨 clip 後の実投入額が 0 (= 投入可能な通貨 cash が無い) なら
        # 発火扱いせず cooldown / budget を消費しない。
        if active_tranche and not dry_run and actual_deploy_jpy > 0:
            if state.get("year") != date.today().year:
                state["year"] = date.today().year
                state["annual_spent_pct"] = 0.0
            state.setdefault("last_fired", {})[active_tranche] = date.today().isoformat()
            remaining = _annual_budget_remaining_pct(state)
            consume = max(0.0, min(deployed_pct, remaining))
            state["annual_spent_pct"] = min(
                ANNUAL_BUDGET_CAP_PCT,
                float(state.get("annual_spent_pct", 0.0)) + consume,
            )
            state["year"] = date.today().year
            _save_state(state)
            payload["state"]["annual_remaining_pct"] = _annual_budget_remaining_pct(state)

        return payload

    # Codex re-review #10: 非 dry_run は load→判定→cooldown→clip→消費→payload を単一 lock 内で
    # 実行し、並行実行が同じ残予算で二重に発動するのを防ぐ。dry_run は read-only なので lock 不要。
    # lock 取得不能時の nullcontext fallback は廃止 (取れなければ raise させる)。
    if dry_run:
        return _decide(_load_state())
    from utils import process_lock
    with process_lock("dca_ladder"):
        return _decide(_load_state())


def persist(signals: dict, path: Path = SIGNALS_FILE) -> None:
    """bottom_fishing_signals.json として atomic write で保存（Opus が読む）。"""
    try:
        from utils import atomic_write_json
        atomic_write_json(path, signals, ensure_ascii=False, indent=2)
    except Exception:
        path.write_text(json.dumps(signals, ensure_ascii=False, indent=2))


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _estimate_cash_breakdown() -> dict:
    """
    account.json から通貨別 cash を構造化して返す。

    Returns: {
      "jpy":       JPY 建て現金 (balance),
      "usd":       USD 建て現金 (usd_balance),
      "usd_jpy":   USD 現金の JPY 換算 (usd_balance * fx_rate_usdjpy),
      "total_jpy": JPY+USD 換算合計 (total_cash),
      "fx_rate":   USDJPY,
      "source":    "account.json" | "snapshot" | "none",
    }
    全 None 可。total_jpy は DCA 予算サイズの分母、jpy/usd_jpy は通貨別充足判定に使う。
    """
    out = {"jpy": None, "usd": None, "usd_jpy": None, "total_jpy": None,
           "fx_rate": None, "source": "none"}
    account_file = BASE_DIR / "account.json"
    if account_file.exists():
        try:
            acct = json.loads(account_file.read_text(encoding="utf-8"))
            jpy = acct.get("balance")
            usd = acct.get("usd_balance")
            stored_total = acct.get("total_cash")
            fx = acct.get("fx_rate_usdjpy")
            usd_jpy = None
            if usd is not None and fx:
                usd_jpy = float(usd) * float(fx)
            total = None
            if usd_jpy is not None:
                total = (float(jpy) if jpy is not None else 0.0) + (float(usd_jpy) if usd_jpy is not None else 0.0)
            elif stored_total is not None:
                total = float(stored_total)
            elif jpy is not None:
                total = float(jpy)
            out.update({
                "jpy":       (float(jpy) if jpy is not None else None),
                "usd":       (float(usd) if usd is not None else None),
                "usd_jpy":   (float(usd_jpy) if usd_jpy is not None else None),
                "total_jpy": (float(total) if total is not None else None),
                "fx_rate":   (float(fx) if fx is not None else None),
                "source":    "account.json",
            })
            return out
        except Exception:
            pass
    # fallback: build_portfolio_snapshot (通貨内訳は無いので total のみ)
    try:
        from portfolio_manager import build_portfolio_snapshot  # type: ignore
        snap = build_portfolio_snapshot()
        cash = snap.get("cash_jpy")
        if cash is not None:
            out.update({"total_jpy": float(cash), "source": "snapshot"})
    except Exception:
        pass
    return out


def _estimate_cash_jpy() -> float | None:
    """
    DCA 予算サイズ用の現金総額 (JPY 換算合計) を返す。
    DCA target は多通貨 (国内投信 + 米国株 + ETF) なので予算分母は total_jpy。
    通貨別の充足判定は _estimate_cash_breakdown() / _build_recommended_buys 側で行う。
    """
    breakdown = _estimate_cash_breakdown()
    return breakdown.get("total_jpy")


if __name__ == "__main__":
    import sys
    dry = "--fire" not in sys.argv
    breakdown = _estimate_cash_breakdown()
    cash = breakdown.get("total_jpy")
    print(f"[dca] cash total=¥{cash} (JPY=¥{breakdown.get('jpy')} / USD換算=¥{breakdown.get('usd_jpy')}) src={breakdown.get('source')}")
    sig = generate_ladder_signals(cash_jpy=cash, dry_run=dry, cash_breakdown=breakdown)
    persist(sig)
    print(f"[dca] active_tranche: {sig['active_tranche']}")
    for t_id, e in sig["evaluations"].items():
        mark = "✅" if e["met"] else "❌"
        print(f"  {mark} {t_id}: {'; '.join(e['reasons'])}")
    if sig["recommended_buys"]:
        print("[dca] recommended:")
        for b in sig["recommended_buys"]:
            _df = b.get("deferred_jpy") or 0
            _suf = f" ⚠️通貨別cash不足 (繰延¥{_df:,.0f})" if _df > 0 else ""
            print(f"   - {b['ticker']}({b.get('currency')}): 投入¥{b['target_jpy']:,.0f} / 要求¥{b.get('requested_jpy', b['target_jpy']):,.0f}{_suf}")
    else:
        print("[dca] 平時: 発動なし")
    print(f"[dca] signals saved: {SIGNALS_FILE}")
