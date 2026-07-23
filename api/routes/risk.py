"""
GET /api/risk    — VaR/CVaR/ドローダウン計算（ポートフォリオOHLCVベース）
GET /api/tearsheet — 最新 QuantStats HTML ティアシート情報
"""
import asyncio
import json
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent

# OHLCV parquetがない投信ティッカー
SKIP_TICKERS = {"SLIM_SP500", "SLIM_ORCAN", "MNXACT", "IFREE_FANGPLUS", "NOMURA_SEMI"}


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _currency_exposure_from_snapshot(snapshot: dict | None) -> dict:
    """portfolio snapshot の通貨内訳から、外貨建て比率をUI向けに整形する。"""
    snapshot = snapshot or {}
    breakdown = snapshot.get("currency_breakdown") or {}

    by_currency: dict[str, dict] = {}
    breakdown_total = 0.0
    for raw_currency, raw_item in breakdown.items():
        currency = str(raw_currency or "UNKNOWN").upper()
        if isinstance(raw_item, dict):
            value_jpy = _as_float(raw_item.get("value_jpy"))
        else:
            value_jpy = _as_float(raw_item)
        value_jpy = max(value_jpy, 0.0)
        breakdown_total += value_jpy
        by_currency[currency] = {"value_jpy": int(round(value_jpy))}

    total_jpy = _as_float(snapshot.get("total_jpy"))
    if total_jpy <= 0:
        total_jpy = breakdown_total

    def _ratio(value: float) -> float:
        return round(value / total_jpy, 4) if total_jpy > 0 else 0.0

    def _pct(value: float) -> float:
        return round(value / total_jpy * 100, 2) if total_jpy > 0 else 0.0

    for currency, item in by_currency.items():
        value = _as_float(item.get("value_jpy"))
        item["ratio"] = _ratio(value)
        item["pct"] = _pct(value)

    jpy_value = _as_float((by_currency.get("JPY") or {}).get("value_jpy"))
    foreign_value = sum(
        _as_float(item.get("value_jpy"))
        for currency, item in by_currency.items()
        if currency != "JPY"
    )
    unknown_value = max(total_jpy - breakdown_total, 0.0)

    return {
        "total_jpy": int(round(total_jpy)),
        "foreign_value_jpy": int(round(foreign_value)),
        "foreign_ratio": _ratio(foreign_value),
        "foreign_pct": _pct(foreign_value),
        "jpy_value_jpy": int(round(jpy_value)),
        "jpy_ratio": _ratio(jpy_value),
        "jpy_pct": _pct(jpy_value),
        "unknown_value_jpy": int(round(unknown_value)),
        "unknown_ratio": _ratio(unknown_value),
        "unknown_pct": _pct(unknown_value),
        "by_currency": by_currency,
    }


def _load_portfolio_snapshot_for_risk() -> dict:
    from api.routes.portfolio import get_cached_snapshot

    return get_cached_snapshot()


def _calc_risk() -> dict:
    currency_exposure = _currency_exposure_from_snapshot({})
    try:
        import pandas as pd
        import numpy as np
        from risk_engine import calculate_var_cornish_fisher, calculate_cvar, calculate_drawdown

        # holdings.json からポートフォリオ構成を取得
        holdings_path = BASE_DIR / "holdings.json"
        if not holdings_path.exists():
            return _empty_risk(currency_exposure)

        holdings = json.loads(holdings_path.read_text())
        ohlcv_dir = BASE_DIR / "data" / "ohlcv"
        currency_exposure = _currency_exposure_from_snapshot(_load_portfolio_snapshot_for_risk())

        # P1-13: weight を current_price × shares (FX 換算済) に変更。
        # 旧コードは shares * entry_price を使っていたため、含み益・含み損で歪み、
        # 危ない時ほどリスクが過小評価されるバグがあった。
        # FX は account.json or utils.get_fx_rate_cached で取得。
        try:
            import sys as _sys
            _sys.path.insert(0, str(BASE_DIR))
            from utils import get_fx_rate_cached
            _fx, _ = get_fx_rate_cached(account_json_path=BASE_DIR / "account.json")
            fx_rate = float(_fx)
        except Exception:
            try:
                fx_rate = float(json.loads((BASE_DIR / "account.json").read_text()).get("fx_rate_usdjpy", 150))
            except Exception:
                fx_rate = 150.0

        # 各銘柄のClose価格を読み込み
        close_frames = {}
        weights = {}
        for key, pos in holdings.items():
            ticker = pos.get("ticker", key)
            if key in SKIP_TICKERS or ticker in SKIP_TICKERS:
                continue
            parquet = ohlcv_dir / f"{ticker}.parquet"
            if not parquet.exists():
                continue
            df = pd.read_parquet(parquet)
            # MultiIndex columns 対応
            if isinstance(df.columns, pd.MultiIndex):
                close_col = [c for c in df.columns if c[0] == "Close"]
                if close_col:
                    close_frames[ticker] = df[close_col[0]]
            elif "Close" in df.columns:
                close_frames[ticker] = df["Close"]

            shares = float(pos.get("shares", 0) or 0)
            currency = (pos.get("currency") or "JPY").upper()
            # 最新 Close を current_price として使う（live yfinance は遅いのでキャッシュ済み parquet 末尾）
            try:
                current_price = float(close_frames[ticker].dropna().iloc[-1])
            except Exception:
                current_price = float(pos.get("entry_price", 0) or 0)
            value_native = shares * current_price
            value_jpy = value_native * fx_rate if currency == "USD" else value_native
            weights[ticker] = max(value_jpy, 0.0)

        if len(close_frames) < 2:
            return _empty_risk(currency_exposure)

        # 全銘柄のClose価格を結合
        prices = pd.DataFrame(close_frames)
        prices = prices.dropna(how="all").ffill().dropna()

        if len(prices) < 30:
            return _empty_risk(currency_exposure)

        # 日次リターン
        daily_returns = prices.pct_change().dropna()

        # ポートフォリオ加重リターン
        total_weight = sum(weights.get(t, 1) for t in daily_returns.columns)
        if total_weight <= 0:
            total_weight = len(daily_returns.columns)
        w = pd.Series({t: weights.get(t, 1) / total_weight for t in daily_returns.columns})
        portfolio_returns = daily_returns.dot(w)

        # 直近90日に絞る
        portfolio_returns = portfolio_returns.tail(90)

        if len(portfolio_returns) < 20:
            return _empty_risk(currency_exposure)

        var_result = calculate_var_cornish_fisher(portfolio_returns, confidence=0.95)
        cvar_result = calculate_cvar(portfolio_returns, confidence=0.95)
        dd_result = calculate_drawdown(portfolio_returns)

        var_95 = var_result.get("var_pct", 0.0) if isinstance(var_result, dict) else 0.0
        cvar_95 = cvar_result.get("cvar_pct", 0.0) if isinstance(cvar_result, dict) else 0.0

        dd_series_raw = dd_result.get("drawdown_series", []) if isinstance(dd_result, dict) else []
        if hasattr(dd_series_raw, "tolist"):
            dd_series_raw = dd_series_raw.tolist()
        dd_series = [round(float(v) * 100, 2) for v in dd_series_raw]

        current_dd = round(float(dd_result.get("current_dd", 0.0)) * 100, 2)
        max_dd = round(float(dd_result.get("max_dd", 0.0)) * 100, 2)

        # VaR を %表示 (小数→%) に変換
        var_pct = round(float(var_95) * 100, 2) if abs(float(var_95)) < 1 else round(float(var_95), 2)
        cvar_pct = round(float(cvar_95) * 100, 2) if abs(float(cvar_95)) < 1 else round(float(cvar_95), 2)

        result = {
            "var_95": var_pct,
            "cvar_95": cvar_pct,
            "current_dd": current_dd,
            "max_dd": max_dd,
            "drawdown_series": dd_series[-60:],  # 直近60日分
            "sample_size": len(portfolio_returns),
            "currency_exposure": currency_exposure,
        }

        try:
            from risk_engine import detect_regime_hmm, behavioral_bias_adjustment
            regime = detect_regime_hmm(portfolio_returns)
            state_probs = regime.get("state_probs", {})
            bbapt = behavioral_bias_adjustment(state_probs)
            result["behavioral_bias"] = bbapt
        except Exception:
            pass

        try:
            import sys
            sys.path.insert(0, str(BASE_DIR))
            from macro_fetcher import get_macro_context
            macro = get_macro_context()
            result["macro"] = {
                "vix": macro.get("vix"),
                "vix_capitulation": macro.get("vix_capitulation", False),
                "vix_fear": macro.get("vix_fear", False),
                "vix_status": macro.get("vix_status", "unknown"),
                "fed_rate": macro.get("fed_rate"),
                "yield_spread": macro.get("yield_spread"),
                "yield_inverted": macro.get("yield_inverted", False),
                "cpi_yoy": macro.get("cpi_yoy"),
                "unemp_rate": macro.get("unemp_rate"),
                "macro_adj": macro.get("macro_adj", 0),
                "source": macro.get("source", "unknown"),
            }
        except Exception:
            pass

        return result
    except Exception as e:
        return {**_empty_risk(currency_exposure), "error": str(e)}


def _empty_risk(currency_exposure: dict | None = None) -> dict:
    return {
        "var_95": 0.0,
        "cvar_95": 0.0,
        "current_dd": 0.0,
        "max_dd": 0.0,
        "drawdown_series": [],
        "sample_size": 0,
        "currency_exposure": currency_exposure or _currency_exposure_from_snapshot({}),
    }


@router.get("/api/risk")
async def get_risk():
    return await asyncio.to_thread(_calc_risk)


@router.get("/api/tearsheet")
async def list_tearsheets():
    """reports/ ディレクトリ内の QuantStats HTML ティアシート一覧を返す"""
    reports_dir = BASE_DIR / "reports"
    if not reports_dir.exists():
        return {"files": [], "latest": None}
    files = sorted(reports_dir.glob("tearsheet_*.html"), reverse=True)
    names = [f.name for f in files]
    return {"files": names, "latest": names[0] if names else None}


@router.get("/api/tearsheet/{filename}")
async def get_tearsheet(filename: str):
    """指定ティアシート HTML を返す"""
    if "/" in filename or "\\" in filename or ".." in filename:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = BASE_DIR / "reports" / filename
    if not path.exists() or not path.name.startswith("tearsheet_"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), media_type="text/html")


# ============================================================
# v5.1 Phase 3: Options Sentiment（IV Rank / Skew / PCR）
# ============================================================

@router.get("/api/options_sentiment")
async def get_options_sentiment():
    """data/options_cache/{ticker}.json の最新キャッシュを集約して返す。
    cron `options_fetcher.py refresh` が更新する想定。"""
    cache_dir = BASE_DIR / "data" / "options_cache"
    if not cache_dir.exists():
        return {"signals": [], "as_of": None}

    signals = []
    latest_ts = None
    for path in cache_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("error"):
                continue
            signals.append({
                "ticker":     data.get("ticker", path.stem),
                "expiry":     data.get("expiry"),
                "last_price": data.get("last_price"),
                "atm_iv":     data.get("atm_iv"),
                "iv_rank":    data.get("iv_rank"),
                "skew_25d":   data.get("skew_25d"),
                "pcr_oi":     data.get("pcr_oi"),
                "pcr_volume": data.get("pcr_volume"),
                "fetched_at": data.get("fetched_at"),
            })
            if data.get("fetched_at") and (latest_ts is None or data["fetched_at"] > latest_ts):
                latest_ts = data["fetched_at"]
        except Exception:
            continue

    # iv_rank 降順（過熱優先）
    signals.sort(key=lambda x: (x.get("iv_rank") or -1), reverse=True)
    return {"signals": signals, "as_of": latest_ts}


# ============================================================
# v5.1 Phase 4: Factor Exposure（MOM/BAB/QMJ 回帰）
# ============================================================

@router.get("/api/factor_exposure")
async def get_factor_exposure():
    """factor_attribution.json を読み、保有銘柄ごとのファクター β / t-stat を返す。
    factor_attribution.py run が定期更新する想定。

    現在の factor_attribution は portfolio 全体の 1 行を出力するため、ここでは
    "positions": [{ticker: "PORTFOLIO", betas: ..., t_stats: ..., r_squared: ...}]
    に整形して返し、front-end (FactorExposurePanel) が一貫して表示できるようにする。
    """
    fa_path = BASE_DIR / "factor_attribution.json"
    if not fa_path.exists():
        return {"positions": [], "as_of": None}
    try:
        raw = json.loads(fa_path.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": [], "as_of": None}

    if raw.get("error"):
        return {"positions": [], "as_of": raw.get("as_of"), "error": raw["error"]}

    # 既存スキーマ: alpha / betas / beta_tstats / r_squared / factors_used
    betas = raw.get("betas") or {}
    tstats = raw.get("beta_tstats") or {}

    # FACTOR_TICKERS の economic_rationale フラグを併送
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from factor_attribution import FACTOR_TICKERS as _FT
        rationales = {k: bool(v.get("economic_rationale", True)) for k, v in _FT.items()}
        descriptions = {k: v.get("description", "") for k, v in _FT.items()}
    except Exception:
        rationales = {}
        descriptions = {}

    positions = [{
        "ticker":     "PORTFOLIO",
        "alpha":      raw.get("alpha"),
        "alpha_t":    raw.get("alpha_tstat"),
        "betas":      betas,
        "t_stats":    tstats,
        "r_squared":  raw.get("r_squared"),
        "months":     raw.get("n_months"),
        "verdict":    raw.get("verdict"),
    }]

    return {
        "positions":      positions,
        "as_of":          raw.get("as_of"),
        "factor_metadata": {
            "rationale":   rationales,
            "description": descriptions,
        },
    }
