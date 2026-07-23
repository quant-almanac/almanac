"""portfolio_risk_returns.py — P1-1: 現在 holdings の ex-ante リスク用リターン再構成

daily_performance の NAV 系列はバグ修正前(汚染)かつ短い(N<200で CVaR テール不安定)。
本モジュールは **現在の holdings ウェイト** を **data/ohlcv parquet の過去市場価格**(会計バグの
影響を受けない・数百営業日)に当てて、ex-ante のポートフォリオ日次リターンを再構成する。

  portfolio_return_t = Σ_i  w_i × r_{i,t}
    w_i  = 現在の JPY 時価ウェイト (covered 銘柄で正規化)
    r_{i,t} = 銘柄 i の JPY 建て日次リターン
              USD 建て (含む proxy) は close×USDJPY の pct_change で FX リスク込み

これは「過去の実成績」ではなく「現ウェイトを過去に当てたストレス推定」。
coverage_ratio が低い (proxy/parquet で賄えない) 場合は呼出側で daily_performance に fallback。
"""
from __future__ import annotations

from typing import Optional, Tuple

# parquet 無し銘柄の proxy (USD index 建て → FX 換算で JPY リターン化)
RISK_PROXY_MAP = {
    "SLIM_SP500": "VOO",
    "SLIM_ORCAN": "VT",
}
# 現金・MMF は riskless (≈0 リターン) として covered 扱い
RISK_RISKLESS_TICKERS = {
    "CASH_JPY", "CASH_USD", "CASH_JPY_SBI", "CASH_JPY_SBI_WIFE", "GS_MMF_USD",
}
# mandate 未確定で proxy 不能 → 除外し coverage ratio に反映 (Codex r3#2 既定)
RISK_EXCLUDE_TICKERS = {"MNXACT"}

# proxy 先は USD 建て扱い (FX 換算する)
_USD_PROXIES = {"VOO", "VT"}


def _close_series(df):
    """parquet DataFrame から Close の 1 次元 Series を取り出す (MultiIndex 対応)。"""
    if df is None or len(df) == 0:
        return None
    try:
        close = df["Close"]
    except Exception:
        return None
    # MultiIndex / DataFrame の場合は最初の列を採用
    if hasattr(close, "columns"):
        if close.shape[1] == 0:
            return None
        close = close.iloc[:, 0]
    return close.dropna()


def _is_usd(effective_ticker: str, currency: Optional[str]) -> bool:
    if effective_ticker in _USD_PROXIES:
        return True
    if currency:
        return str(currency).upper() == "USD"
    # フォールバック: .T / 数字以外は USD とみなす
    return not (effective_ticker.endswith(".T") or effective_ticker.isdigit())


def reconstruct_portfolio_returns(
    positions: list,
    *,
    total_jpy: float,
    cash_jpy: float = 0.0,
    lookback_days: int = 300,
    ohlcv_loader=None,
    fx_ticker: str = "USDJPY=X",
) -> Tuple[Optional["object"], float, dict]:
    """現在 holdings から ex-ante 日次リターン系列を再構成する。

    Returns:
      (returns: pd.Series | None, coverage_ratio: float, meta: dict)
      coverage_ratio = parquet/proxy/riskless で賄えた JPY 時価 / total_jpy
    """
    import pandas as pd

    if ohlcv_loader is None:
        from data_fetcher import load_ohlcv as ohlcv_loader  # type: ignore

    meta = {"covered": [], "uncovered": [], "riskless": [], "excluded": [], "proxied": {}}

    if not positions or not total_jpy or total_jpy <= 0:
        return None, 0.0, {**meta, "error": "no positions / total_jpy"}

    # ── 1. effective ticker ごとに JPY 時価を集約 ──
    value_by_eff: dict = {}
    currency_by_eff: dict = {}
    # 現金 (snapshot の cash_jpy: positions 外で保持) は riskless として covered に含める。
    riskless_value = float(cash_jpy or 0.0)
    if riskless_value > 0:
        meta["riskless"].append("cash_jpy")
    uncovered_value = 0.0

    for p in positions:
        if not isinstance(p, dict):
            continue
        tk = p.get("ticker") or p.get("key")
        val = float(p.get("value_jpy") or 0.0)
        if not tk or val == 0.0:
            continue
        if tk in RISK_EXCLUDE_TICKERS:
            meta["excluded"].append(tk)
            uncovered_value += val
            continue
        if tk in RISK_RISKLESS_TICKERS:
            meta["riskless"].append(tk)
            riskless_value += val
            continue
        eff = RISK_PROXY_MAP.get(tk, tk)
        if eff != tk:
            meta["proxied"][tk] = eff
        value_by_eff[eff] = value_by_eff.get(eff, 0.0) + val
        # currency は position 由来 (proxy は後で USD 判定)
        currency_by_eff.setdefault(eff, p.get("currency"))

    # ── 2. FX 系列 ──
    fx_close = _close_series(ohlcv_loader(fx_ticker))
    if fx_close is None:
        return None, 0.0, {**meta, "error": f"FX series ({fx_ticker}) unavailable"}

    # ── 3. 各 effective ticker の JPY 建てリターン ──
    ret_cols = {}
    covered_value = riskless_value
    for eff, val in value_by_eff.items():
        close = _close_series(ohlcv_loader(eff))
        if close is None:
            meta["uncovered"].append(eff)
            uncovered_value += val
            continue
        if _is_usd(eff, currency_by_eff.get(eff)):
            jpy_close = (close * fx_close.reindex(close.index).ffill()).dropna()
        else:
            jpy_close = close
        r = jpy_close.pct_change().dropna()
        if len(r) < 30:
            meta["uncovered"].append(eff)
            uncovered_value += val
            continue
        ret_cols[eff] = r
        covered_value += val
        meta["covered"].append(eff)

    coverage_ratio = covered_value / total_jpy if total_jpy > 0 else 0.0
    meta["coverage_ratio"] = round(coverage_ratio, 4)
    meta["uncovered_value_jpy"] = round(uncovered_value, 0)

    if not ret_cols:
        return None, coverage_ratio, {**meta, "error": "no return series"}

    # ── 4. 共通日付で揃え、現ウェイトで加重 ──
    df = pd.DataFrame(ret_cols).dropna(how="any")
    if lookback_days and len(df) > lookback_days:
        df = df.iloc[-lookback_days:]
    if len(df) < 30:
        return None, coverage_ratio, {**meta, "error": "insufficient overlapping history"}

    # weights: covered 銘柄 (riskless 含む) で正規化。riskless は return 0 列として加算。
    weight_denom = sum(value_by_eff[e] for e in ret_cols) + riskless_value
    if weight_denom <= 0:
        return None, coverage_ratio, {**meta, "error": "zero weight denom"}
    weights = {e: value_by_eff[e] / weight_denom for e in ret_cols}
    # riskless のウェイト分は return 0 なので加算不要（合計が1未満になるだけ＝現金は無リスク寄与）

    port_ret = sum(df[e] * w for e, w in weights.items())
    port_ret = port_ret.dropna()
    meta["observations"] = int(len(port_ret))
    meta["riskless_weight"] = round(riskless_value / weight_denom, 4) if weight_denom else 0.0
    return port_ret, coverage_ratio, meta
