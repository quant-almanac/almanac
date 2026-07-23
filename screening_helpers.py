"""
screening_helpers.py — スクリーナー共通ユーティリティ
（screener.py / short_screener.py / margin_long_screener.py / long_term_screener.py 共通）

提供:
  - load_universe(key)            tickers.json からティッカーリスト取得
  - filter_us_only / filter_jp_only
  - get_edgar_summary(ticker)     EDGAR ファンダ取得（薄ラッパー、失敗時 dict_with_None）
  - days_to_next_earnings(ticker) 次回決算までの営業日数（None=取得不可）
  - liquidity_check(ticker, hist) 30日平均売買代金が下限を超えるか
  - get_historical_win_rate(strategy, ticker)  signal_history.json の勝率
  - get_regime_confidence()       regime_state.json の confidence
  - calc_composite_score(...)     0-100 正規化された複合スコア

NOTE:
  - 全関数は import 失敗・I/O 失敗を握り潰してデフォルト値を返す（スクリーナーの run を絶対に止めない）
  - キャッシュは call ごと薄く（プロセス内 dict）。プロセス外キャッシュは各 fetcher 側に委ねる。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent

# US: $5M / day, JP: 3億円/day をハードフロアに
US_LIQUIDITY_MIN_USD = 5_000_000
JP_LIQUIDITY_MIN_JPY = 300_000_000

# 既存 holdings は流動性フィルタを免除（保有中銘柄を勝手に外さない）
_HOLDINGS_CACHE: set[str] | None = None


def _holdings_set() -> set[str]:
    global _HOLDINGS_CACHE
    if _HOLDINGS_CACHE is not None:
        return _HOLDINGS_CACHE
    p = BASE_DIR / "holdings.json"
    out: set[str] = set()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                # holdings.json: {ticker: {...}} or {tickers: [...]}
                if "tickers" in data and isinstance(data["tickers"], list):
                    for t in data["tickers"]:
                        if isinstance(t, str):
                            out.add(t)
                        elif isinstance(t, dict):
                            tk = t.get("ticker") or t.get("symbol")
                            if tk:
                                out.add(tk)
                else:
                    for k, v in data.items():
                        if isinstance(v, dict) and (v.get("ticker") or v.get("shares") or v.get("quantity")):
                            out.add(v.get("ticker", k))
                        elif isinstance(k, str) and len(k) <= 8:
                            out.add(k)
            elif isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        tk = entry.get("ticker") or entry.get("symbol")
                        if tk:
                            out.add(tk)
                    elif isinstance(entry, str):
                        out.add(entry)
        except Exception:
            pass
    _HOLDINGS_CACHE = out
    return out


# ─────────────────────────────────────────────────────────────
# ユニバース取得
# ─────────────────────────────────────────────────────────────

def load_universe(key: str = "all") -> list[str]:
    """tickers.json から指定キーのティッカーリストを返す。失敗時は空リスト。"""
    p = BASE_DIR / "tickers.json"
    try:
        data = json.loads(p.read_text())
        return list(data.get(key, []))
    except Exception:
        return []


def filter_us_only(tickers: list[str]) -> list[str]:
    """US 銘柄のみ（.T を含まない）"""
    return [t for t in tickers if not t.endswith(".T")]


def filter_jp_only(tickers: list[str]) -> list[str]:
    """JP 銘柄のみ（.T 終わり）"""
    return [t for t in tickers if t.endswith(".T")]


# ─────────────────────────────────────────────────────────────
# EDGAR ファンダ
# ─────────────────────────────────────────────────────────────

_EDGAR_CACHE: dict[str, dict] = {}


def get_edgar_summary(ticker: str) -> dict:
    """
    EDGAR ファンダ（薄ラッパー）。プロセス内キャッシュ + 失敗時 None フィルド。
    返す主キー: revenue_growth, eps_growth, gross_margin, roe, fcf, source
    """
    if ticker in _EDGAR_CACHE:
        return _EDGAR_CACHE[ticker]
    out = {
        "revenue_growth": None, "eps_growth": None, "gross_margin": None,
        "roe": None, "fcf": None, "source": "unavailable",
    }
    if ticker.endswith(".T"):
        _EDGAR_CACHE[ticker] = out
        return out
    try:
        from edgar_fetcher import get_edgar_financials
        fin = get_edgar_financials(ticker)
        out.update({
            "revenue_growth": fin.get("rev_growth"),
            "eps_growth":     fin.get("eps_growth"),
            "gross_margin":   fin.get("gross_margin"),
            "roe":            fin.get("roe"),
            "fcf":            fin.get("fcf"),
            "source":         fin.get("source", "unavailable"),
        })
    except Exception:
        pass
    _EDGAR_CACHE[ticker] = out
    return out


# ─────────────────────────────────────────────────────────────
# 決算日までの営業日数
# ─────────────────────────────────────────────────────────────

_EARNINGS_CACHE: dict[str, Optional[int]] = {}


def days_to_next_earnings(ticker: str) -> Optional[int]:
    """次回決算までの営業日数。取得不可は None。"""
    if ticker in _EARNINGS_CACHE:
        return _EARNINGS_CACHE[ticker]
    try:
        from earnings_proximity_manager import _next_earnings, _business_days_until
        ed = _next_earnings(ticker)
        if isinstance(ed, datetime):
            ed = ed.date()
        if not isinstance(ed, date):
            _EARNINGS_CACHE[ticker] = None
            return None
        days = _business_days_until(ed)
        _EARNINGS_CACHE[ticker] = days
        return days
    except Exception:
        _EARNINGS_CACHE[ticker] = None
        return None


def is_earnings_imminent(ticker: str, threshold_days: int = 2) -> bool:
    """次回決算まで `threshold_days` 営業日以内なら True。"""
    d = days_to_next_earnings(ticker)
    return d is not None and 0 <= d <= threshold_days


# ─────────────────────────────────────────────────────────────
# 流動性チェック
# ─────────────────────────────────────────────────────────────

def liquidity_ok(ticker: str, close_price: float, avg_volume_30d: float,
                 fx_jpy_per_usd: float = 150.0) -> bool:
    """
    30日平均売買代金が最低基準を満たすか。
    既存 holdings は常に True（保有銘柄を流動性で除外しない）。
    """
    if ticker in _holdings_set():
        return True
    if close_price <= 0 or avg_volume_30d <= 0:
        return False
    if ticker.endswith(".T"):
        # JP: 円建て
        notional_jpy = close_price * avg_volume_30d
        return notional_jpy >= JP_LIQUIDITY_MIN_JPY
    # US: $ 建て
    notional_usd = close_price * avg_volume_30d
    return notional_usd >= US_LIQUIDITY_MIN_USD


# ─────────────────────────────────────────────────────────────
# Historical win-rate from signal_history.json
# ─────────────────────────────────────────────────────────────

_WINRATE_CACHE: dict[tuple[str, str], float] = {}


def get_historical_win_rate(strategy: str, ticker: str,
                            lookback_days: int = 90,
                            min_samples: int = 3) -> float:
    """
    signal_history.json から (strategy, ticker) の勝率を取得。
    サンプル不足や未記録は 0.5（中立）を返す。
    """
    key = (strategy or "", ticker or "")
    if key in _WINRATE_CACHE:
        return _WINRATE_CACHE[key]
    p = BASE_DIR / "signal_history.json"
    if not p.exists():
        _WINRATE_CACHE[key] = 0.5
        return 0.5
    try:
        records = json.loads(p.read_text())
        if isinstance(records, dict):
            records = records.get("history", []) or records.get("signals", [])
        if not isinstance(records, list):
            _WINRATE_CACHE[key] = 0.5
            return 0.5
        cutoff = datetime.now().timestamp() - (lookback_days * 86400)
        wins = total = 0
        for r in records:
            if not isinstance(r, dict):
                continue
            if r.get("ticker") != ticker:
                continue
            if strategy and r.get("strategy") and r.get("strategy") != strategy:
                continue
            ts = r.get("evaluated_at") or r.get("generated_at") or r.get("timestamp")
            try:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if isinstance(ts, (int, float)) and ts < cutoff:
                    continue
            except Exception:
                pass
            outcome = r.get("outcome") or r.get("result")
            if outcome in ("win", "WIN", True, 1):
                wins += 1
                total += 1
            elif outcome in ("loss", "LOSS", False, 0):
                total += 1
        if total < min_samples:
            _WINRATE_CACHE[key] = 0.5
            return 0.5
        wr = wins / total
        _WINRATE_CACHE[key] = wr
        return wr
    except Exception:
        _WINRATE_CACHE[key] = 0.5
        return 0.5


# ─────────────────────────────────────────────────────────────
# Regime confidence
# ─────────────────────────────────────────────────────────────

def get_calibrated_threshold(strategy: str, param: str, default: float) -> float:
    """
    calibrated_thresholds.json があれば (strategy, param) のキャリブ済み閾値を返す。
    ファイルが無い・該当無し・破損時は default を返す。
    threshold_calibrator.py 側で ±10% ハードキャップ済み。
    """
    try:
        from threshold_calibrator import load_calibrated_thresholds
        cal = load_calibrated_thresholds()
        return float(cal.get(strategy, {}).get(param, default))
    except Exception:
        return float(default)


def get_regime_confidence() -> float:
    """regime_state.json の confidence (0-1) を返す。未設定は 0.7 (中庸)。"""
    p = BASE_DIR / "regime_state.json"
    if not p.exists():
        return 0.7
    try:
        data = json.loads(p.read_text())
        c = data.get("confidence") or data.get("hmm_confidence") or 0.7
        return float(c)
    except Exception:
        return 0.7


# ─────────────────────────────────────────────────────────────
# Composite score
# ─────────────────────────────────────────────────────────────

def calc_composite_score(
    technical: float,
    fundamental: float = 0.0,
    ai_conviction: float = 50.0,
    win_rate: float = 0.5,
    weights: tuple[float, float, float, float] = (0.40, 0.40, 0.10, 0.10),
) -> float:
    """
    各サブスコアを 0-100 想定で正規化し、加重平均で合成（出力 0-100）。

    Args:
        technical:     テクニカルスコア（0-100）
        fundamental:   ファンダスコア（0-100、不要なら 0 + weights 第2要素 = 0）
        ai_conviction: AI conviction（0-100）
        win_rate:      過去勝率（0-1）
        weights:       (technical, fundamental, ai, win) の重み（合計 1.0 推奨）
    """
    w_t, w_f, w_a, w_w = weights
    s = (
        max(0.0, min(100.0, technical)) * w_t
        + max(0.0, min(100.0, fundamental)) * w_f
        + max(0.0, min(100.0, ai_conviction)) * w_a
        + max(0.0, min(1.0, win_rate)) * 100.0 * w_w
    )
    return round(s, 1)


# ─────────────────────────────────────────────────────────────
# News / Social Sentiment JOIN（S4D）
# ─────────────────────────────────────────────────────────────

_NEWS_INDEX_CACHE: dict | None = None
_SOCIAL_INDEX_CACHE: dict | None = None


def _load_news_index() -> dict[str, dict]:
    """news_signal_candidates.json を {ticker: candidate_dict} に索引化。"""
    global _NEWS_INDEX_CACHE
    if _NEWS_INDEX_CACHE is not None:
        return _NEWS_INDEX_CACHE
    out: dict[str, dict] = {}
    p = BASE_DIR / "news_signal_candidates.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            for c in (d.get("candidates") or []):
                t = c.get("ticker")
                if t:
                    out[t] = c
        except Exception:
            pass
    _NEWS_INDEX_CACHE = out
    return out


def _load_social_index() -> dict[str, dict]:
    """social_sentiment.json から ticker → {bias, unusual_options, bullish/bearish flag} を索引化。"""
    global _SOCIAL_INDEX_CACHE
    if _SOCIAL_INDEX_CACHE is not None:
        return _SOCIAL_INDEX_CACHE
    out: dict[str, dict] = {}
    p = BASE_DIR / "social_sentiment.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            for o in (d.get("options_unusual") or []):
                t = o.get("ticker")
                if t:
                    out.setdefault(t, {})["options_unusual"] = True
                    out[t]["bias"] = o.get("bias")
            for b in (d.get("top_bullish") or []):
                t = b.get("ticker") if isinstance(b, dict) else b
                if t:
                    out.setdefault(t, {})["bullish"] = True
            for b in (d.get("top_bearish") or []):
                t = b.get("ticker") if isinstance(b, dict) else b
                if t:
                    out.setdefault(t, {})["bearish"] = True
            for t in (d.get("trending_tickers") or []):
                key = t if isinstance(t, str) else t.get("ticker")
                if key:
                    out.setdefault(key, {})["trending"] = True
        except Exception:
            pass
    _SOCIAL_INDEX_CACHE = out
    return out


def get_news_social_boost(ticker: str, *, side: str = "long") -> dict:
    """
    指定 ticker のニュース・SNS センチメントから加点/減点を返す。

    Args:
        ticker: 銘柄
        side:   "long" → bullish 加点 / "short" → bearish 加点（空売りは bearish が追い風）

    Returns:
        {
          news_signal:   "bullish" | "bearish" | "neutral" | None
          news_score:    -1..+1
          news_boost:    -5..+5 (サイドに合わせて符号反転)
          social_bias:   "bullish" | "bearish" | None
          social_buzz:   -3..+3
        }
    """
    out = {
        "news_signal": None, "news_score": None, "news_boost": 0,
        "social_bias": None, "social_buzz": 0,
    }
    news = _load_news_index().get(ticker)
    if news:
        sig = (news.get("signal") or "").lower()
        score = news.get("sentiment_score")
        out["news_signal"] = sig or None
        out["news_score"]  = score
        boost_raw = 0
        if sig == "bullish" and isinstance(score, (int, float)) and score > 0.3:
            boost_raw = 5
        elif sig == "bearish" and isinstance(score, (int, float)) and score < -0.3:
            boost_raw = -5
        # short 戦略は逆転（bearish ニュースが追い風 → +5）
        if side == "short":
            boost_raw = -boost_raw
        out["news_boost"] = boost_raw

    soc = _load_social_index().get(ticker)
    if soc:
        # social_sentiment.json の bias は CALL_HEAVY / PUT_HEAVY / BALANCED
        raw_bias = (soc.get("bias") or "").upper()
        if raw_bias == "CALL_HEAVY":
            normalized = "bullish"
        elif raw_bias == "PUT_HEAVY":
            normalized = "bearish"
        else:
            normalized = "neutral" if raw_bias else None
        out["social_bias"] = normalized
        buzz = 0
        if soc.get("options_unusual") and normalized == "bullish":
            buzz = 3
        elif soc.get("options_unusual") and normalized == "bearish":
            buzz = -3
        elif soc.get("bullish"):
            buzz = 2
        elif soc.get("bearish"):
            buzz = -2
        if side == "short":
            buzz = -buzz
        out["social_buzz"] = buzz
    return out
