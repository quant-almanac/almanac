"""市場指標 API — コマンドストリップ用のリアルタイム相場データ"""

import json
import os
import time
from datetime import datetime
from fastapi import APIRouter
from vix_classification import classify_vix

router = APIRouter()

CACHE_PATH = os.path.expanduser("~/portfolio-bot/market_snapshot.json")
CACHE_TTL = 300  # 5分キャッシュ


def _fetch_market_data() -> dict:
    """yfinance で主要市場指標を一括取得"""
    import yfinance as yf

    tickers = {
        "^VIX":     "VIX",
        "SPY":      "SPY",
        "^N225":    "NK225",
        "USDJPY=X": "USDJPY",
        "^TNX":     "US10Y",
        "^IRX":     "US2Y",
        "GC=F":     "GOLD",
        "CL=F":     "OIL",
        "DX-Y.NYB": "DXY",
    }

    result = {}
    try:
        data = yf.download(
            list(tickers.keys()),
            period="3mo",
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception:
        return result

    for symbol, key in tickers.items():
        try:
            if len(tickers) > 1:
                col = data[symbol] if symbol in data.columns.get_level_values(0) else None
            else:
                col = data

            if col is None or col.empty:
                continue

            close = col["Close"].dropna()
            if close.empty:
                continue

            price = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else price
            chg = ((price - prev) / prev * 100) if prev != 0 else 0

            entry: dict = {
                "price": round(price, 2),
                "change": round(chg, 2),
            }

            # SPY / NK225: 50日MA との乖離率
            if key in ("SPY", "NK225") and len(close) >= 50:
                ma50 = float(close.rolling(50).mean().iloc[-1])
                entry["ma50"] = round(ma50, 2)
                entry["ma50_diff"] = round((price - ma50) / ma50 * 100, 2)

            # VIX: レベル判定
            if key == "VIX":
                entry["level"] = classify_vix(price)

            # 利回り系: スプレッド計算
            if key == "US10Y":
                entry["price"] = round(price, 3)

            result[key] = entry
        except Exception:
            continue

    # イールドスプレッド
    if "US10Y" in result and "US2Y" in result:
        spread = result["US10Y"]["price"] - result["US2Y"]["price"]
        result["YIELD_SPREAD"] = {
            "price": round(spread, 3),
            "inverted": spread < 0,
        }

    return result


def _get_cached_or_fetch() -> dict:
    """キャッシュがあれば返す、なければ取得して保存"""
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            if age < CACHE_TTL:
                return cached
        except Exception:
            pass

    data = _fetch_market_data()
    data["_ts"] = time.time()
    # A time-only value cannot be aged on the following day.  Scenario
    # activation policies consume this field, so persist a full timestamp.
    data["as_of"] = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

    return data


@router.get("/api/market")
def get_market():
    """主要市場指標を返す"""
    try:
        data = _get_cached_or_fetch()
        return data
    except Exception as e:
        return {"error": str(e)}
