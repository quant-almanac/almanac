"""
scenario_engine.py — シナリオマッチングエンジン

scenario_playbook.json の各シナリオの検知条件を評価し、
現在の市場状態と照合して scenario_state.json を出力する。
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path

from utils import atomic_write_json, load_json

try:
    from alert import send_telegram
except ImportError:
    def send_telegram(msg: str) -> None:
        print(f"[TELEGRAM] {msg}")

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# ── 入力ファイルパス ──────────────────────────────────────
PLAYBOOK_PATH       = BASE_DIR / "scenario_playbook.json"
VIX_STATE_PATH      = BASE_DIR / "vix_state.json"
GEO_STATE_PATH      = BASE_DIR / "geopolitical_state.json"
TECH_STATE_PATH     = BASE_DIR / "technical_state.json"
MACRO_STATE_PATH    = BASE_DIR / "macro_state.json"
REGIME_STATE_PATH   = BASE_DIR / "regime_state.json"
MARKET_SNAPSHOT_PATH = BASE_DIR / "market_snapshot.json"
GUARD_STATE_PATH    = BASE_DIR / "guard_state.json"
SCENARIO_STATE_PATH = BASE_DIR / "scenario_state.json"

INCONCLUSIVE_DETAIL = "データ未取得"

# シグナルタイプ別の重み（indicator が最重要、news と technical が補完）
SIGNAL_WEIGHTS = {
    "news":       0.30,
    "indicator":  0.40,
    "technical":  0.30,
}

NEWS_FALLBACK_MIN_SCORE = 3
NEWS_FALLBACK_MIN_CONFIDENCE = 0.65
NEWS_FALLBACK_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _news_min_score(scenario: dict) -> int:
    """Scenario-specific keyword threshold for news fallback matching."""
    detect = scenario.get("detect", {}) if isinstance(scenario, dict) else {}
    raw = detect.get("min_keyword_score", NEWS_FALLBACK_MIN_SCORE)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return NEWS_FALLBACK_MIN_SCORE


def _weighted_readiness(signal_details: list) -> float:
    """シグナルタイプ別重み付きで readiness を計算。
    データ未取得シグナルはスキップ（weight から除外）する。
    """
    weighted_sum = 0.0
    weight_total = 0.0
    for s in signal_details:
        # データ未取得シグナルは分母からも除外
        if s.get("detail") == INCONCLUSIVE_DETAIL:
            continue
        w = SIGNAL_WEIGHTS.get(s.get("type", "indicator"), 0.33)
        weight_total += w
        if s.get("matched"):
            weighted_sum += w
    if weight_total == 0.0:
        return 0.0
    return weighted_sum / weight_total


# ═══════════════════════════════════════════════════════════
#  状態ファイル読み込み
# ═══════════════════════════════════════════════════════════

def _load_states() -> dict:
    """全入力ファイルを読み込み、1つの dict にまとめる。"""
    return {
        "vix":       load_json(VIX_STATE_PATH, {}),
        "geo":       load_json(GEO_STATE_PATH, {}),
        "tech":      load_json(TECH_STATE_PATH, {}),
        "macro":     load_json(MACRO_STATE_PATH, {}),
        "regime":    load_json(REGIME_STATE_PATH, {}),
        "market":    load_json(MARKET_SNAPSHOT_PATH, {}),
        "guard":     load_json(GUARD_STATE_PATH, {}),
    }


# ═══════════════════════════════════════════════════════════
#  ニュースシグナル評価
# ═══════════════════════════════════════════════════════════

def _keyword_match_fallback(scenario: dict, geo_state: dict) -> dict | None:
    """AI 評価に失敗しても raw keyword match をニュースシグナルとして使う。

    geopolitical_monitor は Web Search の keyword match 後に LLM で severity を判定する。
    LLM 側が 529/timeout で落ちると active_alerts が空になり、従来は
    「ニュースなし」に見えていた。score>=3 かつ高重要度の keyword match だけを
    弱いが有効な観測値として scenario readiness に反映する。
    """
    scenario_id = scenario.get("id", "")
    matches = geo_state.get("keyword_matches", [])
    if not isinstance(matches, list):
        return None

    best: dict | None = None
    best_score = 0
    min_score = _news_min_score(scenario)
    for item in matches:
        if not isinstance(item, dict):
            continue
        if item.get("scenario_key") != scenario_id:
            continue
        score = item.get("score")
        if not isinstance(score, (int, float)):
            score = len(item.get("matched_keywords") or [])
        severity = item.get("severity")
        if severity:
            # priority=high シナリオは severity=medium も弱シグナルとして採用する。
            # 2026-06 イラン停戦: AI 評価 severity=medium/conf0.65 が materiality 除外
            # → news シグナル不成立 → war_end が dormant のままラリーを逃した。
            _min_rank = (
                NEWS_FALLBACK_SEVERITY_ORDER["medium"]
                if scenario.get("priority") == "high"
                else NEWS_FALLBACK_SEVERITY_ORDER["high"]
            )
            if NEWS_FALLBACK_SEVERITY_ORDER.get(str(severity), 0) < _min_rank:
                continue
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < NEWS_FALLBACK_MIN_CONFIDENCE:
                    continue
            except (TypeError, ValueError):
                continue
        if score >= min_score and score > best_score:
            best = item
            best_score = int(score)

    if not best:
        return None

    matched_kw = best.get("matched_keywords") or []
    if isinstance(matched_kw, str):
        matched_kw = [matched_kw]
    kw_txt = ", ".join(str(kw) for kw in matched_kw[:3]) or f"score={best_score}"
    status = best.get("assessment_status") or "keyword_only"
    detail = (
        f"キーワード一致(fallback: AI評価={status}): {kw_txt} "
        f"(score={best_score}, threshold={min_score})"
    )
    return {
        "type": "news",
        "key": "news_keywords",
        "matched": True,
        "detail": detail,
        "value": best_score,
        "threshold": min_score,
        "source": "ai_fallback",
    }


def _eval_news(scenario: dict, geo_state: dict) -> dict:
    """geopolitical_state の active_alerts / keyword_matches を評価。"""
    keywords = scenario.get("detect", {}).get("news_keywords", [])
    if not keywords:
        return {"type": "news", "key": "news_keywords",
                "matched": False, "detail": "キーワード未定義"}

    active_alerts = geo_state.get("active_alerts", [])
    if not active_alerts:
        if not geo_state:
            return {"type": "news", "key": "news_keywords",
                    "matched": False, "detail": INCONCLUSIVE_DETAIL}
        fallback = _keyword_match_fallback(scenario, geo_state)
        if fallback:
            return fallback
        return {"type": "news", "key": "news_keywords",
                "matched": False, "detail": "No matching headlines"}

    # 全アラートのタイトル/サマリーを結合して検索対象にする
    combined_text = ""
    for alert in active_alerts:
        if isinstance(alert, dict):
            combined_text += " " + alert.get("title", "")
            combined_text += " " + alert.get("summary", "")
            combined_text += " " + alert.get("headline", "")
        elif isinstance(alert, str):
            combined_text += " " + alert
    combined_lower = combined_text.lower()

    matched_kw = [kw for kw in keywords if kw.lower() in combined_lower]

    # severity チェック — medium 以上でマッチ
    max_severity = _max_severity(active_alerts)
    severity_ok = max_severity in ("medium", "high", "critical")

    matched = bool(matched_kw) and severity_ok
    if matched_kw and not severity_ok:
        detail = f"キーワード合致({', '.join(matched_kw[:3])})だが severity={max_severity}"
        matched = False
    elif matched:
        detail = f"合致: {', '.join(matched_kw[:3])} (severity={max_severity})"
    else:
        fallback = _keyword_match_fallback(scenario, geo_state)
        if fallback:
            return fallback
        detail = "No matching headlines"

    return {"type": "news", "key": "news_keywords",
            "matched": matched, "detail": detail, "source": "active_alerts"}


def _max_severity(alerts: list) -> str:
    """アラートリストから最大 severity を返す。"""
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    best = "low"
    for a in alerts:
        if isinstance(a, dict):
            sev = a.get("severity", "low")
            if order.get(sev, 0) > order.get(best, 0):
                best = sev
    return best


def _build_recommended_actions(scenario: dict) -> dict:
    """playbook actions を Opus/画面向けに欠落なく保持する。"""
    actions = scenario.get("actions", {}) or {}

    def _collect_phase(prefix: str) -> list:
        collected: list = []
        for phase_key, phase in actions.items():
            if not str(phase_key).startswith(prefix) or not isinstance(phase, dict):
                continue
            collected.extend(phase.get("buy", []) or [])
            collected.extend(phase.get("sell", []) or [])
        return collected

    def _collect_confirmations(prefix: str) -> list:
        collected: list = []
        for phase_key, phase in actions.items():
            if not str(phase_key).startswith(prefix) or not isinstance(phase, dict):
                continue
            collected.extend(phase.get("confirmation_required", []) or [])
        return collected

    return {
        "phase_1": _collect_phase("phase_1"),
        "phase_2": _collect_phase("phase_2"),
        "phase_3": _collect_phase("phase_3"),
        "sell_on_trigger": actions.get("sell_on_trigger", []),
        "confirmation_required": {
            "phase_1": _collect_confirmations("phase_1"),
            "phase_2": _collect_confirmations("phase_2"),
            "phase_3": _collect_confirmations("phase_3"),
        },
    }


# ═══════════════════════════════════════════════════════════
#  インジケーターシグナル評価
# ═══════════════════════════════════════════════════════════

def _resolve_indicator_value(ind_key: str, cond: dict,
                             vix_state: dict, macro_state: dict,
                             market_state: dict | None = None) -> float | None:
    """指標名から対応する state ファイルの値を引く。

    vix_state.json の構造（vix_tracker.py が生成）:
      vix.level, vix.change_1d, vix.change_5d
      oil.price, oil.change_5d_pct
      yields.spread_10y_3m, yields.us_2y, yields.us_10y
      fear_greed.score
      vix_term_structure.structure
    """
    # ── ヘルパー: ネストされたパスをドット記法で解決 ──
    def _nested(d: dict, *keys):
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    # vix_state のネスト構造へのショートカット
    vix_obj  = vix_state.get("vix", {}) or {}
    oil_obj  = vix_state.get("oil", {}) or {}
    yld_obj  = vix_state.get("yields", {}) or {}
    fg_obj   = vix_state.get("fear_greed", {}) or {}
    market_state = market_state or {}

    # 明示的な key フィールドがあればそれを使う（フラットキーとネストキー両対応）
    explicit_key = cond.get("key")
    if explicit_key:
        ek = explicit_key.lower()
        # vix_current → vix.level
        if ek in ("vix_current", "vix_level", "vix"):
            return _to_float(vix_obj.get("level"))
        # vix_change_1d / vix_delta_1d
        if ek in ("vix_change_1d", "vix_delta_1d"):
            return _to_float(vix_obj.get("change_1d"))
        # vix_change_5d / vix_delta_5d
        if ek in ("vix_change_5d", "vix_delta_5d"):
            return _to_float(vix_obj.get("change_5d"))
        # oil 系
        if ek in ("oil_wti", "oil_price", "wti"):
            return _to_float(oil_obj.get("price"))
        if ek in ("oil_change_5d", "oil_delta_5d"):
            return _to_float(oil_obj.get("change_5d_pct"))
        # フラットに存在する場合
        if explicit_key in vix_state:
            v = vix_state[explicit_key]
            if isinstance(v, (int, float)):
                return _to_float(v)
        if explicit_key in macro_state:
            return _to_float(macro_state[explicit_key])
        # 新規追加: vix_state の新フィールド
        if ek == "yield_30y_10y_spread":
            return _to_float(yld_obj.get("spread_30y_10y"))
        if ek == "dxy_dollar":
            dxy_obj = vix_state.get("dxy", {}) or {}
            cond_type = cond.get("condition", "")
            if "drop" in cond_type or "pct" in cond_type:
                return _to_float(dxy_obj.get("change_5d_pct"))
            return _to_float(dxy_obj.get("level"))
        if ek == "usdcny":
            usdcny_obj = vix_state.get("usdcny", {}) or {}
            cond_type = cond.get("condition", "")
            if "drop" in cond_type or "pct" in cond_type:
                return _to_float(usdcny_obj.get("change_5d_pct"))
            return _to_float(usdcny_obj.get("level"))
        if ek == "copper_hg":
            copper_obj = vix_state.get("copper", {}) or {}
            cond_type = cond.get("condition", "")
            if "drop" in cond_type or "pct" in cond_type:
                return _to_float(copper_obj.get("change_5d_pct"))
            return _to_float(copper_obj.get("price"))
        if ek == "hy_spread":
            return _to_float(vix_state.get("hy_spread_bps"))
        if ek == "spy_drop_1d":
            spy_obj = vix_state.get("spy", {}) or {}
            return _to_float(spy_obj.get("change_1d"))
        if ek == "spy_drop_5d":
            spy_obj = vix_state.get("spy", {}) or {}
            return _to_float(spy_obj.get("change_5d"))
        if ek == "put_call_ratio":
            return _to_float(vix_state.get("put_call_ratio"))
        if ek in ("spy_dist_from_ma50_pct", "spy_vs_ma50_pct", "spy_ma50_diff"):
            spy_market = market_state.get("SPY", {}) or {}
            spy_vix = vix_state.get("spy", {}) or {}
            return _to_float(
                _first_present(spy_market, "ma50_diff", "vs_ma50_pct")
                if (spy_market.get("ma50_diff") is not None or spy_market.get("vs_ma50_pct") is not None)
                else spy_vix.get("vs_ma50_pct")
            )
        return None

    # キー名からヒューリスティックに解決
    k = ind_key.lower()

    # ── VIX ──
    if k == "vix" or k == "vix_level":
        return _to_float(vix_obj.get("level"))
    if "vix" in k and ("delta_1d" in k or "change_1d" in k):
        return _to_float(vix_obj.get("change_1d"))
    if "vix" in k and ("delta_5d" in k or "change_5d" in k):
        return _to_float(vix_obj.get("change_5d"))
    if k.startswith("vix"):
        return _to_float(vix_obj.get("level"))

    # ── 原油 ──
    condition_type = cond.get("condition", "")
    if "oil" in k and ("change_5d" in k or "delta_5d" in k):
        return _to_float(oil_obj.get("change_5d_pct"))
    if "oil" in k and condition_type in ("drop_pct", "drop_pct_5d", "spike_pct", "rise_pct_5d"):
        # 変化率系の条件には change_5d_pct を返す
        return _to_float(oil_obj.get("change_5d_pct"))
    if "oil" in k:
        return _to_float(oil_obj.get("price"))

    # ── イールド ── (0.0 を欠損扱いしない: _first_non_none で先頭の非 None を採る)
    if "yield_spread" in k:
        v = _first_non_none(yld_obj.get("spread_10y_3m"), macro_state.get("yield_spread"))
        return _to_float(v)
    if "yield_2y" in k:
        v = _first_non_none(yld_obj.get("us_2y"), macro_state.get("yield_2y"))
        return _to_float(v)
    if "yield_10y" in k:
        v = _first_non_none(yld_obj.get("us_10y"), macro_state.get("yield_10y"))
        return _to_float(v)
    if k.startswith("fed_"):
        return _to_float(macro_state.get("fed_rate"))

    # ── Fear & Greed ──
    if "fear" in k or "greed" in k:
        return _to_float(fg_obj.get("score"))

    # ── 30Y-10Y スプレッド ──
    if "30y_10y" in k or "yield_30y" in k:
        return _to_float(yld_obj.get("spread_30y_10y"))

    # ── DXY ドル指数 ──
    if "dxy" in k or "dollar" in k:
        dxy_obj = vix_state.get("dxy", {}) or {}
        cond_type = cond.get("condition", "")
        if "change" in cond_type or "drop" in cond_type or "pct" in cond_type:
            return _to_float(dxy_obj.get("change_5d_pct"))
        return _to_float(dxy_obj.get("level"))

    # ── USD/CNY ──
    if "usdcny" in k or "cny" in k or "yuan" in k:
        usdcny_obj = vix_state.get("usdcny", {}) or {}
        cond_type = cond.get("condition", "")
        if "change" in cond_type or "drop" in cond_type or "pct" in cond_type:
            return _to_float(usdcny_obj.get("change_5d_pct"))
        return _to_float(usdcny_obj.get("level"))

    # ── 銅先物 ──
    if "copper" in k or "hg" in k:
        copper_obj = vix_state.get("copper", {}) or {}
        cond_type = cond.get("condition", "")
        if "change" in cond_type or "drop" in cond_type or "pct" in cond_type:
            return _to_float(copper_obj.get("change_5d_pct"))
        return _to_float(copper_obj.get("price"))

    # ── HY スプレッド ──
    if "hy_spread" in k or "high_yield" in k:
        return _to_float(vix_state.get("hy_spread_bps"))

    # ── SPY 変化率（バブル崩壊用）──
    if "spy_drop_1d" in k or k == "spy_drop_1d":
        spy_obj = vix_state.get("spy", {}) or {}
        return _to_float(spy_obj.get("change_1d"))
    if "spy_drop_5d" in k or k == "spy_drop_5d":
        spy_obj = vix_state.get("spy", {}) or {}
        return _to_float(spy_obj.get("change_5d"))

    # ── Put/Call レシオ ──
    if "put_call" in k or "put_call_ratio" in k:
        return _to_float(vix_state.get("put_call_ratio"))

    if "spy" in k and ("ma50" in k or "dist" in k):
        spy_market = market_state.get("SPY", {}) or {}
        spy_vix = vix_state.get("spy", {}) or {}
        return _to_float(_first_non_none(
            spy_market.get("ma50_diff"), spy_market.get("vs_ma50_pct"), spy_vix.get("vs_ma50_pct")))

    # ── フォールバック: delta 系 ──
    if "delta" in k and "1d" in k:
        return _to_float(vix_obj.get("change_1d"))
    if "delta" in k and "5d" in k:
        return _to_float(vix_obj.get("change_5d"))

    # ── EWJ 20日リターン ──
    # market_snapshot.json["EWJ"]["return_20d"] または technical_state["tickers"]["EWJ"]
    if k in ("ewj_return_20d", "ewj_performance", "ewj_20d"):
        ewj = market_state.get("EWJ", {}) or {}
        val = _first_present(ewj, "return_20d", "change_20d_pct", "return_20d_pct")
        return _to_float(val)

    # ── SPY 対 EWJ 相対リターン (20日) ──
    # 正の値 = EWJ が SPY をアウトパフォーム
    if k in ("spy_ewj_relative", "ewj_vs_spy_20d", "ewj_outperforms_spy_20d"):
        ewj = market_state.get("EWJ", {}) or {}
        spy = market_state.get("SPY", {}) or {}
        ewj_ret = _to_float(_first_present(ewj, "return_20d", "change_20d_pct"))
        spy_ret = _to_float(_first_present(spy, "return_20d", "change_20d_pct"))
        if ewj_ret is not None and spy_ret is not None:
            return ewj_ret - spy_ret
        return None

    return None


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _resolve_ticker_change(ind_key: str, cond: dict, tech_state: dict) -> float | None:
    """ETF/ticker名が指標キーに含まれる場合、technical_state から変化率を返す試み。
    例: defense_etf_ita → ITA, gold_gld → GLD, copper_hg → なし(Parquetなし)
    """
    tickers_data = tech_state.get("tickers", {}) if tech_state else {}
    # キー名からティッカーを推測
    # パターン: 末尾の大文字3-4文字、または既知の別名
    aliases = {
        "defense_etf_ita": "ITA",
        "gold_gld": "GLD",
        "copper_hg": None,
        "soxx_semi": "SOXX",
        "qqq_nasdaq": "QQQ",
        "smh_volume": "SMH",
        "kbw_bank_index": "XLF",   # KBW銀行指数の代替
        "hy_spread": None,         # HYスプレッドはデータなし
        "fed_funds_futures": None, # FF先物はデータなし
        "dxy_dollar": None,        # DXYはParquetなし
        "usdcny": None,            # 人民元はデータなし
        "fxi_china_etf": "FXI",
        "eem_em_etf": "EEM",
        "ew_japan_ewj": "EWJ",   # 円キャリー巻き戻しシナリオ用
    }
    ticker = aliases.get(ind_key.lower())
    if ticker and ticker in tickers_data:
        t = tickers_data[ticker]
        if t.get("data_quality_status") == "blocked":
            return None
        condition_type = cond.get("condition", "")
        if condition_type in ("drop_pct_5d", "drop_pct", "spike_pct", "rise_pct_5d"):
            return _to_float(t.get("change_5d_pct"))
        if condition_type in ("drop_pct_1d", "rise_pct_1d"):
            return _to_float(t.get("change_1d_pct"))
        if condition_type in ("drop_pct_20d", "rise_pct_20d"):
            return _to_float(t.get("change_20d_pct"))
        if condition_type == "above_ma200":
            return _to_float(t.get("ma200_diff_pct"))
        if condition_type == "above_avg_pct":
            volume_ratio = _to_float(t.get("volume_ratio"))
            if volume_ratio is None:
                return None
            return (volume_ratio - 1.0) * 100.0
        if condition_type in ("above", "below"):
            return _to_float(t.get("price"))
    return None


def _eval_indicators(scenario: dict, vix_state: dict,
                     macro_state: dict, tech_state: dict | None = None,
                     market_state: dict | None = None) -> list[dict]:
    """detect.indicators の各条件を評価。"""
    indicators = scenario.get("detect", {}).get("indicators", {})
    results = []

    for ind_key, cond in indicators.items():
        condition_type = cond.get("condition", "")
        threshold = cond.get("threshold")
        value = _resolve_indicator_value(ind_key, cond, vix_state, macro_state, market_state)
        # フォールバック: tech_state から取れるETFがあれば試みる
        if value is None and tech_state:
            value = _resolve_ticker_change(ind_key, cond, tech_state)

        entry = {
            "type": "indicator",
            "key": ind_key,
            "matched": False,
            "detail": "",
            "value": value,
            "threshold": threshold,
        }

        if value is None:
            entry["detail"] = INCONCLUSIVE_DETAIL
            results.append(entry)
            continue

        matched = False
        if condition_type in ("above", "above_bps"):
            matched = value > threshold
            op_str = ">" if not matched else ">"
            entry["detail"] = f"{ind_key} {value:.2f} {'>' if matched else '<='} {threshold}"
        elif condition_type == "below":
            matched = value < threshold
            entry["detail"] = f"{ind_key} {value:.2f} {'<' if matched else '>='} {threshold}"
        elif condition_type in ("drop_pct", "drop_pct_5d", "drop_pct_1d"):
            # threshold is negative (e.g. -10); value should also be negative or small
            matched = value <= threshold if threshold < 0 else value <= -threshold
            entry["detail"] = f"{ind_key} {value:+.1f}% (閾値 {threshold}%)"
        elif condition_type in ("spike_pct", "rise_pct_5d", "rise_pct_20d"):
            matched = value >= threshold
            entry["detail"] = f"{ind_key} {value:+.1f}% (閾値 +{threshold}%)"
        elif condition_type == "between":
            lower = cond.get("lower")
            upper = cond.get("upper")
            if lower is None or upper is None:
                entry["detail"] = cond.get("description", INCONCLUSIVE_DETAIL)
            else:
                compare_value = value
                # Playbook thresholds are often stored as fractions (-0.08),
                # while state files report percentages (-8.0). Normalize only
                # for comparison and keep the detail human-readable.
                if max(abs(float(lower)), abs(float(upper))) <= 1 and abs(value) > 1:
                    compare_value = value / 100.0
                matched = float(lower) <= compare_value <= float(upper)
                entry["detail"] = (
                    f"{ind_key} {value:+.2f}% "
                    f"({'within' if matched else 'outside'} {float(lower)*100:+.1f}%..{float(upper)*100:+.1f}%)"
                )
        elif condition_type in ("drop_bps_5d", "drop_bps_1d"):
            matched = value <= threshold
            entry["detail"] = f"{ind_key} {value:+.1f}bp (閾値 {threshold}bp)"
        elif condition_type == "implies_cut":
            # 量的データがない場合は inconclusive
            entry["detail"] = cond.get("description", INCONCLUSIVE_DETAIL)
        elif condition_type == "above_ma200":
            matched = value > 0
            entry["detail"] = (
                f"{ind_key} MA200乖離 {value:+.1f}% "
                f"({'above' if matched else 'below'} MA200)"
            )
        elif condition_type == "above_avg_pct":
            matched = value >= threshold
            entry["detail"] = f"{ind_key} 出来高平均比 {value:+.1f}% (閾値 +{threshold}%)"
        else:
            entry["detail"] = cond.get("description", f"未対応条件: {condition_type}")

        entry["matched"] = matched
        results.append(entry)

    return results


# ═══════════════════════════════════════════════════════════
#  テクニカルシグナル評価
# ═══════════════════════════════════════════════════════════

def _parse_tech_key(key: str) -> tuple[str, str, str | None]:
    """
    テクニカルキーをパースする。
    例: "SPY_rsi_14" → ("SPY", "rsi", "14")
        "SOXX_macd"  → ("SOXX", "macd", None)
        "market_breadth_below" → ("market_breadth", "below", None)  # 特殊
    """
    parts = key.split("_", 1)
    if len(parts) < 2:
        return (key, "", None)
    ticker = parts[0]
    rest = parts[1]
    # rsi_14 パターン
    m = re.match(r"(rsi|macd)(?:_(\d+))?$", rest)
    if m:
        return (ticker, m.group(1), m.group(2))
    return (ticker, rest, None)


def _regime_bull_confirmed(regime_state: dict) -> bool | None:
    if not isinstance(regime_state, dict) or not regime_state:
        return None
    explicit = regime_state.get("regime_bull_confirmed")
    if isinstance(explicit, bool):
        return explicit
    regime_name = str(regime_state.get("regime") or "").lower()
    macro_score = _to_float(regime_state.get("macro_score"))
    spy_above = regime_state.get("spy_above")
    nk_above = regime_state.get("nk_above")
    if macro_score is None and spy_above is None and nk_above is None and not regime_name:
        return None
    signals = [
        ("強気" in str(regime_state.get("regime") or "")) or ("bull" in regime_name),
        (macro_score or 0) >= 6,
        bool(spy_above),
        bool(nk_above),
    ]
    return sum(1 for item in signals if item) >= 3


def _first_present(d: dict, *keys):
    """最初に「キーが存在し None でない」値を返す。0.0 を欠損扱いしない
    (Codex re-review #6: `a or b` だと 0.0/0% が False 扱いされ次へ落ちる)。"""
    for k in keys:
        if k in d and d.get(k) is not None:
            return d.get(k)
    return None


def _first_non_none(*values):
    """複数ソース横断で最初の非 None 値を返す。0.0 を欠損扱いしない。"""
    for v in values:
        if v is not None:
            return v
    return None


def _eval_special_technical(key: str, cond: dict, market_state: dict | None,
                            regime_state: dict | None,
                            tech_state: dict | None = None) -> dict | None:
    condition_type = cond.get("condition", "")
    if key == "SPY_above_MA50":
        spy = (market_state or {}).get("SPY", {}) or {}
        diff = _to_float(_first_present(spy, "ma50_diff", "vs_ma50_pct"))
        price = _to_float(spy.get("price"))
        ma50 = _to_float(spy.get("ma50"))
        if diff is None and price is not None and ma50:
            diff = (price / ma50 - 1.0) * 100.0
        entry = {"type": "technical", "key": key, "matched": False, "detail": ""}
        if diff is None:
            entry["detail"] = INCONCLUSIVE_DETAIL
        else:
            matched = diff > 0 if condition_type == "true" else diff <= 0
            entry["matched"] = matched
            entry["detail"] = f"SPY ma50_diff {diff:+.2f}% ({'above' if diff > 0 else 'below'} MA50)"
        return entry

    if key == "regime_bull_confirmed":
        confirmed = _regime_bull_confirmed(regime_state or {})
        entry = {"type": "technical", "key": key, "matched": False, "detail": ""}
        if confirmed is None:
            entry["detail"] = INCONCLUSIVE_DETAIL
        else:
            entry["matched"] = bool(confirmed) if condition_type == "true" else not bool(confirmed)
            regime_name = (regime_state or {}).get("regime", "?")
            entry["detail"] = f"regime_bull_confirmed={bool(confirmed)} (regime={regime_name})"
        return entry

    # EWJ が SPY を 20 日で上回っているかを boolean 評価。
    # 実データ: technical_state.json["tickers"]["EWJ"]["change_20d_pct"]
    # (market_snapshot.json には EWJ が無いので tech_state を一次ソースにする)
    if key == "ewj_outperforms_spy_20d":
        tickers = (tech_state or {}).get("tickers", {}) if isinstance(tech_state, dict) else {}
        m = market_state or {}
        ewj = tickers.get("EWJ", {}) or m.get("EWJ", {}) or {}
        spy = tickers.get("SPY", {}) or m.get("SPY", {}) or {}
        ewj_ret = _to_float(_first_present(ewj, "change_20d_pct", "return_20d"))
        spy_ret = _to_float(_first_present(spy, "change_20d_pct", "return_20d"))
        entry = {"type": "technical", "key": key, "matched": False, "detail": ""}
        if ewj_ret is None or spy_ret is None:
            entry["detail"] = INCONCLUSIVE_DETAIL
        else:
            outperf = ewj_ret - spy_ret
            entry["matched"] = outperf > 0
            entry["detail"] = f"EWJ 20d={ewj_ret:+.2f}% SPY 20d={spy_ret:+.2f}% diff={outperf:+.2f}%"
        return entry

    # 日経225 または TOPIX が MA50 を上回っているか。
    # 実データ: market_snapshot.json["NK225"]["ma50_diff"]
    if key == "nikkei_or_topix_above_ma50":
        m = market_state or {}
        tickers = (tech_state or {}).get("tickers", {}) if isinstance(tech_state, dict) else {}
        # market_snapshot の実キーは "NK225"。他候補も後方互換で残す。
        nky_keys = ("NK225", "^N225", "NI225", "NKY", "N225")
        tpx_keys = ("TOPIX", "^TPX", "1306.T")
        entry = {"type": "technical", "key": key, "matched": False, "detail": ""}
        found_any = False

        def _index_above_ma50(keys: tuple) -> tuple[bool | None, str]:
            for k_ in keys:
                obj = m.get(k_) or tickers.get(k_) or {}
                if not obj:
                    continue
                diff = _to_float(_first_present(obj, "ma50_diff", "vs_ma50_pct"))
                price = _to_float(_first_present(obj, "price", "close"))
                ma50 = _to_float(obj.get("ma50"))
                if diff is None and price is not None and ma50:
                    diff = (price / ma50 - 1.0) * 100.0
                if diff is not None:
                    return diff > 0, f"{k_} ma50_diff={diff:+.2f}%"
            return None, ""

        nky_above, nky_detail = _index_above_ma50(nky_keys)
        tpx_above, tpx_detail = _index_above_ma50(tpx_keys)

        if nky_above is not None:
            found_any = True
        if tpx_above is not None:
            found_any = True

        if not found_any:
            entry["detail"] = INCONCLUSIVE_DETAIL
        else:
            above = bool(nky_above) or bool(tpx_above)
            entry["matched"] = above
            parts = []
            if nky_detail:
                parts.append(f"日経{nky_detail}")
            if tpx_detail:
                parts.append(f"TOPIX {tpx_detail}")
            entry["detail"] = " / ".join(parts) or "no data"
        return entry

    return None


def _eval_technical(scenario: dict, tech_state: dict,
                    market_state: dict | None = None,
                    regime_state: dict | None = None) -> list[dict]:
    """detect.technical の各条件を評価。"""
    tech_conds = scenario.get("detect", {}).get("technical", {})
    results = []

    tech_state = tech_state or {}
    tickers_data = tech_state.get("tickers", {}) if isinstance(tech_state, dict) else {}
    breadth_data = tech_state.get("market_breadth", {}) if isinstance(tech_state, dict) else {}

    for key, cond in tech_conds.items():
        condition_type = cond.get("condition", "")
        entry = {"type": "technical", "key": key, "matched": False, "detail": ""}

        special = _eval_special_technical(key, cond, market_state, regime_state, tech_state)
        if special is not None:
            results.append(special)
            continue

        ticker, indicator, param = _parse_tech_key(key)

        # market_breadth 特殊処理
        if ticker.lower() == "market" and "breadth" in indicator:
            val = _to_float(breadth_data.get("pct_above_ma50"))
            threshold = cond.get("threshold")
            if val is None:
                entry["detail"] = INCONCLUSIVE_DETAIL
            elif condition_type == "below" and threshold is not None:
                entry["matched"] = val < threshold
                entry["detail"] = f"market_breadth {val:.2f} {'<' if entry['matched'] else '>='} {threshold}"
            results.append(entry)
            continue

        # ティッカー別データ
        t_data = tickers_data.get(ticker, {})
        if not t_data:
            entry["detail"] = f"{ticker} データなし"
            results.append(entry)
            continue
        if t_data.get("data_quality_status") == "blocked":
            entry["detail"] = INCONCLUSIVE_DETAIL
            entry["data_quality_status"] = "blocked"
            results.append(entry)
            continue

        if indicator == "rsi":
            val = _to_float(_first_present(t_data, "rsi", "rsi_14"))
            threshold = cond.get("threshold")
            if val is None:
                entry["detail"] = INCONCLUSIVE_DETAIL
            elif condition_type == "above" and threshold is not None:
                entry["matched"] = val > threshold
                entry["detail"] = f"{ticker} RSI {val:.1f} {'>' if entry['matched'] else '<='} {threshold}"
            elif condition_type == "below" and threshold is not None:
                entry["matched"] = val < threshold
                entry["detail"] = f"{ticker} RSI {val:.1f} {'<' if entry['matched'] else '>='} {threshold}"
            elif condition_type == "range":
                lo, hi = cond.get("min", 0), cond.get("max", 100)
                entry["matched"] = lo <= val <= hi
                entry["detail"] = f"{ticker} RSI {val:.1f} ({'in' if entry['matched'] else 'out'} {lo}-{hi})"
            else:
                entry["detail"] = INCONCLUSIVE_DETAIL

        elif indicator == "macd":
            crossover = t_data.get("macd_crossover", "")
            if condition_type == "bullish_cross":
                entry["matched"] = crossover == "bullish"
                entry["detail"] = f"{ticker} MACD crossover={crossover}"
            elif condition_type == "bearish_cross":
                entry["matched"] = crossover == "bearish"
                entry["detail"] = f"{ticker} MACD crossover={crossover}"
            else:
                entry["detail"] = cond.get("description", INCONCLUSIVE_DETAIL)
        else:
            entry["detail"] = cond.get("description", INCONCLUSIVE_DETAIL)

        results.append(entry)

    return results


# ═══════════════════════════════════════════════════════════
#  シナリオ評価メインロジック
# ═══════════════════════════════════════════════════════════

def _determine_status(readiness: float) -> str:
    if readiness < 0.3:
        return "dormant"
    if readiness < 0.6:
        return "watching"
    return "active"


def _required_signal_keys(scenario: dict) -> list[str]:
    detect = scenario.get("detect", {}) or {}
    explicit = detect.get("required_signals") or detect.get("required_signal_keys")
    if isinstance(explicit, list):
        return [str(item) for item in explicit if item]
    # bull_pullback must not fire without an actual pullback, even if VIX/regime
    # and breadth signals are otherwise bullish.
    if scenario.get("id") == "bull_pullback":
        return ["vix", "spy_dist_from_ma50_pct", "regime_bull_confirmed"]
    return []


def _missing_required_signals(scenario: dict, signal_details: list[dict]) -> list[str]:
    by_key = {str(row.get("key")): bool(row.get("matched")) for row in signal_details if isinstance(row, dict)}
    missing = []
    for key in _required_signal_keys(scenario):
        if not by_key.get(key, False):
            missing.append(key)
    return missing


def _state_age_minutes(state: object, *, now: datetime) -> float | None:
    if not isinstance(state, dict):
        return None
    raw = None
    for key in ("cached_at", "updated_at", "evaluated_at", "as_of"):
        if state.get(key) not in (None, ""):
            raw = state.get(key)
            break
    if raw is None:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        comparison_now = now
        if stamp.tzinfo is not None and comparison_now.tzinfo is None:
            comparison_now = comparison_now.astimezone()
        elif stamp.tzinfo is None and comparison_now.tzinfo is not None:
            stamp = stamp.replace(tzinfo=comparison_now.tzinfo)
        return max(0.0, (comparison_now - stamp).total_seconds() / 60.0)
    except Exception:
        return None


def _activation_policy_failures(
    scenario: dict,
    signal_details: list[dict],
    *,
    states: dict,
    now: datetime,
) -> list[dict]:
    """Evaluate an opt-in, playbook-defined activation contract.

    The mechanism is generic, but no scenario is affected unless its
    ``detect.activation_policy`` explicitly opts in.  This lets event-specific
    confirmation rules fail closed without silently tightening unrelated
    scenarios.
    """
    detect = scenario.get("detect") or {}
    policy = detect.get("activation_policy") or {}
    if not isinstance(policy, dict) or not policy:
        return []

    by_key = {
        str(row.get("key")): row
        for row in signal_details
        if isinstance(row, dict) and row.get("key")
    }
    failures: list[dict] = []

    freshness = policy.get("freshness_requirements") or {}
    if isinstance(freshness, dict):
        for signal_key, requirement in freshness.items():
            if not isinstance(requirement, dict):
                continue
            state_key = str(requirement.get("state") or "")
            try:
                max_age = float(requirement.get("max_age_minutes"))
            except (TypeError, ValueError):
                max_age = -1
            age = _state_age_minutes(states.get(state_key), now=now) if state_key else None
            if max_age < 0 or age is None or age > max_age:
                row = by_key.get(str(signal_key))
                if isinstance(row, dict):
                    row["matched"] = False
                    row["detail"] = INCONCLUSIVE_DETAIL
                    row["freshness_status"] = "unknown" if age is None else "stale"
                    row["source_age_minutes"] = round(age, 1) if age is not None else None
                failures.append({
                    "code": "activation_source_stale",
                    "signal": str(signal_key),
                    "state": state_key,
                    "age_minutes": round(age, 1) if age is not None else None,
                    "max_age_minutes": max_age,
                })

    disallowed_fallbacks = policy.get("ai_fallback_cannot_satisfy_required") or []
    for signal_key in disallowed_fallbacks if isinstance(disallowed_fallbacks, list) else []:
        row = by_key.get(str(signal_key))
        if isinstance(row, dict) and row.get("matched") and row.get("source") == "ai_fallback":
            failures.append({
                "code": "activation_required_signal_ai_fallback",
                "signal": str(signal_key),
            })

    required_any = policy.get("required_any") or []
    for index, group in enumerate(required_any if isinstance(required_any, list) else []):
        keys = group if isinstance(group, list) else [group]
        keys = [str(key) for key in keys if key]
        if keys and not any(bool((by_key.get(key) or {}).get("matched")) for key in keys):
            failures.append({
                "code": "activation_required_any_missing",
                "group": index,
                "signals": keys,
            })

    vetoes = policy.get("contradiction_veto") or []
    for veto in vetoes if isinstance(vetoes, list) else []:
        if not isinstance(veto, dict):
            continue
        key = str(veto.get("key") or "")
        row = by_key.get(key) or {}
        value = _to_float(row.get("value"))
        threshold = _to_float(veto.get("threshold"))
        condition = str(veto.get("condition") or "")
        if value is None or threshold is None:
            continue
        contradicted = (
            (condition == "above" and value > threshold)
            or (condition == "at_or_above" and value >= threshold)
            or (condition == "below" and value < threshold)
            or (condition == "at_or_below" and value <= threshold)
        )
        if contradicted:
            failures.append({
                "code": "activation_contradiction_veto",
                "signal": key,
                "value": value,
                "condition": condition,
                "threshold": threshold,
            })

    return failures


def _overall_alert_level(scenarios: dict) -> str:
    active = sum(1 for s in scenarios.values() if s["status"] == "active")
    # partial は限定発動 — アラートレベル上は watching 相当として扱う
    watching = sum(1 for s in scenarios.values() if s["status"] in ("watching", "partial"))
    if active >= 2:
        return "critical"
    if active == 1:
        return "high"
    if watching > 0:
        return "elevated"
    return "calm"


def _build_telegram_message(scenario_def: dict, result: dict,
                            guard_state: dict) -> str:
    """ACTIVE 遷移時の Telegram メッセージを構築。"""
    icon = scenario_def.get("icon", "")
    name = scenario_def.get("name", scenario_def["id"])
    readiness_pct = round(result["readiness"] * 100)
    signals_met = result["signals_met"]
    signals_total = result["signals_total"]

    # マッチしたシグナル詳細
    matched_lines = []
    for sd in result["signal_details"]:
        if sd["matched"]:
            matched_lines.append(f"  - [{sd['type']}] {sd['detail']}")
    signal_text = "\n".join(matched_lines) if matched_lines else "  (詳細なし)"

    # Phase 1 アクション
    actions = scenario_def.get("actions", {})
    phase1 = actions.get("phase_1", {})
    phase1_lines = []
    for buy in phase1.get("buy", []):
        phase1_lines.append(f"  BUY {buy['ticker']}: {buy.get('reason', '')}")
    for sell in phase1.get("sell", []):
        phase1_lines.append(f"  SELL {sell['ticker']}: {sell.get('reason', '')}")
    phase1_text = "\n".join(phase1_lines) if phase1_lines else "  (なし)"

    # ガードレール状態
    trading_allowed = guard_state.get("trading_allowed", True)
    new_entry = guard_state.get("new_entry_allowed", True)
    if not trading_allowed:
        guard_text = "トレード全停止中"
    elif not new_entry:
        guard_text = "新規エントリー禁止（売りのみ可）"
    else:
        guard_text = "通常"

    return (
        f"\U0001f3af <b>ALMANAC シナリオ発火</b>\n\n"
        f"{icon} <b>{name}</b> → ACTIVE\n"
        f"準備度: {readiness_pct}% ({signals_met}/{signals_total})\n\n"
        f"検知シグナル:\n{signal_text}\n\n"
        f"推奨アクション:\n"
        f"Phase 1: {phase1.get('label', '')}\n{phase1_text}\n\n"
        f"\u26a0\ufe0f ガードレール: {guard_text}"
    )


def evaluate_scenarios() -> dict:
    """
    全シナリオを評価し scenario_state.json を書き出す。

    Returns:
        scenario_state dict
    """
    playbook = load_json(PLAYBOOK_PATH, {})
    scenarios_def = playbook.get("scenarios", [])
    if not scenarios_def:
        logger.warning("scenario_playbook.json にシナリオが定義されていません")
        return {}

    states = _load_states()
    prev_state = load_json(SCENARIO_STATE_PATH, {})
    prev_scenarios = prev_state.get("scenarios", {})

    evaluation_now = datetime.now()
    now_iso = evaluation_now.isoformat(timespec="seconds")
    evaluated = {}

    for sc in scenarios_def:
        sc_id = sc["id"]
        signal_details: list[dict] = []

        # 1) News signals
        news_result = _eval_news(sc, states["geo"])
        signal_details.append(news_result)

        # 2) Indicator signals
        ind_results = _eval_indicators(
            sc, states["vix"], states["macro"], states.get("tech"), states.get("market")
        )
        signal_details.extend(ind_results)

        # 3) Technical signals
        tech_results = _eval_technical(
            sc, states["tech"], states.get("market"), states.get("regime")
        )
        signal_details.extend(tech_results)

        activation_policy = (sc.get("detect") or {}).get("activation_policy")
        activation_policy_configured = bool(
            isinstance(activation_policy, dict) and activation_policy
        )
        activation_policy_failures = _activation_policy_failures(
            sc,
            signal_details,
            states=states,
            now=evaluation_now,
        )

        # 4) Readiness calculation（重み付き — indicator 40% / news 30% / technical 30%）
        signals_total = len(signal_details)
        signals_met = sum(1 for s in signal_details if s["matched"])
        readiness = _weighted_readiness(signal_details)
        missing_required = _missing_required_signals(sc, signal_details)
        if missing_required and readiness >= 0.6:
            readiness = 0.59

        # 4b) min_signals 強制: 確定的にマッチしたシグナル数が足りなければ dormant に落とす。
        #     ただし required_signals が全成立していれば partial (限定サイズ発動) に留める。
        #     2026-06 イラン停戦: news+VIX (required) は成立したが 5日窓指標が期限切れで
        #     min_signals 3 に届かず dormant 落ち → ラリー不捕捉。イベント系シナリオの
        #     本質シグナル成立時は allocation_scale=0.5 で decision へ渡す。
        detect = sc.get("detect") or {}
        min_signals_req = detect.get("min_signals")
        min_signals_fail = False
        required_keys = _required_signal_keys(sc)
        if isinstance(min_signals_req, int) and min_signals_req > 0:
            # inconclusive は分子から除く (既に _weighted_readiness と同じ扱い)
            conclusive_met = sum(
                1 for s in signal_details
                if s.get("matched") and s.get("detail") != INCONCLUSIVE_DETAIL
            )
            if conclusive_met < min_signals_req:
                min_signals_fail = True

        partial_eligible = bool(required_keys) and not missing_required and min_signals_fail
        if activation_policy_failures:
            partial_eligible = False
            readiness = min(readiness, 0.59)
        if min_signals_fail:
            if partial_eligible:
                readiness = min(readiness, 0.59)  # watching 相当まで (dormant には落とさない)
            else:
                readiness = min(readiness, 0.29)  # dormant 上限

        # 5) Status — partial は active 未満・required 全成立の限定発動
        status = _determine_status(readiness)
        if partial_eligible and status != "active":
            status = "partial"
        allocation_scale = 1.0 if status == "active" else (0.5 if status == "partial" else 0.0)

        # 6) State tracking — first_detected
        prev = prev_scenarios.get(sc_id, {})
        prev_status = prev.get("status", "dormant")
        first_detected = prev.get("first_detected")
        if status != "dormant" and first_detected is None:
            first_detected = now_iso

        # 7) enabled_for_decision / observe_only を playbook から伝播
        enabled_for_decision = sc.get("enabled_for_decision", True)
        observe_only = sc.get("observe_only", False)
        # observe_only=true はシグナル上 active でも decision pipeline に入れない
        if observe_only:
            enabled_for_decision = False

        result = {
            "name": sc.get("name", sc_id),
            "status": status,
            "readiness": round(readiness, 4),
            "signals_met": signals_met,
            "signals_total": signals_total,
            "signal_details": signal_details,
            "required_signals": _required_signal_keys(sc),
            "missing_required_signals": missing_required,
            "min_signals_required": min_signals_req,
            "min_signals_fail": min_signals_fail,
            "activation_policy_status": (
                "not_configured"
                if not activation_policy_configured
                else ("failed" if activation_policy_failures else "passed")
            ),
            "activation_policy_passed": (
                None
                if not activation_policy_configured
                else not activation_policy_failures
            ),
            "activation_policy_failures": activation_policy_failures,
            "allocation_scale": allocation_scale,
            "enabled_for_decision": enabled_for_decision,
            "observe_only": observe_only,
            "recommended_actions": _build_recommended_actions(sc),
            "first_detected": first_detected,
            "last_evaluated": now_iso,
        }
        evaluated[sc_id] = result

        # 7) Telegram alert on transition TO active / partial
        if status == "active" and prev_status in ("dormant", "watching", "partial"):
            msg = _build_telegram_message(sc, result, states["guard"])
            try:
                # ALMANAC: telegram disabled — ai_analysis only
                # send_telegram(msg)
                logger.info("Telegram alert skipped (disabled) for scenario %s", sc_id)
            except Exception as e:
                logger.error("Telegram send failed for %s: %s", sc_id, e)
        elif status == "partial" and prev_status in ("dormant", "watching"):
            _pmsg = (
                f"⚡ <b>ALMANAC シナリオ限定発動 (PARTIAL)</b>\n\n"
                f"{sc.get('icon', '')} <b>{sc.get('name', sc_id)}</b> → PARTIAL\n"
                f"必須シグナル全成立 / min_signals 未達 "
                f"({result['signals_met']}/{result['signals_total']})\n"
                f"プレイブック配分の 50% で発注候補化します。"
            )
            try:
                # ALMANAC: telegram disabled — ai_analysis only
                # send_telegram(_pmsg)
                logger.info("Telegram partial alert skipped (disabled) for scenario %s", sc_id)
            except Exception as e:
                logger.error("Telegram send failed for %s: %s", sc_id, e)

    # ── 集計 ────────────────────────────────────────────
    active_count = sum(1 for s in evaluated.values() if s["status"] == "active")
    partial_count = sum(1 for s in evaluated.values() if s["status"] == "partial")
    watching_count = sum(1 for s in evaluated.values() if s["status"] == "watching")

    output = {
        "scenarios": evaluated,
        "active_count": active_count,
        "partial_count": partial_count,
        "watching_count": watching_count,
        "overall_alert_level": _overall_alert_level(evaluated),
        "evaluated_at": now_iso,
    }

    atomic_write_json(SCENARIO_STATE_PATH, output)
    logger.info("scenario_state.json updated — active=%d watching=%d level=%s",
                active_count, watching_count, output["overall_alert_level"])
    return output


def get_scenario_state() -> dict:
    """キャッシュ済み scenario_state.json を返す（再評価なし）。"""
    return load_json(SCENARIO_STATE_PATH, {})


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

_STATUS_ICON = {"dormant": "  ", "watching": "👀", "partial": "⚡", "active": "🔥"}


def _print_summary(state: dict) -> None:
    """シナリオ一覧をテーブル形式で表示。"""
    scenarios = state.get("scenarios", {})
    if not scenarios:
        print("シナリオなし")
        return

    header = f"{'ID':<16} {'Status':<10} {'Readiness':>9}  {'Met/Total':>9}  First Detected"
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for sc_id, sc in scenarios.items():
        icon = _STATUS_ICON.get(sc["status"], "  ")
        pct = f"{sc['readiness'] * 100:.0f}%"
        ratio = f"{sc['signals_met']}/{sc['signals_total']}"
        first = sc.get("first_detected") or "-"
        print(f"{icon} {sc_id:<14} {sc['status']:<10} {pct:>8}  {ratio:>9}  {first}")

    print("=" * len(header))
    print(f"Alert level: {state.get('overall_alert_level', 'unknown')}  "
          f"| Active: {state.get('active_count', 0)}  "
          f"| Watching: {state.get('watching_count', 0)}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="ALMANAC Scenario Engine")
    parser.add_argument("--force", action="store_true",
                        help="強制的に全シナリオを再評価")
    args = parser.parse_args()

    if args.force:
        state = evaluate_scenarios()
    else:
        # デフォルトでも常に再評価（データが更新されている前提）
        state = evaluate_scenarios()

    _print_summary(state)


if __name__ == "__main__":
    main()
