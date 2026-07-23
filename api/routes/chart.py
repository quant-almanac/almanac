"""
GET /api/chart/{ticker}?days=60
OHLCV + MA20 + MA50 を返す
Parquet キャッシュ優先 → yfinance フォールバック
"""
import asyncio
import math
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _clean(lst: list) -> list:
    """NaN/None を JSON-safe な None に変換"""
    return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else v for v in lst]


def _fetch_chart(ticker: str, days: int) -> dict:
    try:
        import pandas as pd

        parquet_path = BASE_DIR / "data" / "ohlcv" / f"{ticker}.parquet"

        if parquet_path.exists():
            df = pd.read_parquet(parquet_path)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            df = df.sort_index()
        else:
            import yfinance as yf
            raw = yf.download(ticker, period="6mo", auto_adjust=True, progress=False)
            if raw.empty:
                return {"error": f"{ticker} データ取得失敗", "ticker": ticker}
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.droplevel(1)
            df = raw

        # カラム名の重複を除去（MultiIndex droplevel後に発生しうる）
        df = df.loc[:, ~df.columns.duplicated()]

        close_col = next((c for c in ["Close", "close", "Adj Close"] if c in df.columns), df.columns[0])

        # close_col がまだ DataFrame（複数列）の場合は最初の列を使う
        close_series = df[close_col]
        if hasattr(close_series, 'columns'):
            close_series = close_series.iloc[:, 0]

        # MA は切り取り前に計算（精度確保）
        df["ma20"] = close_series.rolling(20).mean()
        df["ma50"] = close_series.rolling(50).mean()
        df = df.tail(days)

        dates = [str(d)[:10] for d in df.index]
        close = close_series.tail(days).round(2).tolist()
        ma20 = df["ma20"].round(2).tolist()
        ma50 = df["ma50"].round(2).tolist()
        volume = df["Volume"].tolist() if "Volume" in df.columns else []

        current = next((v for v in reversed(close) if v is not None), None)

        return {
            "ticker": ticker,
            "dates": dates,
            "close": _clean(close),
            "ma20": _clean(ma20),
            "ma50": _clean(ma50),
            "volume": _clean(volume),
            "current": current,
        }
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


@router.get("/api/chart/{ticker}")
async def get_chart(ticker: str, days: int = 60):
    return await asyncio.to_thread(_fetch_chart, ticker, days)
