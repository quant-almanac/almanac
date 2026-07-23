"""
データ収集モジュール
- 市場指標（VIX・金利・DXY・原油・金）
- ニュース（yfinance / RSS / Finnhub / FinBERT センチメント）
- 決算実績・予定
- 日本株ファンダメンタルズ
- ポートフォリオスナップショット統合
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from analyst.cache import load_json
from almanac.runtime_config import resolve_db_path
from insider_restrictions import is_restricted_ticker
from vix_classification import format_vix_level_ja
from pseudo_tickers import is_non_earnings_ticker

# ── ニュース取得対象 ─────────────────────────────────────
NEWS_MARKET_TICKERS = [
    "SPY", "^VIX", "^TNX", "GC=F", "CL=F", "EWJ", "EWG", "DX-Y.NYB",
]
NEWS_HOLDING_TICKERS = [
    "NVDA", "AVGO", "GLD", "META", "CRWV", "RCL", "EPOL", "6762.T", "EWG",
    "9999.T", "1489.T",  # 日本株
    "IEV", "V",          # 追加保有銘柄
]

_RSS_FEEDS = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters World",    "https://feeds.reuters.com/Reuters/worldNews"),
    ("Yahoo Finance",    "https://finance.yahoo.com/news/rssindex"),
    ("Investing.com",    "https://www.investing.com/rss/news.rss"),
    ("NHK経済",          "https://www3.nhk.or.jp/rss/news/cat3.xml"),
    ("Reuters Japan",    "https://feeds.reuters.com/reuters/JPBusinessNews"),
]
_FINNHUB_TICKERS = ["NVDA", "AVGO", "META", "RCL", "CRWV", "MSFT", "AAPL", "TSLA"]
_JP_TICKERS = ["9999.T", "6762.T", "1489.T"]
CUTOFF_HOURS = 24
_NON_EARNINGS_TICKERS = {
    "CASH_JPY",
    "CASH_USD",
    "CASH_JPY_SBI",
    "CASH_JPY_SBI_WIFE",
    "GS_MMF_USD",
}

_GOOGLE_NEWS_US_QUERIES = [
    "stock earnings beat guidance raise",
    "acquisition merger deal stock",
    "stock price increase hike",
    "FDA approval drug stock",
    "analyst upgrade price target",
]
_EDGAR_8K_QUERIES = [
    "merger agreement",
    "acquisition",
    "dividend increase",
    "share repurchase buyback",
    "price increase",
]

_GOOGLE_NEWS_JP_QUERIES = [
    "日本株 増益 業績",
    "値上げ 株価",
    "上方修正 決算",
    "新製品 発売 株",
]
_TDNET_FILTER_KEYWORDS = [
    "上方修正", "下方修正", "業績修正", "増益", "減益",
    "黒字転換", "赤字転換", "配当", "自社株買い",
    "合併", "買収", "新製品", "値上げ", "契約締結",
]


def _parse_screen_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        ts = None
        for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
            try:
                ts = datetime.strptime(s[:width], fmt)
                break
            except ValueError:
                continue
        if ts is None:
            return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _screen_timestamp(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("timestamp", "generated_at", "as_of", "cached_at", "scan_time"):
        value = raw.get(key)
        if value:
            return str(value)
    return ""


def _is_earnings_context_ticker(ticker: str | None) -> bool:
    """yfinance earnings API に投げてよい上場株 ticker だけ通す。"""
    t = str(ticker or "").strip().upper()
    if not t:
        return False
    if is_non_earnings_ticker(t) or t in _NON_EARNINGS_TICKERS:
        return False
    if t.startswith("^") or "=" in t or "." in t:
        return False
    return True


def _collect_parallel_results(
    futures: dict,
    *,
    fallbacks: dict,
    timeout_seconds: float,
    labels: dict | None = None,
) -> dict:
    """Return completed future results within one global timeout."""
    from concurrent.futures import wait

    labels = labels or {}
    done, not_done = wait(list(futures.values()), timeout=timeout_seconds)
    future_names = {future: name for name, future in futures.items()}
    results: dict = {}

    for future in done:
        name = future_names[future]
        label = labels.get(name, name)
        try:
            results[name] = future.result()
        except Exception as exc:
            print(f"  ⚠️ {label}取得エラー: {exc}")
            results[name] = fallbacks.get(name)

    for future in not_done:
        name = future_names[future]
        label = labels.get(name, name)
        future.cancel()
        print(f"  ⚠️ {label}取得タイムアウト: {timeout_seconds:.0f}s")
        results[name] = fallbacks.get(name)

    for name in futures:
        results.setdefault(name, fallbacks.get(name))
    return results


def _screen_candidate_strength(candidate: dict) -> tuple:
    signal_rank = {"BUY": 3, "WATCH": 2, "HOLD": 1, "SKIP": 0}
    signal = str(candidate.get("ai_signal") or "").upper()
    try:
        confidence = float(candidate.get("ai_confidence") or 0)
    except Exception:
        confidence = 0.0
    try:
        score = float(candidate.get("composite_score", candidate.get("score", 0)) or 0)
    except Exception:
        score = 0.0
    return (signal_rank.get(signal, 1), confidence, score)


def _screen_candidate_preference(candidate: dict) -> tuple:
    ts = _parse_screen_timestamp(candidate.get("screen_timestamp"))
    ts_value = ts.timestamp() if ts else 0.0
    return (ts_value, *_screen_candidate_strength(candidate))


def _load_screen_candidates() -> tuple[list[dict], list[dict]]:
    """短期スクリーニングの通常/朝/JP専用出力を統合する。

    UI から JP-only を実行した結果は screen_results_jp.json に保存されるため、
    AI 側が screen_results.json だけを見ると日本株候補を取りこぼす。
    """
    sources = [
        ("screen_results.json", "all_market"),
        ("screen_results_morning.json", "morning"),
        ("screen_results_jp.json", "jp_only"),
    ]
    loaded_sources: list[dict] = []
    source_meta: list[dict] = []

    for filename, label in sources:
        raw = load_json(BASE_DIR / filename, {})
        if not raw:
            continue
        if isinstance(raw, list):
            candidates = raw
            ts = ""
            total_screened = None
        elif isinstance(raw, dict):
            candidates = raw.get("candidates", []) or []
            ts = _screen_timestamp(raw)
            total_screened = raw.get("total_screened")
        else:
            continue

        ts_dt = _parse_screen_timestamp(ts)
        loaded_sources.append({
            "filename": filename,
            "label": label,
            "timestamp": ts,
            "timestamp_dt": ts_dt,
            "total_screened": total_screened,
            "candidates": candidates,
        })

    newest_ts = max(
        (s["timestamp_dt"] for s in loaded_sources if s.get("timestamp_dt")),
        default=None,
    )
    by_ticker: dict[str, dict] = {}
    for src in loaded_sources:
        ts_dt = src.get("timestamp_dt")
        max_age_hours = 24 if src["label"] == "jp_only" else 12
        included = not (
            newest_ts
            and ts_dt
            and (newest_ts - ts_dt) > timedelta(hours=max_age_hours)
        )
        source_meta.append({
            "file": src["filename"],
            "source": src["label"],
            "timestamp": src["timestamp"],
            "total_screened": src["total_screened"],
            "candidate_count": len(src["candidates"]),
            "included": included,
            "max_age_hours": max_age_hours,
        })
        if not included:
            continue

        for c in src["candidates"]:
            if not isinstance(c, dict):
                continue
            ticker = c.get("ticker")
            if not ticker:
                continue
            item = dict(c)
            item.setdefault("screen_source", src["label"])
            item.setdefault("screen_timestamp", src["timestamp"])
            prev = by_ticker.get(ticker)
            if prev is None or _screen_candidate_preference(item) > _screen_candidate_preference(prev):
                by_ticker[ticker] = item

    merged = sorted(by_ticker.values(), key=_screen_candidate_strength, reverse=True)
    return merged, source_meta


def _load_margin_long_candidates() -> dict:
    """実行時刻に合う信用買い候補ファイルを読む。"""
    now = datetime.now()
    filenames = (
        ["margin_long_candidates_morning.json", "margin_long_candidates.json"]
        if now.hour < 12 else
        ["margin_long_candidates.json", "margin_long_candidates_morning.json"]
    )
    loaded: list[tuple[str, dict]] = []
    for filename in filenames:
        raw = load_json(BASE_DIR / filename, {})
        if raw:
            if isinstance(raw, dict):
                raw = dict(raw)
                raw.setdefault("_selected_source_file", filename)
                loaded.append((filename, raw))
            else:
                return raw
    if not loaded:
        return {}

    selected_file, selected = loaded[0]
    if selected_file == "margin_long_candidates_morning.json":
        selected = _merge_fresh_jp_margin_candidates(selected, loaded[1:], now)
    return selected


def _merge_fresh_jp_margin_candidates(
    selected: dict,
    fallback_sources: list[tuple[str, dict]],
    now: datetime,
    *,
    max_age_hours: int = 72,
) -> dict:
    """朝のUS-only信用買い候補に、鮮度内の通常JP候補だけを足す。

    Morning runs intentionally prioritize fresh US candidates, but using that
    file as a full replacement makes JP candidates invisible to the 06:00 AI
    analysis.  Treat the regular file as a JP overlay only when it is fresh.
    """
    selected_candidates = selected.get("candidates")
    if not isinstance(selected_candidates, list):
        return selected

    seen = {
        str(c.get("ticker") or "")
        for c in selected_candidates
        if isinstance(c, dict) and c.get("ticker")
    }
    merged: list[dict] = []
    source_files: list[str] = []
    max_age = timedelta(hours=max_age_hours)

    for filename, raw in fallback_sources:
        ts = _parse_screen_timestamp(_screen_timestamp(raw))
        if ts is None or (now - ts) > max_age or (now - ts).total_seconds() < 0:
            continue
        for candidate in raw.get("candidates", []) or []:
            if not isinstance(candidate, dict):
                continue
            ticker = str(candidate.get("ticker") or "")
            if not ticker.endswith(".T") or ticker in seen:
                continue
            item = dict(candidate)
            item.setdefault("margin_candidate_source", filename)
            merged.append(item)
            source_files.append(filename)
            seen.add(ticker)

    if not merged:
        return selected

    out = dict(selected)
    out["candidates"] = [*selected_candidates, *merged]
    out["_jp_overlay_source_files"] = sorted(set(source_files))
    out["_jp_overlay_candidate_count"] = len(merged)
    return out


def scenario_context_decision_flags(
    sc_id: str,
    sc_data: dict,
    playbook_entry: dict,
    promotion_by_scenario: dict,
) -> dict:
    """Return observe-first decision-context flags for one scenario.

    observe_only scenarios stay out of the decision context until the measured
    promotion artifact explicitly marks that scenario promotion_ready=true.  The
    playbook itself is not mutated here; this only controls prompt visibility.
    """
    observe_only = sc_data.get("observe_only", playbook_entry.get("observe_only", False))
    enabled_for_decision = sc_data.get(
        "enabled_for_decision",
        playbook_entry.get("enabled_for_decision", True),
    )
    promotion_info = (
        promotion_by_scenario.get(sc_id, {})
        if isinstance(promotion_by_scenario, dict)
        else {}
    )
    promotion_ready = bool(
        isinstance(promotion_info, dict)
        and promotion_info.get("promotion_ready") is True
    )

    if observe_only and not promotion_ready:
        enabled_for_decision = False
    elif observe_only and promotion_ready:
        enabled_for_decision = True

    return {
        "enabled_for_decision": enabled_for_decision is not False,
        "observe_only": bool(observe_only and not promotion_ready),
        "original_observe_only": bool(observe_only),
        "promotion_ready": promotion_ready,
        "promotion": promotion_info if isinstance(promotion_info, dict) else {},
    }


# ── JP equity 動的目標比率 (持株会除外, 2026-07-07) ──────────

def dynamic_jp_equity_target(
    base_pct: float,
    *,
    scenario_monitoring: dict | None = None,
    vix=None,
    guard: dict | None = None,
    span_pct: float = 10.0,
    min_pct: float = 5.0,
    max_pct: float = 20.0,
) -> dict:
    """日本株目標比率 (持株会除外ベース) を市場環境で動的にスケールする。

    決定論ルール (LLM 非依存。入力欠損・例外は base に fail-closed):
      - japan_standalone_bull の readiness (0..1) × span_pct を base に加算する。
        readiness は EWJ vs SPY 20日相対力・日経/TOPIX MA50・グローバル BULL 確認を
        集約した既存計測値で、日本株環境の強さを連続値で表す。
        (dormant はシナリオ文脈に載らないため自然に boost 0 = base へ回帰)
      - リスクオフでは boost を凍結して base に固定:
        VIX ≥ 30 / 実損益ガードの trading_allowed・new_entry_allowed が False。
      - clamp [min_pct, max_pct]。既定で 10% 基準・最大 20% (readiness 100% + 平時)。
    """
    result = {
        "target_pct": base_pct,
        "base_pct": base_pct,
        "boost_pct": 0.0,
        "jp_scenario_readiness": None,
        "frozen_reason": None,
    }
    try:
        frozen = None
        try:
            if vix is not None and float(vix) >= 30:
                frozen = f"VIX {float(vix):.1f} >= 30 (リスクオフ)"
        except (TypeError, ValueError):
            pass
        if frozen is None and isinstance(guard, dict):
            if guard.get("trading_allowed") is False:
                frozen = "実損益ガード trading_allowed=False"
            elif guard.get("new_entry_allowed") is False:
                frozen = "実損益ガード new_entry_allowed=False"

        readiness = None
        if isinstance(scenario_monitoring, dict):
            for sc in scenario_monitoring.get("active_scenarios") or []:
                if isinstance(sc, dict) and sc.get("id") == "japan_standalone_bull":
                    try:
                        readiness = float(sc.get("readiness_pct") or 0) / 100.0
                    except (TypeError, ValueError):
                        readiness = None
                    break
        result["jp_scenario_readiness"] = readiness

        if frozen is not None:
            result["frozen_reason"] = frozen
            result["target_pct"] = max(min_pct, min(base_pct, max_pct))
            return result

        boost = (readiness or 0.0) * float(span_pct)
        target = max(min_pct, min(base_pct + boost, max_pct))
        result["boost_pct"] = round(boost, 2)
        result["target_pct"] = round(target, 2)
        return result
    except Exception:
        return result


# ── 市場指標 ──────────────────────────────────────────────

def gather_market_indicators() -> dict:
    """VIX・米国金利・ドル指数・原油・金をリアルタイムで取得"""
    try:
        import yfinance as yf
    except ImportError:
        return {}

    FETCH_MAP = {
        "^VIX":     ("vix",          "恐怖指数"),
        "^TNX":     ("us10y_yield",  "米10年金利(%)"),
        "^IRX":     ("us2y_yield",   "米2年金利(%)"),
        "DX-Y.NYB": ("dxy",          "ドル指数"),
        "CL=F":     ("crude_oil",    "原油(USD)"),
        "GC=F":     ("gold",         "金(USD)"),
        "^N225":    ("nikkei",       "日経225"),
    }

    result: dict = {}
    for symbol, (key, label) in FETCH_MAP.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if hist.empty:
                continue
            price = round(float(hist["Close"].iloc[-1]), 2)
            prev  = round(float(hist["Close"].iloc[-2]), 2) if len(hist) >= 2 else price
            chg   = round((price - prev) / prev * 100, 2) if prev else 0.0
            result[key] = {"value": price, "change_pct": chg, "label": label}
        except Exception:
            pass

    y10 = result.get("us10y_yield", {}).get("value")
    y2  = result.get("us2y_yield",  {}).get("value")
    if y10 and y2:
        result["yield_curve_spread"] = round(y10 - y2, 3)
        result["yield_curve_status"] = (
            "逆イールド（景気後退シグナル）" if y10 < y2 else "正常"
        )

    vix_val = result.get("vix", {}).get("value")
    if vix_val:
        result["vix_level"] = format_vix_level_ja(vix_val)
        result["vix"] = vix_val

    return result


# ── 決算実績 ──────────────────────────────────────────────

def gather_earnings_context(tickers: list) -> dict:
    """保有銘柄の直近決算実績 + 今後2週間の予定を取得"""

    def _clean(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) else round(f, 3)
        except Exception:
            return None

    results = {}
    try:
        import yfinance as yf
    except ImportError:
        return {}

    now_aware = datetime.now(timezone.utc)
    upcoming_window = now_aware + timedelta(days=14)

    for ticker in tickers:
        if not _is_earnings_context_ticker(ticker):
            continue
        try:
            t = yf.Ticker(ticker)
            ed = t.get_earnings_dates(limit=8)
            if ed is None or ed.empty:
                continue

            past = ed[ed.index < now_aware]
            if not past.empty:
                row = past.iloc[0]
                eps_act_c = _clean(row.get("Reported EPS"))
                eps_est_c = _clean(row.get("EPS Estimate"))
                surprise_c = _clean(row.get("Surprise(%)"))
                if eps_act_c is not None:
                    beat_miss = None
                    if eps_act_c is not None and eps_est_c is not None:
                        beat_miss = "beat" if eps_act_c >= eps_est_c else "miss"
                    results[ticker] = {
                        "status": "reported",
                        "date": str(past.index[0].date()),
                        "days_ago": (now_aware - past.index[0]).days,
                        "eps_actual": eps_act_c,
                        "eps_estimate": eps_est_c,
                        "surprise_pct": surprise_c,
                        "beat_miss": beat_miss,
                    }

            future = ed[(ed.index >= now_aware) & (ed.index <= upcoming_window)]
            if not future.empty:
                next_date = future.index[0]
                days_until = (next_date - now_aware).days
                next_row = future.iloc[0]
                eps_est_c = _clean(next_row.get("EPS Estimate"))
                upcoming = {
                    "status": "upcoming",
                    "date": str(next_date.date()),
                    "days_until": days_until,
                    "eps_estimate": eps_est_c,
                }
                if ticker in results:
                    results[ticker]["upcoming"] = upcoming
                else:
                    results[ticker] = upcoming
        except Exception:
            pass

    return results


def fmt_earnings_section(earnings: dict, tickers: list | None = None) -> str:
    """決算実績＋今後2週間の予定をプロンプト用テキストにフォーマット"""
    if not earnings:
        return ""
    targets = tickers or list(earnings.keys())
    reported_lines = []
    upcoming_lines = []

    for tk in targets:
        if tk not in earnings:
            continue
        e = earnings[tk]

        if e.get("status") == "reported":
            beat = {"beat": "✅ beat", "miss": "❌ miss"}.get(e.get("beat_miss", ""), "")
            surp = f" (surprise: {e['surprise_pct']:+.1f}%)" if e.get("surprise_pct") is not None else ""
            reported_lines.append(
                f"  {tk}: {e['date']}（{e['days_ago']}日前）決算済み "
                f"EPS実績 ${e.get('eps_actual','N/A')} / 予想 ${e.get('eps_estimate','N/A')} {beat}{surp}"
            )
        up = e.get("upcoming") if e.get("status") == "reported" else (e if e.get("status") == "upcoming" else None)
        if up:
            est_str = f" EPS予想 ${up['eps_estimate']}" if up.get("eps_estimate") else ""
            upcoming_lines.append(
                f"  ⚠️ {tk}: {up['date']}（{up['days_until']}日後）決算予定{est_str}"
            )

    lines = []
    if reported_lines:
        lines.append("## 直近決算実績")
        lines.extend(reported_lines)
    if upcoming_lines:
        lines.append("## 今後2週間の決算予定（重要イベント）")
        lines.extend(upcoming_lines)
    return "\n".join(lines) if lines else ""


# ── ニュース収集 ──────────────────────────────────────────

def _parse_age(pub_str: str, now_utc: datetime) -> tuple[float, str]:
    formats = ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
               "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"]
    for fmt in formats:
        try:
            pub_dt = datetime.strptime(pub_str[:30].strip(), fmt)
            if pub_dt.tzinfo:
                pub_dt = pub_dt.astimezone(timezone.utc).replace(tzinfo=None)
            age_h = (now_utc - pub_dt).total_seconds() / 3600
            return age_h, f"{int(age_h)}h前"
        except Exception:
            continue
    return 999.0, "日時不明"


def _fetch_rss_news(now_utc: datetime) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []
    items = []
    for source, url in _RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", "").strip()[:200]
                pub_str = entry.get("published", "")
                if not title:
                    continue
                age_h, age_label = _parse_age(pub_str, now_utc)
                if age_h > CUTOFF_HOURS:
                    continue
                items.append({"title": title, "summary": summary,
                              "date": pub_str[:19], "age": age_label, "source": source})
        except Exception:
            continue
    return items


def _fetch_finnhub_news(tickers: list[str], now_utc: datetime) -> dict[str, list]:
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        return {}
    try:
        import finnhub
    except ImportError:
        return {}

    client = finnhub.Client(api_key=api_key)
    results: dict = {}
    from_dt = (now_utc - timedelta(hours=CUTOFF_HOURS)).strftime("%Y-%m-%d")
    to_dt   = now_utc.strftime("%Y-%m-%d")

    for ticker in tickers:
        try:
            news = client.company_news(ticker, _from=from_dt, to=to_dt)
            items = []
            for n in news[:5]:
                pub_ts = n.get("datetime", 0)
                if pub_ts:
                    pub_dt    = datetime.fromtimestamp(pub_ts, tz=timezone.utc).replace(tzinfo=None)
                    age_h     = (now_utc - pub_dt).total_seconds() / 3600
                    age_label = f"{int(age_h)}h前"
                else:
                    age_h, age_label = 999.0, "日時不明"
                if age_h > CUTOFF_HOURS:
                    continue
                items.append({"title": n.get("headline", "").strip(),
                              "summary": n.get("summary", "").strip()[:200],
                              "date": str(pub_dt)[:19] if pub_ts else "",
                              "age": age_label, "source": "Finnhub"})
            if items:
                results[ticker] = items
        except Exception:
            continue
    return results


def _fetch_google_news_us(now_utc: datetime) -> list[dict]:
    """Google News RSS で米国株の機会ニュースを広く取得"""
    try:
        import feedparser
        import urllib.parse
    except ImportError:
        return []

    items: list[dict] = []
    seen: set = set()

    for query in _GOOGLE_NEWS_US_QUERIES:
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title", "").strip()
                if not title or title[:40] in seen:
                    continue
                pub_str = entry.get("published", "")
                age_h, age_label = _parse_age(pub_str, now_utc)
                if age_h > CUTOFF_HOURS:
                    continue
                seen.add(title[:40])
                items.append({
                    "title":   title,
                    "summary": entry.get("summary", "").strip()[:200],
                    "date":    pub_str[:19],
                    "age":     age_label,
                    "source":  "Google News US",
                })
        except Exception:
            continue

    return items


def _fetch_edgar_8k_today(now_utc: datetime) -> list[dict]:
    """SEC EDGAR EFTS API から直近の重要 8-K 開示を取得（TDNet の米国版）"""
    try:
        import requests
    except ImportError:
        return []

    today     = now_utc.strftime("%Y-%m-%d")
    yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    items: list[dict] = []
    seen: set = set()

    for query in _EDGAR_8K_QUERIES:
        try:
            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q":         f'"{query}"',
                    "forms":     "8-K",
                    "dateRange": "custom",
                    "startdt":   yesterday,
                    "enddt":     today,
                },
                timeout=10,
                headers={"User-Agent": "kaiso/1.0 research@example.com"},
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            for h in hits[:5]:
                src      = h.get("_source", {})
                names    = src.get("display_names", [])
                filed    = src.get("file_date", today)
                # display_names[0] = "COMPANY NAME  (TICKER)  (CIK XXXXXXXXXX)"
                company  = names[0].split("(")[0].strip() if names else ""
                key      = company[:30]
                if not company or key in seen:
                    continue
                seen.add(key)
                items.append({
                    "title":   f"[{company}] 8-K: {query}",
                    "summary": "",
                    "date":    filed,
                    "age":     filed,
                    "source":  "SEC EDGAR",
                })
        except Exception:
            continue

    return items[:20]


def _fetch_google_news_jp(now_utc: datetime) -> list[dict]:
    """Google News RSS で日本株関連の機会ニュースを広く取得"""
    try:
        import feedparser
        import urllib.parse
    except ImportError:
        return []

    items: list[dict] = []
    seen: set = set()

    for query in _GOOGLE_NEWS_JP_QUERIES:
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title", "").strip()
                if not title or title[:40] in seen:
                    continue
                pub_str = entry.get("published", "")
                age_h, age_label = _parse_age(pub_str, now_utc)
                if age_h > CUTOFF_HOURS:
                    continue
                seen.add(title[:40])
                items.append({
                    "title": title,
                    "summary": entry.get("summary", "").strip()[:200],
                    "date": pub_str[:19],
                    "age": age_label,
                    "source": "Google News JP",
                })
        except Exception:
            continue

    return items


def _fetch_tdnet_today(now_utc: datetime) -> list[dict]:
    """東証適時開示（TDNet）から本日の重要開示タイトルを取得"""
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    date_str = now_utc.strftime("%Y%m%d")
    url = f"https://www.release.tdnet.info/inbs/I_list_001_{date_str}.html"

    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (compatible; kaiso/1.0)"
        })
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return []

    items: list[dict] = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        time_text    = cells[0].get_text(strip=True)
        code_text    = cells[1].get_text(strip=True)
        company_text = cells[2].get_text(strip=True)
        title_text   = cells[3].get_text(strip=True)

        if not title_text or not code_text.isdigit():
            continue
        if not any(kw in title_text for kw in _TDNET_FILTER_KEYWORDS):
            continue

        d = date_str
        items.append({
            "title":   f"[{company_text}({code_text})] {title_text}",
            "summary": "",
            "date":    f"{d[:4]}-{d[4:6]}-{d[6:8]} {time_text}",
            "age":     time_text,
            "source":  "TDNet",
            "code":    code_text,
        })

    return items[:20]


def gather_news() -> dict:
    """yfinance + RSS + Finnhub から市場・保有銘柄のニュースを収集（24時間以内）"""
    try:
        import yfinance as yf
    except ImportError:
        return {"market": [], "holdings": {}, "error": "yfinance not available"}

    news_data: dict = {"market": [], "holdings": {}}
    now_utc = datetime.utcnow()

    def _parse_yf_news(raw_list: list, limit: int = 5) -> list:
        items = []
        for n in raw_list:
            if len(items) >= limit:
                break
            c = n.get("content", {})
            title   = c.get("title",   "").strip()
            summary = c.get("summary", "").strip()[:200]
            pub     = c.get("pubDate", "")
            if not title:
                continue
            age_h, age_label = _parse_age(pub, now_utc)
            if age_h > CUTOFF_HOURS:
                continue
            items.append({"title": title, "summary": summary,
                          "date": pub, "age": age_label, "source": "yfinance"})
        return items

    for sym in NEWS_MARKET_TICKERS:
        try:
            raw   = yf.Ticker(sym).news
            items = _parse_yf_news(raw, limit=3)
            news_data["market"].extend(items)
        except Exception:
            pass

    news_data["market"].extend(_fetch_rss_news(now_utc))

    seen: set = set()
    deduped   = []
    for item in news_data["market"]:
        key = item["title"][:50]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    def _age_hours(item):
        s = item.get("age", "999h前").replace("h前", "").strip()
        try:
            return int(s)
        except ValueError:
            return 999

    deduped.sort(key=_age_hours)
    news_data["market"] = deduped[:12]
    news_data["sources"] = list({i.get("source", "") for i in deduped})

    for sym in NEWS_HOLDING_TICKERS:
        try:
            raw   = yf.Ticker(sym).news
            items = _parse_yf_news(raw, limit=3)
            if items:
                news_data["holdings"][sym] = items
        except Exception:
            pass

    finnhub_news = _fetch_finnhub_news(_FINNHUB_TICKERS, now_utc)
    for ticker, items in finnhub_news.items():
        existing = news_data["holdings"].get(ticker, [])
        merged   = existing + [i for i in items
                                if i["title"][:50] not in {e["title"][:50] for e in existing}]
        news_data["holdings"][ticker] = merged[:4]

    # 日本株機会ニュース（Google News + TDNet）
    jp_opp = _fetch_google_news_jp(now_utc)
    jp_opp += _fetch_tdnet_today(now_utc)
    news_data["jp_opportunities"] = jp_opp

    # 米国株機会ニュース（Google News + SEC EDGAR 8-K）
    us_opp = _fetch_google_news_us(now_utc)
    us_opp += _fetch_edgar_8k_today(now_utc)
    news_data["us_opportunities"] = us_opp

    # FinBERT センチメント付与（任意・transformers インストール済み時のみ）
    try:
        from transformers import pipeline as _hf_pipeline
        _finbert = _hf_pipeline(
            "text-classification",
            model="ProsusAI/finbert",
            truncation=True, max_length=512,
        )
        all_items: list[dict] = list(news_data["market"])
        for items in news_data["holdings"].values():
            all_items.extend(items)
        if all_items:
            headlines = [i.get("title", "")[:256] for i in all_items]
            results   = _finbert(headlines, batch_size=16)
            for item, res in zip(all_items, results):
                item["sentiment"] = {"label": res["label"], "score": round(res["score"], 3)}
            try:
                counts = {"positive": 0, "negative": 0, "neutral": 0}
                for res in results:
                    lbl = res["label"].lower()
                    if lbl in counts:
                        counts[lbl] += 1
                summary = {
                    **counts,
                    "total": len(results),
                    "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                from utils import atomic_write_json
                atomic_write_json(BASE_DIR / "news_sentiment_summary.json", summary)
            except Exception:
                pass
    except Exception:
        pass

    return news_data


# ── 日本株ファンダメンタルズ ─────────────────────────────

def gather_jp_fundamentals() -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}

    FIELDS = {
        "trailingPE":    "PER実績",
        "forwardPE":     "PER予想",
        "priceToBook":   "PBR",
        "dividendYield": "配当利回り%",
        "trailingEps":   "EPS実績",
        "returnOnEquity":"ROE%",
        "currentRatio":  "流動比率",
        "debtToEquity":  "DE比率",
        "shortRatio":    "信用倍率参考",
    }

    results = {}
    for ticker in _JP_TICKERS:
        try:
            info = yf.Ticker(ticker).info
            data: dict = {"name": info.get("shortName", ticker)}
            for field, label in FIELDS.items():
                val = info.get(field)
                if val is None:
                    continue
                if field == "dividendYield":
                    data[label] = round(float(val), 2)
                elif field == "returnOnEquity":
                    data[label] = round(float(val) * 100, 1)
                else:
                    data[label] = round(float(val), 2)
            results[ticker] = data
        except Exception:
            pass
    return results


# ── フォーマットユーティリティ ───────────────────────────

from analyst.llm_client import _GEO_KEYWORDS


def fmt_news_section(news: dict, tickers: list[str] | None = None) -> str:
    """ニュースデータをプロンプト用テキストにフォーマット（地政学/マクロ分類付き）"""
    lines = []
    market_items = news.get("market", [])

    geo_items = []
    macro_items = []
    for n in market_items:
        title_lower = n["title"].lower()
        if any(k in title_lower for k in _GEO_KEYWORDS):
            geo_items.append(n)
        else:
            macro_items.append(n)

    if geo_items:
        lines.append("【地政学・世界情勢ニュース（過去24時間）】")
        for n in geo_items[:5]:
            age = n.get("age", n["date"][:10])
            lines.append(f"  [{age}] {n['title']}")
            if n.get("summary"):
                lines.append(f"    {n['summary'][:130]}")

    if macro_items:
        lines.append("\n【市場・マクロニュース（過去24時間）】")
        for n in macro_items[:4]:
            age = n.get("age", n["date"][:10])
            lines.append(f"  [{age}] {n['title']}")
            if n.get("summary"):
                lines.append(f"    {n['summary'][:100]}")

    if not geo_items and not macro_items:
        lines.append("【市場ニュース】過去24時間以内の記事なし")

    if tickers:
        holdings_news = news.get("holdings", {})
        relevant = {t: holdings_news[t] for t in tickers if t in holdings_news}
        if relevant:
            lines.append("\n【関連銘柄ニュース（過去24時間）】")
            for sym, items in relevant.items():
                for n in items[:2]:
                    age = n.get("age", n["date"][:10])
                    s = n.get("sentiment", {})
                    if s and s.get("label"):
                        lbl_map = {"positive": "強気", "negative": "弱気", "neutral": "中立"}
                        lbl = lbl_map.get(s["label"], s["label"])
                        sentiment_tag = f"|{lbl}{int(s.get('score', 0) * 100)}%"
                    else:
                        sentiment_tag = ""
                    lines.append(f"  [{sym}|{age}{sentiment_tag}] {n['title']}")
                    if n.get("summary"):
                        lines.append(f"    {n['summary'][:100]}")

    jp_opp = news.get("jp_opportunities", [])
    if jp_opp:
        lines.append("\n【日本株 機会ニュース（Google News + TDNet）】")
        for n in jp_opp[:12]:
            age = n.get("age", n["date"][:10])
            src = n.get("source", "")
            lines.append(f"  [{src}|{age}] {n['title']}")

    us_opp = news.get("us_opportunities", [])
    if us_opp:
        lines.append("\n【米国株 機会ニュース（Google News + SEC EDGAR）】")
        for n in us_opp[:12]:
            age = n.get("age", n["date"][:10])
            src = n.get("source", "")
            lines.append(f"  [{src}|{age}] {n['title']}")

    return "\n".join(lines) if lines else "ニュースなし"


# ── ポートフォリオデータ収集 ─────────────────────────────

def gather_data() -> dict:
    """各種データを一括収集してdictで返す（ThreadPoolExecutor で並列化）"""
    from concurrent.futures import ThreadPoolExecutor

    holdings_raw = load_json(BASE_DIR / "holdings.json")

    # P0-5: snapshot 失敗時に magic number で続行する fail-silent を廃止。
    # 旧実装は total_jpy=30_639_795（ある時点の実総資産近似値）を fallback にしていたため、
    # snapshot が壊れた状態で AI が「正常そうな間違った値」を見て買い推奨を出す危険があった。
    try:
        from portfolio_manager import build_portfolio_snapshot
        portfolio = build_portfolio_snapshot()
    except Exception as e:
        raise RuntimeError(
            f"build_portfolio_snapshot 失敗のため AI 分析を中断 (root cause: {e})。"
            " portfolio_manager.build_portfolio_snapshot() を手動で実行し holdings.json / yfinance の状態を確認してください。"
        ) from e

    positions = portfolio.get("positions", [])
    portfolio_total = portfolio.get("total_jpy")
    if portfolio_total is None or portfolio_total <= 0:
        raise RuntimeError(
            f"portfolio snapshot が invalid な total_jpy={portfolio_total!r} を返しました。"
            " holdings.json / account.json / yfinance の鮮度を確認してください。"
        )

    now = datetime.now()
    _SL_TRAIL = {"long": 0.85, "medium": 0.90, "swing": 1.10}
    for pos in positions:
        key = pos.get("key", pos.get("ticker", ""))
        h = holdings_raw.get(key, {})
        ed = h.get("entry_date", "")
        pos["entry_date"] = ed
        manual_sl = h.get("stop_loss")
        if manual_sl:
            pos["stop_loss"] = manual_sl
            pos["stop_loss_source"] = "manual"
        else:
            current_price = pos.get("current_price")
            inv_type = pos.get("investment_type", "medium")
            coeff = _SL_TRAIL.get(inv_type, 0.90)
            if current_price:
                suggested = round(current_price * coeff, 2)
                pos["stop_loss"] = suggested
                pos["stop_loss_source"] = f"suggested({int((1-coeff)*100)}%trailing)"
            else:
                pos["stop_loss"] = None
                pos["stop_loss_source"] = "unset"
        if ed:
            try:
                dt = datetime.strptime(ed[:10], "%Y-%m-%d")
                pos["holding_days"] = (now - dt).days
            except Exception:
                pos["holding_days"] = None
        else:
            pos["holding_days"] = None

    bt_raw = load_json(BASE_DIR / "backtest_results.json", [])
    backtest_summary = []
    for s in (bt_raw if isinstance(bt_raw, list) else []):
        backtest_summary.append({
            "strategy":      s.get("strategy", ""),
            "trades":        s.get("trades", 0),
            "win_rate":      s.get("win_rate", 0),
            "avg_pnl":       s.get("avg_pnl", 0),
            "profit_factor": s.get("profit_factor", 0),
            "max_loss":      s.get("max_loss", 0),
            "avg_hold":      s.get("avg_hold", 0),
        })

    lt_screen = load_json(BASE_DIR / "long_term_screen_results.json")
    opt_result = load_json(BASE_DIR / "optimization_result.json")
    short_cands_raw = load_json(BASE_DIR / "short_candidates.json")
    margin_long_raw = _load_margin_long_candidates()
    screening = {
        "long_term": lt_screen,
        "optimization": opt_result,
        "short_candidates": short_cands_raw.get("candidates", []),
        "short_candidates_meta": {
            key: short_cands_raw.get(key)
            for key in ("scanned", "shortable_count", "vix_blocked")
            if key in short_cands_raw
        },
        "margin_long_candidates": margin_long_raw.get("candidates", []),
        "margin_long_blocked": margin_long_raw.get("blocked", False),
        "margin_long_block_reason": margin_long_raw.get("block_reason", ""),
    }

    signals_raw = load_json(BASE_DIR / "signals_log.json")
    screen_candidates, screen_source_meta = _load_screen_candidates()
    jp_screen_candidates = [
        c for c in screen_candidates
        if str(c.get("ticker") or "").endswith(".T") or c.get("screen_source") == "jp_only"
    ]
    screening["screen_candidates"] = screen_candidates
    screening["jp_screen_candidates"] = jp_screen_candidates
    screening["screen_sources"] = screen_source_meta

    ipo_watch_raw = load_json(BASE_DIR / "data" / "ipo_watch_state.json", {})
    ipo_watch = {
        "updated_at": ipo_watch_raw.get("updated_at"),
        "schema_version": ipo_watch_raw.get("schema_version"),
        "last_scan": ipo_watch_raw.get("last_scan"),
        "candidates": [
            {
                "ticker": row.get("ticker"),
                "company": row.get("company"),
                "exchange": row.get("exchange"),
                "ipo_date": row.get("ipo_date"),
                "size_or_rank": row.get("size_or_rank"),
                "confidence": row.get("confidence"),
                "status": row.get("status"),
                "onboarding_path": row.get("onboarding_path"),
                "detected_at": row.get("detected_at"),
            }
            for row in (ipo_watch_raw.get("candidates") or [])[:10]
            if isinstance(row, dict) and row.get("ticker")
        ],
    }

    signals = signals_raw.get("signals", signals_raw) if isinstance(signals_raw, dict) else {}

    signals_generated_at = signals_raw.get("generated_at", "") if isinstance(signals_raw, dict) else ""
    signals_age_hours: float | None = None
    if signals_generated_at:
        try:
            st = datetime.strptime(signals_generated_at[:16], "%Y-%m-%d %H:%M")
            signals_age_hours = round((now - st).total_seconds() / 3600, 1)
        except Exception:
            pass

    regime = load_json(BASE_DIR / "regime_state.json", {"spy_above": True, "nk_above": True})
    guard  = load_json(BASE_DIR / "guard_state.json")

    try:
        from scenario_strategy import get_strategy
        scenario = get_strategy()
        scenario["key"] = scenario.get("scenario", "NEUTRAL")
    except Exception:
        scenario = {"key": "NEUTRAL", "name": "中立相場"}

    try:
        from margin_manager import get_summary
        margin = get_summary()
        margin.pop("open_positions", None)
    except Exception:
        margin = {}

    # ── account / cash を rebalance より先に読み込む（P0-6 で rebalance に渡すため）──
    account_raw = load_json(BASE_DIR / "account.json", {})
    # P0-1: FX rate を utils.get_fx_rate_cached() 経由で取得（TTL 10分 + stale fallback）
    try:
        from utils import get_fx_rate_cached
        _fx, _ = get_fx_rate_cached(account_json_path=BASE_DIR / "account.json")
        fx_rate = float(_fx)
    except Exception:
        fx_rate = account_raw.get("fx_rate_usdjpy", 150)
    usd_balance = account_raw.get("usd_balance", 0)
    jpy_balance = account_raw.get("balance", 0)
    total_cash_jpy = jpy_balance + round(usd_balance * fx_rate)
    cash_info = {
        "jpy_cash": jpy_balance,
        "usd_cash": usd_balance,
        "fx_rate_usdjpy": fx_rate,
        "usd_as_jpy": round(usd_balance * fx_rate),
        "total_cash_jpy": total_cash_jpy,
        "account_last_updated": account_raw.get("last_updated"),
        "note": "USD建て購入はUSD口座残高から、JPY建て購入はJPY口座残高から。USD→JPY転換はFXコスト発生。",
    }

    # P0-6: rebalance に実 cash を渡す（旧コードは available_cash=0 固定で
    # 現金があるのに売却寄りのリバランスが提案される構造だった）。
    #
    # 2026-07 AI動的外貨比率: 通貨目標は currency_policy で解決した有効方針を注入する。
    # AI が見る通貨内訳は whole_portfolio / long_tier の両方を渡し、rebalance に適用する
    # 方針は basis=long_tier のみ。無効/期限切れは static CURRENCY_TARGETS に fail-closed。
    currency_breakdown_whole: dict = {}
    currency_breakdown_long: dict = {}
    current_currency_policy: dict = {"source": "static_fallback", "reason": "未解決"}
    try:
        from rebalance_engine import calculate_rebalance_actions, build_core_snapshot, CURRENCY_TARGETS
        import portfolio_manager as pm
        snap = pm.build_portfolio_snapshot()
        currency_breakdown_whole = snap.get("currency_breakdown", {}) or {}
        try:
            currency_breakdown_long = build_core_snapshot(snap).get("currency_breakdown", {}) or {}
        except Exception:
            currency_breakdown_long = {}
        currency_targets = CURRENCY_TARGETS
        try:
            import currency_policy
            currency_targets, current_currency_policy = currency_policy.resolve_effective_targets(
                static=CURRENCY_TARGETS)
        except Exception as _cp_e:
            current_currency_policy = {"source": "static_fallback", "reason": f"resolve 失敗: {_cp_e}"}
        rebalance = calculate_rebalance_actions(
            snap, available_cash=total_cash_jpy, currency_targets=currency_targets)
    except Exception:
        rebalance = {}

    exec_raw = load_json(BASE_DIR / "action_executions.json", {"executions": []})
    pending_orders = [
        {"ticker": e.get("ticker"), "direction": e.get("direction"),
         "action": e.get("action", ""), "saved_at": e.get("saved_at", "")}
        for e in exec_raw.get("executions", [])
        if e.get("status") == "ordered"
    ]

    risk: dict = {}
    try:
        import sqlite3
        import pandas as pd
        from risk_engine import (
            calculate_cvar,
            calculate_drawdown,
            calculate_var_cornish_fisher,
            calculate_var_historical,
        )
        from config_clean_baseline import clean_nav_since_iso, min_clean_days

        _clean_since = clean_nav_since_iso()
        _min_days = min_clean_days()

        def _risk_from_returns(returns, *, source: str, extra: dict = None) -> dict:
            """returns 系列から VaR/CVaR/DD を算出して risk dict に整形（P0-3/P1-1 共通）。"""
            var_cf = calculate_var_cornish_fisher(returns, 0.95, portfolio_value=float(portfolio_total))
            var_hist = calculate_var_historical(returns, 0.95, portfolio_value=float(portfolio_total))
            cvar = calculate_cvar(returns, 0.95, portfolio_value=float(portfolio_total))
            dd = calculate_drawdown(returns)
            cf_var = float(var_cf.get("var_pct", 0) or 0)
            hist_var = float(var_hist.get("var_pct", 0) or 0)
            # CF は歪度補正で過小化する場合があるため、policy gate には保守的な大きい方を使う。
            policy_var = max(cf_var, hist_var)
            _unstable = bool(cvar.get("cvar_unstable", False))
            current_dd_pct = round(float(dd.get("current_dd", 0) or 0) * 100, 2)
            max_dd_pct = round(float(dd.get("max_dd", 0) or 0) * 100, 2)
            out = {
                "source": source,
                "observations": int(len(returns)),
                # PolicyContext は 0.8 = 0.8% の percent 表記を受け取る設計。
                "var_95": round(policy_var * 100, 2),
                "var_95_decimal": round(policy_var, 6),
                "var_95_cf": round(cf_var * 100, 2),
                "var_95_hist": round(hist_var * 100, 2),
                "cvar_95": round(float(cvar.get("cvar_pct", 0) or 0) * 100, 2),
                "cvar_unstable": _unstable,
                # tail サンプル不足由来 (実データあり) は tail_small_sample。
                "cvar_reason": ("tail_small_sample" if _unstable else None),
                "current_dd": current_dd_pct,
                "max_dd": max_dd_pct,
            }
            if source == "parquet_reconstruction":
                # Synthetic ex-ante DD: current holdings applied to historical prices.
                # Keep it for audit, but never feed it into policy DD hard blocks.
                out["synthetic_current_dd"] = current_dd_pct
                out["synthetic_max_dd"] = max_dd_pct
            if extra:
                out.update(extra)
            return out

        def _actual_dd_from_guard_state() -> dict | None:
            """実損益ガード状態から actual DD/P&L stage を返す。

            guard_state は behavioral_guard の評価額 snapshot P&L 由来で、parquet 合成系列や
            daily_performance の汚染 NAV には依存しない運用ガードとして扱う。
            """
            if not isinstance(guard, dict) or not guard:
                return None
            try:
                daily_pct = float(guard.get("daily_pnl_pct"))
                monthly_pct = float(guard.get("monthly_pnl_pct"))
            except (TypeError, ValueError):
                return None
            try:
                guard_stage = int(guard.get("guardrail_stage") or 0)
            except (TypeError, ValueError):
                guard_stage = 0
            trading_allowed = guard.get("trading_allowed")
            new_entry_allowed = guard.get("new_entry_allowed")
            actual_pct = round(min(daily_pct, monthly_pct) * 100, 2)
            if trading_allowed is False or guard_stage >= 3:
                stage = "stage_3"
            elif guard_stage == 2:
                stage = "stage_2"
            elif guard_stage == 1:
                stage = "stage_1"
            elif new_entry_allowed is False or daily_pct <= -0.04 or monthly_pct <= -0.08:
                stage = "block"
            elif daily_pct <= -0.03 or monthly_pct <= -0.05:
                stage = "caution"
            else:
                stage = "ok"
            return {
                "actual_current_dd": actual_pct,
                "actual_dd_stage": stage,
                "actual_dd_source": "guard_state_snapshot_pnl",
                "actual_daily_pnl_pct": round(daily_pct * 100, 2),
                "actual_rolling30_pnl_pct": round(monthly_pct * 100, 2),
            }

        def _actual_dd_from_clean_nav() -> dict | None:
            """clean NAV 履歴が十分ある場合だけ actual DD を daily_performance から算出する。"""
            db = resolve_db_path(BASE_DIR)
            if not db.exists():
                return None
            try:
                with sqlite3.connect(str(db)) as conn:
                    # Codex re-review #4: 推定 (nav_backfill, estimated=1) 行は actual DD から除外。
                    # 列の有無は PRAGMA で判定し、列がある時のみ filter を付ける。任意の例外で
                    # unfiltered に戻すと推定混入を見逃すため fallback はしない。
                    _cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_performance)").fetchall()]
                    _est = "AND COALESCE(estimated, 0) = 0" if "estimated" in _cols else ""
                    df = pd.read_sql_query(
                        f"""
                        SELECT date, daily_pnl_pct
                        FROM daily_performance
                        WHERE daily_pnl_pct IS NOT NULL AND date >= ?
                          {_est}
                        ORDER BY date
                        """,
                        conn,
                        params=(_clean_since,),
                    )
            except Exception:
                return None
            if df.empty:
                return None
            clean_returns = pd.to_numeric(df["daily_pnl_pct"], errors="coerce").dropna() / 100.0
            if len(clean_returns) < _min_days:
                return None
            dd = calculate_drawdown(clean_returns)
            current_dd_pct = round(float(dd.get("current_dd", 0) or 0) * 100, 2)
            if current_dd_pct <= -8.0:
                stage = "block"
            elif current_dd_pct <= -5.0:
                stage = "caution"
            else:
                stage = "ok"
            return {
                "actual_current_dd": current_dd_pct,
                "actual_dd_stage": stage,
                "actual_dd_source": "daily_performance_clean",
            }

        # P1-1: 主経路 = parquet 再構成（現ウェイト × 過去市場リターン, ex-ante）。
        # 会計バグ非依存・N≥200 を確保でき、cvar_unstable 恒常 true を解消する。
        try:
            from portfolio_risk_returns import reconstruct_portfolio_returns
            _cash_jpy = float((cash_info or {}).get("total_cash_jpy") or 0)
            _pr, _cov, _rmeta = reconstruct_portfolio_returns(
                positions, total_jpy=float(portfolio_total or 0), cash_jpy=_cash_jpy,
            )
            if _pr is not None and _cov >= 0.85 and len(_pr) >= _min_days:
                risk = _risk_from_returns(
                    _pr, source="parquet_reconstruction",
                    extra={
                        "coverage_ratio": round(float(_cov), 4),
                        "risk_basis": "ex_ante_current_weights",
                        # 合成系列の DD は「現ウェイトを過去に当てた仮想 DD」で actual ではないため、
                        # policy の dd_stage には渡さない (実 DD は日次/月次 behavioral guard が担当)。
                        "current_dd": None,
                        "max_dd": None,
                        "reconstruction": {
                            k: _rmeta.get(k) for k in
                            ("observations", "excluded", "uncovered", "proxied", "riskless_weight")
                        },
                    },
                )
        except Exception as _pe:
            print(f"  ⚠️ parquet 再構成 skip → daily_performance fallback: {_pe}")

        # fallback (P0-3): daily_performance は clean_since 以降のみ採用（汚染期間を除外）。
        db = resolve_db_path(BASE_DIR)
        returns = pd.Series(dtype="float64")
        if not risk and db.exists():
            with sqlite3.connect(str(db)) as conn:
                # Codex re-review #4: 推定 (estimated=1) 行は VaR/returns fallback から除外。
                # PRAGMA で列判定し、例外時に unfiltered へ戻さない (推定混入を見逃さない)。
                _cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_performance)").fetchall()]
                _est = "AND COALESCE(estimated, 0) = 0" if "estimated" in _cols else ""
                df = pd.read_sql_query(
                    f"""
                    SELECT date, daily_pnl_pct
                    FROM daily_performance
                    WHERE daily_pnl_pct IS NOT NULL AND date >= ?
                      {_est}
                    ORDER BY date
                    """,
                    conn,
                    params=(_clean_since,),
                )
            if not df.empty:
                # daily_pnl_pct は percent 表記（例: -0.42 = -0.42%）で保存されている。
                returns = pd.to_numeric(df["daily_pnl_pct"], errors="coerce").dropna() / 100.0

        if risk:
            pass  # parquet 主経路で確定済み
        elif len(returns) >= _min_days:
            risk = _risk_from_returns(returns, source="daily_performance_clean")
        else:
            # P0-3: クリーン履歴不足 — VaR/CVaR/DD を「確定値」として policy に渡さない。
            # 数値を None にして var_budget / dd_stage を skip させ、cvar_unstable で soft 扱い。
            risk = {
                "source": "insufficient_clean_history",
                "observations": int(len(returns)),
                "clean_since": _clean_since,
                "min_clean_days": _min_days,
                "var_95": None,
                "var_95_decimal": None,
                "cvar_95": None,
                "cvar_unstable": True,
                "cvar_reason": "insufficient_clean_history",
                "current_dd": None,
                "max_dd": None,
            }
        actual_dd = _actual_dd_from_guard_state() or _actual_dd_from_clean_nav()
        if actual_dd:
            risk.update(actual_dd)
        else:
            risk.setdefault("actual_current_dd", None)
            risk.setdefault("actual_dd_stage", None)
            risk.setdefault("actual_dd_source", None)
    except Exception as e:
        risk = {"source": "unavailable", "error": str(e)}

    nisa: dict = {}
    nisa_path = BASE_DIR / "nisa_portfolio.json"
    if nisa_path.exists():
        raw_nisa = load_json(nisa_path)
        for person in ["husband", "wife"]:
            p = raw_nisa.get(person, {})
            if not p:
                continue
            tsumitate_limit = p.get("tsumitate_limit_annual", 1_200_000)
            growth_limit    = p.get("growth_limit_annual", 2_400_000)
            lifetime_limit  = p.get("lifetime_limit", 18_000_000)
            tsumitate_used  = p.get("tsumitate_used_this_year", 0)
            growth_used     = p.get("growth_used_this_year", 0)
            tsumitate_planned = p.get("tsumitate_planned_this_year", 0)
            growth_planned    = p.get("growth_planned_this_year", 0)
            lifetime_used   = p.get("lifetime_used_estimate", 0)
            tsumitate_before_planned = max(0, tsumitate_limit - tsumitate_used)
            growth_before_planned    = max(0, growth_limit - growth_used)
            tsumitate_remaining = max(0, tsumitate_limit - tsumitate_used - tsumitate_planned)
            growth_remaining    = max(0, growth_limit - growth_used - growth_planned)
            nisa[person] = {
                "broker": p.get("broker", ""),
                "tsumitate_used":      tsumitate_used,
                "tsumitate_planned":   tsumitate_planned,
                "tsumitate_remaining_before_planned": tsumitate_before_planned,
                "tsumitate_remaining": tsumitate_remaining,
                "growth_used":         growth_used,
                "growth_planned":      growth_planned,
                "growth_remaining_before_planned": growth_before_planned,
                "growth_remaining":    growth_remaining,
                "lifetime_remaining":  lifetime_limit  - lifetime_used,
                "tsumitate_used_pct":  round(tsumitate_used / tsumitate_limit * 100, 1) if tsumitate_limit else 0,
                "growth_used_pct":     round(growth_used    / growth_limit    * 100, 1) if growth_limit    else 0,
                "tsumitate_committed_pct": round((tsumitate_used + tsumitate_planned) / tsumitate_limit * 100, 1) if tsumitate_limit else 0,
                "growth_committed_pct": round((growth_used + growth_planned) / growth_limit * 100, 1) if growth_limit else 0,
                "lifetime_used_pct":   round(lifetime_used  / lifetime_limit  * 100, 1) if lifetime_limit  else 0,
                "screen_as_of": p.get("screen_as_of", raw_nisa.get("last_updated", "")),
                "schedule_note": p.get("tsumitate_schedule", {}).get("note", "") if p.get("tsumitate_schedule") else "",
                "notes": p.get("notes", ""),
            }
    else:
        nisa = {"note": "nisa_portfolio.json が存在しないため NISA 枠は不明"}

    # ── 税務コンテキスト（tax_optimizer） ────────────────────
    tax_context: dict = {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE_DIR))
        from tax_optimizer import get_full_tax_report
        tax_context = get_full_tax_report()
    except Exception as _e:
        tax_context = {"error": str(_e)}

    # ── 持株会コンテキスト（espp_plan_manager） ─────────────
    espp_context: dict = {}
    try:
        from espp_plan_manager import get_dashboard_data as _kd
        espp_context = _kd(portfolio_total)
    except Exception as _e:
        espp_context = {"error": str(_e)}

    # ── シナリオモニタリングデータ ────────────────────────────
    scenario_monitoring: dict = {}
    try:
        sc_state = load_json(BASE_DIR / "scenario_state.json", {})
        sc_playbook = load_json(BASE_DIR / "scenario_playbook.json", {})
        geo_state = load_json(BASE_DIR / "geopolitical_state.json", {})
        vix_state = load_json(BASE_DIR / "vix_state.json", {})
        tech_state_for_scenario = load_json(BASE_DIR / "technical_state.json", {})
        macro_state_for_scenario = load_json(BASE_DIR / "macro_state.json", {})
        promotion_summary = load_json(BASE_DIR / "scenario_promotion_summary.json", {})
        promotion_by_scenario = (
            promotion_summary.get("by_scenario", {})
            if isinstance(promotion_summary, dict)
            else {}
        )

        scenarios_dict = sc_state.get("scenarios", {})
        raw_playbook_scenarios = sc_playbook.get("scenarios", {})
        if isinstance(raw_playbook_scenarios, list):
            playbook_by_id = {
                item.get("id"): item
                for item in raw_playbook_scenarios
                if isinstance(item, dict) and item.get("id")
            }
        elif isinstance(raw_playbook_scenarios, dict):
            playbook_by_id = raw_playbook_scenarios
        else:
            playbook_by_id = {}

        def _compact_signals(details: list, *, matched: bool, limit: int = 4) -> list[dict]:
            out: list[dict] = []
            for sig in details or []:
                if not isinstance(sig, dict) or bool(sig.get("matched")) is not matched:
                    continue
                item = {
                    "type": sig.get("type"),
                    "key": sig.get("key"),
                    "detail": str(sig.get("detail", ""))[:160],
                }
                if sig.get("value") is not None:
                    item["value"] = sig.get("value")
                if sig.get("threshold") is not None:
                    item["threshold"] = sig.get("threshold")
                out.append(item)
                if len(out) >= limit:
                    break
            return out

        def _technical_snapshot(ticker: str | None) -> dict:
            if not ticker:
                return {}
            tickers = (
                tech_state_for_scenario.get("tickers", {})
                if isinstance(tech_state_for_scenario, dict)
                else {}
            )
            raw = tickers.get(str(ticker), {}) if isinstance(tickers, dict) else {}
            if not isinstance(raw, dict) or not raw:
                return {}
            return {
                "price": raw.get("price"),
                "change_5d_pct": raw.get("change_5d_pct"),
                "change_20d_pct": raw.get("change_20d_pct"),
                "rsi": raw.get("rsi"),
                "volume_ratio": raw.get("volume_ratio"),
                "composite_signal": raw.get("composite_signal"),
            }

        def _is_restricted_scenario_ticker(ticker: object) -> bool:
            return bool(ticker) and is_restricted_ticker(ticker)

        def _compact_actions(recommended: dict, limit: int = 10) -> list[dict]:
            out: list[dict] = []
            if not isinstance(recommended, dict):
                return out
            for phase in ("phase_1", "phase_2", "phase_3"):
                items = recommended.get(phase) or []
                if not isinstance(items, list):
                    continue
                for action in items:
                    if not isinstance(action, dict):
                        continue
                    ticker = action.get("ticker")
                    if _is_restricted_scenario_ticker(ticker):
                        continue
                    out.append({
                        "phase": phase,
                        "ticker": ticker,
                        "action": action.get("action") or action.get("type") or "buy",
                        "allocation_usd": action.get("allocation_usd"),
                        "allocation_jpy": action.get("allocation_jpy"),
                        "technical": _technical_snapshot(ticker),
                        "reason": str(action.get("reason", ""))[:180],
                    })
                    if len(out) >= limit:
                        return out
            return out

        def _compact_sell_triggers(recommended: dict, limit: int = 8) -> list[str]:
            triggers = recommended.get("sell_on_trigger") if isinstance(recommended, dict) else []
            if not isinstance(triggers, list):
                return []
            out: list[str] = []
            for t in triggers:
                if not t:
                    continue
                ticker = str(t)
                if _is_restricted_scenario_ticker(ticker):
                    continue
                tech = _technical_snapshot(ticker)
                if tech:
                    out.append(
                        f"{ticker} (price={tech.get('price')}, RSI={tech.get('rsi')}, "
                        f"20d={tech.get('change_20d_pct')}%)"
                    )
                else:
                    out.append(ticker)
                if len(out) >= limit:
                    return out
            return out

        def _compact_confirmations(recommended: dict, limit: int = 8) -> list[str]:
            req = recommended.get("confirmation_required") if isinstance(recommended, dict) else {}
            out: list[str] = []
            if isinstance(req, dict):
                for phase in ("phase_1", "phase_2", "phase_3"):
                    items = req.get(phase) or []
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if item:
                            out.append(f"{phase}: {str(item)[:140]}")
                        if len(out) >= limit:
                            return out
            elif isinstance(req, list):
                out = [str(item)[:140] for item in req[:limit] if item]
            return out

        # watchingとactiveシナリオだけ抽出。
        # F1: observe_only=true / enabled_for_decision=false のシナリオは
        # decision context (active_scenarios → プロンプト) から除外する。
        # ただし UI / coverage のために observe_only_scenarios へ別途保持する。
        active_scenarios = []
        observe_only_scenarios = []
        for sc_id, sc_data in scenarios_dict.items():
            status = sc_data.get("status", "dormant")
            # partial = required 全成立の限定サイズ発動 (allocation_scale 0.5) も decision 文脈へ
            if status in ("watching", "active", "partial"):
                pb = playbook_by_id.get(sc_id, {}) if isinstance(playbook_by_id, dict) else {}
                signal_details = sc_data.get("signal_details", [])
                flags = scenario_context_decision_flags(
                    sc_id, sc_data, pb, promotion_by_scenario
                )
                enabled_for_decision = flags["enabled_for_decision"]
                entry = {
                    "id": sc_id,
                    "name": sc_data.get("name", sc_id),
                    "description": pb.get("description", ""),
                    "priority": pb.get("priority", "medium"),
                    "status": status,
                    # 旧 scenario_state (allocation_scale 無し) は status から導出
                    "allocation_scale": sc_data.get(
                        "allocation_scale",
                        1.0 if status == "active" else (0.5 if status == "partial" else 0.0),
                    ),
                    "readiness_pct": round(sc_data.get("readiness", 0) * 100),
                    "signals_met": sc_data.get("signals_met", 0),
                    "signals_total": sc_data.get("signals_total", 0),
                    "matched_signals": _compact_signals(signal_details, matched=True),
                    "missing_signals": _compact_signals(signal_details, matched=False),
                    "activation_policy_status": sc_data.get(
                        "activation_policy_status",
                        (
                            "passed" if sc_data.get("activation_policy_passed") is True
                            else "failed" if sc_data.get("activation_policy_passed") is False
                            else "not_configured"
                        ),
                    ),
                    "activation_policy_passed": sc_data.get("activation_policy_passed"),
                    "activation_policy_failures": sc_data.get("activation_policy_failures", []),
                    "playbook_actions": _compact_actions(sc_data.get("recommended_actions", {})),
                    "sell_triggers": _compact_sell_triggers(sc_data.get("recommended_actions", {})),
                    "confirmation_required": _compact_confirmations(sc_data.get("recommended_actions", {})),
                    "first_detected": sc_data.get("first_detected"),
                    "last_evaluated": sc_data.get("last_evaluated"),
                    "observe_only": flags["observe_only"],
                    "original_observe_only": flags["original_observe_only"],
                    "enabled_for_decision": enabled_for_decision,
                    "promotion_ready": flags["promotion_ready"],
                    "promotion": flags["promotion"],
                }
                if enabled_for_decision:
                    active_scenarios.append(entry)
                else:
                    observe_only_scenarios.append(entry)
        active_scenarios.sort(key=lambda x: x["readiness_pct"], reverse=True)
        observe_only_scenarios.sort(key=lambda x: x["readiness_pct"], reverse=True)

        # 地政学アラートは高重要度・高確度だけを合成へ渡す。
        # medium は背景ノイズになりやすいため dashboard の raw state に留める。
        geo_alerts = []
        for alert in geo_state.get("active_alerts", []):
            if isinstance(alert, dict):
                sev = alert.get("severity", "low")
                try:
                    confidence = float(alert.get("confidence", 0) or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if sev in ("high", "critical") and confidence >= 0.65:
                    geo_alerts.append({
                        "scenario": alert.get("scenario_key", ""),
                        "severity": sev,
                        "headline": alert.get("headline", "")[:80],
                        "confidence": confidence,
                    })
        geo_alerts = sorted(
            geo_alerts,
            key=lambda a: (a["severity"] == "critical", a["confidence"]),
            reverse=True,
        )[:2]

        # VIX・恐怖指数詳細
        vix_obj = vix_state.get("vix", {})
        fear_greed = vix_state.get("fear_greed", {})
        term_structure = vix_state.get("vix_term_structure", {})
        spy_obj = vix_state.get("spy", {})
        sector_flows_raw = vix_state.get("sector_flows", {}) or {}
        sector_flows = []
        if isinstance(sector_flows_raw, dict):
            for ticker, sec in sector_flows_raw.items():
                if not isinstance(sec, dict):
                    continue
                sector_flows.append({
                    "ticker": ticker,
                    "return_5d_pct": sec.get("return_5d_pct"),
                    "vs_spy_5d_pct": sec.get("vs_spy_5d_pct"),
                })
            sector_flows.sort(key=lambda x: abs(x.get("vs_spy_5d_pct") or 0), reverse=True)
        vix_detail = {
            "level": vix_obj.get("level"),
            "classification": vix_obj.get("classification"),
            "change_5d": vix_obj.get("change_5d"),
            "decay_from_peak_5d_pct": vix_obj.get("decay_from_peak_5d_pct"),
            "percentile": vix_obj.get("percentile_1y"),
            "term_structure": term_structure.get("structure"),
            "term_ratio": term_structure.get("ratio"),
            "fear_greed_score": fear_greed.get("score"),
            "fear_greed_label": fear_greed.get("label"),
            "oil_change_5d_pct": vix_state.get("oil", {}).get("change_5d_pct"),
            "yield_spread_10y3m": vix_state.get("yields", {}).get("spread_10y_3m"),
            "spy_vs_ma50_pct": spy_obj.get("vs_ma50_pct"),
            "put_call_ratio": vix_state.get("put_call_ratio"),
            "hy_spread_bps": vix_state.get("hy_spread_bps"),
            "sector_flows": sector_flows[:5],
        }
        macro_detail = {
            "fed_rate": macro_state_for_scenario.get("fed_rate"),
            "yield_10y": macro_state_for_scenario.get("yield_10y"),
            "yield_2y": macro_state_for_scenario.get("yield_2y"),
            "cpi_yoy": macro_state_for_scenario.get("cpi_yoy"),
            "unemp_rate": macro_state_for_scenario.get("unemp_rate"),
            "hy_oas_bps": macro_state_for_scenario.get("hy_oas_bps"),
            "fear_greed": macro_state_for_scenario.get("fear_greed"),
        }
        technical_detail = {
            "market_breadth": tech_state_for_scenario.get("market_breadth", {}),
            "cached_at": tech_state_for_scenario.get("cached_at"),
        }

        scenario_monitoring = {
            "active_scenarios": active_scenarios,
            "observe_only_scenarios": observe_only_scenarios,
            "overall_alert_level": sc_state.get("overall_alert_level", "green"),
            "geo_alerts": geo_alerts,
            "vix_detail": vix_detail,
            "macro_detail": macro_detail,
            "technical_detail": technical_detail,
            "evaluated_at": sc_state.get("evaluated_at", ""),
        }
    except Exception as _e:
        scenario_monitoring = {"error": str(_e)}

    # ── SNS感情・オプション分析（social_screener.py出力） ──────────────
    social_sentiment = load_json(BASE_DIR / "social_sentiment.json", {})

    # ── リバランスレポート詳細（rebalance_engine.py出力） ───────────────
    rebalance_report = load_json(BASE_DIR / "rebalance_report.json", {})
    execution_plan = load_json(BASE_DIR / "execution_plan_state.json", {})

    # ── 内部台帳整合性（executions / holdings / account / event_ledger） ─────
    portfolio_integrity: dict = {}
    try:
        from portfolio_integrity import run_integrity_check
        portfolio_integrity = run_integrity_check()
    except Exception as _e:
        portfolio_integrity = {"ok": False, "error": str(_e)}

    # ── テクニカル状態（individual ticker RSI/MACD/BB/composite） ──────
    technical_state_raw = load_json(BASE_DIR / "technical_state.json")
    technical_state: dict = {}
    if technical_state_raw and isinstance(technical_state_raw.get("tickers"), dict):
        technical_state = technical_state_raw["tickers"]  # dict[ticker, {rsi, macd_histogram, bb_pct_b, volume_ratio, composite_score, composite_signal, ...}]

    # ── ニュース感情集計（FinBERT aggregate） ────────────────────────
    news_sentiment_summary = load_json(BASE_DIR / "news_sentiment_summary.json", {})

    sector_strength_raw = load_json(BASE_DIR / "sector_strength.json")
    sector_strength: dict = {}
    if sector_strength_raw:
        for sec, v in sector_strength_raw.items():
            if isinstance(v, dict):
                sector_strength[sec] = {
                    "score": v.get("score", 0),
                    "strong": v.get("strong", False),
                    "mom_1m": v.get("mom_1m"),
                    "mom_3m": v.get("mom_3m"),
                }

    market_meta: dict = {}
    screen_meta_raw = load_json(BASE_DIR / "screen_results.json")
    if isinstance(screen_meta_raw, dict) and "market_meta" in screen_meta_raw:
        mm = screen_meta_raw["market_meta"]
        market_meta = {
            "sp500_price": mm.get("sp500_price"),
            "sp500_ma50": mm.get("sp500_ma50"),
            "sp500_vs_ma50_pct": round(
                (mm.get("sp500_price", 0) / mm.get("sp500_ma50", 1) - 1) * 100, 2
            ) if mm.get("sp500_ma50") else None,
            "nikkei_price": mm.get("nikkei_price"),
            "nikkei_ma50": mm.get("nikkei_ma50"),
            "nikkei_vs_ma50_pct": round(
                (mm.get("nikkei_price", 0) / mm.get("nikkei_ma50", 1) - 1) * 100, 2
            ) if mm.get("nikkei_ma50") else None,
            "meta_text": screen_meta_raw.get("meta_text", ""),
        }

    all_tickers = sorted({
        pos.get("ticker", "")
        for pos in positions
        if _is_earnings_context_ticker(pos.get("ticker"))
    })

    from analyst.cache import write_progress
    write_progress(1, 8, "📡 市場データ並列取得中", "VIX・金利・ニュース・決算・日本株ファンダメンタルズを同時取得")
    print("📡 市場データ並列取得中 (市場指標/日本株/ニュース/決算)…")

    # タイムアウト付き並列取得: いずれかがネットワーク待ちで詰まっても90秒で打ち切る
    # with文を使わず shutdown(wait=False) で主スレッドをブロックさせない
    _GATHER_TIMEOUT = 90
    _exec = ThreadPoolExecutor(max_workers=4)
    futures = {
        "indicators": _exec.submit(gather_market_indicators),
        "jp": _exec.submit(gather_jp_fundamentals),
        "news": _exec.submit(gather_news),
        "earnings": _exec.submit(gather_earnings_context, all_tickers),
    }
    collector_results = _collect_parallel_results(
        futures,
        fallbacks={
            "indicators": {},
            "jp": {},
            "news": {"market": [], "holdings": {}},
            "earnings": {},
        },
        timeout_seconds=_GATHER_TIMEOUT,
        labels={
            "indicators": "市場指標",
            "jp": "日本株ファンダメンタルズ",
            "news": "ニュース",
            "earnings": "決算データ",
        },
    )

    market_meta.update(collector_results["indicators"] or {})
    jp_fundamentals = collector_results["jp"] or {}
    news = collector_results["news"] or {"market": [], "holdings": {}}
    earnings_ctx = collector_results["earnings"] or {}

    _exec.shutdown(wait=False, cancel_futures=True)  # ハングスレッドをバックグラウンドに解放
    print("  ✅ 市場データ取得完了")

    # ── JP equity 比率 (持株会除外, 2026-07-07) ──
    # 持株会 (9999.T) は売買自由度が低く月次積立で自動増加するため、日本株
    # エクスポージャーの目標比較には含めない。目標は固定値ではなく
    # dynamic_jp_equity_target() で市場環境 (japan_standalone_bull readiness /
    # VIX / 実損益ガード) に連動してスケールする (base 10% / band 5〜20%)。
    jp_exposure: dict = {}
    try:
        from rebalance_engine import EMPLOYER_STOCK_TICKERS
        try:
            from tunable_params import get as _tp_get
            _jp_base = float(_tp_get("jp_equity_target_base_pct", 10.0))
            _jp_span = float(_tp_get("jp_equity_target_scenario_span_pct", 10.0))
            _jp_min = float(_tp_get("jp_equity_target_min_pct", 5.0))
            _jp_max = float(_tp_get("jp_equity_target_max_pct", 20.0))
        except Exception:
            _jp_base, _jp_span, _jp_min, _jp_max = 10.0, 10.0, 5.0, 20.0
        _jp_tgt = dynamic_jp_equity_target(
            _jp_base,
            scenario_monitoring=(scenario_monitoring if isinstance(scenario_monitoring, dict) else None),
            vix=market_meta.get("vix"),
            guard=(guard if isinstance(guard, dict) else None),
            span_pct=_jp_span, min_pct=_jp_min, max_pct=_jp_max,
        )
        _jp_target = float(_jp_tgt["target_pct"])
        _jp_vals = [
            float(p.get("value_jpy") or 0) for p in positions
            if str(p.get("ticker") or "").endswith(".T")
            and str(p.get("ticker") or "") not in EMPLOYER_STOCK_TICKERS
        ]
        _emp_vals = [
            float(p.get("value_jpy") or 0) for p in positions
            if str(p.get("ticker") or "") in EMPLOYER_STOCK_TICKERS
        ]
        _jp_total = sum(_jp_vals)
        jp_exposure = {
            "jp_equity_ex_employer_jpy": round(_jp_total),
            "jp_equity_ex_employer_pct": round(_jp_total / float(portfolio_total) * 100, 2),
            "employer_value_jpy": round(sum(_emp_vals)),
            "employer_tickers": sorted(EMPLOYER_STOCK_TICKERS),
            "target_pct": _jp_target,
            "target_base_pct": _jp_base,
            "target_detail": _jp_tgt,
            "headroom_jpy": round(max(0.0, _jp_target / 100.0 * float(portfolio_total) - _jp_total)),
        }
    except Exception as _je:
        jp_exposure = {"error": str(_je)}

    return {
        "positions": positions,
        "portfolio_total": portfolio_total,
        "currency_breakdown": portfolio.get("currency_breakdown", {}),
        "jp_exposure": jp_exposure,
        # 2026-07: AI 外貨比率判断用。whole_portfolio と long_tier(rebalance適用母数)を分離。
        "currency_breakdown_whole": currency_breakdown_whole,
        "currency_breakdown_long": currency_breakdown_long,
        "current_currency_policy": current_currency_policy,
        "screening": screening,
        "signals": signals,
        "signals_age_hours": signals_age_hours,
        "signals_generated_at": signals_generated_at,
        "screen_candidates": screen_candidates,
        "scenario": scenario,
        "regime": regime,
        "guard": guard,
        "margin": margin,
        "rebalance": rebalance,
        "risk": risk,
        "nisa": nisa,
        "sector_strength": sector_strength,
        "market_meta": market_meta,
        "news": news,
        "jp_fundamentals": jp_fundamentals,
        "backtest_summary": backtest_summary,
        "earnings": earnings_ctx,
        "cash_info": cash_info,
        "pending_orders": pending_orders,
        "tax_context": tax_context,
        "espp_context": espp_context,
        "scenario_monitoring": scenario_monitoring,
        "ipo_watch": ipo_watch,
        "technical_state": technical_state,
        "news_sentiment_summary": news_sentiment_summary,
        "social_sentiment": social_sentiment,
        "rebalance_report": rebalance_report,
        "execution_plan": execution_plan,
        "portfolio_integrity": portfolio_integrity,
    }
