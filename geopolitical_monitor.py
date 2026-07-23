"""
geopolitical_monitor.py — ALMANAC 地政学モニター
Claude Web Search API で市場に直結する地政学ニュースだけを取得し、
scenario_playbook.json のキーワードとマッチング。
Telegram は高重要度・高確度のケースだけ送信。

crontab (1日3回):
  0 7  * * 1-5  cd ~/portfolio-bot && venv/bin/python geopolitical_monitor.py
  0 12 * * 1-5  cd ~/portfolio-bot && venv/bin/python geopolitical_monitor.py
  0 18 * * 1-5  cd ~/portfolio-bot && venv/bin/python geopolitical_monitor.py
"""

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from analyst.llm_client import _append_llm_call_log
from utils import load_json, atomic_write_json

try:
    from alert import send_telegram
except ImportError:
    def send_telegram(msg):
        print(f"[TELEGRAM] {msg}")

logging.basicConfig(
    level=logging.INFO,
    format="[geopolitical_monitor] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "geopolitical_state.json"
PLAYBOOK_FILE = BASE_DIR / "scenario_playbook.json"

CACHE_TTL_HOURS = 2  # fallback (tunable_params: geopolitical_cache_hours で上書き可能)
DEDUP_HOURS = 48
MIN_ASSESSMENT_SCORE = 3
TELEGRAM_MIN_SEVERITY = "high"
TELEGRAM_MIN_CONFIDENCE = 0.75
STATE_MIN_SEVERITY = "high"
STATE_MIN_CONFIDENCE = 0.65
MODEL_ID = "claude-haiku-4-5-20251001"


def _get_cache_ttl_hours() -> int:
    """tunable_params: geopolitical_cache_hours を優先、なければ CACHE_TTL_HOURS。"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get("geopolitical_cache_hours")
        return int(v) if v is not None else CACHE_TTL_HOURS
    except Exception:
        return CACHE_TTL_HOURS

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITY_ICONS = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🟠",
    "critical": "🔴",
}


def _severity_at_least(severity: str, threshold: str) -> bool:
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(threshold, 0)


def _confidence(assessment: dict) -> float:
    try:
        return float(assessment.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_material_alert(assessment: dict, *, state: bool = False) -> bool:
    """通知/state に残す価値がある高シグナル地政学イベントか。"""
    severity = assessment.get("severity", "low")
    min_sev = STATE_MIN_SEVERITY if state else TELEGRAM_MIN_SEVERITY
    min_conf = STATE_MIN_CONFIDENCE if state else TELEGRAM_MIN_CONFIDENCE
    return _severity_at_least(severity, min_sev) and _confidence(assessment) >= min_conf


def _server_tool_use_row(usage) -> dict:
    server_tool_use = getattr(usage, "server_tool_use", None)
    if server_tool_use is None:
        return {}
    row = {}
    for key in ("web_search_requests",):
        value = getattr(server_tool_use, key, None)
        if value is not None:
            row[key] = value
    return row

# ─────────────────────────────────────────────────────────
# Web Search クエリ
# ─────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    # 市場に直接効くものだけ。Fed/BOJ/決算/景気後退は別モジュールに任せる。
    "market moving geopolitical escalation sanctions conflict oil supply disruption today 2026",
    "China Taiwan military blockade sanctions export controls market risk latest 2026",
    "Middle East conflict oil shipping chokepoint escalation market impact latest 2026",
    "Iran Israel ceasefire truce de-escalation Strait of Hormuz market impact latest 2026",
    "US China tariff export control sanctions escalation semiconductor market impact 2026",
    "Russia Ukraine NATO ceasefire escalation sanctions energy market impact latest 2026",
]

# ─────────────────────────────────────────────────────────
# AI Assessment Tool Schema
# ─────────────────────────────────────────────────────────

_ASSESSMENT_TOOL = {
    "name": "assess_geopolitical",
    "description": "Assess geopolitical news relevance to investment scenario",
    "input_schema": {
        "type": "object",
        "properties": {
            "scenario_key": {"type": "string"},
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
            },
            "headline": {
                "type": "string",
                "description": "1-line summary in Japanese",
            },
            "detail": {
                "type": "string",
                "description": "2-3 sentence analysis in Japanese",
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0",
            },
        },
        "required": ["scenario_key", "severity", "headline", "detail", "confidence"],
    },
}

# ─────────────────────────────────────────────────────────
# Web Search
# ─────────────────────────────────────────────────────────


def _web_search(query: str) -> list[dict]:
    """Claude Web Search で1クエリ分のニュースを取得。
    Returns list of {"headline": ..., "snippet": ...}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY が未設定")
        return []

    try:
        client = anthropic.Anthropic(api_key=api_key)
        started = time.monotonic()
        response = client.beta.messages.create(
            model=MODEL_ID,
            max_tokens=1024,
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 5,
                "allowed_callers": ["direct"],
            }],
            messages=[{"role": "user", "content": query}],
        )
        usage = getattr(response, "usage", None)
        server_tool_use = _server_tool_use_row(usage)
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "geopolitical_web_search",
            "model": MODEL_ID,
            "use_tool": True,
            "max_tokens": 1024,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "prompt_chars": len(query),
            "status": "ok",
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            **({"server_tool_use": server_tool_use} if server_tool_use else {}),
        })

        items = []
        for block in response.content:
            if hasattr(block, "text") and block.text.strip():
                text = block.text.strip()
                # 最初の行をヘッドライン、残りをスニペットとして扱う
                lines = text.split("\n", 1)
                headline = lines[0].strip()
                snippet = lines[1].strip() if len(lines) > 1 else ""
                items.append({"headline": headline, "snippet": snippet})
        return items

    except Exception as e:
        try:
            _append_llm_call_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "role": "geopolitical_web_search",
                "model": MODEL_ID,
                "use_tool": True,
                "max_tokens": 1024,
                "elapsed_sec": round(time.monotonic() - started, 2) if "started" in locals() else None,
                "prompt_chars": len(query),
                "status": "error",
                "error_type": type(e).__name__,
                "error": str(e)[:500],
            })
        except Exception:
            pass
        log.error(f"Web Search エラー ({query[:40]}...): {e}")
        return []


def _fetch_all_news() -> list[dict]:
    """8つのクエリを並列実行してニュースを集約"""
    all_items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_web_search, q): q for q in SEARCH_QUERIES}
        for future in as_completed(futures):
            query = futures[future]
            try:
                items = future.result()
                log.info(f"取得: {len(items)}件 ← {query[:50]}")
                all_items.extend(items)
            except Exception as e:
                log.error(f"クエリ失敗 ({query[:40]}): {e}")
    return all_items


# ─────────────────────────────────────────────────────────
# キーワードマッチング
# ─────────────────────────────────────────────────────────


def _match_keywords(news_items: list[dict], playbook: dict) -> list[dict]:
    """各シナリオについてニュースとキーワードをマッチング。
    Returns list of {"scenario": ..., "matched_keywords": [...], "score": int, "relevant_news": [...]}
    """
    raw_scenarios = playbook.get("scenarios", [])
    if isinstance(raw_scenarios, dict):
        scenarios = []
        for scenario_id, scenario in raw_scenarios.items():
            if not isinstance(scenario, dict):
                continue
            scenarios.append({"id": scenario_id, **scenario})
    elif isinstance(raw_scenarios, list):
        scenarios = [s for s in raw_scenarios if isinstance(s, dict)]
    else:
        scenarios = []
    results = []

    # 全ニューステキストを結合（検索用に小文字化）
    news_texts = []
    for item in news_items:
        combined = f"{item.get('headline', '')} {item.get('snippet', '')}".lower()
        news_texts.append(combined)
    full_text = " ".join(news_texts)

    for scenario in scenarios:
        scenario_id = scenario.get("id", "")
        detect = scenario.get("detect", {})
        keywords = detect.get("news_keywords", [])

        matched = []
        relevant_news = []
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in full_text:
                matched.append(kw)
                # どのニュースがマッチしたか特定
                for i, nt in enumerate(news_texts):
                    if kw_lower in nt and i < len(news_items):
                        if news_items[i] not in relevant_news:
                            relevant_news.append(news_items[i])

        if matched:
            results.append({
                "scenario": scenario,
                "matched_keywords": list(set(matched)),
                "score": len(set(matched)),
                "relevant_news": relevant_news,
            })

    # スコア降順
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def _scenario_min_keyword_score(scenario: dict) -> int:
    """Return scenario-specific keyword score needed before AI assessment."""
    detect = scenario.get("detect", {}) if isinstance(scenario, dict) else {}
    raw = detect.get("min_keyword_score", MIN_ASSESSMENT_SCORE)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MIN_ASSESSMENT_SCORE


def _compact_news_items(items: list[dict], limit: int = 5) -> list[dict]:
    """state に残す raw news を小さく整形する。"""
    compacted = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append({
            "headline": item.get("headline", ""),
            "snippet": item.get("snippet", ""),
            "url": item.get("url"),
            "source": item.get("source"),
            "published_at": item.get("published_at"),
        })
    return compacted


# ─────────────────────────────────────────────────────────
# AI 評価
# ─────────────────────────────────────────────────────────


def _assess_scenario(scenario: dict, matched_keywords: list[str],
                     relevant_news: list[dict]) -> dict | None:
    """Claude Haiku で地政学ニュースのシナリオ適合度を評価"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    news_summary = "\n".join(
        f"- {item['headline']}: {item['snippet'][:200]}"
        for item in relevant_news[:5]
    )

    prompt = (
        f"以下のニュースが投資シナリオ「{scenario['name']}」({scenario['id']}) に"
        f"どの程度関連するか評価してください。\n\n"
        f"シナリオ説明: {scenario.get('description', '')}\n\n"
        f"マッチしたキーワード: {', '.join(matched_keywords)}\n\n"
        f"関連ニュース:\n{news_summary}\n\n"
        "評価基準:\n"
        "- high/critical は、主要指数・原油・半導体規制・為替・既存ポジションに"
        "数日以内の直接影響が見込める場合だけ。\n"
        "- 背景説明、通常の外交発言、既知イベントの続報、相場影響が曖昧なものは low/medium。\n"
        "- ユーザーに今すぐ通知すべきでないニュースは severity を low に寄せる。\n\n"
        f"assess_geopolitical ツールを使って評価結果を返してください。"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        started = time.monotonic()
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=1024,
            tools=[_ASSESSMENT_TOOL],
            tool_choice={"type": "tool", "name": "assess_geopolitical"},
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(response, "usage", None)
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "geopolitical_assessment",
            "model": MODEL_ID,
            "use_tool": True,
            "max_tokens": 1024,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "prompt_chars": len(prompt),
            "scenario_key": scenario.get("id"),
            "matched_keyword_count": len(matched_keywords),
            "relevant_news_count": len(relevant_news),
            "status": "ok",
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })

        for block in response.content:
            if block.type == "tool_use" and block.name == "assess_geopolitical":
                return block.input
        return None

    except Exception as e:
        try:
            _append_llm_call_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "role": "geopolitical_assessment",
                "model": MODEL_ID,
                "use_tool": True,
                "max_tokens": 1024,
                "elapsed_sec": round(time.monotonic() - started, 2) if "started" in locals() else None,
                "prompt_chars": len(prompt),
                "scenario_key": scenario.get("id"),
                "matched_keyword_count": len(matched_keywords),
                "relevant_news_count": len(relevant_news),
                "status": "error",
                "error_type": type(e).__name__,
                "error": str(e)[:500],
            })
        except Exception:
            pass
        log.error(f"AI評価エラー ({scenario['id']}): {e}")
        return None


# ─────────────────────────────────────────────────────────
# 重複排除
# ─────────────────────────────────────────────────────────


def _is_recently_alerted(state: dict, scenario_key: str) -> bool:
    """同じシナリオに DEDUP_HOURS 以内にアラート済みか"""
    cutoff = datetime.now() - timedelta(hours=DEDUP_HOURS)
    for alert in state.get("active_alerts", []):
        if alert.get("scenario_key") == scenario_key and alert.get("alerted"):
            try:
                detected = datetime.fromisoformat(alert["detected_at"])
                if detected > cutoff:
                    return True
            except (KeyError, ValueError):
                pass
    return False


# ─────────────────────────────────────────────────────────
# Telegram アラート
# ─────────────────────────────────────────────────────────


def _send_alert(scenario: dict, assessment: dict):
    """Telegram にアラートを送信"""
    severity = assessment.get("severity", "medium")
    icon = SEVERITY_ICONS.get(severity, "🟡")
    scenario_key = assessment.get("scenario_key", scenario.get("id", ""))
    scenario_name = scenario.get("name", scenario_key)

    msg = (
        f"🌍 <b>ALMANAC 地政学アラート</b>\n"
        f"シナリオ: {scenario_name} ({scenario_key})\n"
        f"重要度: {icon} {severity}\n"
        f"{assessment.get('headline', '')}\n"
        f"{assessment.get('detail', '')}"
    )

    try:
        # ALMANAC: telegram disabled — ai_analysis only
        # send_telegram(msg)
        log.info(f"Telegram 送信スキップ (disabled): {scenario_key} ({severity})")
    except Exception as e:
        log.error(f"Telegram 送信エラー: {e}")


# ─────────────────────────────────────────────────────────
# メインスキャン
# ─────────────────────────────────────────────────────────


def scan(force: bool = False) -> dict:
    """地政学ニュースをスキャンし、アラートを送信。
    Returns updated state dict.
    """
    # キャッシュチェック
    state = load_json(STATE_FILE, default={})
    if not force and state.get("cached_at"):
        try:
            cached = datetime.fromisoformat(state["cached_at"])
            if datetime.now() - cached < timedelta(hours=_get_cache_ttl_hours()):
                log.info("キャッシュ有効 — スキップ")
                return state
        except (ValueError, TypeError):
            pass

    # プレイブック読み込み
    playbook = load_json(PLAYBOOK_FILE, default={})
    if not playbook.get("scenarios"):
        log.error("scenario_playbook.json が見つからないかシナリオが空です")
        return state

    # ニュース取得
    log.info("Web Search 開始...")
    news_items = _fetch_all_news()
    if not news_items:
        log.warning("ニュース取得ゼロ — 前回状態を維持")
        return state

    log.info(f"合計 {len(news_items)} 件のニュースを取得")

    # キーワードマッチング
    matches = _match_keywords(news_items, playbook)
    log.info(f"マッチしたシナリオ: {len(matches)} 件")

    # AI評価 (既定は score>=3。ただし war_end など強い停戦キーワードは
    # scenario.detect.min_keyword_score で 2 件から評価へ通す)
    active_alerts = []
    keyword_matches = []
    assessment_errors = []
    for match in matches:
        scenario = match["scenario"]
        score = match["score"]
        matched_kw = match["matched_keywords"]
        min_score = _scenario_min_keyword_score(scenario)

        if score < min_score:
            log.info(
                f"  {scenario['id']}: スコア {score} < {min_score} — スキップ"
            )
            continue

        keyword_matches.append({
            "scenario_key": scenario.get("id", ""),
            "scenario_name": scenario.get("name", scenario.get("id", "")),
            "score": score,
            "threshold": min_score,
            "matched_keywords": matched_kw,
            "relevant_news": _compact_news_items(match.get("relevant_news", [])),
            "assessment_status": "pending",
            "detected_at": datetime.now().isoformat(),
        })

        log.info(f"  {scenario['id']}: スコア {score} — AI評価中...")
        assessment = _assess_scenario(
            scenario, matched_kw, match["relevant_news"]
        )

        if not assessment:
            log.warning(f"  {scenario['id']}: AI評価失敗")
            keyword_matches[-1]["assessment_status"] = "failed"
            assessment_errors.append({
                "scenario_key": scenario.get("id", ""),
                "score": score,
                "matched_keywords": matched_kw,
                "reason": "ai_assessment_failed",
                "detected_at": datetime.now().isoformat(),
            })
            continue

        keyword_matches[-1]["assessment_status"] = "assessed"
        severity = assessment.get("severity", "low")
        confidence = _confidence(assessment)
        keyword_matches[-1]["severity"] = severity
        keyword_matches[-1]["confidence"] = confidence
        log.info(f"  {scenario['id']}: severity={severity}, "
                 f"confidence={confidence:.2f}")

        alert_entry = {
            "scenario_key": scenario["id"],
            "headline": assessment.get("headline", ""),
            "severity": severity,
            "detail": assessment.get("detail", ""),
            "confidence": confidence,
            "detected_at": datetime.now().isoformat(),
            "keywords_matched": matched_kw,
            "alerted": False,
        }

        if not _is_material_alert(assessment, state=True):
            log.info(
                f"  {scenario['id']}: materiality不足 "
                f"(severity={severity}, confidence={confidence:.2f}) — active_alerts から除外"
            )
            continue

        # high 以上・高確度かつ未送信ならアラート送信
        if _is_material_alert(assessment):
            if not _is_recently_alerted(state, scenario["id"]):
                _send_alert(scenario, assessment)
                alert_entry["alerted"] = True
            else:
                log.info(f"  {scenario['id']}: {DEDUP_HOURS}h以内にアラート済み — スキップ")
        else:
            log.info(
                f"  {scenario['id']}: Telegram 閾値未満 "
                f"(severity={severity}, confidence={confidence:.2f})"
            )

        active_alerts.append(alert_entry)

    # scan_count_today の更新
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_date = state.get("last_scan", "")[:10]
    if prev_date == today_str:
        scan_count = state.get("scan_count_today", 0) + 1
    else:
        scan_count = 1

    # 状態保存
    new_state = {
        "last_scan": datetime.now().isoformat(),
        "scan_count_today": scan_count,
        "active_alerts": active_alerts,
        "keyword_matches": keyword_matches,
        "assessment_errors": assessment_errors,
        "news_items": news_items,
        "cached_at": datetime.now().isoformat(),
    }
    atomic_write_json(STATE_FILE, new_state)
    log.info(f"状態保存完了: {STATE_FILE}")

    return new_state


# ─────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────


def get_geopolitical_context() -> dict:
    """地政学コンテキストを返す（2時間キャッシュ）。
    キャッシュが有効ならファイルから読み込み、期限切れなら再スキャン。
    """
    state = load_json(STATE_FILE, default={})
    if state.get("cached_at"):
        try:
            cached = datetime.fromisoformat(state["cached_at"])
            if datetime.now() - cached < timedelta(hours=_get_cache_ttl_hours()):
                return state
        except (ValueError, TypeError):
            pass

    # キャッシュ切れ → 再スキャン
    return scan(force=True)


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ALMANAC 地政学モニター")
    parser.add_argument("--force", action="store_true",
                        help="キャッシュを無視して強制スキャン")
    args = parser.parse_args()

    result = scan(force=args.force)

    alerts = result.get("active_alerts", [])
    news_count = len(result.get("news_items", []))
    print(f"\n=== ALMANAC 地政学モニター ===")
    print(f"ニュース取得: {news_count} 件")
    print(f"アクティブアラート: {len(alerts)} 件")
    for a in alerts:
        icon = SEVERITY_ICONS.get(a["severity"], "?")
        print(f"  {icon} {a['scenario_key']}: {a['headline']} "
              f"(severity={a['severity']}, confidence={a['confidence']:.2f})")
