"""
options_fetcher — ALMANAC v5.1
==============================
yfinance options chain から IV Rank / 25Δ Skew / Put-Call Ratio を取得。

責務:
- 指定 ticker のオプション最近接月次 expiration を取得
- ATM IV / 25Δ Skew / Put-Call Ratio を計算
- 24h TTL で data/options_cache/{ticker}.json にキャッシュ
- 252 日 ATM IV 履歴を data/options_iv/{ticker}.parquet に append し IV Rank を percentile で計算
- 取得不可 ticker（投信、日本株のオプション無し銘柄等）は None で継続

使い方:
    python options_fetcher.py refresh --top 30
    → priority_actions 対象 top 30 を更新

Notes:
- yfinance の options chain は不安定。失敗時は None を返してフラグなしで継続
- 日本株の option_chain は基本空。米株中心に運用
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pseudo_tickers import is_pseudo_market_ticker

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "options_cache"
IV_HISTORY_DIR = BASE_DIR / "data" / "options_iv"
CACHE_TTL_HOURS = 24

# 日本株や投信などオプション無しと判明したらキャッシュに negative エントリーを残す
NEGATIVE_TTL_HOURS = 24

SKIP_TICKERS = frozenset({
    "SLIM_SP500", "SLIM_ORCAN", "MNXACT", "IFREE_FANGPLUS",
    "NOMURA_SEMI", "AVGO_特定", "AVGO_一般", "AVGO_toku",
})


# ファイル名として安全な ticker のみ許可（path traversal 対策）
import re as _re
_SAFE_TICKER_RE = _re.compile(r"^[A-Za-z0-9._=^-]+$")


def _is_safe_ticker(t: str) -> bool:
    """ticker がファイル名として安全か検証。
    `../`, `/`, NUL バイト等を弾く。yfinance ティッカー（USDJPY=X, BRK-B, 1489.T 等）は許可。"""
    if not t or not isinstance(t, str) or len(t) > 32:
        return False
    return bool(_SAFE_TICKER_RE.match(t))


# ============================================================
# 計算ヘルパー
# ============================================================

def _atm_iv_from_chain(chain, last_price: float) -> Optional[float]:
    """ATM 近傍の call と put の IV 平均"""
    try:
        calls = chain.calls
        puts = chain.puts
        if calls is None or puts is None:
            return None
        # Closest strike to last_price
        c_idx = (calls["strike"] - last_price).abs().idxmin()
        p_idx = (puts["strike"] - last_price).abs().idxmin()
        c_iv = float(calls.loc[c_idx, "impliedVolatility"])
        p_iv = float(puts.loc[p_idx, "impliedVolatility"])
        if not math.isfinite(c_iv) or not math.isfinite(p_iv):
            return None
        if c_iv <= 0 or p_iv <= 0:
            return None
        return (c_iv + p_iv) / 2
    except Exception:
        return None


def _skew_25d(chain, last_price: float) -> Optional[float]:
    """25Δ Skew 近似: OTM 25% 近辺の put IV - 同程度 OTM call IV"""
    try:
        calls = chain.calls
        puts = chain.puts
        # OTM 25% のストライク（簡易プロキシ）
        target_call = last_price * 1.10  # OTM 10% call
        target_put = last_price * 0.90   # OTM 10% put
        c_idx = (calls["strike"] - target_call).abs().idxmin()
        p_idx = (puts["strike"] - target_put).abs().idxmin()
        c_iv = float(calls.loc[c_idx, "impliedVolatility"])
        p_iv = float(puts.loc[p_idx, "impliedVolatility"])
        if not math.isfinite(c_iv) or not math.isfinite(p_iv):
            return None
        return float(p_iv - c_iv)
    except Exception:
        return None


def _put_call_ratio(chain) -> Optional[dict]:
    """OI ベースと出来高ベースの両方"""
    try:
        calls = chain.calls
        puts = chain.puts
        c_oi = float(calls["openInterest"].fillna(0).sum())
        p_oi = float(puts["openInterest"].fillna(0).sum())
        c_vol = float(calls["volume"].fillna(0).sum())
        p_vol = float(puts["volume"].fillna(0).sum())
        out = {}
        if c_oi > 0:
            out["pcr_oi"] = round(p_oi / c_oi, 3)
        if c_vol > 0:
            out["pcr_volume"] = round(p_vol / c_vol, 3)
        return out or None
    except Exception:
        return None


# ============================================================
# IV 履歴管理（IV Rank 計算用）
# ============================================================

def _append_iv_history(ticker: str, atm_iv: float, dt: Optional[str] = None) -> None:
    """data/options_iv/{ticker}.parquet に (date, atm_iv) を append。
    重複 date は上書き。"""
    if not _is_safe_ticker(ticker):
        return
    try:
        import pandas as pd  # type: ignore
        IV_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = IV_HISTORY_DIR / f"{ticker}.parquet"
        date_str = (dt or datetime.now().isoformat())[:10]
        new_row = pd.DataFrame([{"date": date_str, "atm_iv": atm_iv}])
        if path.exists():
            try:
                df = pd.read_parquet(path)
                df = df[df["date"] != date_str]  # 同日重複を除去
                df = pd.concat([df, new_row], ignore_index=True)
            except Exception:
                df = new_row
        else:
            df = new_row
        df = df.sort_values("date").tail(400)  # 最大 400 営業日
        df.to_parquet(path)
    except Exception:
        pass


def _compute_iv_rank(ticker: str, current_iv: float) -> Optional[float]:
    """過去 252 営業日の ATM IV から percentile rank を返す。30 日未満は None."""
    try:
        import pandas as pd  # type: ignore
        path = IV_HISTORY_DIR / f"{ticker}.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path).tail(252)
        if len(df) < 30:
            return None
        ivs = df["atm_iv"].astype(float).dropna()
        below = (ivs < current_iv).sum()
        rank = below / len(ivs) * 100
        return round(float(rank), 1)
    except Exception:
        return None


# ============================================================
# キャッシュ
# ============================================================

def _cache_path(ticker: str) -> Path:
    """ticker の cache file path を返す。安全でない名前は '_invalid_' に置換。
    実利用パスでは fetch_one 側でも _is_safe_ticker チェックしている（defense in depth）。"""
    safe = ticker if _is_safe_ticker(ticker) else "_invalid_"
    return CACHE_DIR / f"{safe}.json"


def _load_cached(ticker: str) -> Optional[dict]:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data.get("fetched_at", ""))
        ttl = NEGATIVE_TTL_HOURS if data.get("error") else CACHE_TTL_HOURS
        if datetime.now() - fetched_at < timedelta(hours=ttl):
            return data
    except Exception:
        return None
    return None


def _save_cache(ticker: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(ticker)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 公開関数
# ============================================================

def fetch_one(ticker: str, *, force: bool = False) -> Optional[dict]:
    """1 ticker の options シグナルを返す。SKIP/失敗時は None."""
    if ticker in SKIP_TICKERS or is_pseudo_market_ticker(ticker):
        return None
    if not _is_safe_ticker(ticker):
        # path traversal 等の不正 ticker 名は黙ってスキップ
        return None

    if not force:
        cached = _load_cached(ticker)
        if cached is not None:
            return None if cached.get("error") else cached

    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        opts = t.options or []
        if not opts:
            _save_cache(ticker, {"error": "no_options", "fetched_at": datetime.now().isoformat()})
            return None
        # 最近接の expiration（>3 日先）
        today = datetime.now().date()
        expiry = None
        for exp_str in opts:
            try:
                d = datetime.strptime(exp_str, "%Y-%m-%d").date()
                if (d - today).days >= 3:
                    expiry = exp_str
                    break
            except Exception:
                continue
        if expiry is None:
            expiry = opts[0]

        chain = t.option_chain(expiry)
        last_price = None
        try:
            fi = t.fast_info
            last_price = float(getattr(fi, "last_price", None) or 0) or None
        except Exception:
            pass
        if last_price is None or last_price <= 0:
            try:
                last_price = float((t.info or {}).get("regularMarketPrice") or 0) or None
            except Exception:
                last_price = None
        if not last_price:
            _save_cache(ticker, {"error": "no_last_price", "fetched_at": datetime.now().isoformat()})
            return None

        atm_iv = _atm_iv_from_chain(chain, last_price)
        skew = _skew_25d(chain, last_price)
        pcr = _put_call_ratio(chain) or {}

        if atm_iv is None:
            _save_cache(ticker, {"error": "no_atm_iv", "fetched_at": datetime.now().isoformat()})
            return None

        # IV history を update して IV Rank を計算
        _append_iv_history(ticker, atm_iv)
        iv_rank = _compute_iv_rank(ticker, atm_iv)

        result = {
            "ticker":      ticker,
            "expiry":      expiry,
            "last_price":  round(last_price, 4),
            "atm_iv":      round(atm_iv, 4),
            "iv_rank":     iv_rank,        # None なら履歴不足
            "skew_25d":    round(skew, 4) if skew is not None else None,
            **pcr,
            "fetched_at":  datetime.now().isoformat(timespec="seconds"),
        }
        _save_cache(ticker, result)
        return result
    except Exception as e:
        _save_cache(ticker, {"error": str(e)[:120], "fetched_at": datetime.now().isoformat()})
        return None


def get_option_signals(tickers: list[str], *, force: bool = False, max_n: int = 30) -> dict[str, dict]:
    """複数 ticker のオプションシグナル。失敗 ticker は dict から省略。"""
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for t in tickers:
        if not t or t in seen:
            continue
        seen.add(t)
        if len(out) >= max_n:
            break
        sig = fetch_one(t, force=force)
        if sig:
            out[t] = sig
        # rate limit ガード
        time.sleep(0.2)
    return out


def format_for_prompt(sigs: dict[str, dict]) -> str:
    """Opus プロンプト用の簡潔ブロック"""
    if not sigs:
        return ""
    lines = ["## 📊 OPTIONS_SENTIMENT（過熱・流動性低下フラグ）",
             "| ticker | atm_iv | iv_rank | skew_25d | pcr_oi | pcr_vol |",
             "|---|---|---|---|---|---|"]
    for t, s in sigs.items():
        lines.append(
            f"| {t} | {s.get('atm_iv','—')} | "
            f"{s.get('iv_rank','—')} | {s.get('skew_25d','—')} | "
            f"{s.get('pcr_oi','—')} | {s.get('pcr_volume','—')} |"
        )
    lines.append("")
    lines.append("【活用方法】")
    lines.append("- IV Rank > 70: 過熱、buy 推奨を控えるか trim/take_profit 優先")
    lines.append("- IV Rank < 30: 平穏、新規エントリーの好機")
    lines.append("- skew_25d > 0.05: テールリスクプレミアム拡大、防御寄り")
    lines.append("- pcr_oi > 1.2: ベアセンチメント、コントラリアン目線（買い検討）")
    lines.append("- pcr_oi < 0.5: ブルセンチメント過剰、過熱注意")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def _top_tickers_from_priority_actions(top: int = 30) -> list[str]:
    """ai_portfolio_analysis.json の priority_actions から ticker 集約"""
    try:
        path = BASE_DIR / "ai_portfolio_analysis.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        synth = data.get("synthesis") or {}
        actions = synth.get("priority_actions") or []
        out: list[str] = []
        seen: set[str] = set()
        for a in actions:
            t = a.get("ticker") if isinstance(a, dict) else None
            if t and t not in seen and t not in SKIP_TICKERS and not is_pseudo_market_ticker(t):
                seen.add(t)
                out.append(t)
                if len(out) >= top:
                    break
        return out
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    cmd = args[0] if args else "selftest"

    if cmd == "refresh":
        top = 30
        force = False
        for i, a in enumerate(args[1:]):
            if a == "--top" and i + 2 < len(args):
                top = int(args[i + 2])
            if a == "--force":
                force = True
        tickers = _top_tickers_from_priority_actions(top=top)
        print(f"options_fetcher.refresh: {len(tickers)} tickers from priority_actions")
        sigs = get_option_signals(tickers, force=force, max_n=top)
        print(json.dumps(sigs, ensure_ascii=False, indent=2))
        print(f"✓ {len(sigs)}/{len(tickers)} tickers had options data")

    elif cmd == "selftest":
        tickers = args[1:] if len(args) > 1 else ["NVDA", "META", "SPY"]
        sigs = get_option_signals(tickers, force=True)
        print(json.dumps(sigs, ensure_ascii=False, indent=2))
        print()
        print(format_for_prompt(sigs))

    else:
        print("Usage: options_fetcher.py [selftest [tickers...] | refresh [--top N] [--force]]")
