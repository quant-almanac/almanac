"""
chart_analyzer — ALMANAC v5.1
============================
priority_actions の指値判断（成行 vs 指値、limit_price）を AI が決めるための
チャート派生指標を集約する。

責務:
- 既存 Parquet 日足から VWAP_30d / ATR_14d / HV_30d / ADV_30d / pivots / spread を計算
- 決定時のみ yfinance 5m intraday 30 日 + bid/ask snapshot を取得（cron 常駐なし）
- 取得失敗は freshness='eod_only' で日足ベース近似にフォールバック

呼び出し:
- analyst/__init__.py:_synthesize の直前で gather_chart_context(tickers, intraday=True)
  を実行して、Opus プロンプトに injection する

設計判断:
- 投資信託（SLIM_*, IFREE_*, NOMURA_*, MNXACT）は intraday も日足 spread も
  取得できないため empty を返す（SKIP_TICKERS で除外）
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from pseudo_tickers import is_pseudo_market_ticker

BASE_DIR = Path(__file__).parent
INTRADAY_CACHE_DIR = BASE_DIR / "data" / "intraday_cache"
OHLCV_DIR = BASE_DIR / "data" / "ohlcv"

# Opus に投げる ticker 上限（コストとレイテンシ管理）
DEFAULT_MAX_TICKERS = 30
INTRADAY_TTL_HOURS = 24

# 投資信託・取得不可ティッカー（intraday も spread も無意味）
SKIP_TICKERS = frozenset({
    "SLIM_SP500", "SLIM_ORCAN", "MNXACT", "IFREE_FANGPLUS",
    "NOMURA_SEMI", "AVGO_特定", "AVGO_一般",
})

# ファイル名として安全な ticker のみ許可（path traversal 対策）
import re as _re
_SAFE_TICKER_RE = _re.compile(r"^[A-Za-z0-9._=^-]+$")


def _is_safe_ticker(t: str) -> bool:
    """ticker がファイル名として安全か検証。`../`, `/`, NUL バイト等を弾く。"""
    if not t or not isinstance(t, str) or len(t) > 32:
        return False
    return bool(_SAFE_TICKER_RE.match(t))


# ============================================================
# 日足ベース指標（Parquet 既存資産を再利用）
# ============================================================

def _load_daily_ohlcv(ticker: str, lookback_days: int = 90):
    """data/ohlcv/{ticker}.parquet を読む。失敗時は None"""
    if not _is_safe_ticker(ticker):
        return None
    try:
        import pandas as pd  # type: ignore
        path = OHLCV_DIR / f"{ticker}.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        if df.empty:
            return None
        # MultiIndex 対策
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.droplevel(1)
        df = df.tail(lookback_days).copy()
        return df
    except Exception:
        return None


def _atr_14(df) -> Optional[float]:
    """Wilder's ATR(14)"""
    try:
        import pandas as pd  # type: ignore
        if df is None or len(df) < 15:
            return None
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
        v = float(atr.iloc[-1])
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _vwap_30d(df) -> Optional[float]:
    """日足の (H+L+C)/3 × Volume / Σ Volume（30 日）"""
    try:
        if df is None or len(df) < 5:
            return None
        d = df.tail(30)
        typical = (d["High"].astype(float) + d["Low"].astype(float) + d["Close"].astype(float)) / 3.0
        vol = d["Volume"].astype(float).clip(lower=0)
        denom = float(vol.sum())
        if denom <= 0:
            return None
        v = float((typical * vol).sum() / denom)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _hv_30d_pct(df) -> Optional[float]:
    """30 日対数リターンの年率ボラ（%表示）"""
    try:
        import numpy as np  # type: ignore
        if df is None or len(df) < 31:
            return None
        d = df.tail(31)
        log_ret = np.log(d["Close"].astype(float)).diff().dropna()
        if len(log_ret) < 5:
            return None
        v = float(log_ret.std() * math.sqrt(252) * 100)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _adv_30d(df) -> Optional[float]:
    try:
        if df is None or len(df) < 5:
            return None
        d = df.tail(30)
        v = float(d["Volume"].astype(float).mean())
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def _spread_estimate_bps_from_daily(df, hv_pct: Optional[float]) -> Optional[float]:
    """intraday bid/ask が無いときの spread 近似。
    優先: 直近10日の (H-L)/Close 中央値 → 不可なら HV から近似 (8 + 0.5×HV%)
    """
    try:
        if df is None or len(df) < 5:
            return 8.0 + 0.5 * (hv_pct or 25.0)
        d = df.tail(10)
        hl = (d["High"].astype(float) - d["Low"].astype(float))
        close = d["Close"].astype(float).replace(0, float("nan"))
        rel = (hl / close).dropna()
        if rel.empty:
            return 8.0 + 0.5 * (hv_pct or 25.0)
        # 中央値の半分を「typical spread」近似（高値安値レンジは spread+ノイズ）
        v = float(rel.median() * 10000 * 0.15)  # 15% を spread 寄与と仮定
        if not math.isfinite(v) or v <= 0:
            return 8.0 + 0.5 * (hv_pct or 25.0)
        return max(2.0, min(v, 200.0))  # 2bps〜200bps にクランプ
    except Exception:
        return 8.0 + 0.5 * (hv_pct or 25.0)


def _pivots_from_daily(df, lookback: int = 30, k_fractal: int = 3) -> list[dict]:
    """直近 lookback 日のスイング高安を fractal 検出。
    スコアは近接価格帯への接触回数 + 反発幅。"""
    try:
        if df is None or len(df) < lookback + k_fractal * 2:
            return []
        d = df.tail(lookback + k_fractal * 2).reset_index(drop=True)
        highs = d["High"].astype(float).tolist()
        lows = d["Low"].astype(float).tolist()
        closes = d["Close"].astype(float).tolist()
        last_close = closes[-1] if closes else 0
        if not last_close or not math.isfinite(last_close):
            return []
        pivots: list[dict] = []
        n = len(d)
        for i in range(k_fractal, n - k_fractal):
            # 上下 k_fractal 本ずつより高い/低い → fractal 高/安
            if all(highs[i] >= highs[i - j] for j in range(1, k_fractal + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, k_fractal + 1)):
                pivots.append({"price": round(highs[i], 4), "type": "resistance",
                               "age_days": n - 1 - i})
            if all(lows[i] <= lows[i - j] for j in range(1, k_fractal + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, k_fractal + 1)):
                pivots.append({"price": round(lows[i], 4), "type": "support",
                               "age_days": n - 1 - i})
        # 現値からの距離が大きすぎるピボットは除外（±15% 以内）
        filtered: list[dict] = []
        for p in pivots:
            dist_pct = abs(p["price"] - last_close) / last_close * 100
            if dist_pct > 15:
                continue
            # strength: 0..1（age が新しい・距離が近いほど高い）
            age_factor = max(0.0, 1.0 - p["age_days"] / 30.0)
            dist_factor = max(0.0, 1.0 - dist_pct / 15.0)
            p["strength"] = round(age_factor * 0.5 + dist_factor * 0.5, 3)
            filtered.append(p)
        # 強度上位 6 件
        filtered.sort(key=lambda x: x["strength"], reverse=True)
        return filtered[:6]
    except Exception:
        return []


def _summarize_pivots(pivots: list[dict], last_close: Optional[float]) -> dict:
    """Opus に渡しやすい形に縮約: 直近 support / resistance を 1 つずつ"""
    if not pivots or not last_close:
        return {"support": None, "resistance": None}
    supports = [p for p in pivots if p["type"] == "support" and p["price"] < last_close]
    resistances = [p for p in pivots if p["type"] == "resistance" and p["price"] > last_close]
    s = max(supports, key=lambda p: p["price"]) if supports else None
    r = min(resistances, key=lambda p: p["price"]) if resistances else None
    return {
        "support": round(s["price"], 4) if s else None,
        "resistance": round(r["price"], 4) if r else None,
    }


# ============================================================
# Intraday snapshot（決定時のみ yfinance を叩く）
# ============================================================

def _intraday_cache_path(ticker: str) -> Path:
    """安全な ticker のみ。攻撃文字は '_invalid_' に置換（defense in depth）。"""
    safe = ticker if _is_safe_ticker(ticker) else "_invalid_"
    today = datetime.now().strftime("%Y-%m-%d")
    return INTRADAY_CACHE_DIR / f"{safe}_{today}.parquet"


def _load_or_fetch_intraday(ticker: str):
    """24h 以内に取得済みならキャッシュ、なければ yfinance で 5m 30d 取得"""
    if not _is_safe_ticker(ticker):
        return None
    INTRADAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _intraday_cache_path(ticker)
    if path.exists():
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if datetime.now() - mtime < timedelta(hours=INTRADAY_TTL_HOURS):
                import pandas as pd  # type: ignore
                df = pd.read_parquet(path)
                if not df.empty:
                    return df
        except Exception:
            pass
    try:
        import yfinance as yf  # type: ignore
        df = yf.Ticker(ticker).history(period="30d", interval="5m", auto_adjust=True)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.droplevel(1)
        try:
            df.to_parquet(path)
        except Exception:
            pass
        return df
    except Exception:
        return None


def _bid_ask_snapshot(ticker: str) -> Optional[dict]:
    """yfinance fast_info / info から bid/ask/last を取得。失敗時 None."""
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        # まず fast_info（軽量）
        bid = ask = last = None
        try:
            fi = t.fast_info
            last = float(getattr(fi, "last_price", None) or 0) or None
        except Exception:
            pass
        # info で bid/ask を取得（重い・失敗しがち）
        try:
            info = t.info or {}
            bid = float(info.get("bid") or 0) or None
            ask = float(info.get("ask") or 0) or None
            if last is None:
                last = float(info.get("regularMarketPrice") or 0) or None
        except Exception:
            pass
        if last is None and bid is None and ask is None:
            return None
        return {"bid": bid, "ask": ask, "last": last,
                "ts": datetime.now().isoformat(timespec="seconds")}
    except Exception:
        return None


def _intraday_vwap_30d(df_intraday) -> Optional[float]:
    """5m バーから VWAP_30d を再計算（日足 VWAP より精度高）"""
    try:
        if df_intraday is None or df_intraday.empty:
            return None
        d = df_intraday
        typical = (d["High"].astype(float) + d["Low"].astype(float) + d["Close"].astype(float)) / 3.0
        vol = d["Volume"].astype(float).clip(lower=0)
        denom = float(vol.sum())
        if denom <= 0:
            return None
        v = float((typical * vol).sum() / denom)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _frame_as_of(df) -> Optional[datetime]:
    """Return the newest market timestamp carried by a price frame."""
    try:
        value = df.index.max()
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        return value if isinstance(value, datetime) else None
    except Exception:
        return None


# ============================================================
# 公開関数: 単一ticker / 一括
# ============================================================

def gather_one(ticker: str, *, intraday: bool = True) -> Optional[dict]:
    """1 ticker のチャート派生指標を返す。SKIP_TICKERS は None."""
    if ticker in SKIP_TICKERS or is_pseudo_market_ticker(ticker):
        return None
    if not _is_safe_ticker(ticker):
        # path traversal 等の不正 ticker は弾く
        return None
    daily = _load_daily_ohlcv(ticker)
    last_close = None
    daily_as_of = _frame_as_of(daily)
    price_source = "daily_close"
    if daily is not None and len(daily):
        try:
            last_close = float(daily["Close"].iloc[-1])
        except Exception:
            last_close = None

    atr = _atr_14(daily)
    hv = _hv_30d_pct(daily)
    adv = _adv_30d(daily)
    vwap = _vwap_30d(daily)
    pivots = _pivots_from_daily(daily)
    sr = _summarize_pivots(pivots, last_close)

    snapshot = None
    freshness = "eod_only"
    spread_bps: Optional[float] = None
    intraday_as_of = None

    if intraday:
        df_in = _load_or_fetch_intraday(ticker)
        if df_in is not None and not df_in.empty:
            intraday_as_of = _frame_as_of(df_in)
            v_in = _intraday_vwap_30d(df_in)
            if v_in:
                vwap = v_in  # 5m VWAP の方が精度高いので上書き
            # The daily parquet can lag by one session even when yfinance's
            # intraday history already contains the latest completed session.
            # Use the newest actual bar for sizing and label it honestly as
            # session data rather than claiming a live intraday quote.
            try:
                intraday_close = float(df_in["Close"].iloc[-1])
            except Exception:
                intraday_close = 0.0
            if intraday_close > 0 and (
                daily_as_of is None
                or intraday_as_of is None
                or intraday_as_of.date() >= daily_as_of.date()
            ):
                last_close = intraday_close
                price_source = "intraday_last_bar"
                sr = _summarize_pivots(pivots, last_close)
            freshness = "last_session"
        snapshot = _bid_ask_snapshot(ticker)
        if snapshot and snapshot.get("last"):
            freshness = "quote_snapshot"
        if snapshot and snapshot.get("bid") and snapshot.get("ask"):
            mid = (snapshot["bid"] + snapshot["ask"]) / 2
            if mid > 0:
                spread_bps = (snapshot["ask"] - snapshot["bid"]) / mid * 10000

    if spread_bps is None:
        spread_bps = _spread_estimate_bps_from_daily(daily, hv)

    selected_as_of = intraday_as_of if price_source == "intraday_last_bar" else daily_as_of
    return {
        "ticker": ticker,
        "freshness": freshness,
        "price_source": price_source,
        "data_as_of": selected_as_of.isoformat() if selected_as_of else None,
        "daily_as_of": daily_as_of.isoformat() if daily_as_of else None,
        "intraday_as_of": intraday_as_of.isoformat() if intraday_as_of else None,
        "last_close": round(last_close, 4) if last_close else None,
        "vwap_30d": round(vwap, 4) if vwap else None,
        "atr_14d": round(atr, 4) if atr else None,
        "hv_30d_pct": round(hv, 2) if hv else None,
        "adv_30d": round(adv, 0) if adv else None,
        "support": sr.get("support"),
        "resistance": sr.get("resistance"),
        "spread_bps": round(spread_bps, 1) if spread_bps else None,
        "intraday_snapshot": snapshot,
        "pivots_top": pivots[:3],
    }


def gather_chart_context(
    tickers: Iterable[str],
    *,
    intraday: bool = True,
    max_tickers: int = DEFAULT_MAX_TICKERS,
) -> dict[str, dict]:
    """複数ティッカーをまとめて分析。max_tickers でクランプ。
    戻り値: {ticker: chart_dict}（取得不可の ticker はキー除外）
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for t in tickers or []:
        if not t or t in seen:
            continue
        if t in SKIP_TICKERS or is_pseudo_market_ticker(t):
            continue
        seen.add(t)
        ordered.append(t)
        if len(ordered) >= max_tickers:
            break
    out: dict[str, dict] = {}
    for t in ordered:
        try:
            ctx = gather_one(t, intraday=intraday)
            if ctx:
                out[t] = ctx
            # rate limit ガード（yfinance）
            if intraday:
                time.sleep(0.15)
        except Exception as e:
            out[t] = {"ticker": t, "error": str(e), "freshness": "eod_only"}
    return out


def format_for_prompt(ctx_map: dict[str, dict]) -> str:
    """Opus プロンプトに injection するための簡潔なテーブル。"""
    if not ctx_map:
        return ""
    lines = ["## CHART_CONTEXT（指値判断用）",
             "| ticker | last | as_of/source | vwap30 | atr14 | sup | res | spread_bps | adv30 | hv% | bid/ask | fresh |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t, c in ctx_map.items():
        if not c or c.get("error"):
            lines.append(f"| {t} | (取得失敗 {c.get('error','')}) |||||||||||| |")
            continue
        snap = c.get("intraday_snapshot") or {}
        quote_parts = []
        if snap.get("last"):
            quote_parts.append(f"last={snap.get('last')}")
        if snap.get("bid") and snap.get("ask"):
            quote_parts.append(f"bid/ask={snap.get('bid')}/{snap.get('ask')}")
        ba = " ".join(quote_parts) or "—"
        lines.append(
            f"| {t} | {c.get('last_close','—')} | {c.get('data_as_of','—')}/{c.get('price_source','—')} | "
            f"{c.get('vwap_30d','—')} | "
            f"{c.get('atr_14d','—')} | {c.get('support','—')} | {c.get('resistance','—')} | "
            f"{c.get('spread_bps','—')} | {c.get('adv_30d','—')} | "
            f"{c.get('hv_30d_pct','—')} | {ba} | {c.get('freshness','—')} |"
        )
    lines.append("")
    lines.append("【指値判断ルール】")
    lines.append("- 成行(market): urgency=high の追随 / ADV<1M & spread>30bps / 金額<¥100K / イベント±3日でIVR>70")
    lines.append("- 指値(limit): 標準。limit = (買) min(last, vwap30) − atr14×k （k=0.3 high / 0.5 medium / 0.8 low）")
    lines.append("           (売) max(last, vwap30) + atr14×k （同じ）")
    lines.append("- limit は support 上 +0.2×atr / resistance 下 -0.2×atr を超えないこと")
    lines.append("- bid/ask が取得できている場合は、買いなら ask に1tick内側、売りなら bid に1tick内側へ寄せる")
    lines.append("- expiry_minutes: 240 標準 / urgency=high → 60 / urgency=low → 720+")
    lines.append("- decision_price = snapshot.last があればそれを優先、無ければ last_close。as_of/source を必ず確認する")
    lines.append("")
    lines.append("【No-Trade 判定】")
    lines.append("- target_5d_pct を bp 換算して、spread_bps + 推定手数料(5bps) + 過去 IS 中央値 を下回るなら no_trade_zone=true")
    lines.append("- skip_reason は 1 文で（例: 「スプレッド広く edge 下回る」「support 遠く待ち時間過大」）")
    lines.append("- no_trade_zone=true のときは limit_price/order_type は出さなくてよい")
    return "\n".join(lines)


# ============================================================
# CLI（selftest 用）
# ============================================================

if __name__ == "__main__":
    import sys
    args = sys.argv[1:] or ["NVDA", "META", "1489.T"]
    no_intraday = "--no-intraday" in args
    args = [a for a in args if not a.startswith("--")]
    res = gather_chart_context(args, intraday=not no_intraday)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("\n" + format_for_prompt(res))
