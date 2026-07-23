"""
analyst パッケージ — portfolio_analyst.py のモジュール分割版

使い方:
    from analyst import run_analysis, get_cached, send_to_telegram

モジュール構成:
    cache.py         — キャッシュ管理（保存/読み込み/有効性チェック）
    llm_client.py    — Claude API クライアント（Prompt Caching / Tool Use）
    data_gatherer.py — データ収集（市場指標・ニュース・決算・スナップショット）
    __init__.py      — run_analysis / get_cached / send_to_telegram（公開 API）
"""
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from analyst.cache import (
    CACHE_PATH, HISTORY_MAX,
    write_progress, save_cache, get_cached, is_cache_valid, load_json,
    load_history_context,
)
from analyst.llm_client import (
    call_claude, call_tier_analysis, fetch_web_search_news,
    _SUBMIT_TOOL, _SYSTEM_SONNET, _GEO_KEYWORDS, _append_llm_call_log,
)
from analyst.data_gatherer import (
    gather_data, fmt_news_section, fmt_earnings_section,
)
from almanac.runtime_config import get_env
from instrument_metadata import (
    jp_trading_unit_prompt,
    quantity_label_for_ticker,
    trading_unit_for_ticker,
)
from pseudo_tickers import is_pseudo_market_ticker


def _env_float(name: str, default: float) -> float:
    raw = get_env(name, str(default))
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = get_env(name, str(default))
    try:
        return int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = get_env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _tier_llm_timeout_seconds() -> float:
    return max(30.0, _env_float("ALMANAC_TIER_LLM_TIMEOUT_SECONDS", 300.0))


def _tier_max_tokens() -> int:
    return max(8192, _env_int("ALMANAC_TIER_MAX_TOKENS", 16000))


def _tier_retry_max_tokens() -> int:
    return max(_tier_max_tokens(), _env_int("ALMANAC_TIER_RETRY_MAX_TOKENS", 24000))


def _redteam_max_tokens() -> int:
    return max(6000, _env_int("ALMANAC_REDTEAM_MAX_TOKENS", 12000))


def _is_max_tokens_error(exc: Exception) -> bool:
    text = str(exc)
    return "stop_reason=max_tokens" in text or "max_tokens=" in text


def _compact_tier_retry_prompt(prompt: str, tier_name: str) -> str:
    """Retry prompt used when Anthropic tool_use JSON is truncated."""
    return (
        prompt
        + "\n\n"
        + "## 再出力制約（max_tokens 対策・必須）\n"
        + f"{tier_name} の前回出力が長すぎて tool_use JSON が途中で切れました。\n"
        + "- summary / health_reason / news_impact は各1文\n"
        + "- priority_actions は実行可能な高優先候補を最大12件目安（6件固定で圧縮しない）\n"
        + "- hold_notes は最大8件、各120字以内\n"
        + "- reason / action / execution_reason は各160字以内\n"
        + "- new_candidates / profit_taking / new_entries は最大5件\n"
        + "- 冗長な市場概況や同じ根拠の繰り返しは禁止\n"
        + "この制約を守り、必ず submit_analysis tool に compact JSON を入れてください。"
    )


def _call_sonnet_tier_json(role: str, prompt: str, shared_ctx: str,
                           tier_name: str) -> dict:
    """Call Sonnet tier analysis with a max_tokens retry path.

    Long/Medium tiers can legitimately produce larger JSON than the old 8k
    budget. A max_tokens truncation is not a model-quality failure, so retry
    once with a larger budget and explicit compact-output constraints.
    """
    timeout = _tier_llm_timeout_seconds()
    primary_tokens = _tier_max_tokens()
    try:
        return call_claude(
            _SYSTEM_SONNET,
            prompt,
            max_tokens=primary_tokens,
            cached_prefix=shared_ctx,
            use_tool=True,
            role=role,
            request_timeout=timeout,
        )
    except Exception as exc:
        if not _is_max_tokens_error(exc):
            raise
        retry_tokens = _tier_retry_max_tokens()
        print(
            f"  ↻ {tier_name}: max_tokens={primary_tokens} で切れたため "
            f"{retry_tokens} + compact prompt で再試行"
        )
        return call_claude(
            _SYSTEM_SONNET,
            _compact_tier_retry_prompt(prompt, tier_name),
            max_tokens=retry_tokens,
            cached_prefix=shared_ctx,
            use_tool=True,
            role=role,
            request_timeout=max(timeout, 300.0),
        )
# v5.1: 指値判断用チャート派生指標
try:
    from chart_analyzer import (
        gather_chart_context as _gather_chart_context,
        format_for_prompt as _format_chart_for_prompt,
    )
except Exception:
    _gather_chart_context = None
    _format_chart_for_prompt = None

# v5.1 Phase 3: オプション市場由来センチメント
try:
    from options_fetcher import (
        get_option_signals as _get_option_signals,
        format_for_prompt as _format_options_for_prompt,
    )
except Exception:
    _get_option_signals = None
    _format_options_for_prompt = None


def _collect_priority_tickers(*tier_analyses, positions_raw=None, max_tickers: int = 30) -> list[str]:
    """各ティア分析の priority_actions / margin_long_picks / short_opportunities から
    候補ティッカーを集約。保有銘柄も追加（管理アクション用）。重複除去・上限クランプ。"""
    seen: set[str] = set()
    ordered: list[str] = []

    def _push(t):
        if not t or not isinstance(t, str):
            return
        t = t.strip()
        if not t or t in seen or is_pseudo_market_ticker(t):
            return
        seen.add(t)
        ordered.append(t)

    for ana in tier_analyses:
        if not isinstance(ana, dict):
            continue
        for key in ("priority_actions", "margin_long_picks",
                    "short_opportunities", "new_candidates", "new_entries"):
            for item in ana.get(key) or []:
                if isinstance(item, dict):
                    _push(item.get("ticker"))
    for p in positions_raw or []:
        if isinstance(p, dict):
            _push(p.get("ticker"))
    return ordered[:max_tickers]


# ── tunable_params 制約コンテキスト（AI に目標値・上限を伝達）──

def _fmt_tunable_limits_context() -> str:
    """tunable_params の単一銘柄上限・通貨ターゲット・シグナル鮮度・ニューススコア閾値を
    AI に伝えるプロンプトブロック。コードで直接判定できない soft constraint を
    LLM 側で守らせるため。"""
    try:
        from tunable_params import get as _tp_get
    except Exception:
        return ""
    long_max  = _tp_get("long_max_single_pct", None)
    med_max   = _tp_get("medium_max_single_pct", None)
    usd_tgt   = _tp_get("currency_usd_target_pct", None)
    jpy_tgt   = _tp_get("currency_jpy_target_pct", None)
    stale_d   = _tp_get("stale_signal_days", None)
    news_thr  = _tp_get("news_score_threshold", None)
    news_per  = _tp_get("news_articles_per_ticker", None)
    disable_sl = _tp_get("disable_stop_loss_recommendations", True)
    disable_cumulative = _tp_get("disable_cumulative_recommendations", True)
    margin_conf = _tp_get("margin_buy_min_confidence_pct", None)
    margin_score = _tp_get("margin_buy_min_score", None)
    margin_ret = _tp_get("margin_buy_min_expected_return_pct_annual", None)
    lines = ["## ⚙️ チューニング制約（TUNABLE_LIMITS — 必ず遵守）"]
    if disable_sl:
        lines.append(
            "- 🛑 **stop_loss / 逆指値推奨 全銘柄禁止**: "
            "type=stop_loss も「逆指値発注」を含む sell も提案禁止。"
            "ユーザーが broker で手動 SL 管理しているため、AI 推奨は無効化されている。"
            "違反するとシステムが自動除去（type 偽装も含めて検出）。"
        )
    if disable_cumulative:
        lines.append(
            "- 🛑 **買い側の定期/自動積立アクション 全銘柄禁止**: "
            "type=dca や、buy/add の action に「毎月」「自動積立」「クレカ積立」「月次積立」「積立設定/増額」などを含む提案は禁止。"
            "ユーザーが broker でクレカ積立・NISA つみたて枠・持株会を**自動設定済み**のため、"
            "AI が「サブ口座NISAつみたて即時化」「SLIM_ORCAN 月次積立増額」「クレカ積立設定」などを提案するのは冗長で混乱の原因。"
            "NISAつみたて投資枠はスポット一括買付の枠ではないため、"
            "「NISAつみたて枠で一括/スポット/一時買付」という表現は禁止。"
            "一括買付を提案する場合は、必ず NISA成長投資枠 または特定/一般口座として明示すること。"
            "つみたて投資枠の年内消化は、broker の積立設定/使い切り設定/ボーナス設定の確認事項として hold_notes に回し、priority_actions にしないこと。"
            "ただし、sell/trim の理由説明として「持株会」「月次積立は継続」を記載することは許可。"
            "違反するとシステムが自動除去。SLIM_SP500/SLIM_ORCAN/IFREE_FANGPLUS/MNXACT/NOMURA_SEMI は積立対象なので"
            "成長投資枠以外での一括スポット買いは避けること。"
        )
    if long_max is not None:
        lines.append(f"- Long ティア単一銘柄上限: {long_max}%（超過時は trim/rebalance を提案）")
    if med_max is not None:
        lines.append(f"- Medium ティア単一銘柄上限: {med_max}%（超過時は trim を提案）")
    if usd_tgt is not None and jpy_tgt is not None:
        lines.append(
            f"- 通貨配分目標（現行 static）: USD {usd_tgt}% / JPY {jpy_tgt}%（±5pt以内で許容）。"
            "外貨比率は市況に応じて**あなたが判断**し currency_target_recommendation に出すこと。"
            "ただし rebalance が実際に適用する母数は **long tier 限定** の通貨比率 (currency_breakdown_long) "
            "であり、whole_portfolio (cash/medium/swing 込み) ではない。"
            "目標を出すときは必ず basis=\"long_tier\" とし、long母数の比率を見て決めること。"
            "usd+jpy=100、confidence_pct/horizon_days/valid_until を必ず付すこと（欠落・自信不足・"
            "basis不一致は不採用となり static 目標に戻る）。1回の変更幅は ±10pt 以内が望ましい。"
            "現状維持が妥当なら現行値をそのまま confidence 付きで出してよい。自動発注はしない。"
        )
    lines.append(f"- {jp_trading_unit_prompt()}")
    if stale_d is not None:
        lines.append(f"- シグナル鮮度許容: {stale_d}日（これより古いシグナルは判断に使わない）")
    if news_thr is not None:
        lines.append(f"- ニューススコア閾値: {news_thr}（これ未満のニュースは無視）")
    if news_per is not None:
        lines.append(f"- 銘柄あたりニュース上限: {news_per}件")
    if margin_conf is not None or margin_score is not None or margin_ret is not None:
        parts = []
        if margin_conf is not None:
            parts.append(f"confidence_pct {margin_conf}%以上")
        if margin_score is not None:
            parts.append(f"score {margin_score}以上")
        if margin_ret is not None:
            parts.append(f"期待リターン年率 {margin_ret}%以上")
        lines.append("- 信用買い高conviction閾値: " + " / ".join(parts))
    return "\n".join(lines)


# ── 税務・NISA・持株会コンテキスト整形 ──────────────────────

def _fmt_tax_context(data: dict) -> str:
    """税務・NISA・持株会の状況をプロンプト用テキストに整形する"""
    lines = []

    # NISA枠
    nisa = data.get("nisa", {})
    if isinstance(nisa, dict) and "husband" in nisa:
        h = nisa["husband"]
        w = nisa.get("wife", {})
        lines.append(
            f"NISA残枠（利用予定反映後）— メイン: つみたて¥{h.get('tsumitate_remaining',0):,.0f}"
            f"（予定¥{h.get('tsumitate_planned',0):,.0f}） / 成長¥{h.get('growth_remaining',0):,.0f}"
            + (
                f" / サブ: つみたて¥{w.get('tsumitate_remaining',0):,.0f}"
                f"（予定¥{w.get('tsumitate_planned',0):,.0f}） / 成長¥{w.get('growth_remaining',0):,.0f}"
                if w else ""
            )
        )

    # 税務コンテキスト
    tax = data.get("tax_context", {})
    if tax and not tax.get("error"):
        # 損出し
        lh = tax.get("loss_harvest", {})
        candidates = lh.get("candidates", [])
        if candidates:
            days = lh.get("days_to_deadline", -1)
            saving = lh.get("total_tax_saving", 0)
            deadline_note = f"（期限まで{days}日）" if days >= 0 else ""
            lines.append(
                f"損出し候補{deadline_note}: {len(candidates)}銘柄 / 節税効果¥{saving:,.0f} "
                + " | ".join(f"{c['ticker']}({c['unrealized_jpy']:,.0f}円)" for c in candidates[:3])
            )
        elif not candidates:
            lines.append("損出し候補: なし（含み損銘柄なし）")

        # 緊急アクション
        urgent = tax.get("urgent_actions", [])
        for u in urgent[:2]:
            lines.append(f"⚠️ 税務緊急: {u.get('message','')}")

    # 持株会
    kub = data.get("espp_context", {})
    if kub and not kub.get("error"):
        ratio = kub.get("portfolio_ratio", 0) * 100
        alert = kub.get("concentration_alert", "normal")
        sell_rec = kub.get("sell_recommendation", 0)
        lines.append(
            f"持株会(9999.T): ポートフォリオ比率{ratio:.1f}% / アラート:{alert}"
            + (f" / 売却推奨¥{sell_rec:,.0f}" if sell_rec > 0 else "")
        )
        if kub.get("concentration_message"):
            lines.append(f"  → {kub['concentration_message']}")

    # JP equity 比率 (持株会除外・動的目標, 2026-07-07)
    jp_exp = data.get("jp_exposure", {})
    if isinstance(jp_exp, dict) and jp_exp.get("jp_equity_ex_employer_pct") is not None:
        _tgt_detail = jp_exp.get("target_detail") or {}
        if _tgt_detail.get("frozen_reason"):
            _tgt_note = f"（リスクオフ凍結: {_tgt_detail['frozen_reason']} → base固定）"
        elif (_tgt_detail.get("boost_pct") or 0) > 0:
            _tgt_note = (
                f"（base {jp_exp.get('target_base_pct', 10):.0f}% + "
                f"日本株シナリオreadiness連動 +{_tgt_detail['boost_pct']:.1f}pt）"
            )
        else:
            _tgt_note = f"（base {jp_exp.get('target_base_pct', 10):.0f}%・シナリオ非発動）"
        lines.append(
            f"日本株比率（持株会9999.T除外）: {jp_exp['jp_equity_ex_employer_pct']:.1f}% "
            f"/ 動的目標 {jp_exp.get('target_pct', 10):.1f}% {_tgt_note}"
            + (
                f" / 残ヘッドルーム ¥{jp_exp.get('headroom_jpy', 0):,.0f}"
                if (jp_exp.get("headroom_jpy") or 0) > 0 else "（目標到達済 — 新規JP買いは抑制）"
            )
            + "。持株会は売買自由度が低く月次で自動増加するため配分母数から除外済み。"
            "目標未達なら日本株（ETF/個別）の buy 提案を検討すること。"
        )

    return "\n".join(lines) if lines else "税務データ取得不可"


def _extract_tax_urgent_actions(data: dict) -> str:
    """Opus合成用: 税務緊急アクションのサマリー文字列を返す"""
    tax = data.get("tax_context", {})
    if not tax or tax.get("error"):
        return ""
    urgent = tax.get("urgent_actions", [])
    if not urgent:
        return ""
    return "【税務緊急アクション】\n" + "\n".join(f"- {u.get('message','')}" for u in urgent)


# ── Black-Litterman: Sonnetティア分析からLLMビューを抽出 ────────────

_ACTION_RETURN_MAP_BASE = {
    ('buy',         'high'):   0.15,  ('buy',         'medium'): 0.10,  ('buy',         'low'):    0.05,
    ('add',         'high'):   0.12,  ('add',         'medium'): 0.08,  ('add',         'low'):    0.04,
    ('dca',         'high'):   0.12,  ('dca',         'medium'): 0.08,  ('dca',         'low'):    0.05,
    ('sell',        'high'):  -0.15,  ('sell',        'medium'):-0.10,  ('sell',        'low'):   -0.05,
    ('trim',        'high'):  -0.10,  ('trim',        'medium'):-0.07,  ('trim',        'low'):   -0.03,
    ('stop_loss',   'high'):  -0.20,  ('stop_loss',   'medium'):-0.15,  ('stop_loss',   'low'):   -0.10,
    ('take_profit', 'high'):   0.05,  ('take_profit', 'medium'): 0.03,  ('take_profit', 'low'):    0.02,
    ('rebalance',   'high'):   0.02,  ('rebalance',   'medium'): 0.01,  ('rebalance',   'low'):    0.00,
}
# 後方互換のためエイリアスを保持
_ACTION_RETURN_MAP = _ACTION_RETURN_MAP_BASE


def _vol_adjusted_return(action: str, urgency: str, vix: float, regime_bull: bool = False) -> float:
    """VIX水準・レジーム合意に基づいてBL期待リターンをスケーリングする（Phase 2A+）"""
    base = _ACTION_RETURN_MAP_BASE.get((action, urgency),
           _ACTION_RETURN_MAP_BASE.get((action, 'medium'), 0.0))
    vix_f = float(vix) if vix else 20.0
    # 強気レジーム合意時(3/4指標)は積極スケール、それ以外は従来スケール
    if regime_bull:
        if base >= 0:
            # 買いシグナルは強気相場で増幅（過激な2.5xを抑制）
            vix_scale = 1.5 if vix_f < 20 else (1.3 if vix_f < 30 else (1.2 if vix_f < 40 else 1.0))
        else:
            # 売りシグナルは強気相場では懐疑的に（0.5x に抑制）
            vix_scale = 0.5 if vix_f < 20 else (0.7 if vix_f < 30 else (1.0 if vix_f < 40 else 1.3))
    else:
        vix_scale = 0.8 if vix_f < 20 else (1.0 if vix_f < 30 else (1.3 if vix_f < 40 else 1.5))
    return round(base * vix_scale, 4)


def _extract_bl_views(long_a: dict, medium_a: dict, short_a: dict, vix: float = 20.0, regime_bull: bool = False) -> dict:
    """
    3つのSonnetティア分析から銘柄別リターン期待値（%）を抽出し bl_views.json に保存。
    各ティア（Long/Medium/Short）の priority_actions をそれぞれ Bull/Bear/Macro 視点と見なし、
    3ティア間の分散を Ω（信頼度行列）として使用する（ICLR 2025 BL+LLM方式）。

    Omega改善:
    - Agent間の意見の分散（disagreement）
    - 予測信頼度（confidence_pct）の平均
    - シグナル数による不確実性スケーリング
    """
    ticker_views: dict[str, list[float]] = {}
    ticker_confidence: dict[str, list[float]] = {}

    for analysis in [long_a, medium_a, short_a]:
        if not isinstance(analysis, dict):
            continue
        for action in analysis.get("priority_actions", []):
            ticker = action.get("ticker", "")
            if not ticker or ticker == "_cash":
                continue
            atype   = str(action.get("type", "")).lower()
            urgency = str(action.get("urgency", "medium")).lower()
            # trim / rebalance は「配分調整」であり価格方向性の予測ではない
            # BLビューに含めるとネガティブな期待リターンのフィードバックループが発生するためスキップ
            if atype in ("trim", "rebalance"):
                continue
            ret = _vol_adjusted_return(atype, urgency, vix, regime_bull=regime_bull)
            ticker_views.setdefault(ticker, []).append(ret)

            # 予測信頼度を収集
            conf = action.get("confidence_pct")
            if conf is not None:
                try:
                    ticker_confidence.setdefault(ticker, []).append(float(conf))
                except (ValueError, TypeError):
                    pass

    import numpy as _np
    views_output: dict = {}
    for ticker, view_list in ticker_views.items():
        arr    = _np.array(view_list, dtype=float)
        mean_v = float(arr.mean())

        # 極端なビューをクランプ（-15% ～ +15%）
        MAX_ABS_VIEW = 0.15
        mean_v = max(-MAX_ABS_VIEW, min(MAX_ABS_VIEW, mean_v))

        # === 改善されたΩ計算 ===
        # 1. Agent間分散（disagreement）
        agent_var = float(arr.var()) if len(view_list) > 1 else 0.02

        # 2. 信頼度による調整（高信頼 → 低分散）
        conf_list = ticker_confidence.get(ticker, [])
        if conf_list:
            avg_confidence = sum(conf_list) / len(conf_list) / 100.0  # 0-1スケール
            confidence_factor = max(0.3, 1.0 - avg_confidence * 0.7)  # 高信頼→0.3倍, 低信頼→1.0倍
        else:
            confidence_factor = 1.0  # 信頼度データなし → 調整なし

        # 3. シグナル数による調整（多い→低不確実性）
        n_signals = len(view_list)
        signal_factor = 1.0 / max(1, n_signals ** 0.5)  # √n で分散を縮小

        # 最終Omega: agent分散 × 信頼度係数 × シグナル係数
        omega = max(agent_var * confidence_factor * signal_factor, 0.001)

        # n_signals=1 の場合は信頼度が低い → 分散を大きく設定
        if len(view_list) == 1:
            omega = max(omega, 0.04)

        views_output[ticker] = {
            "bull_view":  round(float(view_list[0]), 4) if len(view_list) > 0 else round(mean_v, 4),
            "bear_view":  round(float(view_list[1]), 4) if len(view_list) > 1 else round(mean_v * 0.7, 4),
            "macro_view": round(float(view_list[2]), 4) if len(view_list) > 2 else round(mean_v * 0.9, 4),
            "mean_view":  round(mean_v, 4),
            "variance":   round(omega, 6),
            "n_signals":  n_signals,
            "avg_confidence": round(sum(conf_list) / len(conf_list), 1) if conf_list else None,
        }

    # P2-27: BL_USE_INDEPENDENT_ALPHA で LLM 由来 view を独立 source に差し替え/混合する。
    # Codex Round 2 の "confidence laundering" 指摘への構造的解決策。
    #   - "0" or 未設定: LLM only (P1-16 で Ω deweight 済み、後方互換)
    #   - "1":           independent only (LLM views を完全に廃棄)
    #   - "mix":         independent を優先し、欠落 ticker のみ LLM で埋める
    import os as _os
    bl_mode = (_os.environ.get("BL_USE_INDEPENDENT_ALPHA", "0") or "0").lower()
    independent_count = 0
    if bl_mode in ("1", "true", "mix"):
        try:
            from bl_alpha_sources import compute_independent_views
            indep = compute_independent_views(tickers=list(views_output.keys()) or [])
            independent_count = len(indep)
            if bl_mode == "mix":
                # independent 優先、LLM は欠落を補完
                merged = dict(views_output)
                merged.update(indep)
                views_output = merged
            else:
                # independent only: LLM views を捨てる
                views_output = indep
        except Exception as _e:
            print(f"  ⚠️ independent alpha source 取得失敗: {_e} — LLM view にフォールバック")

    bl_path = BASE_DIR / "bl_views.json"
    with open(bl_path, "w", encoding="utf-8") as f:
        json.dump({
            "views":     views_output,
            "as_of":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "n_tickers": len(views_output),
            "bl_mode":   bl_mode,
            "independent_count": independent_count,
        }, f, ensure_ascii=False, indent=2)
    bull_tag = " [強気×2.5]" if regime_bull else ""
    mode_tag = "" if bl_mode in ("0", "false") else f" [bl_mode={bl_mode}, indep={independent_count}]"
    print(f"  📐 BLビュー抽出: {len(views_output)}銘柄 → bl_views.json{bull_tag}{mode_tag}")
    return views_output


# ── FinCon Verbal Reinforcement: エージェント投資信念管理 ────────────

_BELIEFS_PATH = BASE_DIR / "beliefs" / "agent_beliefs.json"

# P3-13: 30 日無更新の MARKET 汎用 (conviction<=55) belief を枝刈りする閾値
_MARKET_STALE_DAYS = 30


def _is_stale_market_belief(b: dict, now: datetime | None = None) -> bool:
    """MARKET 汎用 belief で last_updated が 30 日以上前のものを stale と判定。

    個別銘柄 belief は対象外。conviction>55 の強い MARKET 主張も残す
    （opus_synthesis が "opportunity_highlights" で生成する一律 conviction=55 の
    汎用テーマだけを刈り取る）。
    """
    if b.get("ticker") != "MARKET":
        return False
    if int(b.get("conviction_score", 60) or 0) > 55:
        return False
    try:
        lu = datetime.fromisoformat(b.get("last_updated") or b.get("created_at") or "")
    except Exception:
        return True
    return lu < ((now or datetime.now()) - timedelta(days=_MARKET_STALE_DAYS))


def _load_execution_quality_summary() -> dict | None:
    """v5.1: beliefs ファイル末尾に保存された execution_quality を取り出す。
    n>=10 のとき有効。Opus プロンプトで「執行品質を踏まえた判断」を促す材料に使う。"""
    if not _BELIEFS_PATH.exists():
        return None
    try:
        with open(_BELIEFS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        eq = raw.get("execution_quality")
        if isinstance(eq, dict) and eq.get("n", 0) >= 10:
            return eq
    except Exception:
        pass
    return None


def _format_execution_quality_for_prompt(eq: dict | None) -> str:
    """Opus プロンプト用に execution_quality を 1 ブロック整形。"""
    if not eq:
        return ""
    lines = ["## 📐 EXECUTION_QUALITY（過去N件約定の品質統計）"]
    n = eq.get("n", 0)
    median = eq.get("median_shortfall_bps")
    iqr = eq.get("iqr_bps")
    rate = eq.get("ai_compliance_rate")
    lines.append(f"- 約定数: {n} 件 / 中央 shortfall: {median}bps / IQR: {iqr}bps")
    if rate is not None:
        lines.append(f"- AI 指値遵守率: {rate*100:.0f}%")
    lines.append("")
    lines.append("【判断ガイド】")
    if median is not None and median > 50:
        lines.append("- 過去の median shortfall が 50bps 超 → urgency=high の指値で `expiry_minutes` を短くし、刺さらないなら成行に切り替える設計を意識")
    elif median is not None and median <= 0:
        lines.append("- 過去 median shortfall が良好（≤0bps）→ 指値 k 係数（atr_14d×k）を 0.5→0.3 に攻める余地あり")
    if rate is not None and rate < 0.5:
        lines.append("- AI 指値遵守率 50% 未満 → 推奨が現実的でない可能性。指値帯（band）を活用するか urgency 設計を再考")
    return "\n".join(lines)


def _format_agent_reliability_for_prompt(max_entries: int = 8) -> str:
    path = BASE_DIR / "agent_reliability.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    rows: list[dict] = []
    agents = data.get("agents")
    if isinstance(agents, dict):
        for agent, groups in agents.items():
            if not isinstance(groups, dict):
                continue
            for group_key, stats in groups.items():
                if not isinstance(stats, dict):
                    continue
                role, _, stance = str(group_key).partition("/")
                row = dict(stats)
                row.setdefault("agent", agent)
                row.setdefault("role", role)
                if stance:
                    row.setdefault("stance", stance)
                rows.append(row)
    else:
        groups = data.get("groups") or data.get("by_agent") or []
        if isinstance(groups, dict):
            groups = list(groups.values())
        if isinstance(groups, list):
            rows = [g for g in groups if isinstance(g, dict)]
    if not rows:
        return ""

    def _weight(row: dict) -> float:
        try:
            return float(row.get("weight"))
        except (TypeError, ValueError):
            return -1.0

    rows = [
        row for row in rows
        if row.get("measured", True) is not False and row.get("weight") is not None
    ]
    if not rows:
        return ""
    rows = sorted(rows, key=_weight, reverse=True)[:max_entries]
    lines = ["## 🧭 AGENT_RELIABILITY（source別の事後信頼度）"]
    for row in rows:
        agent = row.get("agent") or row.get("source_agent") or row.get("name") or "?"
        role = row.get("role") or ""
        stance = row.get("stance") or ""
        role_label = "/".join(str(p) for p in (role, stance) if p)
        n = row.get("n") or row.get("count") or row.get("episodes")
        measured_n = row.get("measured_n")
        weight = row.get("weight")
        ev = row.get("mean_excess_return_bps")
        if ev is None:
            ev = row.get("ev_bps")
        n_label = f"n={n}"
        if measured_n is not None:
            n_label += f" measured_n={measured_n}"
        lines.append(f"- {agent}{('/' + role_label) if role_label else ''}: {n_label} weight={weight} ev_bps={ev}")
    lines.append("→ weight が低いsource由来の候補は urgency/size を下げる。高いsourceは根拠強化に使うが、hard safety gate は上書き不可。")
    return "\n".join(lines)


def _load_beliefs() -> list[dict]:
    """期限切れ + 30 日無更新 MARKET 汎用を除外した有効な投資信念リストを返す"""
    if not _BELIEFS_PATH.exists():
        return []
    try:
        with open(_BELIEFS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        beliefs = raw.get("beliefs", [])
        now = datetime.now()
        return [
            b for b in beliefs
            if (not b.get("expires_at") or
                datetime.fromisoformat(b["expires_at"]) > now)
            and not _is_stale_market_belief(b, now)
        ]
    except Exception:
        return []


def _update_beliefs(synthesis: dict) -> None:
    """Opus合成結果から投資信念をエピソード的に更新・追加し保存（FinCon方式）"""
    import uuid as _uuid
    from datetime import timedelta
    now    = datetime.now()
    expiry = (now + timedelta(days=30)).replace(microsecond=0).isoformat()

    existing      = _load_beliefs()
    ticker_index  = {b["ticker"]: i for i, b in enumerate(existing) if b.get("ticker")}
    new_beliefs: list[dict] = []

    for action in synthesis.get("priority_actions", [])[:10]:
        ticker = action.get("ticker", "")
        if not ticker or ticker == "_cash":
            continue
        urgency_str = str(action.get("urgency", "medium")).lower()
        base_conviction = {"high": 80, "medium": 60, "low": 40}.get(urgency_str, 60)
        # Phase 2C: accuracy_adj — 過去の推奨精度(win_rate + 期待値)でconviction調整
        try:
            from recommendation_verifier import verify_recommendations as _vr
            _acc = _vr()
            _stats = _acc.get("stats", {})
            _key = f"{action.get('type', '')}({urgency_str})"
            _stat = _stats.get(_key, {})
            _wr = _stat.get("win_rate")
            if _wr is not None and _stat.get("total", 0) >= 5:
                _wr_adj = round((_wr - 0.5) * 20)  # 70%→+4, 40%→-2
                # 期待値調整: EV = p×avg_win − (1-p)×|avg_loss|（±5pt cap）
                _avg_win  = float(_stat.get("avg_win_pct",  0) or 0)
                _avg_loss = float(_stat.get("avg_loss_pct", 0) or 0)
                _ev = float(_wr) * _avg_win - (1.0 - float(_wr)) * abs(_avg_loss)
                _ev_adj = max(-5, min(5, round(_ev * 100)))
                conviction = max(20, min(95, base_conviction + _wr_adj + _ev_adj))
            else:
                conviction = base_conviction
        except Exception:
            conviction = base_conviction

        # v5.1: 執行品質キャップ（掛け算ではなく上限制約）
        # 過去の中央 shortfall が 50bps 超 + n>=20 なら conviction を 75 にキャップ
        # → 「執行が下手なうちは強い conviction を持たせない」という安全装置
        try:
            from execution_quality import shortfall_summary as _sf_summary_belief
            _sf = _sf_summary_belief(min_n=20)
            _med = _sf.get("median_shortfall_bps")
            if _sf.get("n", 0) >= 20 and _med is not None and _med > 50:
                conviction = min(conviction, 75)
        except Exception:
            pass

        belief = {
            "id":               _uuid.uuid4().hex[:8],
            "ticker":           ticker,
            "theme":            action.get("type", ""),
            "conviction_score": conviction,
            "rationale":        str(action.get("reason", ""))[:200],
            "source_agent":     "opus_synthesis",
            "evidence":         str(action.get("action", ""))[:150],
            "created_at":       now.isoformat(),
            "last_updated":     now.isoformat(),
            "expires_at":       expiry,
        }
        if ticker in ticker_index:
            existing[ticker_index[ticker]] = belief
        else:
            new_beliefs.append(belief)

    for opp in synthesis.get("opportunity_highlights", [])[:5]:
        if isinstance(opp, str) and len(opp) > 5:
            new_beliefs.append({
                "id":               _uuid.uuid4().hex[:8],
                "ticker":           "MARKET",
                "theme":            "opportunity",
                "conviction_score": 55,
                "rationale":        opp[:200],
                "source_agent":     "opus_synthesis",
                "evidence":         synthesis.get("stance_reason", "")[:100],
                "created_at":       now.isoformat(),
                "last_updated":     now.isoformat(),
                "expires_at":       expiry,
            })

    all_beliefs = (existing + new_beliefs)[-100:]
    # conviction < 30 のbeliefは7日に短縮して早期退場
    from datetime import timedelta as _td
    for b in all_beliefs:
        if b.get("conviction_score", 60) < 30 and b.get("expires_at"):
            try:
                orig_exp = datetime.fromisoformat(b["expires_at"])
                short_exp = (now + _td(days=7)).replace(microsecond=0)
                if orig_exp > short_exp:
                    b["expires_at"] = short_exp.isoformat()
            except Exception:
                pass

    # P3-13: 30 日無更新の MARKET 汎用 (conviction<=55) belief を物理的に削除
    before = len(all_beliefs)
    all_beliefs = [b for b in all_beliefs if not _is_stale_market_belief(b, now)]
    pruned = before - len(all_beliefs)
    if pruned > 0:
        print(f"  🧹 P3-13: 30日無更新の MARKET 汎用 belief を {pruned} 件枝刈り")
    # v5.1: execution_quality 統計を beliefs ファイルに併記（Implementation Shortfall 学習材料）
    _exec_quality = None
    try:
        from execution_quality import shortfall_summary as _sf_summary
        _exec_quality = _sf_summary(min_n=10)
    except Exception:
        pass

    _BELIEFS_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "beliefs":      all_beliefs,
        "last_updated": now.isoformat(),
        "version":      "1.1",  # v5.1 で execution_quality 追加
    }
    if _exec_quality and _exec_quality.get("n", 0) >= 10:
        payload["execution_quality"] = {
            "n":                    _exec_quality.get("n"),
            "median_shortfall_bps": _exec_quality.get("median_shortfall_bps"),
            "iqr_bps":              _exec_quality.get("iqr_bps"),
            "ai_compliance_rate":   _exec_quality.get("ai_compliance_rate"),
            "as_of":                now.isoformat(timespec="seconds"),
        }
    with open(_BELIEFS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  🧠 投資信念更新: {len(all_beliefs)}件 (新規{len(new_beliefs)}件) → beliefs/agent_beliefs.json")


def _format_beliefs_context(beliefs: list[dict], max_items: int = 15) -> str:
    """Sonnet注入用の投資信念サマリーテキストを生成"""
    if not beliefs:
        return ""
    recent = sorted(beliefs, key=lambda b: b.get("last_updated", ""), reverse=True)[:max_items]
    lines  = ["## 📝 過去の投資信念（FinCon Verbal Memory）",
              "※以下は過去の分析から蓄積された投資信念です。現在の分析の参考として活用してください。"]
    for b in recent:
        lines.append(
            f"- [{b.get('ticker','?')}] {b.get('theme','')} "
            f"(確信度{b.get('conviction_score',50)}%): {b.get('rationale','')[:100]}"
        )
    return "\n".join(lines)


# ── Phase 1B: 一次判断間の不一致スコアリング ──────────────────
def _compute_disagreement(long_a: dict, medium_a: dict, sp_a: dict, ml_a: dict | None = None, ss_a: dict | None = None) -> str:
    """
    5つの一次判断（Sonnet×3 + book-aware×2）の銘柄別方向性不一致を定量化し、Opus注入用テキストを生成。
    不一致を明示することでOpusの統合判断精度を向上させる。
    """
    try:
        from collections import defaultdict

        # 各エージェントの priority_actions から (ticker → [types]) を収集
        ticker_actions: dict[str, list[tuple[str, str]]] = defaultdict(list)
        agent_labels = [
            ("LongA", long_a),
            ("MediumA", medium_a),
            ("ShortA", sp_a),
            ("MarginLongD", ml_a),
            ("ShortSellD", ss_a),
        ]

        for label, analysis in agent_labels:
            if not isinstance(analysis, dict):
                continue
            for action in analysis.get("priority_actions", []):
                ticker = action.get("ticker", "")
                atype = str(action.get("type", "")).lower()
                if ticker and ticker != "_cash" and atype:
                    ticker_actions[ticker].append((label, atype))

        if not ticker_actions:
            return ""

        # 方向分類: buy系 vs sell系
        # take_profit は経済的に sell（利益確定売り）なので sell に分類する
        BUY_TYPES  = {"buy", "add", "dca", "margin_buy", "cover"}
        SELL_TYPES = {"sell", "trim", "stop_loss", "take_profit", "short"}

        disagreements = []
        agreements = []

        for ticker, actions in ticker_actions.items():
            if len(actions) < 2:
                continue  # 1エージェントのみは不一致判定不可
            directions = []
            for label, atype in actions:
                if atype in BUY_TYPES:
                    directions.append("buy")
                elif atype in SELL_TYPES:
                    directions.append("sell")

            if not directions:
                continue

            buy_count  = directions.count("buy")
            sell_count = directions.count("sell")
            coverage   = len(actions)

            if buy_count > 0 and sell_count > 0:
                # 方向不一致
                detail = " / ".join(f"{lbl}={atype}" for lbl, atype in actions)
                disagreements.append(
                    f"  {ticker}: {coverage}/5エージェント言及 ⚠️ 方向不一致 "
                    f"(buy系{buy_count}件 vs sell系{sell_count}件: {detail})"
                )
            elif coverage >= 3:
                direction_str = "buy方向" if buy_count >= sell_count else "sell方向"
                agreements.append(f"  {ticker}: {coverage}/5言及・全員{direction_str} → 高合意")

        if not disagreements and not agreements:
            return ""

        lines = ["【エージェント間合意分析】"]
        lines.extend(disagreements)
        lines.extend(agreements[:3])  # 合意銘柄は最大3件のみ表示
        if disagreements:
            lines.append("→ 不一致銘柄は双方向のリスクを考慮し、urgencyを慎重に設定すること")

        return "\n".join(lines)

    except Exception:
        return ""


# ── Phase 1C: データ鮮度スコアリング ────────────────────────────────
def _data_source_age_hours(
    base_dir: Path,
    fname: str,
    ts_keys: tuple[str, ...],
    *,
    local_tz,
    now: datetime,
) -> float | None:
    fpath = base_dir / fname
    if not fpath.exists():
        return None
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
        ts_str = ""
        for k in ts_keys:
            if k == "__mtime__":
                ts_str = datetime.fromtimestamp(
                    fpath.stat().st_mtime,
                    tz=local_tz,
                ).isoformat(timespec="seconds")
                break
            if isinstance(data, dict) and data.get(k):
                ts_str = str(data.get(k))
                break
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            try:
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
        if ts.tzinfo is not None:
            ts = ts.astimezone(local_tz).replace(tzinfo=None)
        return (now - ts).total_seconds() / 3600
    except Exception:
        return None


def _freshest_source_file(
    candidates: list[str],
    ts_keys: tuple[str, ...],
    *,
    local_tz,
    now: datetime,
) -> str:
    ages = [
        (fname, _data_source_age_hours(BASE_DIR, fname, ts_keys, local_tz=local_tz, now=now))
        for fname in candidates
    ]
    ages = [(fname, age) for fname, age in ages if age is not None]
    if not ages:
        return candidates[0]
    return min(ages, key=lambda item: item[1])[0]


def _ensure_news_candidates_fresh(
    *,
    base_dir: Path = BASE_DIR,
    max_age_hours: float | None = None,
    refresher=None,
) -> bool:
    if _env_bool("ALMANAC_SKIP_NEWS_CANDIDATE_REFRESH", False):
        return False
    local_tz = ZoneInfo(get_env("ALMANAC_LOCAL_TIMEZONE", "Asia/Tokyo") or "Asia/Tokyo")
    now = datetime.now(local_tz).replace(tzinfo=None)
    max_age = _env_float("ALMANAC_NEWS_CANDIDATE_REFRESH_HOURS", 6.0) if max_age_hours is None else float(max_age_hours)
    age_h = _data_source_age_hours(
        base_dir,
        "news_signal_candidates.json",
        ("generated_at", "scan_time"),
        local_tz=local_tz,
        now=now,
    )
    if age_h is not None and age_h <= max_age:
        return False

    if refresher is None:
        from news_screener import screen_news_sentiment
        refresher = screen_news_sentiment
    refresher()
    return True


def _ensure_technical_state_fresh(
    *,
    base_dir: Path = BASE_DIR,
    max_age_hours: float | None = None,
    refresher=None,
) -> bool:
    local_tz = ZoneInfo(get_env("ALMANAC_LOCAL_TIMEZONE", "Asia/Tokyo") or "Asia/Tokyo")
    now = datetime.now(local_tz).replace(tzinfo=None)
    max_age = _env_float("ALMANAC_TECHNICAL_REFRESH_HOURS", 4.0) if max_age_hours is None else float(max_age_hours)
    age_h = _data_source_age_hours(
        base_dir,
        "technical_state.json",
        ("cached_at",),
        local_tz=local_tz,
        now=now,
    )
    source_is_current = False
    universe_is_complete = False
    try:
        state = json.loads((base_dir / "technical_state.json").read_text(encoding="utf-8"))
        source_health = state.get("source_health") or {}
        max_lag = source_health.get("max_lag_sessions")
        missing_count = source_health.get("missing_count")
        quality_counts = source_health.get("data_quality_counts")
        cached_rows = state.get("tickers") or {}
        quality_schema_current = (
            isinstance(quality_counts, dict)
            and isinstance(cached_rows, dict)
            and all(
                isinstance(row, dict) and row.get("data_quality_status") in {"ok", "blocked"}
                for row in cached_rows.values()
            )
        )
        source_is_current = (
            max_lag is not None
            and int(max_lag) < 2
            and missing_count is not None
            and int(missing_count) == 0
            and quality_schema_current
        )
        import technical_signals
        requested = set(technical_signals._build_ticker_universe())
        cached_tickers = set(cached_rows.keys())
        universe_is_complete = bool(requested) and requested.issubset(cached_tickers)
    except Exception:
        source_is_current = False
        universe_is_complete = False
    if age_h is not None and age_h <= max_age and source_is_current and universe_is_complete:
        return False
    if refresher is None:
        import technical_signals
        refresher = lambda: technical_signals.get_technical_context(force=True)
    refresher()
    return True


def _ensure_macro_event_state_fresh(
    *,
    base_dir: Path = BASE_DIR,
    max_age_hours: float = 24.0,
    refresher=None,
) -> bool:
    """Refresh official CPI/employment/FOMC calendar when missing or stale."""
    path = base_dir / "macro_event_state.json"
    age_h = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(str(raw.get("refreshed_at") or "").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600
        except Exception:
            age_h = None
    if age_h is not None and age_h <= max_age_hours:
        return False
    if refresher is None:
        from macro_event_calendar import refresh_macro_event_state
        refresher = lambda: refresh_macro_event_state(state_file=path)
    refresher()
    return True


def _compute_data_freshness() -> str:
    """
    各データソースのタイムスタンプを検査し、鮮度スコアとOpus注入用テキストを生成。
    古いデータに依存する high urgency アクションを抑制させる。
    """
    try:
        from datetime import timedelta

        local_tz = ZoneInfo(get_env("ALMANAC_LOCAL_TIMEZONE", "Asia/Tokyo") or "Asia/Tokyo")
        now = datetime.now(local_tz).replace(tzinfo=None)
        short_screen_file = _freshest_source_file(
            ["screen_results_morning.json", "screen_results.json"],
            ("timestamp", "generated_at"),
            local_tz=local_tz,
            now=now,
        )
        sources = [
            # (file_path, timestamp_keys, description, warn_hours, stale_hours, weight)
            # P3-14: timestamp_keys を tuple にして複数候補対応（generated_at / cached_at 揺れ）
            ("macro_state.json",             ("cached_at",),                "macro_state(FRED指標)",       4,  12, 0.25),
            ("technical_state.json",         ("cached_at",),                "technical_state(RSI/MACD)",  4,  8,  0.25),
            ("vix_state.json",               ("cached_at",),                "vix_state",                  4,  8,  0.20),
            ("regime_state.json",            ("updated",),                  "regime_state(HMM)",          8,  24, 0.15),
            (short_screen_file,                ("timestamp", "generated_at"), "screen_results(短期)",       8,  24, 0.10),
            ("screen_results_jp.json",        ("timestamp", "generated_at"), "screen_results_jp(日本株)",  8,  24, 0.10),
            ("margin_long_candidates_morning.json", ("generated_at",),        "margin_long_candidates_morning", 8, 24, 0.10),
            ("margin_long_candidates.json",   ("generated_at",),             "margin_long_candidates",     8,  24, 0.10),
            ("long_term_screen_results.json", ("as_of",),                   "long_term_screen",          24,  72, 0.05),
            ("account.json",                  ("last_updated",),             "account_cash",              24,  96, 0.10),
            ("holdings.json",                 ("__mtime__",),                "holdings",                  24,  96, 0.10),
            ("news_signal_candidates.json",  ("generated_at", "scan_time"), "news_candidates",            6,  12, 0.10),
            ("geopolitical_state.json",      ("cached_at",),                "geopolitical_state",        12,  24, 0.05),
        ]

        scores = []
        lines = ["【データ鮮度スコア】"]
        overall_score = 0.0
        total_weight  = 0.0

        for fname, ts_keys, label, warn_h, stale_h, weight in sources:
            age_h = _data_source_age_hours(
                BASE_DIR,
                fname,
                ts_keys,
                local_tz=local_tz,
                now=now,
            )
            if age_h is None:
                continue

            try:
                freshness = max(0.0, 1.0 - age_h / stale_h)
                if fname == "technical_state.json":
                    try:
                        technical = json.loads((BASE_DIR / fname).read_text(encoding="utf-8"))
                        max_lag = (technical.get("source_health") or {}).get("max_lag_sessions")
                        if max_lag is None:
                            freshness = min(freshness, 0.25)
                        elif int(max_lag) >= 2:
                            freshness = 0.0
                        elif int(max_lag) == 1:
                            freshness = min(freshness, 0.5)
                    except Exception:
                        freshness = min(freshness, 0.25)
                overall_score += freshness * weight
                total_weight  += weight

                if age_h < warn_h:
                    status = "✅ FRESH"
                elif age_h < stale_h:
                    status = "⚠️ STALE"
                else:
                    status = "❌ VERY_STALE"

                lines.append(f"  {status} {label}: {age_h:.0f}h前")
            except Exception:
                continue

        if total_weight == 0:
            return ""

        score = round(overall_score / total_weight, 2)
        lines.insert(1, f"  総合スコア: {score:.2f}/1.00")

        if score < 0.5:
            lines.append("⚠️ データ鮮度が低い — high urgencyアクションは慎重に。可能なら再スキャンを推奨。")
        elif score < 0.7:
            lines.append("→ 一部データが古い — 影響を受けるアクションのurgencyを1段階下げることを検討。")

        return "\n".join(lines)

    except Exception:
        return ""


def _extract_data_freshness_score(data_freshness_context: str) -> float | None:
    """Extract the numeric 0..1 freshness score from the prompt context text."""
    if not data_freshness_context:
        return None
    patterns = (
        r"総合スコア[:：]\s*([0-9.]+)\s*/\s*1(?:\.0+)?",
        r"overall[_ ]?score[:\s]*([0-9.]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, data_freshness_context)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            return value
    return None


# ── Phase 2B: レジーム合意ゲート ────────────────────────────────────
def _compute_regime_consensus(data: dict) -> str:
    """
    HMM・macro_score・VIX・SPY-MA50 の4指標からレジーム合意度を計算し
    Shared Context注入用テキストを生成する（Phase 2B）。
    """
    try:
        regime_raw = data.get("regime", {})
        macro_meta = data.get("market_meta", {})

        hmm_regime  = regime_raw.get("regime", "")          # A_強気/B_中立/C_弱気
        macro_score = regime_raw.get("macro_score", 5)
        vix         = float(macro_meta.get("vix") or 20)
        spy_above   = regime_raw.get("spy_above", True)

        # 各指標を bull / neutral / bear に正規化
        hmm_sig   = "bull"    if "強気" in hmm_regime else ("bear" if "弱気" in hmm_regime else "neutral")
        macro_sig = "bull"    if macro_score >= 6     else ("bear" if macro_score <= 3     else "neutral")
        vix_sig   = "bull"    if vix < 20             else ("bear" if vix >= 30            else "neutral")
        spy_sig   = "bull"    if spy_above            else "bear"

        signals   = [hmm_sig, macro_sig, vix_sig, spy_sig]
        bull_cnt  = signals.count("bull")
        bear_cnt  = signals.count("bear")
        max_agree = max(bull_cnt, bear_cnt, signals.count("neutral"))
        confidence = {4: 1.0, 3: 0.75, 2: 0.5}.get(max_agree, 0.25)

        direction = "強気" if bull_cnt > bear_cnt else ("弱気" if bear_cnt > bull_cnt else "中立")
        lines = [
            f"【レジーム合意: {hmm_regime or '不明'} / 信頼度{confidence:.0%}】",
            f"  HMM={hmm_sig} / macro_score={macro_score}→{macro_sig} / "
            f"VIX={vix:.0f}→{vix_sig} / SPY-MA50→{spy_sig}",
        ]
        if confidence < 0.6:
            lines.append("  → 指標間に矛盾あり。方向性確信は低い — ポジションサイズを抑制推奨")
        else:
            lines.append(f"  → {max_agree}/4指標が{direction}に合意")
        return "\n".join(lines)
    except Exception:
        return ""


def _fmt_guard_warnings(guard: dict) -> str:
    """ガードレール状態を原因別に整形してコンテキストに注入する文字列を返す"""
    if not guard:
        return ""
    lines = []
    alerts = guard.get("alerts", [])
    alert_msgs = " ".join(a.get("message", "") for a in alerts)

    if not guard.get("trading_allowed", True):
        lines.append("## 🚨 ガードレール: 全トレード停止（月間損失閾値-8%超過）")
    elif not guard.get("new_entry_allowed", True):
        # 原因を判定: アクティブトレード数超過 vs 日次損失超過
        if "アクティブトレード" in alert_msgs:
            n = guard.get("active_trades", "?")
            lines.append(f"## ⚠️ ガードレール: 新規エントリー禁止（保有ポジション数{n}件が上限到達）※損失制限ではない")
        else:
            lines.append("## ⚠️ ガードレール: 新規エントリー禁止（日次損失閾値-4%超過）")

    return "\n".join(lines)


# ── ストレステスト要約 ──────────────────────────────────

def _fmt_stress_tests(positions: list) -> str:
    """ポートフォリオに対するストレスシナリオの想定損失額を算出。"""
    try:
        from risk_engine import STRESS_SCENARIOS
    except ImportError:
        return "（ストレスシナリオ未取得）"
    if not positions:
        return "（ポジションなし）"
    # ティッカー → 評価額マップ
    val_map: dict[str, float] = {}
    for p in positions:
        t = p.get("ticker", "")
        val_map[t] = val_map.get(t, 0) + (p.get("value_jpy") or 0)
    lines = []
    for name, shocks in STRESS_SCENARIOS.items():
        impact = 0.0
        for ticker, shock in shocks.items():
            # 直接一致 or 部分一致（SP500→SPY含むポジション等は除外、ティッカー完全一致のみ）
            impact += val_map.get(ticker, 0) * shock
        lines.append(f"  {name}: 推定損失 ¥{abs(impact):,.0f}")
    return "\n".join(lines) if lines else "（シナリオ定義なし）"


# ── 共有市場コンテキスト ──────────────────────────────────

def _build_shared_market_context(data: dict) -> str:
    mm  = data["market_meta"]
    sc  = data["scenario"]
    today = datetime.now().strftime("%Y-%m-%d（%A）")

    # FinBERT ニュース感情集計
    nss = data.get("news_sentiment_summary", {})
    nss_text = ""
    if nss and nss.get("total", 0) > 0:
        pos = nss.get("positive", 0)
        neg = nss.get("negative", 0)
        neu = nss.get("neutral", 0)
        total = nss.get("total", 1)
        sentiment_bias = "強気優勢" if pos > neg * 1.3 else ("弱気優勢" if neg > pos * 1.3 else "中立")
        nss_text = f"\n## FinBERTニュース感情集計（過去24h / {nss.get('as_of','')}）\n強気{pos}件 / 弱気{neg}件 / 中立{neu}件（計{total}件） → {sentiment_bias}"

    # レジーム合意ゲート（Phase 2B）
    regime_consensus_text = _compute_regime_consensus(data)
    execution_plan_text = _fmt_execution_plan(data.get("execution_plan", {}))
    currency_basis_text = (
        "## 通貨比率（母数別・混同禁止）\n"
        f"whole_portfolio（全ティア・現金込み）: {json.dumps(data.get('currency_breakdown_whole') or data.get('currency_breakdown', {}), ensure_ascii=False)}\n"
        f"long_tier（target/rebalance判定専用）: {json.dumps(data.get('currency_breakdown_long', {}), ensure_ascii=False)}\n"
        "USD不足/超過を述べる場合は必ず母数を明記し、long_tierの判断にwhole_portfolio比率を流用しない。"
    )

    return f"""## 本日の日付: {today}
※ニュース記事の日付を必ず本日日付と照合すること。過去イベントを「今後の予定」として戦略に組み込まないこと。

## マーケット環境（リアルタイム指標 / 全ティア共通）
VIX: {mm.get('vix','不明')} ({mm.get('vix_level','')})
米10年金利: {mm.get('us10y_yield',{}).get('value','不明')}% (前日比{mm.get('us10y_yield',{}).get('change_pct','?')}%) / 米2年金利: {mm.get('us2y_yield',{}).get('value','不明')}%
イールドカーブ: スプレッド{mm.get('yield_curve_spread','不明')}% → {mm.get('yield_curve_status','')}
ドル指数(DXY): {mm.get('dxy',{}).get('value','不明')} (前日比{mm.get('dxy',{}).get('change_pct','?')}%)
原油: ${mm.get('crude_oil',{}).get('value','不明')} (前日比{mm.get('crude_oil',{}).get('change_pct','?')}%) / 金: ${mm.get('gold',{}).get('value','不明')}
S&P500 vs MA50: {mm.get('sp500_vs_ma50_pct','不明')}% / 日経 vs MA50: {mm.get('nikkei_vs_ma50_pct','不明')}%
日経225: {mm.get('nikkei',{}).get('value','不明')} (前日比{mm.get('nikkei',{}).get('change_pct','?')}%)

## マーケットレジーム
spy_above={data['regime'].get('spy_above')}, nk_above={data['regime'].get('nk_above')}

{_fmt_guard_warnings(data.get("guard_state", {}))}
## 現在のシナリオ: {sc.get('key')} — {sc.get('name','')}
推奨アクション: {json.dumps(sc.get('actions',[])[:3], ensure_ascii=False)}
ハイリターン機会: {json.dumps(sc.get('high_return_opportunities',[])[:4], ensure_ascii=False)}
空売り許可: {sc.get('short_allowed', False)} / レバレッジ許可: {sc.get('leverage_allowed', False)}

## リスク指標（ポートフォリオ全体）
{json.dumps(data.get('risk', {}), ensure_ascii=False)}{nss_text}

{currency_basis_text}

## ストレステスト（ポートフォリオ影響度）
{_fmt_stress_tests(data.get('positions', []))}
{execution_plan_text + chr(10) if execution_plan_text else ""}
{regime_consensus_text + chr(10) if regime_consensus_text else ""}"""


def _build_public_market_context(data: dict) -> str:
    """PUBLIC-only market context for EXTERNAL (non-Anthropic) models.

    Privacy boundary (see ``almanac/llm_safety.py``): this is the version safe to
    send outside Anthropic. It mirrors the *public* sections of
    :func:`_build_shared_market_context` but NEVER the book-derived ones —
    no portfolio risk metrics, no stress-test 推定損失, no guard state (which
    leaks position count / loss), no beliefs. Use this for the external Red Team
    legs; the full ``shared_ctx`` stays on Anthropic only.
    """
    mm = data["market_meta"]
    sc = data["scenario"]
    today = datetime.now().strftime("%Y-%m-%d（%A）")

    nss = data.get("news_sentiment_summary", {})
    nss_text = ""
    if nss and nss.get("total", 0) > 0:
        pos = nss.get("positive", 0)
        neg = nss.get("negative", 0)
        neu = nss.get("neutral", 0)
        total = nss.get("total", 1)
        sentiment_bias = "強気優勢" if pos > neg * 1.3 else ("弱気優勢" if neg > pos * 1.3 else "中立")
        nss_text = f"\n## FinBERTニュース感情集計（過去24h / {nss.get('as_of','')}）\n強気{pos}件 / 弱気{neg}件 / 中立{neu}件（計{total}件） → {sentiment_bias}"

    regime_consensus_text = _compute_regime_consensus(data)

    return f"""## 本日の日付: {today}
※ニュース記事の日付を必ず本日日付と照合すること。過去イベントを「今後の予定」として戦略に組み込まないこと。

## マーケット環境（リアルタイム指標 / 公開情報のみ）
VIX: {mm.get('vix','不明')} ({mm.get('vix_level','')})
米10年金利: {mm.get('us10y_yield',{}).get('value','不明')}% (前日比{mm.get('us10y_yield',{}).get('change_pct','?')}%) / 米2年金利: {mm.get('us2y_yield',{}).get('value','不明')}%
イールドカーブ: スプレッド{mm.get('yield_curve_spread','不明')}% → {mm.get('yield_curve_status','')}
ドル指数(DXY): {mm.get('dxy',{}).get('value','不明')} (前日比{mm.get('dxy',{}).get('change_pct','?')}%)
原油: ${mm.get('crude_oil',{}).get('value','不明')} (前日比{mm.get('crude_oil',{}).get('change_pct','?')}%) / 金: ${mm.get('gold',{}).get('value','不明')}
S&P500 vs MA50: {mm.get('sp500_vs_ma50_pct','不明')}% / 日経 vs MA50: {mm.get('nikkei_vs_ma50_pct','不明')}%
日経225: {mm.get('nikkei',{}).get('value','不明')} (前日比{mm.get('nikkei',{}).get('change_pct','?')}%)

## マーケットレジーム
spy_above={data.get('regime', {}).get('spy_above')}, nk_above={data.get('regime', {}).get('nk_above')}

## 現在のシナリオ: {sc.get('key')} — {sc.get('name','')}
推奨アクション: {json.dumps(sc.get('actions',[])[:3], ensure_ascii=False)}
ハイリターン機会: {json.dumps(sc.get('high_return_opportunities',[])[:4], ensure_ascii=False)}
空売り許可: {sc.get('short_allowed', False)} / レバレッジ許可: {sc.get('leverage_allowed', False)}{nss_text}
{regime_consensus_text + chr(10) if regime_consensus_text else ""}"""


# ── ティア別 Sonnet 分析 ─────────────────────────────────

def _compute_ginn_vol(tickers: list[str]) -> tuple[str, dict]:
    """
    ティッカーリストに対してGINN強化GJR-GARCHボラティリティを推定する。
    data/ohlcv/{ticker}.parquetから日次リターンを読み込み、年率ボラを返す。

    Returns:
        (prompt_str, ginn_vol_dict)
        prompt_str: プロンプト注入用テキスト（空文字の場合あり）
        ginn_vol_dict: {ticker: float} 年率ボラ%
    """
    import pandas as _pd
    ginn_vol_dict: dict = {}
    lines: list[str] = []
    ohlcv_dir = BASE_DIR / "data" / "ohlcv"
    for ticker in tickers:
        try:
            pq_path = ohlcv_dir / f"{ticker}.parquet"
            if not pq_path.exists():
                continue
            df = _pd.read_parquet(pq_path)
            # MultiIndex対応
            if isinstance(df.columns, _pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            close_col = next((c for c in df.columns if str(c).lower() in ("close", "adj close", "adjclose")), None)
            if close_col is None:
                continue
            prices = _pd.to_numeric(df[close_col], errors="coerce").dropna()
            if len(prices) < 62:
                continue
            returns = prices.pct_change().dropna()
            if len(returns) < 60:
                continue
            from risk_engine import estimate_gjr_garch as _gjr
            # データ不足（1年未満）の銘柄は GINN を使わず GARCH のみ
            use_ginn = len(returns) >= 252
            if not use_ginn:
                print(f"  [GINN] {ticker}: データ不足({len(returns)}日) → GARCHフォールバック")
            result = _gjr(returns, use_ginn=use_ginn)
            if "error" in result:
                continue
            vol_pct = round(result.get("forecast_vol", 0) * 100, 1)
            model = result.get("model", "GJR-GARCH")
            ginn_vol_dict[ticker] = vol_pct
            lines.append(f"  {ticker}: {vol_pct:.1f}%年率（{model}）")
        except Exception:
            pass
    prompt_str = ""
    if lines:
        prompt_str = "### GINN推定ボラティリティ（年率）\n" + "\n".join(lines)
    return prompt_str, ginn_vol_dict


def _fmt_technical_state(tickers: list[str], technical_state: dict) -> str:
    """保有銘柄のテクニカル指標をフォーマット（Sonnet注入用）"""
    if not technical_state:
        return ""
    lines = ["## 保有銘柄テクニカル状態（RSI/MACD/BB/composite）"]
    found = False
    for ticker in tickers:
        td = technical_state.get(ticker)
        if not td:
            continue
        found = True
        if td.get("data_quality_status") == "blocked":
            reasons = td.get("data_quality_reasons") or []
            dates = ",".join(str(row.get("date")) for row in reasons[:3] if isinstance(row, dict))
            lines.append(
                f"  {ticker}: 指標無効（未調整の分割・併合候補{f' {dates}' if dates else ''}）"
            )
            continue
        rsi = td.get("rsi")
        rsi_sig = td.get("rsi_signal", "")
        macd_h = td.get("macd_histogram")
        bb_pct = td.get("bb_pct_b")
        vol_r = td.get("volume_ratio")
        comp = td.get("composite_score")
        comp_sig = td.get("composite_signal", "")
        chg1d = td.get("change_1d_pct")
        chg5d = td.get("change_5d_pct")
        parts = []
        if rsi is not None:
            parts.append(f"RSI={rsi:.0f}({rsi_sig})")
        if macd_h is not None:
            parts.append(f"MACD_hist={macd_h:+.3f}")
        if bb_pct is not None:
            parts.append(f"BB%B={bb_pct:.2f}")
        if vol_r is not None:
            parts.append(f"Vol×{vol_r:.1f}")
        if comp is not None:
            parts.append(f"総合={comp:.1f}({comp_sig})")
        if chg1d is not None:
            parts.append(f"1d={chg1d:+.1f}%")
        if chg5d is not None:
            parts.append(f"5d={chg5d:+.1f}%")
        lines.append(f"  {ticker}: " + " / ".join(parts))
    return "\n".join(lines) if found else ""


def _fmt_social_sentiment(tickers: list[str], social_data: dict) -> str:
    """SNS感情・オプション異常をSonnet注入用テキストにフォーマット（Phase B）"""
    # NOTE: social_screener.py の出力キーは "stocktwits"（"tickers" ではない）
    stocktwits = social_data.get("stocktwits", {})
    options_unusual = social_data.get("options_unusual", [])
    top_bullish = social_data.get("top_bullish", [])
    top_bearish = social_data.get("top_bearish", [])

    if not stocktwits and not options_unusual:
        return ""

    lines = ["## SNS感情・オプション異常（StockTwits/Options）"]

    # --- StockTwits 感情（対象ティッカーのみ） ---
    ticker_set = set(tickers)
    found_st = False
    for t in tickers:
        d = stocktwits.get(t)
        if not d:
            continue
        found_st = True
        bull = d.get("bullish_pct", 0)
        bear = d.get("bearish_pct", 0)
        sig  = d.get("sentiment", "")
        parts = [f"強気{bull}%/弱気{bear}%({sig})"]
        # 極端な感情は警告
        if bull >= 80:
            parts.append("⚠️過熱(SNS過度強気)")
        elif bear >= 80:
            parts.append("⚠️悲観(SNS過度弱気)")
        lines.append(f"  {t}: " + " ".join(parts))

    # --- オプション異常（対象ティッカー or unusual=True の上位件） ---
    # 対象ティッカーのオプションデータ
    for opt in options_unusual:
        t = opt.get("ticker", "")
        if t not in ticker_set:
            continue
        cpr   = opt.get("call_put_ratio", 0)
        bias  = opt.get("bias", "")
        unusu = opt.get("unusual", False)
        flag  = "⚠️異常CP比" if unusu else ""
        bias_ja = {"CALL_HEAVY": "コール偏重(強気オプション活動)", "PUT_HEAVY": "プット偏重(ヘッジ/弱気)", "BALANCED": "均衡"}.get(bias, bias)
        lines.append(f"  {t} オプション: CP比={cpr:.2f} {bias_ja} {flag}".rstrip())

    # --- 市場全体の異常サマリー（ティッカー問わず上位3件） ---
    market_anomalies = [o for o in options_unusual if o.get("unusual") and o.get("ticker") not in ticker_set]
    if market_anomalies:
        lines.append("  [市場異常オプション]")
        for opt in market_anomalies[:3]:
            t    = opt.get("ticker", "")
            cpr  = opt.get("call_put_ratio", 0)
            bias = opt.get("bias", "")
            bias_ja = {"CALL_HEAVY": "コール偏重", "PUT_HEAVY": "プット偏重", "BALANCED": "均衡"}.get(bias, bias)
            lines.append(f"    ⚠️ {t}: CP={cpr:.2f} {bias_ja}")

    # --- SNS全体のトップ強気/弱気（文脈として） ---
    cross_bull = [t for t in top_bullish[:5] if t not in ticker_set]
    cross_bear = [t for t in top_bearish[:5] if t not in ticker_set]
    if cross_bull:
        lines.append(f"  [SNS強気TOP(他銘柄)]: {', '.join(cross_bull)}")
    if cross_bear:
        lines.append(f"  [SNS弱気TOP(他銘柄)]: {', '.join(cross_bear)}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _fmt_rebalance_report(report: dict) -> str:
    """リバランスレポート詳細をSonnet注入用テキストにフォーマット（Phase C）"""
    if not report:
        return ""
    lines = [f"## リバランスレポート詳細（{report.get('as_of', '')}）"]
    action_plan = report.get("action_plan", [])
    buy_candidates = report.get("buy_candidates", {})
    if not action_plan and not buy_candidates:
        return ""
    # action_plan items: {priority, level, type, sector, message, amount_jpy, nisa_warning}
    for a in action_plan[:5]:
        amt = a.get("amount_jpy", 0) or 0
        lines.append(
            f"  [{a.get('type','')}] {a.get('sector','')} "
            f"¥{amt:,.0f} — {str(a.get('message',''))[:60]}"
        )
    # buy_candidates: {"sectors": [{sector, gap_jpy, current, target}], "currencies": [...]}
    if isinstance(buy_candidates, dict):
        for c in buy_candidates.get("sectors", [])[:3]:
            lines.append(
                f"  [buy候補-sector] {c.get('sector','')} "
                f"gap=¥{c.get('gap_jpy',0):,.0f} (現{c.get('current','')}→目標{c.get('target','')})"
            )
        for c in buy_candidates.get("currencies", [])[:2]:
            lines.append(
                f"  [buy候補-currency] {c.get('currency',c.get('sector',''))} "
                f"gap=¥{c.get('gap_jpy',0):,.0f}"
            )
    elif isinstance(buy_candidates, list):
        for c in buy_candidates[:3]:
            lines.append(f"  [buy候補] {c.get('ticker', c.get('sector',''))} — {str(c.get('reason', c.get('message','')))[:60]}")
    return "\n".join(lines)


def _fmt_execution_plan(plan: dict) -> str:
    """Execution plan summary for shared AI context."""
    if not isinstance(plan, dict) or not plan:
        return ""
    items = [i for i in (plan.get("items") or []) if isinstance(i, dict)]
    budgets = plan.get("budgets") if isinstance(plan.get("budgets"), dict) else {}
    summary = plan.get("consumption_summary") if isinstance(plan.get("consumption_summary"), dict) else {}
    if not items and not budgets:
        return ""

    def _yen(value) -> str:
        try:
            return f"¥{float(value or 0):,.0f}"
        except (TypeError, ValueError):
            return "¥0"

    horizon = plan.get("horizon") if isinstance(plan.get("horizon"), dict) else {}
    lines = [
        f"## 週次/月次 実行計画（{plan.get('as_of', '')}）",
        (
            f"  horizon={horizon.get('week_start','?')}..{horizon.get('week_end','?')} "
            f"/ monthly={_yen(budgets.get('monthly_total_jpy'))} "
            f"/ weekly_normal={_yen(budgets.get('weekly_normal_jpy'))} "
            f"/ opportunity_reserve={_yen(budgets.get('weekly_opportunity_reserve_jpy'))}"
        ),
        (
            f"  consumed_open={_yen(summary.get('open_order_consumed_jpy'))} "
            f"/ consumed_filled={_yen(summary.get('filled_consumed_jpy'))} "
            f"/ remaining_normal={_yen(summary.get('remaining_normal_jpy'))} "
            f"/ remaining_opportunity={_yen(summary.get('remaining_opportunity_jpy'))}"
        ),
        (
            "  ルール: 通常 buy/add は active plan item と残予算・品質条件に従う。"
            "既存注文/約定で covered の項目は追加発注しない。"
            "ただし high-conviction は opportunistic override、risk-reduction は defensive override として既存H2 cap以下で検討可。"
        ),
    ]
    for item in sorted(items, key=lambda x: int(x.get("priority") or 99))[:6]:
        lines.append(
            f"  - {item.get('objective')}: status={item.get('status')} "
            f"budget={_yen(item.get('normal_budget_jpy'))} "
            f"consumed={_yen(item.get('consumed_jpy'))} "
            f"remaining={_yen(item.get('remaining_jpy'))} "
            f"id={item.get('plan_item_id')}"
        )
    for warn in (plan.get("warnings") or [])[:3]:
        lines.append(f"  warning: {warn}")
    return "\n".join(lines)


def _self_consistent_long(data: dict, shared_ctx: str = "") -> dict:
    """
    Longティアを2回並列実行し、アクション方向の一致度を検証。
    一致→confirmedフラグ、不一致→disputedフラグを付与。
    """
    if not _env_bool("ALMANAC_LONG_SELF_CONSISTENCY", False):
        result = _analyze_long(data, shared_ctx)
        if isinstance(result, dict):
            for action in result.get("priority_actions", []) or []:
                if isinstance(action, dict):
                    action.setdefault("self_consistency", "single_run")
        return result

    from concurrent.futures import ThreadPoolExecutor as _SCPool, wait as _cf_wait

    timeout = _tier_llm_timeout_seconds() + 10.0
    ex = _SCPool(max_workers=2)
    try:
        f1 = ex.submit(_analyze_long, data, shared_ctx)
        f2 = ex.submit(_analyze_long, data, shared_ctx)
        done, not_done = _cf_wait([f1, f2], timeout=timeout)
        if f1 not in done:
            print(f"  ⚠️ Long自己一致性 run1 タイムアウト: {timeout:.0f}s")
            r1 = {"error": "long self-consistency run1 timeout", "health": "caution", "priority_actions": []}
        else:
            r1 = f1.result()
        if f2 not in done:
            print(f"  ⚠️ Long自己一致性 run2 タイムアウト: {timeout:.0f}s")
            r2 = {}
        else:
            r2 = f2.result()
        for fut in not_done:
            fut.cancel()
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)

    base = r1 if isinstance(r1, dict) else {}
    check = r2 if isinstance(r2, dict) else {}

    actions_1 = {a.get("ticker"): a for a in base.get("priority_actions", []) if a.get("ticker")}
    actions_2 = {a.get("ticker"): a for a in check.get("priority_actions", []) if a.get("ticker")}

    for ticker, a1 in actions_1.items():
        a2 = actions_2.get(ticker)
        if a2 and a1.get("type") == a2.get("type"):
            a1["self_consistency"] = "confirmed"
            c1 = a1.get("confidence_pct") or 50
            c2 = a2.get("confidence_pct") or 50
            a1["confidence_pct"] = round((c1 + c2) / 2)
        elif a2:
            a1["self_consistency"] = f"disputed (run2: {a2.get('type')})"
        else:
            a1["self_consistency"] = "unconfirmed"

    only_in_r2 = set(actions_2) - set(actions_1)
    if only_in_r2:
        notes = base.setdefault("hold_notes", [])
        if isinstance(notes, list):
            for t in only_in_r2:
                a2 = actions_2[t]
                notes.append(f"[自己一致性] {t}: run2のみ{a2.get('type')}推奨（run1では未推奨）")

    n_confirmed = sum(1 for a in actions_1.values() if a.get("self_consistency") == "confirmed")
    n_disputed = sum(1 for a in actions_1.values() if "disputed" in str(a.get("self_consistency", "")))
    print(f"  🔄 Long自己一致性: confirmed={n_confirmed}, disputed={n_disputed}, unconfirmed={len(actions_1) - n_confirmed - n_disputed}")
    return base


def _analyze_long(data: dict, shared_ctx: str = "") -> dict:
    positions = [p for p in data["positions"] if p.get("investment_type") == "long"]
    candidates = data["screening"]["long_term"].get("passed", [])[:5]
    opt = data["screening"]["optimization"]
    rec_method = opt.get("recommended", "max_sharpe") if opt else "max_sharpe"
    rec_weights = (opt.get("results", {}).get(rec_method, {}).get("weights", {}) if opt else {})
    rebalance_actions = data["rebalance"].get("action_plan", [])[:4]

    total = sum(p.get("value_jpy", 0) for p in positions) or 1
    current_weights = {p["ticker"]: round(p.get("value_jpy", 0) / total * 100, 1) for p in positions}
    rec_weights_pct = {k: round(v * 100, 1) for k, v in rec_weights.items()}

    # GINN-enhanced volatility per position
    _long_tickers = [p["ticker"] for p in positions]
    _ginn_vol_str, _ginn_vol_dict = _compute_ginn_vol(_long_tickers)

    # テクニカル状態を抽出（Long保有銘柄のみ）
    long_tickers = [p.get("ticker", p.get("symbol", "")) for p in positions]
    tech_text = _fmt_technical_state(long_tickers, data.get("technical_state", {}))
    rebalance_report_text = _fmt_rebalance_report(data.get("rebalance_report", {}))

    pos_summary = [{
        "ticker": p["ticker"], "name": p.get("name", ""),
        "shares": p.get("shares"), "value_jpy": p.get("value_jpy"),
        "unrealized_pct": round((p.get("unrealized_pct") or 0) * 100, 1),
        "unrealized_jpy": p.get("unrealized_jpy"),
        "holding_days": p.get("holding_days"), "entry_date": p.get("entry_date", ""),
        "stop_loss": p.get("stop_loss"), "account": p.get("account", ""),
        "sector": p.get("sector", ""),
    } for p in positions]

    nisa_text = json.dumps(data["nisa"], ensure_ascii=False)
    sec_sorted = sorted([(k, v["score"]) for k, v in data["sector_strength"].items()],
                        key=lambda x: x[1], reverse=True)
    sec_text = f"強い: {[k for k, _ in sec_sorted[:3]]} / 弱い: {[k for k, _ in sec_sorted[-3:]]}"
    news_text = fmt_news_section(data["news"], tickers=["NVDA", "AVGO", "GLD", "1489.T"])
    earnings_text = fmt_earnings_section(data.get("earnings", {}), tickers=["NVDA", "AVGO", "GLD"])

    prompt = f"""## Longティア（コア戦略 / 5年+ホールド）の分析
※市場環境・レジーム・シナリオはキャッシュコンテキスト参照。

{earnings_text}

### 現在のポジション
{json.dumps(pos_summary, ensure_ascii=False)}

### 現在ウェイト（%）
{json.dumps(current_weights, ensure_ascii=False)}

### 最適化推奨ウェイト（{rec_method} / %）
{json.dumps(rec_weights_pct, ensure_ascii=False)}

### リバランスアクションリスト
{json.dumps(rebalance_actions, ensure_ascii=False)}

### 長期スクリーニング通過銘柄（上位5件）
{json.dumps(candidates, ensure_ascii=False)}

### セクター強度
{sec_text}

### NISA 残枠
{nisa_text}

### 現金残高・通貨制約
{json.dumps(data.get('cash_info', {}), ensure_ascii=False)}
※ USD建て銘柄売却 → USD口座へ入金。JPY建て銘柄購入 → JPY口座から出金。ドル転なしにUSD売却益でJPY銘柄（1489.T等）は購入不可。

### 日本株ファンダメンタルズ（PER/PBR/配当利回り/ROE）
{json.dumps(data.get('jp_fundamentals', {}), ensure_ascii=False)}

### 税務・NISA・持株会状況
{_fmt_tax_context(data)}

{_ginn_vol_str}

{tech_text}

{rebalance_report_text + chr(10) if rebalance_report_text else ""}
### 関連銘柄ニュース
{news_text}

---
Longティアとして以下のJSON形式で分析してください:
※税務・NISA・持株会の状況を踏まえ、売買タイミング・口座選択・損出しのアドバイスを priority_actions や nisa_strategy に反映すること。
※楽天証券は米国株の端株取引不可。amount_hint は必ず整数株単位（"1株"、"2株"など）で記載すること。
{{
  "health": "good|caution|critical",
  "health_reason": "1文",
  "summary": "3文以内",
  "priority_actions": [{{"rank":1,"urgency":"high|medium|low","type":"buy|add|sell|rebalance|trim|dca","ticker":"XXX","action":"具体的なアクション","reason":"根拠","amount_hint":"整数のみ（株式は株、投資信託は口単位）","return_20d_rank":"top|middle|bottom","confidence_pct":75}}],
  "hold_notes": ["保有継続すべき銘柄とその理由（アクション不要のもの）"],
  "new_candidates": [{{"ticker":"XXX","reason":"理由","score":0}}],
  "optimization_insight": "現在vs最適ウェイト乖離",
  "rebalance_summary": "リバランス要約",
  "nisa_strategy": "NISA戦略",
  "high_return_opportunity": "機会",
  "news_impact": "ニュース影響"
}}"""

    try:
        result = _call_sonnet_tier_json(
            "tier_analysis_long",
            prompt,
            shared_ctx,
            "Long分析",
        )
        if not isinstance(result, dict) or not result:
            raise RuntimeError("Sonnet returned empty result (possible max_tokens truncation)")
        result["ginn_vol"] = _ginn_vol_dict
        # Fix H (2026-04-20): 観測性 — 使用モデル ID を記録
        try:
            from model_router import get_model as _gm
            result.setdefault("model_used", _gm("tier_analysis_long"))
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"  ⚠️ Long分析エラー: {e}")
        return {"error": str(e), "health": "caution", "summary": "分析エラー", "priority_actions": [], "ginn_vol": _ginn_vol_dict}


def _analyze_medium(data: dict, shared_ctx: str = "") -> dict:
    positions = [p for p in data["positions"] if p.get("investment_type") == "medium"]
    signals = data["signals"]
    signals_age = data.get("signals_age_hours")
    signals_stale_warn = (
        f"⚠️ シグナルが {signals_age:.0f}時間前 のデータです（鮮度低下）"
        if signals_age and signals_age > 24 else ""
    )
    candidates = list(data["screen_candidates"][:6])
    _seen_screen_tickers = {c.get("ticker") for c in candidates if isinstance(c, dict)}
    for c in data.get("screening", {}).get("jp_screen_candidates", [])[:4]:
        if isinstance(c, dict) and c.get("ticker") not in _seen_screen_tickers:
            candidates.append(c)
            _seen_screen_tickers.add(c.get("ticker"))
    pos_summary = [{
        "ticker": p["ticker"], "name": p.get("name", ""),
        "shares": p.get("shares"), "value_jpy": p.get("value_jpy"),
        "unrealized_pct": round((p.get("unrealized_pct") or 0) * 100, 1),
        "unrealized_jpy": p.get("unrealized_jpy"),
        "holding_days": p.get("holding_days"), "entry_date": p.get("entry_date", ""),
        "stop_loss": p.get("stop_loss"),
        "investment_type": "medium（目標6〜18ヶ月）",
    } for p in positions]

    # Medium 層のリバランス情報（drift チェック）
    rebalance_medium = data.get("rebalance_medium", {})

    # GINN-enhanced volatility per position
    _medium_tickers = [p["ticker"] for p in positions]
    _ginn_vol_str, _ginn_vol_dict = _compute_ginn_vol(_medium_tickers)

    # テクニカル状態を抽出（Medium保有銘柄のみ）
    medium_tickers = [p.get("ticker", p.get("symbol", "")) for p in positions]
    tech_text = _fmt_technical_state(medium_tickers, data.get("technical_state", {}))
    social_text_med = _fmt_social_sentiment(medium_tickers, data.get("social_sentiment", {}))

    news_text = fmt_news_section(data["news"], tickers=medium_tickers + ["6762.T"])
    earnings_text = fmt_earnings_section(data.get("earnings", {}), tickers=["META", "RCL"])

    # 長期スクリーニングで合格済みの銘柄情報を注入（矛盾した売り推奨を防ぐ）
    lt_passed = data.get("screening", {}).get("long_term", {}).get("passed", [])
    _med_ticker_set = {p.get("ticker") for p in positions}
    lt_context_items = [c for c in lt_passed if c.get("ticker") in _med_ticker_set]
    lt_context = ""
    if lt_context_items:
        _lt_lines = ["### 長期スクリーニング評価（保有中Medium銘柄）",
                     "※以下の銘柄は長期スクリーニングで合格済み。売却推奨には「スクリーニング時から状況が変わった」具体的根拠が必要。"]
        for c in lt_context_items:
            _lt_lines.append(f"  {c.get('ticker')}: score={c.get('score','?')} EPS成長={c.get('eps_growth','?')} ROE={c.get('roe','?')}")
        lt_context = "\n".join(_lt_lines)

    prompt = f"""## Mediumティア（戦術枠 / 6〜18ヶ月）の分析
※市場環境・レジーム・シナリオはキャッシュコンテキスト参照。

{earnings_text}

### 現在のポジション
{json.dumps(pos_summary, ensure_ascii=False)}

### ドリフト状況（目標ウェイトとの乖離）
{json.dumps(rebalance_medium, ensure_ascii=False)}

### アクティブシグナル {signals_stale_warn}
生成時刻: {data.get('signals_generated_at', '不明')}
{json.dumps(signals, ensure_ascii=False)}

### スクリーニング候補（モメンタム・短期）
{json.dumps(candidates, ensure_ascii=False)}

    ### 現金残高
    {json.dumps(data.get('cash_info', {}), ensure_ascii=False)}

{_ginn_vol_str}

{tech_text}

{social_text_med + chr(10) if social_text_med else ""}{lt_context + chr(10) if lt_context else ""}
### 関連銘柄ニュース
{news_text}

---
Mediumティアとして以下のJSON形式で分析してください:
※楽天証券は米国株の端株取引不可。amount_hint は必ず整数株単位（"1株"、"2株"など）で記載すること。
⚠️ 新規購入クーリング期間ルール: DONE_LIST に直近14日以内の買い執行記録がある銘柄に対して trim / sell / stop_loss を推奨してはならない。ただし、-10%以上の含み損が発生している場合のみ例外とする。執行済み買いポジションは必ず hold_notes に記載すること。holding_days だけでは判断しないこと（インポートや手動入力で不正確な場合があるため）。
⚠️ stop_loss 全体禁止ルール: tunable_params の disable_stop_loss_recommendations=true（既定）の場合、type=stop_loss も「逆指値発注」を含む sell も**一切提案してはならない**。ユーザーが broker で手動 SL 管理しているため、AI 推奨は再提案ループの原因になる。違反した場合はシステムが自動除去する。
{{
  "health": "good|caution|critical",
  "health_reason": "根拠",
  "summary": "3文以内",
  "priority_actions": [{{"rank":1,"urgency":"high|medium|low","type":"buy|add|sell|trim|stop_loss|take_profit","ticker":"XXX","action":"具体的なアクション","reason":"根拠","amount_hint":"整数株単位のみ","return_20d_rank":"top|middle|bottom","confidence_pct":75}}],
  "hold_notes": ["保有継続すべき銘柄とその理由（アクション不要のもの）"],
  "profit_taking": [{{"ticker":"XXX","reason":"理由","target_pct":0}}],
  "new_entries": [{{"ticker":"XXX","reason":"理由","risk_level":"medium|high","entry_condition":"条件"}}],
  "medium_high_return_strategy": "戦略案",
  "watchlist_alert": "注意事項",
  "news_impact": "ニュース影響",
  "signals_quality": "シグナル品質"
}}"""

    try:
        result = _call_sonnet_tier_json(
            "tier_analysis_medium",
            prompt,
            shared_ctx,
            "Medium分析",
        )
        if not isinstance(result, dict) or not result:
            raise RuntimeError("Sonnet returned empty result (possible max_tokens truncation)")
        result["ginn_vol"] = _ginn_vol_dict
        # Fix H (2026-04-20): 観測性 — 使用モデル ID を記録
        try:
            from model_router import get_model as _gm
            result.setdefault("model_used", _gm("tier_analysis_medium"))
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"  ⚠️ Medium分析エラー: {e}")
        return {"error": str(e), "health": "caution", "summary": "分析エラー", "priority_actions": [], "ginn_vol": _ginn_vol_dict}


def _analyze_margin_long(data: dict, shared_ctx: str = "") -> dict:
    """信用買い一次判断。設定モデルが book-aware に評価し、最終 Opus が採否を決める。"""
    margin = data.get("margin", {}) or {}
    screening = data.get("screening", {}) or {}
    raw_candidates = screening.get("margin_long_candidates", []) or []
    blocked = bool(screening.get("margin_long_blocked", False))

    ranked = sorted(
        [c for c in raw_candidates if isinstance(c, dict)],
        key=lambda x: -(x.get("score") or x.get("composite_score") or 0),
    )
    top = ranked[:8]
    jp_extra = [
        c for c in ranked
        if str(c.get("ticker") or "").endswith(".T")
        and c.get("ticker") not in {x.get("ticker") for x in top}
    ][:5]
    candidates = top + jp_extra

    margin_detail = {
        "blocked": blocked,
        "block_reason": screening.get("margin_long_block_reason", ""),
        "status": margin.get("margin_status", "safe"),
        "maintenance_ratio": margin.get("maintenance_ratio"),
        "collateral": margin.get("collateral", 0),
        "total_unrealized": margin.get("total_unrealized", 0),
        "open_positions_count": len(margin.get("open_positions", []) or []),
        "alerts": margin.get("alerts", []),
    }

    if blocked or not candidates:
        return {
            "health": "good" if not blocked else "caution",
            "margin_health": margin_detail.get("status", "safe"),
            "summary": "信用買い候補なし" if not blocked else f"信用買いブロック: {margin_detail.get('block_reason')}",
            "priority_actions": [],
            "margin_long_picks": [],
            "margin_actions": [],
            "_source": "local:no_candidates",
        }

    prompt = f"""## 信用買い一次判断
※あなたは信用買い・レバレッジ活用だけを担当する一次アナリストです。
※Long/Medium/Swingの通常現物判断は別Sonnetが担当します。
※最終採否はOpusが行うため、ここでは候補の質・期待alpha・信用リスクを率直に評価してください。

### 信用買い候補
{json.dumps(candidates, ensure_ascii=False)}

### 信用建玉・証拠金状況
{json.dumps(margin_detail, ensure_ascii=False)}

### 現金残高・攻めモード入力
{json.dumps(data.get('cash_info', {}), ensure_ascii=False)}

### 判断ルール
- margin_health が warning/danger/emergency の場合、新規 margin_buy は原則禁止。
- 候補 score≥100 でも、金利コスト・流動性・決算・ボラティリティに見合わなければ reject/hold でよい。
- type は `margin_buy` または `buy` のみ。現金で十分かつレバレッジ不要なら `buy` とする。
- 投信 (SLIM_/MNXACT/IFREE_/NOMURA_) は信用買い不可。
- 日本株の信用買いは通常100株単位。現物 buy の場合、ローカルのかぶミニ対象台帳で確認できる銘柄だけ `execution_channel="rakuten_kabu_mini_open"` を付けて1株単位で提案可（信用買いには使わない）。

以下のJSON形式で回答してください:
{{
  "health": "good|caution|critical",
  "margin_health": "safe|warning|danger|emergency",
  "summary": "3文以内",
  "margin_long_picks": [{{"rank":1,"ticker":"XXX","strategy":"戦略名","reason":"採用/監視理由","stop_loss_pct":-7,"urgency":"high|medium|low","score":120,"confidence_pct":70}}],
  "priority_actions": [{{"rank":1,"urgency":"high|medium|low","type":"margin_buy|buy","ticker":"XXX","action":"具体的なアクション","reason":"根拠","amount_hint":"整数株単位のみ","return_20d_rank":"top|middle|bottom","confidence_pct":75}}],
  "margin_actions": [{{"urgency":"high|medium|low","action":"証拠金/信用枠に関する管理アクション","reason":"根拠"}}],
  "risk_warnings": ["信用買い固有の注意点"],
  "no_trade_rationale": "全候補見送りの場合の理由"
}}"""

    try:
        result = call_tier_analysis(
            _SYSTEM_SONNET, prompt,
            role="tier_analysis_margin_long",
            max_tokens=_tier_max_tokens(),
            cached_prefix=shared_ctx,
            request_timeout=_tier_llm_timeout_seconds(),
        )
        if not isinstance(result, dict) or not result or result.get("error"):
            raise RuntimeError(f"tier_analysis_margin_long empty/error: {result.get('error') if isinstance(result, dict) else 'non-dict'}")
        try:
            from model_router import get_model as _gm
            result.setdefault("model_used", _gm("tier_analysis_margin_long"))
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"  ⚠️ Margin_Long分析エラー: {e}")
        return {
            "error": str(e),
            "health": "caution",
            "margin_health": margin_detail.get("status", "safe"),
            "summary": "信用買い分析エラー",
            "priority_actions": [],
            "margin_long_picks": [],
        }


def _analyze_short_positions(data: dict, shared_ctx: str = "") -> dict:
    positions = [p for p in data["positions"] if p.get("investment_type") == "swing"]

    if not positions:
        return {"health": "good", "summary": "swingポジションなし", "priority_actions": [],
                "hold_notes": [], "loss_management": "なし", "ginn_vol": {}}

    pos_summary = [{
        "ticker": p["ticker"], "name": p.get("name", ""),
        "shares": p.get("shares"), "value_jpy": p.get("value_jpy"),
        "current_price": p.get("current_price"),
        "unrealized_pct": round((p.get("unrealized_pct") or 0) * 100, 1),
        "unrealized_jpy": p.get("unrealized_jpy"),
        "holding_days": p.get("holding_days"), "entry_date": p.get("entry_date", ""),
        "stop_loss": p.get("stop_loss"), "stop_loss_source": p.get("stop_loss_source", ""),
    } for p in positions]

    # GINN-enhanced volatility per position
    _swing_tickers = [p["ticker"] for p in positions]
    _ginn_vol_str, _ginn_vol_dict = _compute_ginn_vol(_swing_tickers)

    swing_tickers = [p.get("ticker", "") for p in positions]
    tech_text_swing = _fmt_technical_state(swing_tickers, data.get("technical_state", {}))
    social_text_swing = _fmt_social_sentiment(swing_tickers, data.get("social_sentiment", {}))

    news_text = fmt_news_section(data["news"], tickers=["CRWV", "NVDA", "META", "TSLA", "AMD"])
    earnings_text = fmt_earnings_section(data.get("earnings", {}), tickers=["CRWV", "NVDA", "META"])

    # スクリーニング結果から新規スイングエントリー候補を抽出
    # (A) Opus が BUY と判定した高確信候補
    # (B) WATCH でも 強気アナリストが BULLISH かつ score >= 25 の候補
    #     （bear/macro が空応答で Opus が保守的判定を下したケースの救済）
    screen_cands = list(data.get("screen_candidates", []) or [])
    _seen_screen_tickers = {
        c.get("ticker") for c in screen_cands if isinstance(c, dict) and c.get("ticker")
    }
    for c in data.get("screening", {}).get("jp_screen_candidates", []) or []:
        if isinstance(c, dict) and c.get("ticker") not in _seen_screen_tickers:
            screen_cands.append(c)
            _seen_screen_tickers.add(c.get("ticker"))
    buy_signals: list = []
    watch_signals: list = []
    for c in screen_cands:
        if not isinstance(c, dict):
            continue
        signal = str(c.get("ai_signal", "")).upper()
        conf   = c.get("ai_confidence") or 0
        score  = float(c.get("score") or 0)
        if signal == "BUY" and conf >= 60:
            buy_signals.append(c)
        elif signal == "WATCH" and _screen_candidate_has_bullish_support(c) and score >= 25:
            watch_signals.append(c)
    jp_watch_signals = []
    for c in screen_cands:
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker") or "")
        is_jp = ticker.endswith(".T") or c.get("screen_source") == "jp_only" or bool(c.get("is_japan"))
        if not is_jp:
            continue
        try:
            score = float(c.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        signal = str(c.get("ai_signal") or "").upper()
        deterministic_jp = not signal and score >= 25 and (
            c.get("screen_source") == "jp_only" or bool(c.get("is_japan"))
        )
        if (signal == "WATCH" and score >= 20) or deterministic_jp:
            item = dict(c)
            if deterministic_jp:
                item.setdefault("ai_signal", "deterministic")
            jp_watch_signals.append(item)
    screen_buy_text = ""
    if buy_signals or watch_signals or jp_watch_signals:
        lines = []
        if buy_signals:
            lines.append("### スクリーニングBUYシグナル（新規スイングエントリー候補）")
            lines.append("※強気レジーム下では type:\"buy\" で積極的にエントリー推奨すること。")
            for c in buy_signals[:5]:
                lines.append(
                    f"- {c.get('ticker','?')}: {c.get('strategy','?')} "
                    f"確信度{c.get('ai_confidence',0)}% RSI{c.get('rsi','?')} "
                    f"1m={c.get('mom_1m','?')}% SL={c.get('stop_loss_atr','?')} "
                    f"→ {c.get('ai_reason','')[:80]}"
                )
        if watch_signals:
            if buy_signals:
                lines.append("")
            lines.append("### スクリーニングWATCH（強気支持あり・条件付エントリー候補）")
            lines.append("※ Opus最終統合はWATCH判定だが、強気アナリストはBULLISH評価かつモメンタムスコア25以上。")
            lines.append("※ 強気レジーム下では type:\"buy\" urgency:\"low\" で条件付エントリー（寄付後確認、指値等）を推奨可能。")
            for c in watch_signals[:5]:
                db = c.get("ai_debate") or {}
                lines.append(
                    f"- {c.get('ticker','?')}: {c.get('strategy','?')} "
                    f"score={c.get('score','?')} RSI{c.get('rsi','?')} "
                    f"1m={c.get('mom_1m','?')}% SL={c.get('stop_loss_atr','?')} "
                    f"| bull: {str(db.get('bull','')).strip()[:70]}"
                )
        if jp_watch_signals:
            if lines:
                lines.append("")
            lines.append("### 日本株スクリーニングWATCH（比較対象・条件付候補）")
            lines.append("※ 日本株候補も米国株候補と同じ期待alpha基準で評価し、根拠が弱ければ採用しない。")
            for c in sorted(jp_watch_signals, key=lambda x: -(x.get("score") or 0))[:5]:
                db = c.get("ai_debate") or {}
                lines.append(
                    f"- {c.get('ticker','?')}: {c.get('strategy','?')} "
                    f"signal={c.get('ai_signal','?')} conf{c.get('ai_confidence','?')}% "
                    f"score={c.get('score','?')} RSI{c.get('rsi','?')} "
                    f"1m={c.get('mom_1m','?')}% | bull: {str(db.get('bull','')).strip()[:70]}"
                )
        screen_buy_text = "\n".join(lines) + "\n"

    prompt = f"""## Swingティア分析：保有ポジション管理 ＋ 新規スイング候補評価
※このSonnetは空売り・信用は扱わない。既存ポジションの継続/損切り判断と、スクリーニングBUYシグナルからの新規エントリー推奨を行う。
※市場環境・レジーム・シナリオはキャッシュコンテキスト参照。

{earnings_text}

### 現在のポジション（保有日数・損益・ストップロス含む）
{json.dumps(pos_summary, ensure_ascii=False)}
⚠️ 損切りラインの根拠を必ず確認すること。"stop_loss_source": "suggested" は自動計算値。
   含み損が-20%超の場合、継続理由を明確に示すか、損切りを強く推奨すること。

{screen_buy_text}
{_ginn_vol_str}

{tech_text_swing}

{social_text_swing + chr(10) if social_text_swing else ""}
### 関連銘柄ニュース
{news_text}

---
以下のJSON形式で分析してください:
※楽天証券は米国株の端株取引不可。amount_hint は必ず整数株単位で記載すること。
※スクリーニングBUYシグナルがある場合、強気レジーム下では新規エントリー（type:"buy"）を推奨すること。
※スクリーニングWATCHでも bull BULLISH かつ score≥25 の候補は、必ず type:"buy" urgency:"low" で条件付エントリー（寄付後の出来高・価格アクション確認、1%〜2%下での指値等）として priority_actions に1〜2件含めること。amount_hint は 1〜3株 の小ロットでよい。hold_notes への退避は禁止（保有していない候補を「保有継続」と扱うことはできない）。
{{
  "health": "good|caution|critical",
  "health_reason": "根拠",
  "summary": "3文以内",
  "priority_actions": [{{"rank":1,"urgency":"high|medium|low","type":"buy|sell|stop_loss|add","ticker":"XXX","amount_hint":"整数株単位のみ（楽天証券は端株不可）","action":"具体的なアクション","reason":"根拠","return_20d_rank":"top|middle|bottom","confidence_pct":75}}],
  "hold_notes": ["保有継続すべき銘柄とその理由（アクション不要のもの）"],
  "loss_management": "含み損管理戦略",
  "recovery_scenario": "回復条件",
  "stop_loss_alerts": ["即時損切りが必要なポジションと理由"],
  "news_impact": "ニュース影響"
}}"""

    try:
        # call_tier_analysis が model_router に従って adapter を自動選択（anthropic / deepseek）
        result = call_tier_analysis(
            _SYSTEM_SONNET, prompt,
            role="tier_analysis_short",
            max_tokens=6000,
            cached_prefix=shared_ctx,
            request_timeout=_tier_llm_timeout_seconds(),
        )
        if not isinstance(result, dict) or not result or result.get("error"):
            raise RuntimeError(f"tier_analysis_short empty/error: {result.get('error') if isinstance(result, dict) else 'non-dict'}")
        result["ginn_vol"] = _ginn_vol_dict
        # Fix H (2026-04-20): 観測性 — 使用モデル ID を記録
        try:
            from model_router import get_model as _gm
            result.setdefault("model_used", _gm("tier_analysis_short"))
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"  ⚠️ Short_Positions分析エラー: {e}")
        return {"error": str(e), "health": "caution", "summary": "分析エラー", "priority_actions": [], "ginn_vol": _ginn_vol_dict}


def _analyze_short_selling(data: dict, shared_ctx: str = "") -> dict:
    short_candidates = data["screening"]["short_candidates"][:8]
    margin = data["margin"]

    margin_detail = {
        "status": margin.get("margin_status", "safe"),
        "maintenance_ratio": margin.get("maintenance_ratio"),
        "collateral": margin.get("collateral", 0),
        "total_unrealized": margin.get("total_unrealized", 0),
        "open_positions_count": len(margin.get("open_positions", [])),
        "alerts": margin.get("alerts", []),
        "safety_levels": {
            "safe": "200%+", "warning": "130%（新規建て禁止）",
            "danger": "110%（追証リスク）", "emergency": "100%以下（強制決済）",
        },
    }

    news_text = fmt_news_section(data["news"], tickers=["TSLA", "NVDA", "META", "AMD", "MSTR", "ARKK"])
    earnings_text_ss = fmt_earnings_section(data.get("earnings", {}))
    short_sell_tickers = [c.get("ticker", "") for c in data.get("screening", {}).get("short_candidates", [])[:10] if c.get("ticker")]
    social_text_short = _fmt_social_sentiment(short_sell_tickers, data.get("social_sentiment", {}))
    ss_tickers = [c.get("ticker", "") for c in data["screening"].get("short_candidates", [])[:8]]
    _ginn_vol_str_ss, _ = _compute_ginn_vol(ss_tickers)

    prompt = f"""## 空売り戦略 + 信用建玉管理の専門分析
※この一次判断は空売り（ショートセリング）と信用建玉リスクを担当する。投機ロングポジションは別ティアが担当。
※市場環境・レジーム・シナリオはキャッシュコンテキスト参照。

### 空売りスクリーニング候補
{json.dumps(short_candidates, ensure_ascii=False)}

### 信用建玉・証拠金状況
{json.dumps(margin_detail, ensure_ascii=False)}
⚠️ maintenance_ratio が 130% 未満 → 新規空売り禁止。110% 未満 → 追証アラート発令。

{earnings_text_ss}

{_ginn_vol_str_ss}

{social_text_short + chr(10) if social_text_short else ""}
### 空売り関連ニュース
{news_text}

---
以下のJSON形式で分析してください:
{{
  "margin_health": "safe|warning|danger|emergency",
  "margin_summary": "証拠金評価",
  "short_opportunities": [{{"rank":1,"ticker":"XXX","urgency":"high|medium|low","entry_zone":"価格帯","target_price":"目標","stop_loss":"損切り","rsi":null,"risk_reward":"1:X","catalyst":"触媒","reason":"根拠","return_20d_rank":"20営業日後の相対順位(top=上位30%/middle=中位/bottom=下位30%)","confidence_pct":"確信度(0-100の数値)"}}],
  "margin_actions": [{{"urgency":"high|medium|low","action":"アクション","reason":"根拠"}}],
  "crisis_strategy": "急落時戦略",
  "short_not_recommended": "禁止理由",
  "news_impact": "ニュース影響"
}}"""

    try:
        # call_tier_analysis が model_router に従って adapter を自動選択（anthropic / deepseek）
        result = call_tier_analysis(
            _SYSTEM_SONNET, prompt,
            role="tier_analysis_shortsell",
            max_tokens=_tier_max_tokens(),
            cached_prefix=shared_ctx,
            request_timeout=_tier_llm_timeout_seconds(),
        )
        # Fix H (2026-04-20): 観測性 — 使用モデル ID を記録
        if isinstance(result, dict) and not result.get("error"):
            try:
                from model_router import get_model as _gm
                result.setdefault("model_used", _gm("tier_analysis_shortsell"))
            except Exception:
                pass
        return result
    except Exception as e:
        return {"error": str(e), "margin_health": "warning", "summary": "分析エラー（マージン状況を手動確認してください）"}


# ── Red Team OpenAI互換ヘルパー ──────────────────────────────────
def _call_openai_compat_redteam(base_url: str, api_key: str, model_id: str,
                                 system: str, user: str) -> dict:
    """OpenAI 互換 API（DeepSeek / Groq / Gemini / Qwen）で Red Team 仮説を生成。

    Privacy: 送信は ``almanac.llm_safety.call_external_llm`` 経由に限定する。payload
    は ``anonymized_market_gap``（公開市場情報のみ）として検証され、保有・損益・
    サイズ等の book が混入していれば PrivacyViolation で fail-closed する
    （外部に送らず空の attacks を返す）。token usage は logs/llm_calls.jsonl に記録。
    """
    import re as _re
    try:
        from almanac.llm_safety import Payload, call_external_llm
        res = call_external_llm(
            Payload(kind="anonymized_market_gap", system=system, user=user),
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
            role="red_team",
            max_tokens=1200,
            # P3-16: deterministic モード時は 0、通常時は 0.7
            temperature=__import__('utils').get_llm_temperature(default=0.7),
        )
        raw = res.content or ""
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
        if not m:
            m = _re.search(r"(\{.*\})", raw, _re.DOTALL)
        if m:
            parsed = json.loads(m.group(1))
            if parsed.get("attacks"):
                return parsed
    except Exception as e:
        # PrivacyViolation を含め fail-closed（送信せず空を返す）。型名でガード作動を可視化。
        print(f"  [RedTeam/{model_id}] スキップ: {type(e).__name__}: {str(e)[:120]}")
    return {"attacks": [], "underutilized": []}


# ── Red Team アタッカー（最大リターン追求仮説生成） ──────────────
def _build_tier_hints_block(tier_hints: dict | None) -> str:
    """
    Sonnet ティア分析の high_return_* を Red Team プロンプト用の参考ブロックに整形。
    tier_hints: {"long": str|None, "medium": str|None, "short": str|None}
    値が None / 空文字列の項目は出力しない。全項目欠損なら空文字列を返す。
    """
    if not tier_hints:
        return ""
    lines = []
    if tier_hints.get("long"):
        lines.append(f"- Long ティア（Sonnet 自己整合性合成）: {tier_hints['long']}")
    if tier_hints.get("medium"):
        lines.append(f"- Medium ティア（Sonnet）: {tier_hints['medium']}")
    if tier_hints.get("short"):
        lines.append(f"- Short/Swing ティア（Sonnet）: {tier_hints['short']}")
    if not lines:
        return ""
    return (
        "\n### Sonnet ティア分析が示唆したハイリターン候補（参考）\n"
        + "\n".join(lines)
        + "\n→ 上記を踏まえ、Sonnet が見落とした角度や現ポジで未カバーの機会を優先して攻撃仮説に組み込め。\n"
          "  Sonnet と完全に同じ提案を繰り返すのではなく、補完・拡張・反証の視点で攻めること。\n"
    )


def _build_anonymized_market_gap_user(shared_ctx: str = "") -> str:
    """Build the PUBLIC-only Red Team prompt for external (non-Anthropic) models.

    Privacy boundary (see ``almanac/llm_safety.py``): external models must never
    receive the book — no holdings, position sizes, unrealized P&L, or beliefs.
    They get the public market environment only and propose aggressive ideas
    from the liquid public universe; Opus later reconciles against the actual
    portfolio. The book-aware "攻め不足" analysis stays on the Haiku (Anthropic)
    leg via :func:`_analyze_redteam`, where full holdings context is permitted.
    """
    return f"""## Red Team — 最大リターン追求仮説生成（公開情報のみ）
あなたの役割: 現在の市場環境から、最大リターンを狙える積極的な投資仮説を3つ提示する。
あなたには個別ポートフォリオの非公開情報は一切渡されない（公開市場情報のみで判断する）。後段でOpusが実際の状況と照合しリスク検証する。

### 市場環境（抜粋・公開情報）
{shared_ctx[:1500]}

### 指示
1. 現在の市場環境で見落とされがちな積極リターン機会を3点挙げよ
2. 最大リターンを追求する具体的な投資仮説を3つ提示（ticker + action + 期待リターン%）
3. 仮説ごとにリスク要因を1文で明記（Opusが検証しやすくするため）

required JSON output:
{{
  "attacks": [
    {{
      "ticker": "NVDA",
      "action": "具体的なアクション",
      "expected_return_pct": 35,
      "rationale": "根拠1文",
      "risk_note": "主なリスク1文",
      "model": "モデル名"
    }}
  ],
  "underutilized": ["市場で見落とされがちな積極機会1", "機会2", "機会3"]
}}"""


def _analyze_redteam(data: dict, shared_ctx: str = "", beliefs: list | None = None,
                     tier_hints: dict | None = None) -> dict:
    """
    Red Teamエージェント: リスクペナルティなしで最大リターン追求仮説を3つ生成。
    温度相当の多様性を出すため claude-haiku を使用。
    後段 Opus が検証・採択を判断する（Phase 2D）。
    tier_hints: Sonnet ティア分析が出した high_return_* を Red Team の参考情報として注入。
    """
    positions = data.get("positions", [])
    pos_summary = [
        {"ticker": p.get("ticker"), "tier": p.get("investment_type"),
         "unrealized_pct": round((p.get("unrealized_pct") or 0) * 100, 1),
         "value_jpy": p.get("value_jpy")}
        for p in positions[:12]
    ]
    beliefs_ctx = _format_beliefs_context(beliefs or [], max_items=5)
    tier_block  = _build_tier_hints_block(tier_hints)

    prompt = f"""## Red Team — 最大リターン追求仮説生成
あなたの役割: 現ポートフォリオの "攻め不足" を洗い出し、最大リターンを狙う3つの仮説を提示する。
リスク計算は不要。後段のOpusが最終リスク検証を行う。

{beliefs_ctx}
{tier_block}
### 現在のポジション概要
{json.dumps(pos_summary, ensure_ascii=False)}

### 市場環境（抜粋）
{shared_ctx[:1500]}

### 指示
1. このポートフォリオの「守りすぎ」「攻め不足」な点を3点挙げよ
2. 最大リターンを追求するなら今すぐ何をすべきか — 3つの具体的な投資仮説（ticker + action + 期待リターン%）を提示
3. 仮説ごとにリスク要因を1文で明記（Opusが検証しやすくするため）

required JSON output:
{{
  "attacks": [
    {{
      "ticker": "NVDA",
      "action": "具体的なアクション",
      "expected_return_pct": 35,
      "rationale": "根拠1文",
      "risk_note": "主なリスク1文"
    }}
  ],
  "underutilized": ["攻め不足な点1", "攻め不足な点2", "攻め不足な点3"]
}}"""

    try:
        result = call_claude(
            system="あなたは攻撃的なリターン追求型の投資アナリストです。リスクペナルティを持たず最大リターンの可能性を率直に提示してください。後段でOpusが最終リスク検証を行います。",
            user=prompt,
            model="claude-haiku-4-5-20251001",
            max_tokens=_redteam_max_tokens(),
            use_tool=True,
        )
        if isinstance(result, dict) and result.get("attacks"):
            print(f"  ⚔️ Red Team仮説: {len(result['attacks'])}件生成")
            return result
        return {"attacks": [], "underutilized": []}
    except Exception as e:
        print(f"  ⚠️ Red Team分析スキップ: {e}")
        return {"attacks": [], "underutilized": []}


# ── Red Team マルチモデル版 ───────────────────────────────────────
def _analyze_redteam_multi(data: dict, shared_ctx: str = "",
                            beliefs: list | None = None,
                            tier_hints: dict | None = None) -> dict:
    """
    4モデル並列 Red Team:
      - Claude Haiku  (call_claude / tool-use JSON)
      - DeepSeek V3   (DEEPSEEK_API_KEY, $0.14/1M tokens)
      - Groq Llama-3.3-70B (GROQ_API_KEY, 無料枠)
      - Gemini 2.0 Flash   (GEMINI_API_KEY, 無料枠)
    キー未設定のプロバイダはスキップ。全モデルの attacks を統合して返す。
    tier_hints: Sonnet ティア分析が出した high_return_* を全モデルの prompt に注入。
    """
    # Privacy (almanac/llm_safety.py): the external Red Team legs
    # (DeepSeek / Groq / Gemini / Qwen) receive PUBLIC market context ONLY —
    # never the book (holdings / sizes / P&L / beliefs). The book-aware
    # "攻め不足" analysis stays on the Haiku (Anthropic) leg via _analyze_redteam,
    # which builds its own holdings-aware prompt below. `data` / `beliefs` /
    # `tier_hints` are therefore consumed only by _haiku() from here on.
    _SYSTEM = ("あなたは攻撃的なリターン追求型の投資アナリストです。"
               "リスクペナルティを持たず最大リターンの可能性を率直に提示してください。"
               "後段でOpusが最終リスク検証を行います。")
    # shared_ctx itself carries book-derived figures (portfolio risk JSON,
    # stress-test 推定損失, guard state). Build a PUBLIC-only context for the
    # external legs so none of that leaves the process. (P1 R-round fix)
    _USER = _build_anonymized_market_gap_user(_build_public_market_context(data))

    def _haiku():
        return _analyze_redteam(data, shared_ctx, beliefs, tier_hints=tier_hints)

    def _deepseek():
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            return {"attacks": [], "underutilized": []}
        return _call_openai_compat_redteam(
            base_url="https://api.deepseek.com",
            api_key=key, model_id="deepseek-chat",
            system=_SYSTEM, user=_USER,
        )

    def _groq():
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            return {"attacks": [], "underutilized": []}
        return _call_openai_compat_redteam(
            base_url="https://api.groq.com/openai/v1",
            api_key=key, model_id="llama-3.3-70b-versatile",
            system=_SYSTEM, user=_USER,
        )

    def _gemini():
        """
        Red Team macro 視点。Gemini 無料枠 quota=0 の運用が続いているため、
        2026-04-20 Fix F: Gemini で attacks が得られなかった場合は
        DeepSeek R1 (`deepseek-reasoner`) にフォールバックして推論特化モデルの
        マクロ視点を代替取得する（model_router の deepseek_r を再利用）。
        """
        # GEMINI_API_KEY / GOOGLE_AI_API_KEY の両方受け入れ（secrets の慣習に合わせる）
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY") or ""
        result: dict = {"attacks": [], "underutilized": []}

        if key:
            # model_router 経由でモデル ID を解決（現行: gemini-flash-latest）
            try:
                from model_router import get_model as _gm
                _gemini_model = _gm("red_team_3")
            except Exception:
                _gemini_model = "gemini-flash-latest"
            result = _call_openai_compat_redteam(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=key, model_id=_gemini_model,
                system=_SYSTEM, user=_USER,
            )

        # Gemini quota 切れ or 未設定時の fallback: DeepSeek R1 (推論特化)
        if not result.get("attacks"):
            dk = os.environ.get("DEEPSEEK_API_KEY", "")
            if dk:
                print("  [RedTeam/gemini] attacks=0 → DeepSeek R1 にフォールバック")
                try:
                    from model_router import MODEL_REGISTRY as _REG
                    _r1_model = _REG.get("deepseek_r", "deepseek-reasoner")
                except Exception:
                    _r1_model = "deepseek-reasoner"
                result = _call_openai_compat_redteam(
                    base_url="https://api.deepseek.com",
                    api_key=dk, model_id=_r1_model,
                    system=_SYSTEM, user=_USER,
                )
        return result

    def _qwen():
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            return {"attacks": [], "underutilized": []}
        return _call_openai_compat_redteam(
            base_url="https://openrouter.ai/api/v1",
            api_key=key, model_id="qwen/qwen3-235b-a22b-2507",
            system=_SYSTEM, user=_USER,
        )

    from concurrent.futures import ThreadPoolExecutor as _TPE, wait as _cf_wait
    providers = {"haiku": _haiku, "deepseek": _deepseek, "groq": _groq, "gemini": _gemini, "qwen": _qwen}
    results: dict[str, dict] = {}
    _RT_TIMEOUT = 120  # Red Team 全体で最大 2 分
    _ex_rt = _TPE(max_workers=len(providers))
    futs = {_ex_rt.submit(fn): name for name, fn in providers.items()}
    _done_rt, _not_done_rt = _cf_wait(futs, timeout=_RT_TIMEOUT)
    for fut in _done_rt:
        name = futs[fut]
        try:
            res = fut.result(timeout=1)
            if not isinstance(res, dict):
                res = {"attacks": [], "underutilized": []}
            results[name] = res
            atk_n = len(res.get("attacks", []))
            if atk_n == 0:
                print(f"  [RedTeam/{name}] attacks=0 (provider 返却空 or キー未設定)")
            else:
                print(f"  [RedTeam/{name}] attacks={atk_n}")
        except Exception as e:
            print(f"  [RedTeam/{name}] タイムアウト/エラー: {type(e).__name__}: {str(e)[:120]}")
            results[name] = {"attacks": [], "underutilized": []}
    for fut in _not_done_rt:
        name = futs[fut]
        print(f"  [RedTeam/{name}] タイムアウト: {_RT_TIMEOUT}s 超過 → スキップ")
        results[name] = {"attacks": [], "underutilized": []}
    _ex_rt.shutdown(wait=False)

    # マージ（ticker+action先頭20字で重複除去、最大12件）
    seen: set[str] = set()
    merged_attacks: list[dict] = []
    merged_under: list[str] = []
    for name, res in results.items():
        for atk in res.get("attacks", []):
            key_ = f"{atk.get('ticker','')}-{(atk.get('action','') or '')[:20]}"
            if key_ not in seen:
                seen.add(key_)
                atk["model"] = name
                merged_attacks.append(atk)
        merged_under.extend(res.get("underutilized", []))

    merged_attacks = merged_attacks[:12]
    seen_u: set[str] = set()
    deduped_under: list[str] = []
    for u in merged_under:
        if u not in seen_u:
            seen_u.add(u)
            deduped_under.append(u)

    active = [k for k, v in results.items() if v.get("attacks")]
    print(f"  ⚔️ Red Team マルチモデル: {len(merged_attacks)}件（{', '.join(active)}）")
    return {"attacks": merged_attacks, "underutilized": deduped_under[:6]}


# ── DeepSeek-R1 Judge Agent ──────────────────────────────

def _r1_judge_transport(*, base_url: str, api_key: str, model_id: str,
                        system: str, user: str, max_tokens: int, temperature: float):
    """Transport for the DeepSeek-R1 Judge via almanac.llm_safety.call_external_llm.

    R1 has no system role (the original code merged system+user into one user
    message) and returns its answer in ``content`` with a ``reasoning_content``
    fallback — both preserved here. ``temperature`` is accepted but unused (R1).
    """
    from openai import OpenAI as _OAI
    client = _OAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model_id,
        messages=[{"role": "user", "content": f"{system}\n\n{user}"}],
        max_tokens=max_tokens,
    )
    _msg = resp.choices[0].message
    raw = _msg.content or ""
    if not raw and getattr(_msg, "reasoning_content", None):
        raw = _msg.reasoning_content
    usage = getattr(resp, "usage", None)
    return raw, {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }


def _judge_sonnet_outputs(long_a: dict, medium_a: dict,
                           sp_a: dict, ml_a: dict, ss_a: dict,
                           redteam_a: dict) -> str:
    """
    DeepSeek-R1でSonnet×3 + book-aware×2 出力のクロスバリデーション。

    Privacy: 外部 (DeepSeek R1) には実 ticker と自由記述 (reason/rationale) を渡さない。
    ticker は T1/T2/... に擬名化し自由記述は落として「構造」だけ送る (kind=
    anonymized_recommendations, almanac.llm_safety 経由)。Judge 出力の擬名はローカルで
    実 ticker に復元する。これで保有銘柄の外部漏洩を防ぎつつ矛盾/過信/合意の構造検査は維持。
    失敗時は空文字を返し、現行動作にフォールバック。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return ""

    def _extract_actions(analysis, tier_name):
        actions = analysis.get("priority_actions", []) if isinstance(analysis, dict) else []
        return [{"tier": tier_name, "ticker": a.get("ticker"),
                 "type": a.get("type"), "urgency": a.get("urgency"),
                 "confidence_pct": a.get("confidence_pct")}
                for a in actions[:8]]

    all_actions = (
        _extract_actions(long_a, "Long") +
        _extract_actions(medium_a, "Medium") +
        _extract_actions(sp_a, "Short") +
        _extract_actions(ml_a, "MarginLong") +
        _extract_actions(ss_a, "ShortSelling")
    )
    rt_top = redteam_a.get("attacks", [])[:5]

    # Privacy: ticker を T1/T2/... に擬名化し、自由記述(reason/rationale)は送らない。
    # 保有銘柄(= tiers の ticker)を外部 R1 に出さず、構造検査だけ依頼する。
    pseudo: dict[str, str] = {}

    def _ps(tk):
        if not tk:
            return None
        if tk not in pseudo:
            pseudo[tk] = f"T{len(pseudo) + 1}"
        return pseudo[tk]

    actions_anon = [{"tier": a["tier"], "ticker": _ps(a.get("ticker")),
                     "type": a.get("type"), "urgency": a.get("urgency"),
                     "confidence_pct": a.get("confidence_pct")} for a in all_actions]
    rt_anon = [{"ticker": _ps(a.get("ticker")),
                "expected_return_pct": a.get("expected_return_pct")} for a in rt_top]
    inverse = {v: k for k, v in pseudo.items()}

    _SYS = "あなたは投資判断の品質管理審査官です。複数のアナリストの推奨を精査し、矛盾・論理的欠陥・過信を検出してください。"
    _USER = f"""## 一次判断のクロスバリデーション（ticker は匿名ラベル T1, T2, ...）

### 全ティアのアクション一覧
{json.dumps(actions_anon, ensure_ascii=False, indent=1)}

### Red Team仮説（上位5件・匿名ラベル）
{json.dumps(rt_anon, ensure_ascii=False, indent=1)}

### 審査項目
1. **矛盾検出**: 同一ラベルに対し異なるティアが矛盾する方向（buy vs sell/trim）を推奨していないか
2. **過信フラグ**: confidence_pct > 85 のアクションに十分な根拠があるか
3. **欠落リスク**: Red Team が提示したラベルがどのティアにも反映されていないケース
4. **合意度ランキング**: 複数ティアが同方向のアクションを推奨しているものを「高合意」としてマーク

### 出力形式（JSON / ticker は与えられた匿名ラベルをそのまま使う）
{{
  "contradictions": ["T1: LongはbuyだがMediumはtrim — 矛盾点"],
  "overconfidence_flags": ["T2: confidence 90%だが根拠が薄い"],
  "unaddressed_risks": ["Red Team ラベル T5 がどのティアでも未検討"],
  "consensus_ranking": [
    {{"ticker": "T1", "direction": "buy", "agreeing_tiers": 3, "avg_confidence": 82}}
  ],
  "judge_summary": "全体評価を2文で"
}}"""

    try:
        import re as _re
        from almanac.llm_safety import Payload, call_external_llm
        res = call_external_llm(
            Payload(kind="anonymized_recommendations", system=_SYS, user=_USER),
            base_url="https://api.deepseek.com",
            api_key=key,
            model_id="deepseek-reasoner",
            role="judge",
            max_tokens=2000,
            transport=_r1_judge_transport,
        )
        raw = res.content or ""
        # 複数パターンで JSON 抽出を試みる
        judge = None
        for _pat in [
            r"```(?:json)?\s*(\{.*?\})\s*```",   # コードブロック
            r"(\{[^{}]*\"contradictions\"[^{}]*\})",  # contradictions キーを含む
            r"(\{.*?\})\s*$",                     # 末尾のJSONオブジェクト
            r"(\{.*\})",                           # 最初のJSONオブジェクト
        ]:
            _m = _re.search(_pat, raw, _re.DOTALL)
            if _m:
                try:
                    judge = json.loads(_m.group(1))
                    break
                except json.JSONDecodeError:
                    continue
        if judge:
            # 擬名ラベル (T1, T2, ...) を実 ticker に復元する。
            _pseudo_re = _re.compile(r"\bT(\d+)\b")

            def _restore(text: object) -> str:
                return _pseudo_re.sub(
                    lambda m: inverse.get(f"T{m.group(1)}", m.group(0)), str(text)
                )

            parts = []
            if judge.get("contradictions"):
                parts.append("### ⚠️ ティア間矛盾\n" + "\n".join(f"- {_restore(c)}" for c in judge["contradictions"]))
            if judge.get("overconfidence_flags"):
                parts.append("### 🔍 過信フラグ\n" + "\n".join(f"- {_restore(f)}" for f in judge["overconfidence_flags"]))
            if judge.get("unaddressed_risks"):
                parts.append("### 🛡️ 未検討リスク\n" + "\n".join(f"- {_restore(r)}" for r in judge["unaddressed_risks"]))
            if judge.get("consensus_ranking"):
                top3 = judge["consensus_ranking"][:3]
                parts.append("### ✅ 高合意アクション\n" + "\n".join(
                    f"- {inverse.get(c.get('ticker',''), c.get('ticker','?'))}: {c.get('direction','?')}（{c.get('agreeing_tiers',0)}ティア合意, 平均確信度{c.get('avg_confidence',0)}%）"
                    for c in top3))
            if judge.get("judge_summary"):
                parts.append(f"### 📋 Judge総評\n{_restore(judge['judge_summary'])}")
            result = "\n\n".join(parts)
            n_cont = len(judge.get("contradictions", []))
            n_over = len(judge.get("overconfidence_flags", []))
            print(f"  ⚖️ Judge完了: 矛盾{n_cont}件, 過信{n_over}件")
            return f"## ⚖️ DeepSeek-R1 Judge Report\n{result}" if result else ""
        print("  ⚠️ Judge: JSON抽出失敗（スキップ）")
        return ""
    except Exception as e:
        print(f"  ⚠️ Judge失敗（スキップ）: {e}")
        return ""


# ── シナリオモニタリングコンテキスト整形 ─────────────────

def _fmt_scenario_monitoring(sm: dict | None) -> str:
    """シナリオモニタリングデータをOpusプロンプト用テキストに整形する。
    readiness >= 60% (ACTIVE) のシナリオがある場合、強く意思決定への反映を促す。
    """
    if not sm or sm.get("error"):
        return ""

    lines = ["## 📡 マクロシナリオモニタリング（イベント駆動戦略）"]

    vix_d = sm.get("vix_detail", {})
    if vix_d:
        fg = vix_d.get("fear_greed_label", "N/A")
        fg_score = vix_d.get("fear_greed_score")
        fg_str = f"{fg}({fg_score})" if fg_score is not None else fg
        lines.append(
            f"恐怖&貪欲指数: {fg_str} / VIX: {vix_d.get('level','N/A')} "
            f"({vix_d.get('classification','N/A')}, 5日変化 {vix_d.get('change_5d','N/A')}%, "
            f"ピーク比 {vix_d.get('decay_from_peak_5d_pct','N/A')}%) / "
            f"VIX term: {vix_d.get('term_structure','N/A')} ratio={vix_d.get('term_ratio','N/A')} / "
            f"イールドスプレッド(10Y-3M): {vix_d.get('yield_spread_10y3m','N/A')}% / "
            f"WTI原油5日変化: {vix_d.get('oil_change_5d_pct','N/A')}% / "
            f"SPY vs MA50: {vix_d.get('spy_vs_ma50_pct','N/A')}%"
        )
        flows = vix_d.get("sector_flows") or []
        if flows:
            flow_bits = []
            for f in flows[:5]:
                if not isinstance(f, dict):
                    continue
                flow_bits.append(
                    f"{f.get('ticker')}:5d {f.get('return_5d_pct','?')}% "
                    f"vsSPY {f.get('vs_spy_5d_pct','?')}%"
                )
            if flow_bits:
                lines.append("セクターフロー: " + " / ".join(flow_bits))

    macro_d = sm.get("macro_detail", {})
    if macro_d:
        lines.append(
            "マクロ: "
            f"Fed {macro_d.get('fed_rate','N/A')}% / "
            f"10Y {macro_d.get('yield_10y','N/A')}% / "
            f"2Y {macro_d.get('yield_2y','N/A')}% / "
            f"CPI YoY {macro_d.get('cpi_yoy','N/A')}% / "
            f"失業率 {macro_d.get('unemp_rate','N/A')}% / "
            f"HY OAS {macro_d.get('hy_oas_bps','N/A')}bps"
        )

    tech_d = sm.get("technical_detail", {})
    breadth = tech_d.get("market_breadth") if isinstance(tech_d, dict) else None
    if isinstance(breadth, dict) and breadth:
        lines.append(f"テクニカル breadth: {json.dumps(breadth, ensure_ascii=False)[:300]}")

    active_sc = sm.get("active_scenarios", [])
    if active_sc:
        lines.append("\n### 発動中・監視中シナリオ")
        for sc in active_sc:
            status_icon = {"active": "🔴 ACTIVE", "partial": "⚡ PARTIAL(限定50%)"}.get(
                sc["status"], "🟡 WATCHING")
            lines.append(
                f"  {status_icon} [{sc['id']}] {sc['name']} — 準備度 {sc['readiness_pct']}% "
                f"({sc['signals_met']}/{sc['signals_total']} シグナル達成, priority={sc.get('priority','medium')})"
            )
            if sc.get("description"):
                lines.append(f"    説明: {str(sc['description'])[:180]}")
            if sc.get("first_detected"):
                lines.append(f"    初検知: {sc['first_detected']}")
            matched = sc.get("matched_signals") or []
            missing = sc.get("missing_signals") or []
            if matched:
                lines.append("    成立シグナル:")
                for sig in matched[:4]:
                    if isinstance(sig, dict):
                        lines.append(f"      - {sig.get('key','?')}: {sig.get('detail','')}")
            if missing:
                lines.append("    未達シグナル:")
                for sig in missing[:4]:
                    if isinstance(sig, dict):
                        lines.append(f"      - {sig.get('key','?')}: {sig.get('detail','')}")
            playbook_actions = sc.get("playbook_actions") or []
            if playbook_actions:
                lines.append("    プレイブック候補（仮説。採用は期待alpha/リスクで再評価）:")
                for act in playbook_actions[:10]:
                    if not isinstance(act, dict):
                        continue
                    ticker = act.get("ticker") or act.get("action") or "?"
                    allocation_usd = act.get("allocation_usd")
                    allocation_jpy = act.get("allocation_jpy")
                    if isinstance(allocation_usd, (int, float)):
                        alloc_txt = f" ${allocation_usd:,.0f}"
                    elif isinstance(allocation_jpy, (int, float)):
                        alloc_txt = f" ¥{allocation_jpy:,.0f}"
                    else:
                        alloc_txt = ""
                    tech = act.get("technical") if isinstance(act, dict) else {}
                    tech_txt = ""
                    if isinstance(tech, dict) and tech:
                        tech_txt = (
                            f" | tech price={tech.get('price')} RSI={tech.get('rsi')} "
                            f"5d={tech.get('change_5d_pct')}% 20d={tech.get('change_20d_pct')}% "
                            f"vol={tech.get('volume_ratio')} signal={tech.get('composite_signal')}"
                        )
                    lines.append(
                        f"      - {act.get('phase','?')} {ticker}{alloc_txt}: "
                        f"{act.get('reason','')}{tech_txt}"
                    )
            sell_triggers = sc.get("sell_triggers") or []
            if sell_triggers:
                lines.append("    売却トリガー:")
                for trigger in sell_triggers[:6]:
                    lines.append(f"      - {trigger}")
            confirmations = sc.get("confirmation_required") or []
            if confirmations:
                lines.append("    追加確認条件:")
                for item in confirmations[:6]:
                    lines.append(f"      - {item}")

        # ACTIVE（readiness >= 60%）があればプレイブック候補を厳格に採否判定
        active_list = [sc for sc in active_sc if sc["status"] == "active"]
        if active_list:
            lines.append(
                "\n⚠️ 【重要】以下のシナリオが ACTIVE 状態です。"
                "プレイブック候補を機械的に採用せず、期待alpha・リスク・既存ポジション・Policy制約を再評価し、"
                "採用できるものだけ priority_actions に変換してください："
            )
            for sc in active_list:
                lines.append(f"  - {sc['name']}（{sc['id']}）: Phase 1 実行タイミング")
        else:
            # WATCHING（readiness 30-59%）の場合は準備を促す
            watching_list = [sc for sc in active_sc if sc["status"] == "watching"]
            if watching_list:
                lines.append(
                    "\n💡 以下のシナリオが WATCHING 状態（準備度30〜59%）です。"
                    "成立シグナル/未達シグナルを使い、発動時の候補を opportunity_highlights または条件付き小ロット action として評価してください："
                )
                for sc in watching_list:
                    lines.append(f"  - {sc['name']}（{sc['id']}）: 残シグナルが揃い次第 Phase 1 実行へ")
    else:
        lines.append("現在アクティブ・監視中のシナリオなし（全て dormant）")

    geo_alerts = sm.get("geo_alerts", [])
    if geo_alerts:
        lines.append("\n### 地政学アラート（high以上・高確度のみ）")
        for ga in geo_alerts[:4]:
            sev_icon = {"medium": "🟡", "high": "🟠", "critical": "🔴"}.get(ga["severity"], "⚪")
            lines.append(f"  {sev_icon} [{ga['scenario']}] {ga['headline']} (信頼度 {ga.get('confidence', 0)*100:.0f}%)")

    lines.append(f"\n最終更新: {sm.get('evaluated_at', 'N/A')[:16]}")
    return "\n".join(lines)


def _fmt_ipo_watch_context(ipo_watch: dict | None) -> str:
    """Format alert-only IPO watch state for final synthesis context."""
    if not isinstance(ipo_watch, dict):
        return ""
    candidates = [
        row for row in (ipo_watch.get("candidates") or [])
        if isinstance(row, dict) and row.get("ticker")
    ]
    if not candidates:
        return ""
    lines = [
        "## 🆕 IPO Watch（alert-only / human onboarding required）",
        "※ 自動ユニバース追加・自動発注は禁止。採用時も推奨/検討まで。"
        " information_lane_verdicts に ipo_watch として adopt/reject/ignore を記録すること。",
    ]
    if ipo_watch.get("updated_at"):
        lines.append(f"- updated_at: {ipo_watch.get('updated_at')}")
    scan = ipo_watch.get("last_scan")
    if isinstance(scan, dict):
        lines.append(
            f"- last_scan: searched={scan.get('searched_items')} "
            f"extracted={scan.get('extracted_listings')} new={scan.get('new_candidates')}"
        )
    for row in candidates[:8]:
        lines.append(
            f"- {row.get('ticker')}: {row.get('company','')} "
            f"exchange={row.get('exchange','?')} ipo_date={row.get('ipo_date','?')} "
            f"confidence={row.get('confidence','?')} status={row.get('status','?')} "
            f"onboarding={row.get('onboarding_path','download_tickers.py:NEW_LISTINGS')} "
            f"{row.get('size_or_rank','')}"
        )
    return "\n".join(lines)


# ── Opus 最終合成 ─────────────────────────────────────────

def _is_synthesis_failure(synthesis: dict) -> bool:
    """最終合成のAPI/ツール失敗を no-trade と区別する。

    priority_actions=[] は正当な no-trade になり得るが、error 付きの空結果は
    モデル/API失敗なので分析キャッシュへ保存してはいけない。
    """
    if not isinstance(synthesis, dict):
        return True
    if not synthesis.get("error"):
        return False
    if synthesis.get("priority_actions"):
        return False
    if synthesis.get("hold_notes"):
        return False
    return True


_DEGRADED_CONFIDENCE_PENALTY = 15
_RISK_REDUCTION_ACTION_TYPES = {"sell", "trim", "stop_loss", "take_profit"}
_US_ACTION_TYPES = {"buy", "add", "margin_buy", "sell", "trim", "stop_loss", "take_profit", "short"}
_US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
}


def _tier_candidate_count(result: dict) -> int:
    if not isinstance(result, dict):
        return 0
    total = 0
    for key in ("priority_actions", "margin_long_picks", "short_opportunities", "margin_actions"):
        value = result.get(key)
        if isinstance(value, list):
            total += len([item for item in value if isinstance(item, dict)])
    return total


def _tier_failure_reason(name: str, result: dict) -> str | None:
    """Return a concise reason when a tier output is unusable for synthesis."""
    if not isinstance(result, dict):
        return "invalid_result"

    error = result.get("error")
    if error:
        return str(error)[:180]

    summary = str(result.get("summary") or "")
    summary_l = summary.lower()
    if "timeout" in summary_l or "タイムアウト" in summary:
        return summary[:180] or "timeout"

    health = str(result.get("health") or result.get("margin_health") or "").lower()
    if health in {"critical", "error", "failed"}:
        return f"health={health}"

    # A caution with no candidates is often a valid no-trade, so only classify it
    # as degraded when the text explicitly indicates an operational failure.
    if health == "caution" and _tier_candidate_count(result) == 0:
        if any(token in summary_l for token in ("error", "failed", "exception", "overload")) or "エラー" in summary:
            return summary[:180] or "health=caution"

    return None


def _build_degraded_mode_info(tier_results: dict[str, dict]) -> dict:
    failed = []
    for name, result in (tier_results or {}).items():
        reason = _tier_failure_reason(name, result)
        if reason:
            failed.append({"tier": name, "reason": reason})

    failed_names = {item["tier"] for item in failed}
    sonnet_failed = len({"Long分析", "Medium分析", "Swing分析"} & failed_names)
    deepseek_failed = len({"MarginLong分析", "ShortSell分析"} & failed_names)
    enabled = (
        len(failed) >= 2
        or sonnet_failed >= 2
        or (sonnet_failed >= 1 and deepseek_failed >= 1)
    )
    reason = ""
    if enabled:
        detail = ", ".join(f"{item['tier']}={item['reason']}" for item in failed[:5])
        reason = f"tier failures {len(failed)}/{max(len(tier_results or {}), 1)}: {detail}"

    return {
        "enabled": enabled,
        "failed_count": len(failed),
        "failed_tiers": failed,
        "confidence_penalty": _DEGRADED_CONFIDENCE_PENALTY if enabled else 0,
        "reason": reason,
    }


def _format_degraded_mode_context(info: dict) -> str:
    if not isinstance(info, dict) or not info.get("enabled"):
        return ""
    lines = [
        "## ⚠️ DEGRADED MODE（一次ティア分析の障害）",
        f"- reason: {info.get('reason')}",
        f"- confidence_penalty: -{info.get('confidence_penalty', _DEGRADED_CONFIDENCE_PENALTY)}pt",
        "→ priority_actions は件数制限だけで非表示化しない。出す場合は degraded_mode 下の不確実性を明記すること。",
        "→ 各 action には degraded_mode 下の不確実性を reason に反映し、過度に高い confidence を出さないこと。",
    ]
    return "\n".join(lines)


def _is_risk_reduction_action(action: dict) -> bool:
    if not isinstance(action, dict):
        return False
    atype = str(action.get("type") or "").lower()
    return atype in _RISK_REDUCTION_ACTION_TYPES


def _mark_degraded_telegram(synthesis: dict, info: dict) -> None:
    current = str(synthesis.get("telegram_message") or "").strip()
    header = f"⚠️ DEGRADED MODE: {info.get('failed_count', 0)} tier failures; actions annotated"
    if current.startswith("⚠️ DEGRADED MODE"):
        return
    synthesis["telegram_message"] = (header + ("\n" + current if current else ""))[:400]


def _prepend_telegram_header(synthesis: dict, header: str) -> None:
    current = str(synthesis.get("telegram_message") or "").strip()
    if current.startswith(header):
        return
    synthesis["telegram_message"] = (header + ("\n" + current if current else ""))[:400]


def _apply_degraded_mode(synthesis: dict, info: dict) -> dict:
    """Deterministically surface tier failures without hiding actions.

    This is intentionally code-side enforcement. Prompt instructions alone can be
    ignored by the synthesis model, which made tier failures look like normal
    operation in production output. Degraded mode should lower confidence and
    annotate uncertainty, but should not silently remove candidate actions.
    """
    if not isinstance(synthesis, dict) or not isinstance(info, dict) or not info.get("enabled"):
        return synthesis

    actions = synthesis.get("priority_actions")
    if not isinstance(actions, list):
        actions = []

    adjusted: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        item = dict(action)
        item["confidence_degraded"] = True
        item["degraded_reason"] = info.get("reason")
        conf = item.get("confidence_pct")
        if isinstance(conf, (int, float)):
            item["confidence_before_degraded"] = conf
            item["confidence_pct"] = max(0, min(100, int(round(conf - info.get("confidence_penalty", _DEGRADED_CONFIDENCE_PENALTY)))))
        adjusted.append(item)

    synthesis["priority_actions"] = adjusted
    synthesis["degraded_mode"] = True
    synthesis["degraded_reason"] = info.get("reason")
    synthesis["degraded_failed_tiers"] = info.get("failed_tiers", [])
    synthesis["degraded_action_policy"] = "annotate_only"
    if synthesis.get("health") == "good" or not synthesis.get("health"):
        synthesis["health"] = "caution"
    synthesis["health_reason"] = (
        f"DEGRADED MODE: {info.get('reason')}"
        if not synthesis.get("health_reason")
        else f"{synthesis.get('health_reason')} / DEGRADED MODE: {info.get('reason')}"
    )
    warnings = synthesis.setdefault("risk_warnings", [])
    if isinstance(warnings, list):
        warnings.append(f"DEGRADED MODE: {info.get('reason')}")
    _mark_degraded_telegram(synthesis, info)
    return synthesis


def _is_us_listed_action(action: dict) -> bool:
    ticker = str(action.get("ticker") or "").upper()
    if not ticker:
        return False
    if ticker.endswith(".T"):
        return False
    if ticker.startswith(("SLIM_", "IFREE_", "NOMURA_", "MNXACT")):
        return False
    atype = str(action.get("type") or action.get("action_type") or "").lower()
    return atype in _US_ACTION_TYPES


def _is_nyse_trading_day(now: datetime | None = None) -> tuple[bool, str, str]:
    current = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    ny_now = current.astimezone(ZoneInfo("America/New_York"))
    ny_date = ny_now.date()
    date_iso = ny_date.isoformat()
    if ny_now.weekday() >= 5:
        return False, date_iso, "weekend"
    try:
        import pandas_market_calendars as mcal  # type: ignore
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=date_iso, end_date=date_iso)
        return (not schedule.empty), date_iso, "pandas_market_calendars"
    except Exception:
        if date_iso in _US_MARKET_HOLIDAYS_2026:
            return False, date_iso, "static_holiday_calendar"
        return True, date_iso, "weekday_fallback"


def _annotate_us_holiday_actions(synthesis: dict, now: datetime | None = None) -> dict:
    """Annotate US-listed actions on NYSE holidays instead of hard-blocking."""
    if not isinstance(synthesis, dict):
        return synthesis
    actions = synthesis.get("priority_actions")
    if not isinstance(actions, list) or not actions:
        return synthesis
    is_open, ny_date, source = _is_nyse_trading_day(now)
    if is_open:
        return synthesis

    affected = 0
    for action in actions:
        if not isinstance(action, dict) or not _is_us_listed_action(action):
            continue
        affected += 1
        action["market_closed_degraded"] = True
        action["market_closed_date"] = ny_date
        action["market_closed_source"] = source
        note = f"NYSE休場({ny_date})のため、次営業日の寄付後に板・ギャップ確認して執行"
        action["execution_note"] = note
        reason = str(action.get("reason") or "")
        if note not in reason:
            action["reason"] = (reason + " / " + note).strip(" /")
        conf = action.get("confidence_pct")
        if isinstance(conf, (int, float)):
            action["confidence_before_market_closed"] = conf
            action["confidence_pct"] = max(0, min(100, int(round(conf - 10))))

    if affected:
        synthesis["us_market_closed"] = True
        synthesis["us_market_closed_date"] = ny_date
        synthesis["us_market_closed_affected_actions"] = affected
        warnings = synthesis.setdefault("risk_warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"NYSE休場({ny_date})のため米国株アクション {affected} 件は次営業日確認後に執行")
        _prepend_telegram_header(synthesis, f"⚠️ NYSE休場 {ny_date}: 米国株は次営業日確認")
    return synthesis


def _screen_candidate_has_bullish_support(candidate: dict) -> bool:
    """スクリーナー出力の強気根拠を複数スキーマで読む。"""
    if not isinstance(candidate, dict):
        return False
    debate = candidate.get("ai_debate") or {}
    if not isinstance(debate, dict):
        return False

    bull_view = str(debate.get("bull_view") or "").strip().upper()
    if bull_view in {"BULLISH", "STRONG_BULLISH", "BUY"}:
        return True

    bull_text = str(debate.get("bull") or "").strip()
    if not bull_text:
        return False
    return bull_text.lower() not in {"なし", "none", "n/a", "na", "-", "null"}


def _load_bl_views_for_opus() -> str:
    """Format BL views as a compact context string for Opus synthesis."""
    views_path = BASE_DIR / "bl_views.json"
    if not views_path.exists():
        return ""
    try:
        views_root = json.loads(views_path.read_text())
        if not views_root:
            return ""
        # bl_views.json の構造: {"views": {ticker: {...}}, "as_of": ..., "n_tickers": ...}
        views = views_root.get("views", views_root)
        if not views:
            return ""
        lines = ["【Black-Litterman LLMビュー（定量モデル期待リターン）】"]
        for ticker, v in list(views.items())[:10]:  # top 10
            if not isinstance(v, dict):
                continue
            mean_pct = round(v.get("mean_view", 0) * 100, 1)
            lines.append(f"  {ticker}: 期待リターン {mean_pct:+.1f}%（Ω={v.get('variance',0):.4f}）")
        return "\n".join(lines)
    except Exception:
        return ""


def _load_catalyst_context_for_opus(scenario: dict | None = None) -> str:
    """Run the catalyst layer and return a compact text block for Opus injection.

    AI autonomy v2: the pipeline runs, writes to ``catalyst_hypothesis_log.jsonl``
    for observability, and returns a capped review block for Opus by default.
    Set ``ALMANAC_DISABLE_CATALYST_CONTEXT=1`` to keep log-only shadow behavior.

    Fail-open: any exception returns ``""`` with a console warning so the
    daily analysis is never blocked by a catalyst pipeline failure.
    """
    try:
        import uuid as _uuid
        from almanac.observability.catalyst_layer import (
            compact_for_opus as _compact,
            run as _catalyst_run,
        )

        analysis_id = str(_uuid.uuid4())
        analysis_date = datetime.now().strftime("%Y-%m-%d")

        # Extract scenario readiness for the compact_for_opus admission threshold
        scenario_readiness = 0.0
        if scenario:
            raw_r = scenario.get("readiness") or scenario.get("readiness_pct") or 0
            try:
                scenario_readiness = float(raw_r)
                if scenario_readiness > 1.0:
                    scenario_readiness /= 100.0
            except (TypeError, ValueError):
                pass

        output = _catalyst_run(
            revision_state_path=BASE_DIR / "revision_state.json",
            scenario_state_path=BASE_DIR / "scenario_state.json",
            proxy_seed_map_path=BASE_DIR / "proxy_seed_map.json",
            legacy_analysis_path=BASE_DIR / "ai_portfolio_analysis.json",
            catalyst_log_path=BASE_DIR / "catalyst_hypothesis_log.jsonl",
            screener_payloads={
                "short": load_json(BASE_DIR / "short_candidates.json", {}),
                "margin_long": load_json(BASE_DIR / "margin_long_candidates.json", {}),
                "pair": load_json(BASE_DIR / "pair_trade_candidates.json", {}),
                "squeeze": load_json(BASE_DIR / "squeeze_candidates.json", {}),
            },
            analysis_id=analysis_id,
            analysis_date=analysis_date,
            write_log=True,
        )
        n = output.n_hypotheses_total
        print(f"  🔬 触媒レイヤー: {n} 件の仮説を生成")

        # Explicit opt-out: log only, do not inject.
        if get_env("ALMANAC_DISABLE_CATALYST_CONTEXT"):
            return ""

        return _compact(output, scenario_readiness=scenario_readiness)
    except Exception as exc:
        print(f"  ⚠️ catalyst layer failed (skipping injection): {exc}")
        return ""


def _synthesize(long_a: dict, medium_a: dict, short_positions_a: dict,
                margin_long_a: dict, short_selling_a: dict, portfolio_total: int, scenario: dict,
                risk: dict, market_meta: dict, news: dict, earnings: dict,
                backtest_summary: list | None = None,
                cash_info: dict | None = None,
                pending_orders: list | None = None,
                positions_raw: list | None = None,
                portfolio_integrity: dict | None = None,
                tax_context: dict | None = None,
                espp_context: dict | None = None,
                scenario_monitoring: dict | None = None,
                disagreement_context: str = "",
                data_freshness_context: str = "",
                accuracy_context: str = "",
                judge_context: str = "",
                redteam_context: str = "",
                screening_context: str = "",
                dca_context: str = "",
                ipo_watch_context: str = "",
                news_topic_context: str = "",
                social_topic_context: str = "",
                alpha_context: str = "",
                twr_context: str = "",
                degraded_context: str = "",
                currency_breakdown_whole: dict | None = None,
                currency_breakdown_long: dict | None = None,
                current_currency_policy: dict | None = None) -> dict:

    market_news_text = fmt_news_section(news)
    earnings_text = fmt_earnings_section(earnings)

    print("🌐 Web検索で最新市場ニュース取得中…")
    web_news = fetch_web_search_news()

    history_text = load_history_context()
    today = datetime.now().strftime("%Y-%m-%d（%A）")

    # 未発注アクション状況（Opus用）
    _pending_actions_ctx = ""
    try:
        from action_state_tracker import format_pending_for_prompt as _fmt_pending
        _pending_actions_ctx = _fmt_pending()
    except Exception:
        pass

    # BLビュー・投資信念コンテキスト（Opus用）
    bl_context = _load_bl_views_for_opus()
    catalyst_ctx = _load_catalyst_context_for_opus(scenario)
    beliefs_ctx = _format_beliefs_context(_load_beliefs())
    # v5.1: 執行品質コンテキスト（Implementation Shortfall 学習）
    exec_quality_ctx = _format_execution_quality_for_prompt(_load_execution_quality_summary())
    agent_reliability_ctx = _format_agent_reliability_for_prompt()

    # tunable_params の soft limit 注入（単一銘柄上限・通貨ターゲット・シグナル鮮度等）
    tunable_limits_ctx = _fmt_tunable_limits_context()

    # leverage_health コンテキスト（Option B-3: VIX 連動 portfolio leverage 健全性）
    leverage_health_ctx = ""
    try:
        from behavioral_guard import evaluate_leverage_health as _elh
        _lh = _elh(portfolio_total_jpy=float(portfolio_total or 0))
        _lh_lines = [
            "## 📊 Portfolio Leverage 健全性（Option B-3）",
            f"- current_leverage: {_lh.get('current_leverage')}x （信用込み総ポジ÷純資産）",
            f"- leverage_cap (VIX={_lh.get('vix','?')} 連動): {_lh.get('leverage_cap')}x",
            f"- max_leverage_setting: {_lh.get('max_leverage_setting')}x",
            f"- status: {_lh.get('status')} — {_lh.get('action','')}",
            f"- new_buy_allowed: {_lh.get('new_buy_allowed')} / margin_buy_allowed: {_lh.get('margin_buy_allowed')}",
            "→ status='warn'/'deleverage'/'emergency' のとき新規 buy 抑制、margin_buy 禁止、trim 推奨優先。",
            "→ margin_buy_allowed=False で type='margin_buy' を出すのは禁止（システムが自動除去）。",
        ]
        leverage_health_ctx = "\n".join(_lh_lines)
        # synthesis 結果へのパススルー用に保存
        _leverage_health_snapshot = _lh
    except Exception as _e:
        _leverage_health_snapshot = None
        print(f"  ⚠️ leverage_health 計算スキップ: {_e}")

    # Phase 1 (2026-04-28): 自己矛盾防止 + 決算 blackout コンテキスト
    recent_own_recs_ctx = _format_recent_own_recs_for_prompt(days=14)
    try:
        from tunable_params import get as _tp_get_eb
        _eb_days = int(_tp_get_eb("earnings_blackout_days", 5))
        _done_days_prompt = int(_tp_get_eb("done_list_same_direction_days", 7))
    except Exception:
        _eb_days = 5
        _done_days_prompt = 7
    earnings_blackout_ctx = _format_earnings_blackout_for_prompt(within_business_days=_eb_days)
    # Phase 2: 発注済み/約定済み DONE_LIST（同方向の重複推奨禁止）
    done_list_ctx = _format_done_list_for_prompt(days=_done_days_prompt)
    # Phase 2: 集中銘柄の一括 trim プラン（細切れ防止）
    rebal_plan_ctx = _build_consolidated_rebalance_context(
        positions_raw or [],
        float(portfolio_total or 0),
        fx_rate=float((cash_info or {}).get("fx_rate_usdjpy") or 150.0),
    )

    # v5.1: チャート派生指標（指値判断・No-Trade判定用） + オプション市場センチメント
    chart_ctx_block = ""
    options_ctx_block = ""
    _chart_map: dict[str, dict] = {}
    if _gather_chart_context and _format_chart_for_prompt:
        try:
            _target_tickers = _collect_priority_tickers(
                long_a, medium_a, short_positions_a, margin_long_a, short_selling_a,
                positions_raw=positions_raw,
                max_tickers=30,
            )
            if _target_tickers:
                print(f"📊 chart_analyzer: {len(_target_tickers)} ticker の指値文脈を構築…")
                _chart_map = _gather_chart_context(_target_tickers, intraday=True, max_tickers=30)
                chart_ctx_block = _format_chart_for_prompt(_chart_map)
                # Phase 3: オプションシグナルも同じターゲットセットで取得（24h キャッシュ活用）
                if _get_option_signals and _format_options_for_prompt:
                    try:
                        # SPY を必ず混ぜる（指数レベルの過熱判定用）
                        _opt_targets = ["SPY"] + [t for t in _target_tickers if t != "SPY"][:14]
                        print(f"📊 options_fetcher: {len(_opt_targets)} ticker のオプションシグナル取得…")
                        _opt_map = _get_option_signals(_opt_targets, max_n=15)
                        options_ctx_block = _format_options_for_prompt(_opt_map)
                    except Exception as _oe:
                        print(f"⚠️ options_fetcher エラー: {_oe}")
        except Exception as _ce:
            print(f"⚠️ chart_analyzer エラー: {_ce}")

    # 税務・持株会サマリー（Opus用）
    _tax_urgent = _extract_tax_urgent_actions({"tax_context": tax_context or {}})
    _espp_note = ""
    if espp_context and not espp_context.get("error"):
        ratio = espp_context.get("portfolio_ratio", 0) * 100
        alert = espp_context.get("concentration_alert", "normal")
        sell_rec = espp_context.get("sell_recommendation", 0)
        _espp_note = (
            f"持株会(9999.T): {ratio:.1f}% / {alert}"
            + (f" / 売却推奨¥{sell_rec:,.0f}" if sell_rec > 0 else "")
        )

    _cash_ctx = dict(cash_info or {})
    try:
        _total_cash = float(_cash_ctx.get("total_cash_jpy") or 0)
        _portfolio_total = float(portfolio_total or 0)
        _target_cash_pct = float(scenario.get("cash_ratio_target") or 0)
        _target_cash_jpy = _portfolio_total * _target_cash_pct / 100.0
        _cash_ctx.update({
            "cash_ratio_pct": round(_total_cash / _portfolio_total * 100, 1) if _portfolio_total > 0 else None,
            "target_cash_pct": _target_cash_pct,
            "target_cash_jpy": round(_target_cash_jpy),
            "deployable_cash_to_target_jpy": round(max(0.0, _total_cash - _target_cash_jpy)),
        })
    except Exception:
        pass

    _integrity_ctx = ""
    if isinstance(portfolio_integrity, dict) and portfolio_integrity:
        _summary = portfolio_integrity.get("summary") or {}
        _issues = [
            i for i in (portfolio_integrity.get("issues") or [])
            if isinstance(i, dict) and i.get("severity") in {"critical", "high"}
        ]
        _lines = [
            "## 🚨 Portfolio Ledger Integrity（保有・現金台帳の整合性）",
            f"- ok: {portfolio_integrity.get('ok')} / blocking_issue_count: {portfolio_integrity.get('blocking_issue_count', 0)}",
            f"- unapplied_executed_count: {_summary.get('unapplied_executed_count', 0)}",
            f"- legacy_executions_without_event_id: {_summary.get('legacy_executions_without_event_id', 0)}",
        ]
        if _issues:
            _lines.append("### blocking issues（上位5件）")
            for item in _issues[:5]:
                _lines.append(
                    f"- {item.get('check')}: {item.get('execution_id') or item.get('event_id') or item.get('tx_id') or ''} "
                    f"{item.get('ticker') or ''} {item.get('direction') or ''} "
                    f"qty={item.get('quantity')} price={item.get('price')}"
                )
            _lines.append("→ blocking issue がある場合、holdings/account は未反映の可能性がある。新規大口 buy/add/margin_buy は台帳照合後に回すか、risk_warnings に明記して保守的に扱うこと。")
        _integrity_ctx = "\n".join(_lines)

    prompt = f"""## 本日の日付: {today}
※ニュース記事の「○h前」表示を必ず確認すること。過去のイベントを「今後の予定」として戦略に組み込まないこと。

## 注文中アクション（既に発注済み — priority_actions に含めないこと）
{json.dumps(pending_orders or [], ensure_ascii=False)}

{f"## 税務緊急アクション（priority_actions に含めること）{chr(10)}{_tax_urgent}" if _tax_urgent else ""}
{f"## 持株会状況{chr(10)}{_espp_note}" if _espp_note else ""}

## 過去の分析履歴（直近{HISTORY_MAX}件）
{history_text}

{earnings_text}

## 現金残高（口座別・攻めモード判定の最重要入力）
{json.dumps(_cash_ctx, ensure_ascii=False)}
※ target_cash_pct までの deployable_cash_to_target_jpy は、優先的に buy/add で使い切る検討対象。投信・日本株・米国株の通貨制約も考慮すること。
## 通貨比率（母数を混同しないこと）
- whole_portfolio（全ティア・現金込み、表示用）: {json.dumps(currency_breakdown_whole or {}, ensure_ascii=False)}
- long_tier（rebalance/target判定専用）: {json.dumps(currency_breakdown_long or {}, ensure_ascii=False)}
- current_policy: {json.dumps(current_currency_policy or {}, ensure_ascii=False)}
※ 「USD不足/超過」は必ず母数を明記し、currency_target_recommendation は long_tier とのみ比較する。whole_portfolio比率から long_tier の不足を推測しない。
{_integrity_ctx + chr(10) if _integrity_ctx else ""}
## 現在のシナリオ: {scenario.get('key')} — {scenario.get('name','')}
## バックテスト実績: {json.dumps(backtest_summary or [], ensure_ascii=False)}
## リスク指標: {json.dumps(risk, ensure_ascii=False)}
## 市場環境（リアルタイム）:
VIX={market_meta.get('vix','不明')} {market_meta.get('vix_level','')} / 米10Y={market_meta.get('us10y_yield',{}).get('value','不明')}% / 米2Y={market_meta.get('us2y_yield',{}).get('value','不明')}% / イールド:{market_meta.get('yield_curve_status','')}

## リアルタイム Web検索ニュース
{web_news if web_news else "（取得失敗 — 以下のRSS/yfinanceニュースを参照）"}

## 最新ニュース・世界情勢（RSS/yfinance 過去24時間）
{market_news_text}

## ポジション実データ
{json.dumps([
    {"ticker": p.get("ticker"), "tier": p.get("investment_type"),
     "shares": p.get("shares"), "value_jpy": p.get("value_jpy"),
     "unrealized_pct": round((p.get("unrealized_pct") or 0) * 100, 1),
     "stop_loss": p.get("stop_loss")}
    for p in (positions_raw or [])
], ensure_ascii=False)}

{_fmt_scenario_monitoring(scenario_monitoring)}

{bl_context + chr(10) if bl_context else ""}{catalyst_ctx + chr(10) if catalyst_ctx else ""}{beliefs_ctx + chr(10) if beliefs_ctx else ""}{exec_quality_ctx + chr(10) if exec_quality_ctx else ""}{agent_reliability_ctx + chr(10) if agent_reliability_ctx else ""}{tunable_limits_ctx + chr(10) if tunable_limits_ctx else ""}{leverage_health_ctx + chr(10) if leverage_health_ctx else ""}{recent_own_recs_ctx + chr(10) if recent_own_recs_ctx else ""}{earnings_blackout_ctx + chr(10) if earnings_blackout_ctx else ""}{done_list_ctx + chr(10) if done_list_ctx else ""}{rebal_plan_ctx + chr(10) if rebal_plan_ctx else ""}
{_pending_actions_ctx + chr(10) if _pending_actions_ctx else ""}{accuracy_context + chr(10) if accuracy_context else ""}{disagreement_context + chr(10) if disagreement_context else ""}{data_freshness_context + chr(10) if data_freshness_context else ""}{degraded_context + chr(10) if degraded_context else ""}{judge_context + chr(10) if judge_context else ""}{redteam_context + chr(10) if redteam_context else ""}{twr_context + chr(10) if twr_context else ""}
{screening_context + chr(10) if screening_context else ""}
{dca_context + chr(10) if dca_context else ""}
{ipo_watch_context + chr(10) if ipo_watch_context else ""}
{news_topic_context + chr(10) if news_topic_context else ""}{social_topic_context + chr(10) if social_topic_context else ""}{alpha_context + chr(10) if alpha_context else ""}
{chart_ctx_block + chr(10) if chart_ctx_block else ""}{options_ctx_block + chr(10) if options_ctx_block else ""}
## Longティア分析（Sonnet A）
{json.dumps(long_a, ensure_ascii=False, indent=2)}

## Mediumティア分析（Sonnet B）
{json.dumps(medium_a, ensure_ascii=False, indent=2)}

## Short/Swing投機ポジション分析（Sonnet C / 出典: {short_positions_a.get('_source','?') if isinstance(short_positions_a, dict) else '?'}）
{json.dumps(short_positions_a, ensure_ascii=False, indent=2)}

## 信用買い一次判断（出典: {margin_long_a.get('_source','?') if isinstance(margin_long_a, dict) else '?'}）
{json.dumps(margin_long_a, ensure_ascii=False, indent=2)}

## 空売り一次判断（出典: {short_selling_a.get('_source','?') if isinstance(short_selling_a, dict) else '?'}）
{json.dumps(short_selling_a, ensure_ascii=False, indent=2)}

---
上記を統合し、以下のJSON形式で最終戦略を策定してください:
{{
  "overall_stance": "defensive|neutral|moderately_aggressive|aggressive",
  "stance_reason": "根拠を2文で",
  "aggressive_override_block": "（aggressive 昇格条件を満たすが override で格下げした場合のみ）具体的な block 根拠（例: 'VIX>30 急騰により defensive 強制', 'regime急変によりA→B切替'）。短期警戒だけでは block 不可。空文字またはフィールド省略でも可。",
  "priority_actions": [{{"rank":1,"tier":"Long|Medium|Short|All","urgency":"high|medium|low","type":"buy|add|margin_buy|sell|rebalance|short|cover|dca|trim|reduce|stop_loss|take_profit","ticker":"XXX","action":"具体的なアクション","reason":"根拠","amount_hint":"整数のみ（株式は株、投資信託は口単位。米国株は端株不可）","confidence_pct":75,"return_20d_rank":"top|middle|bottom","execution_account":"特定|一般|NISA成長投資枠|NISAつみたて投資枠|信用","execution_owner":"husband|wife","execution_broker":"rakuten|sbi"}}],
  "hold_notes": ["保有継続すべき銘柄とその理由（アクション不要のもの）"],
  "telegram_message": "📊 Telegram通知（200文字以内）",
  "risk_warnings": ["重要なリスク警告"],
  "opportunity_highlights": ["注目機会"],
  "weekly_theme": "今週の投資テーマを1文で",
  "geopolitical_note": "地政学リスク総括（2〜3文）",
  "currency_target_recommendation": {{"basis":"long_tier","usd_target_pct":65,"jpy_target_pct":35,"confidence_pct":75,"horizon_days":14,"valid_until":"YYYY-MM-DD","reason":"long母数の通貨比率をどう動かすか/維持かの理由","review_triggers":["USDJPYや金利の再評価条件"],"risk_notes":"想定外リスク"}},
  "information_lane_verdicts": [{{"lane":"ipo_watch|news_topic|social_topic|geopolitical|disclosure|catalyst","ticker":"XXX","verdict":"adopt|reject|ignore","verdict_reason":"採否理由を1文","adopted_as":"priority_actionsに変換した場合のみ内容"}}],
  "red_team_verdict": [{{"ticker":"XXX","action":"提案内容","verdict":"adopt|partial|reject","verdict_reason":"採否理由を1文","adopted_as":"adoptの場合のみ：実際に変換したアクション（例: buy 100株）"}}]
}}"""

    system = """\
あなたはユーザーのチーフ投資戦略家です。
各ティアの分析・最新ニュース・世界情勢を統合し、最も重要かつ実行可能な最終戦略を提示してください。

重要ルール:
- priority_actions には「実行すべきアクション」のみを含めること（buy/add/margin_buy/sell/rebalance/trim/reduce/dca/stop_loss/take_profit/short/cover）
- 「保有継続」「様子見」「ホールド」はアクションではない → hold_notes に記載すること
- priority_actions に hold/continue/maintain/watch を入れないこと
- 過去の推奨精度データが提供されている場合: sell/trim/short の勝率は SPY相対（銘柄-SPY差）で算出されている点に注意。警告（⚠️）が明示され「事後95%CI上限<50%」かつサンプル数10件以上のアクション種別のみ urgency を1段階下げるか根拠を強化すること。サンプル数10件未満または警告なしの勝率は「データ不足／有意でない」として無視すること（⚠️警告なしに生の勝率だけで過度に保守化してはならない）
- エージェント間不一致がある銘柄: ⚠️マークが付いた銘柄は単一方向への強いコミットを避け、小さいポジションサイズや条件付きアクションを提案すること
- データ鮮度スコアが0.7未満の場合: high urgencyアクションの根拠をより保守的に評価し、STALEと表示されたデータへの依存を明記すること
- DEGRADED MODE コンテキストが提供されている場合: 一次ティア障害により根拠の独立確認が弱い。priority_actions は件数制限だけで非表示化しない。confidence は控えめにし、各 action の reason に不確実性を明記し、telegram_message 冒頭に「⚠️ DEGRADED MODE」を含めること。
- Red Team仮説が提供されている場合: 仮説を1件ずつ評価し red_team_verdict に必ず全件記載すること。リスクが高い場合は reject でよい（全件 reject も valid な結論）。「最低1件 adopt」のような件数ノルマは設けない — 採用基準は「期待 alpha が手数料・税後で 50bps 以上」のみ。
- display/context-only 情報レーン（ipo_watch/news_topic/social_topic/geopolitical/disclosure/catalyst）が提供されている場合: information_lane_verdicts に lane ごとの adopt/reject/ignore を必ず記録すること。action化するには ticker解決・価格/流動性/鮮度・insider制限・policy通過が必要。observe_only 由来を action化する場合、生の observe_only=true は禁止し、source_observe_only=true / provisional_decision=true / source_lane / ai_override_reason を付けること。
- AI bounded gate を越えて action化する場合: tax_loss_harvest_conflict は tax_override_reason、earnings_blackout は earnings_event_trade=true と earnings_event_reason、too_small は small_notional_exception=true と small_notional_exception_reason を付けること。これらが無い action は post-filter で除去される。
- **アクションtype定義（重要）**:
  * `buy`    = 新規エントリー（未保有銘柄への新規購入）
  * `add`    = 既存保有銘柄への追加購入（押し目買い・買い増し含む）← QCOMや既保有株の追加はこれ
  * `margin_buy` = 信用買いエントリー（leverage_health.margin_buy_allowed=True かつ信用買い活用ルールを満たす場合のみ）
  * `dca`    = drawdown_dca_engineのラダートランシェ（source="dca_ladder"が付くもの専用）。通常の追加購入に `dca` を使うことは**禁止**。「DCA方向」という理由文であっても既保有への追加は `add` を使うこと。
  * `trim`   = 部分利確・ポジション縮小
  * `sell`   = 全額売却
  * `rebalance` = ティア間比率調整
  * `stop_loss` / `take_profit` = 損切り・利食い指値更新
  * `short`  = 空売りエントリー
- 証券会社制約: 楽天証券は米国株の端株取引不可。buy/add/dca アクションの amount_hint は必ず整数株単位で記載すること（例: "1株"、"2株"。"0.3株"や"0.5株"は不可）。
- **NISAルーティング必須**: NISA成長投資枠の buy/add は `execution_account` に加え、`execution_owner`（husband|wife）と `execution_broker`（rakuten|sbi）を必ず明示する。名義・証券会社を入力根拠から特定できない場合は推測せず、priority_actions ではなく hold_notes に「口座確認が必要」と記載すること。
- **売却元口座の整合必須**: 同一銘柄を複数口座で保有する sell/trim/reduce では、`execution_account` を実際に売却するロットの口座（特定/一般/NISA等）と一致させ、`action` 本文の口座名・保有株数とも食い違わせないこと。どのロットか確定できない場合は推測で priority_actions に入れず、hold_notes に口座確認が必要と記載すること。
- **日本株制約（.T 銘柄）: 通常の普通株の現物単元注文と信用買いは100株単位。ただし、TUNABLE_LIMITSに明示されたJPX ETFは銘柄別の公式売買単位（1489.T=1口、1306.T=10口）を使い、100株へ丸めないこと。普通株でもローカルのかぶミニ対象台帳で確認できる現物 `buy/add` は、`execution_channel="rakuten_kabu_mini_open"` を付ければ1株単位で提案可。台帳未確認の普通株は100株単位に戻す。かぶミニは現物専用で `margin_buy` には使わない。リアルタイム取引は0.22%スプレッドがあるため、原則は寄付取引/寄付後確認を優先し、急ぐ理由がある場合のみリアルタイムを明記。** 投信（SLIM_*, IFREE_*, MNXACT, NOMURA_*）は円ベース（"¥XX,XXX 相当"）の amount_hint を使う。米国株は 1 株単位可。
- **試験エントリー「1〜3株の小ロット」ルール**: 米国株は従来どおり1〜3株可。日本株 WATCH+BULLISH 候補も、かぶミニ現物または公式売買単位が1口のETFなら小口で提案可。それ以外は銘柄別売買単位に従うこと。
- 新規購入クーリング期間: Sonnet出力は既にクーリングフィルタ適用済み（直近14日以内に買い執行記録のある銘柄の trim/sell/stop_loss は除去済み）。Opusで追加のクーリング判断は不要。priority_actions に残っている銘柄はそのまま採用してよい。holding_days だけでは判断しないこと。
- overall_stance の判定基準（**aggressive 自動昇格 + override 禁止ルール**）:
  * **defensive**: VIX>30 または current_dd<=-8% または margin_health danger/emergency。これは強制で override 不可
  * **neutral**: 基本状態
  * **moderately_aggressive**: regime_bull_confirmed かつ VIX<25
  * **aggressive**: regime_bull_confirmed かつ VIX<20 かつ 現金比率>3%（target_cash 0% への余地ある場合）
  * 🛑 **stance override 禁止ルール**: aggressive 昇格条件を満たしている場合、短期警戒（決算1週間以内・データ鮮度<0.5・原油急騰・地政学リスク等）を理由に `moderately_aggressive` や `neutral` に **格下げしてはならない**。短期警戒は urgency 1段下げで対応せよ。
  * 🛑 **excess α / TWR 実績 / CVaR / cvar_unstable を stance 格下げの根拠にしてはならない**。これらは測定指標であり、クリーン履歴不足時は「未確定」で提示される（TWRセクション参照）。実績αが負・cvar_unstable=true 等を理由に aggressive を見送ることは override 規則違反。これらは候補の sizing / urgency にのみ反映し、stance には反映しない。
  * 有効な override 条件は **実データの VIX>30 / current_dd<=-8% / margin_health danger・emergency / regime 急変** のみ。違反時は `aggressive_override_block` フィールドに該当する**実データ条件**を記録すること（測定指標や短期警戒は不可。該当が無ければ aggressive を維持し block しない）。
  * 短期警戒や測定指標で stance 全体を保守化することは「攻めモード言葉だけ実装ゼロ」の元凶なので明示的に禁止する。
- **FinBERT センチメント反映**: shared_ctx の「FinBERTニュース感情集計」が「強気優勢」のとき、aggressive 昇格を後押しする方向に解釈すること（urgency 上げ・新規候補増）。「弱気優勢」のときは urgency 1段下げで対応（stance 全体の格下げは不可）。
- **FRED マクロ stance 補正**:
  * **利下げサイクル**（FF金利↓ + 米10Y < 4.5% + CPI < 3%）→ aggressive 寄りに解釈、信用買い積極採用
  * **利上げサイクル**（FF金利↑ + 米10Y > 4.5% + CPI > 3%）→ urgency 1段下げ、レバレッジ抑制
  * **逆イールド継続中**: defensive 寄り（信用買い禁止＋現金比率 target 引き上げ可）
  * **失業率急上昇** (前月比+0.3pt超): risk_warnings に「景気減速シグナル」を必ず記載
- Judge Reportが提供されている場合: ティア間矛盾がフラグされた銘柄は矛盾を解決する根拠を明記すること。高合意アクションは優先的に採用すること。過信フラグがついたアクションはurgencyを1段下げるか根拠を強化すること。
- urgency降格ルール: 過去推奨精度・Judge過信フラグ・データ鮮度の各ルールによるurgency降格は同一アクションに対して最大1段階までとする。high→lowの2段階降格は禁止。
- Longティアのpriority_actionsにself_consistencyフィールドがある場合: "confirmed"は2回実行で一致→信頼度高。"disputed"は不一致→慎重に扱い根拠を厳格に評価すること。"unconfirmed"は通常通り評価。
- ストレステスト結果が提供されている場合: 集中リスクの高いポジション（ストレス推定損失が大きい銘柄）への買い増しは特に慎重に検討すること。
- 信用買い一次判断（margin_long_analysis）に margin_long_picks がある場合: margin_health.statusがwarning/danger/emergencyでなく、overall_stanceがneutral以上であれば、候補を期待 alpha と sizing 制約に照らして個別に評価する。「urgency=high は必ず採用」「medium も最低1件」のような件数ノルマは設けない — リスクに見合わない候補は全件 hold_notes 退避でもよい。amount_hint は整数株単位（SBI端株不可）。`_auto_filled=True` の picks は一次判断未評価時のセーフティネット補完だが、score根拠は有効なので同様に検討対象とすること。
  ⚠️ **信用買い活用ルール（Option B-3 / 攻めモード時の最重要）**: 攻めモード時は「現金 0% target + portfolio×1.2x レバレッジまで」のフルレバ設計。
  **leverage_health コンテキスト（提供されている場合）**: current_leverage / leverage_cap / margin_buy_allowed を確認。
  margin_buy_allowed=False なら信用買い type="margin_buy" は禁止（既存ポジション保持のみ）。
  **stance="aggressive" or "moderately_aggressive" + 現金>0%**:
  - 通常は現金から優先消化（type="buy"）→ 現金 0% target に近づける
  - **高 conviction 例外**: TUNABLE_LIMITS の信用買い高conviction閾値を満たし、かつ leverage_health.margin_buy_allowed=True（vix_margin_buy_block 未満） + 期待リターンが信用金利を十分上回る個別銘柄/上場ETF（GLD/IEV/1489.T 等）は、現金があっても type="margin_buy" として採用 OK（攻めの判断）。現行スクリーナーの `score` は 100 点台上位が高convictionであり、旧尺度の score≥130 を追加条件として使わないこと。
  - 投信 (SLIM_/MNXACT/IFREE_/NOMURA_) は信用買い不可 → 必ず type="buy"
  **stance="aggressive" + 現金 ≈ 0%**:
  - leverage < leverage_cap（VIX 連動）なら type="margin_buy" 積極採用
  **stance="neutral" or "defensive"**:
  - 従来通り現金優先。type="buy" 基本、現金不足のみ type="margin_buy"。
  **VIX デリバレッジ強制（leverage_health.status で判定）**:
  - status="warn"/"deleverage"/"emergency" → 新規 buy 抑制、margin_buy 禁止。trim 提案優先。
- short_selling_analysis.short_opportunities に候補がある場合: margin_health が safe かつ overall_stance が aggressive/moderately_aggressive の場合のみ、urgency=high のものを priority_actions に type="short" tier="Swing" として採用検討すること。margin_health が warning/danger/emergency または信用建玉可能枠が不足している場合は priority_actions に含めず risk_warnings に「空売りセットアップ有効だが維持率/建玉枠で実行不可」と記載すること。`_auto_filled=True` の opportunities はスクリーナーの一次候補を直接提示したもので、一次判断未評価時の補完である点を明記すること。
- 未発注アクション積み残し（注文中アクションリスト）は新規エントリーの障害にならない。guard_stateのnew_entry_allowed=Falseが明示されていない限り、スクリーニング候補への新規エントリーを積極的に検討すること。保有ポジション数に上限はない。
- overall_stanceがmoderately_aggressiveまたはaggressiveの場合: Shortティア（投機ロング）のbuy推奨でconfidence_pct≥60のものは、RSIが高くても「ブレイクアウト継続」として priority_actions に含めること。RSI過熱を理由にhold_notesへ退避させてはならない。
- 📊 短期スクリーニング候補（生データ）セクションが提供されている場合: WATCH+強気BULLISH候補は試験エントリー候補として検討する（採用する場合は type="buy" tier="Swing" urgency="low"、amount_hint=1〜3株の小ロット）。「必ず採用」「最低1件」のノルマは設けない — 期待 alpha が低い場合は hold_notes 退避が正解。**注意: この「1〜3株の小ロット」ルールはWATCH+BULLISH短期スクリーニング候補の試験エントリー専用であり、margin_long_picksやJudge高合意銘柄・Long/Medium銘柄には適用しない。**
- **少額ポジション整理ルール（出口免除）**: 評価額が ¥30 万未満の保有銘柄については、整理（sell / trim / stop_loss / take_profit）を**小額でも提案してよい**。最低取引額 ¥150K ルールの例外。例: TXN 1 株 ¥41K 保有 → 「全数売却 1 株」を提案可。これは「永久ホールド罠」（少額ゆえに整理できない状態）を解消するためのガードレール。
- **最低ロット強制ルール**（stance=moderately_aggressive/aggressive かつ 現金比率>10% の場合）:
  * buy/add/dca の amount_hint が「1株」となる提案は、以下のいずれかを満たす場合のみ許可:
    (a) 1株あたり株価 ≥ ¥10万（例: META $660 × 150円/$ ≈ ¥99,000は境界）
    (b) WATCH+BULLISH の試験エントリー（reason に「試験エントリー」明記）
    (c) 信用維持率・NISA残枠・資金制約でそれ以上買えない（reason に制約を明記）
  * 上記(a)〜(c)以外で単株提案する場合は reason に「最小試験エントリー」と明記。
  * post-filter の最小金額への自動増額は、confidence/rank が高く urgency=low ではない場合だけ許可。低順位の「慎重に1株」提案を機械的に3株化してはならない。
  * margin_long_picks の score ≥100 top1 候補は最低 2-3 株を提案すること（信用枠制約がない限り）。
  * 投資信託 (SLIM_SP500 / SLIM_ORCAN / IFREE_FANGPLUS / NOMURA_SEMI / MNXACT) の buy/dca は、
    NISA 残枠 ¥100 万以上ある場合、1回あたり ¥30 万未満の提案は禁止（月次積立化 or 一括 ¥50 万+ に統合）。
    ¥30 万未満を提案する場合は hold_notes に「NISA月次積立分として既に計上済み」等の理由を明記すること。
  * Long ティアの主要銘柄 (AVGO / GLD / 1489.T / NVDA) への add/dca は最低 ¥20 万相当以上とすること。
  * データ鮮度スコア<0.7 を理由に high urgency を保守化する場合でも、amount_hint を
    「1株」に機械的に縮小してはならない — 保守化は urgency の1段階降格または条件付き発注で対応すること。
- priority_actions の件数は「期待 alpha が手数料・税後で 50bps 以上ある候補のみを採用」した結果であり、件数ノルマや固定上限（例: 6件固定）は設けない。実行可能な高優先候補は落とさず残し、`priority_actions = []`（no-trade）も valid な出力とする。市況が不確実な日は積極的に空配列を選んでよい。空配列を返す場合は headline か no_action_rationale で理由を簡潔に述べること。
- **ティア分析の出典について（重要）**: Long / Medium / Short(Swing) は Sonnet、信用買い / 空売り は設定モデル（DeepSeek 等）による一次判断である。出典はモデル名の思い込みではなく、各ティアの `_source` フィールドを必ず参照すること（例: "deepseek:deepseek-v4-pro" は DeepSeek 経由、"anthropic:claude-*" は Anthropic 経由）。信用買い・空売りは損失拡大リスクがあるため、一次判断をそのまま通さず、最終Opusで期待alpha・証拠金・市況・サイズ制約を再評価して採否を決めること。
- **必須観測性フィールド**: synthesis 出力には以下を**必ず**埋めること:
  * `health`: 全体健全性 ∈ {good, caution, critical} — 5判断（Long/Medium/Short/信用買い/空売り）の health を統合
  * `health_reason`: 1文で health 判定の根拠
  * `margin_health`: 信用建玉健全性 ∈ {safe, warning, danger, emergency} — 各ティアの margin_health を統合
  * `market_meta_snapshot`: {vix, vix_level, us10y_yield, yield_curve_status} — 参照した市場スナップショット
- **【持株会 9999.T の取扱い（重要）】**
  * 9999.T は **売買可能** な資産。「強制売却不可」「持株会のため自動停止なし」等の判定は禁止（過去のハルシネーション）。
  * 集中度 (`espp_context.portfolio_ratio`) が `concentration_alert` で warning/danger と表示された場合、または `sell_recommendation > 0` の場合は **sell/trim 推奨を堂々と priority_actions に出すこと**。
  * ただし amount_hint には settlement window を明示すること。例: `"100株 sell（社内システム経由・約定まで5〜10営業日）"`。
  * 月次積立額と奨励金率はローカルの非公開設定を正とする。積立継続中でも売却推奨は可能だが、金額を推測してはならない。
- **【Phase 1: 細切れリバランス抑制ルール（2026-04-28 追加）】**
  * 1 アクションあたり最低 ¥150,000 相当を目安とすること。米国株 1 株単価が ¥150,000 未満の銘柄に対する単株 trim/buy/add 推奨は禁止（複数株でまとめるか、保留）。
  * 同一銘柄に対する trim/buy 分割は同月内で最大 2 回まで。3 回以上に分けるくらいなら 1 回にまとめてロット拡大すること（手数料率の頭打ちを取りに行く）。
  * `RECENT_OWN_RECS` セクションで自分の直近 14 日の推奨履歴が提供されている場合: 同一銘柄に逆方向（buy ↔ sell/trim）の推奨を出すときは、reason フィールドに「市況の何が有意に変化したか（VIX 急騰、決算サプライズ、規制発表など）」を **必ず明示** すること。市況が同一なら推奨を出さない（=自己矛盾の禁止）。
  * `EARNINGS_BLACKOUT` セクションで決算 5 営業日以内の銘柄リストが提供されている場合: その銘柄への buy/add/dca 推奨は出さない（trim/hold は許可）。決算後 1 営業日以降に再評価。
  * `DONE_LIST` セクションで直近 7 日の発注/約定済みアクションが提供されている場合: 同一 ticker × 同方向（buy or sell）の重複推奨は禁止。例: 7751.T を 100 株 buy 済みなら同銘柄への新規 buy/add は出さない（trim 方向は可）。「自動積立」設定済みの SLIM_*/IFREE_* も同じく追加 dca 提案禁止。
  * **損出し候補との矛盾禁止**: `tax_context.loss_harvest.candidates` に含まれる銘柄に buy/add/dca を提案してはならない（節税効果を捨てるため矛盾）。代わりに trim/sell で確定損益を取り、12/26 期限までに損益通算を完了させること。買い直しは 30 日以上空けてから別途検討。
  * GLD・NVDA・AVGO 等の集中銘柄をターゲット比率まで縮小する場合: 「総株数何株 / 何回に分割 / 1 回あたり何株」を `action` フィールドで明示し、毎日 1〜5 株の細切れ発注は避けること。
- **【v5.1: 執行方式 AI 決定（CHART_CONTEXT があるとき必須）】**
  * priority_actions の各エントリーに以下フィールドを必ず埋めること:
    - `order_type`: "market" | "limit" | "stop_limit"
    - `limit_price`: 数値（order_type が "limit" / "stop_limit" のとき必須）
    - `expiry_minutes`: 240 標準。urgency=high の指値は 60、urgency=low は 720+ 推奨
    - `execution_reason`: VWAP/ATR/支持線/spread を引いて 1〜2 文で根拠
    - `decision_price`: CHART_CONTEXT.last_close（intraday があれば snapshot.last）。後で shortfall_bps 計算に使う
  * 【成行を選べる条件】:
    - 投資信託、または urgency=high の緊急リスク削減であること
    - decision_price と bid/ask が確認でき、spread_bps<=30 であること
    - 低流動性、spread_bps>30、少額、CHART_CONTEXT欠落は成行理由にならない。これらは指値またはno_trade_zoneにする
    - 重要指標の24時間以内は成行禁止。6時間前から発表後1時間は新規リスク注文を出さない
  * 【指値の決定】:
    - buy/add/dca:    limit = min(last, vwap_30d) − atr_14d × k_buy （k_buy = 0.3 high / 0.5 medium / 0.8 low）
    - sell/trim/stop: limit = max(last, vwap_30d) + atr_14d × k_sell（同じ）
    - support 上 +0.2×atr を下回らない／resistance 下 -0.2×atr を上回らないこと
    - bid/ask が見えていれば、買いは ask に1tick内側、売りは bid に1tick内側へ寄せる
  * 【No-Trade Band（Nakagawa流）】:
    - target_5d_pct を bp 換算（×100）して、`spread_bps + 5(手数料) + 過去IS中央値` を下回るなら `no_trade_zone=true` + `skip_reason` を 1 文で
    - no_trade_zone=true のときは limit_price/order_type は省略可（urgency=low の経過観察扱い）
  * 【Multi-Horizon target hint（任意）】:
    - 自信があれば `target_5d_pct` / `target_20d_pct` に期待リターン%を入れる（撤退判断材料）
    - 不確実なら省略してよい
必ずJSONのみを出力してください。"""

    import anthropic as _anthropic, time as _time
    # タイムアウト明示設定 (10 分) — dead socket での無限待ち防止。
    # Extended Thinking 8k + max_tokens 16k でも通常 2-5 分で完走するため 600s で十分。
    _client = _anthropic.Anthropic(timeout=600.0, max_retries=0)

    # Opus 4.7 に昇格：全ティア合成の最重要ステップ。model_router で一元管理。
    from model_router import get_model as _get_model
    _synthesis_model = _get_model("final_synthesis")
    _prompt_chars = len(prompt or "")
    _started = _time.monotonic()

    for attempt in range(4):
        try:
            # Anthropic API 制約 (2026-05-06 確認):
            # thinking=adaptive と tool_choice=force は同時使用不可 → 400 Bad Request。
            # thinking なし + tool_choice=force が唯一の確実解。
            # Opus 4.7 は tool_choice=force でも内部推論を行うため品質は維持される。
            response = _client.messages.create(
                model=_synthesis_model,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                tools=[_SUBMIT_TOOL],
                tool_choice={"type": "tool", "name": "submit_analysis"},
            )
            _usage = getattr(response, "usage", None)
            _append_llm_call_log({
                "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                "role": "final_synthesis",
                "model": _synthesis_model,
                "use_tool": True,
                "max_tokens": 16000,
                "timeout_sec": 600.0,
                "attempt": attempt + 1,
                "elapsed_sec": round(_time.monotonic() - _started, 2),
                "prompt_chars": _prompt_chars,
                "status": "ok",
                "stop_reason": getattr(response, "stop_reason", None),
                "content_types": [getattr(b, "type", None) for b in getattr(response, "content", [])],
                "input_tokens": getattr(_usage, "input_tokens", None),
                "output_tokens": getattr(_usage, "output_tokens", None),
            })
            thinking_text = ""
            thinking_signature = ""  # Opus 4.7 adaptive: 暗号化 signature が thinking 実施の proxy
            result_dict: dict = {}
            raw_text = ""
            for block in response.content:
                if block.type == "thinking":
                    thinking_text = getattr(block, "thinking", "") or ""
                    thinking_signature = getattr(block, "signature", "") or ""
                elif block.type == "tool_use" and block.name == "submit_analysis":
                    result_dict = block.input.get("result", block.input)
                elif block.type == "text":
                    raw_text += block.text
            # tool_use がない場合はテキストからJSON抽出
            if not result_dict and raw_text:
                try:
                    import re as _re
                    m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, _re.DOTALL)
                    if not m:
                        m = _re.search(r"(\{.*\})", raw_text, _re.DOTALL)
                    if m:
                        result_dict = json.loads(m.group(1))
                except Exception:
                    result_dict = {}

            if thinking_text:
                try:
                    from utils import atomic_write_json
                    log_path = BASE_DIR / "opus_thinking_log.json"
                    history: list = []
                    if log_path.exists():
                        try:
                            existing = json.loads(log_path.read_text(encoding="utf-8"))
                            history = existing if isinstance(existing, list) else [existing]
                        except Exception:
                            history = []
                    history.append({"as_of": datetime.now().isoformat(),
                                    "thinking_len": len(thinking_text), "thinking": thinking_text})
                    history = history[-7:]
                    atomic_write_json(log_path, history)
                    print(f"  💭 Opus thinking: {len(thinking_text)}文字 → opus_thinking_log.json ({len(history)}件保持)")
                except Exception:
                    pass

            # 観測性: 使用モデル ID / thinking mode / 実施フラグを記録
            # Opus 4.7 adaptive は thinking 本文を隠蔽するため、signature 存在を proxy 指標とする
            if isinstance(result_dict, dict):
                result_dict.setdefault("model_used", _synthesis_model)
                result_dict.setdefault("thinking_mode", "adaptive")
                result_dict.setdefault("thinking_len", len(thinking_text))
                result_dict.setdefault("thinking_fired", bool(thinking_signature))
                result_dict.setdefault("thinking_signature_len", len(thinking_signature))
                _context_blocks = result_dict.get("context_blocks")
                if not isinstance(_context_blocks, dict):
                    _context_blocks = {}
                    result_dict["context_blocks"] = _context_blocks
                _context_blocks["catalyst"] = bool(catalyst_ctx.strip())
            if thinking_signature and not thinking_text:
                print(f"  💭 Opus thinking fired (signature={len(thinking_signature)}chars, body encrypted by Anthropic)")

            # v5.1: 空結果ガード — priority_actions も hold_notes も空 + overall_stance なし
            # は明らかな異常（tool 呼ばれず or schema 失敗）。1 度だけ retry し、それでも空なら警告。
            _is_empty = (
                isinstance(result_dict, dict)
                and not result_dict.get("priority_actions")
                and not result_dict.get("hold_notes")
                and not result_dict.get("overall_stance")
                and not result_dict.get("error")
            )
            if _is_empty and attempt < 2:
                print(f"⚠️ synthesis 空結果検知 (attempt {attempt+1}) — リトライします")
                _time.sleep(10)
                continue
            if _is_empty:
                result_dict["error"] = (
                    "empty_synthesis: tool_use が呼ばれず、テキストも空。"
                    "プロンプト過大 or Opus 4.7 が tool 選択を断念した可能性。"
                    "max_tokens / tool_choice / system プロンプトの肥大化を見直してください。"
                )
                print(f"⛔ {result_dict['error']}")

            # Persist the deterministic chart inputs used for order-quality
            # validation.  The LLM explanation alone is not an auditable source
            # for spread/freshness decisions.
            for action in result_dict.get("priority_actions") or []:
                if not isinstance(action, dict):
                    continue
                chart = _chart_map.get(str(action.get("ticker") or ""))
                if not isinstance(chart, dict):
                    continue
                for source_key, target_key in (
                    ("spread_bps", "spread_bps"),
                    ("freshness", "chart_freshness"),
                    ("data_as_of", "chart_data_as_of"),
                    ("price_source", "chart_price_source"),
                    ("last_close", "chart_last_close"),
                    ("atr_14d", "chart_atr_14d"),
                    ("adv_30d", "chart_adv_30d"),
                ):
                    if chart.get(source_key) is not None:
                        action[target_key] = chart.get(source_key)
                snapshot = chart.get("intraday_snapshot")
                if isinstance(snapshot, dict):
                    action["quote_bid"] = snapshot.get("bid")
                    action["quote_ask"] = snapshot.get("ask")
                    action["quote_last"] = snapshot.get("last")
                    action["quote_as_of"] = snapshot.get("timestamp") or snapshot.get("as_of") or snapshot.get("ts")

            return result_dict

        except _anthropic.APIStatusError as e:
            # 500/502/503/529 は一時的インフラ障害 → リトライ対象。
            # Anthropic 500(api_error/Internal server error) は final synthesis で
            # 単発発生しやすく、即 RuntimeError にすると portfolio_analyst.py --force
            # 全体が落ちるため、他の transient と同じ扱いにする。
            if e.status_code in (500, 502, 503, 529) and attempt < 3:
                wait = 30 * (2 ** attempt)
                print(f"⚠️ Anthropic {e.status_code} エラー、{wait}秒後にリトライ ({attempt+1}/3)…")
                _time.sleep(wait)
            else:
                raise
        except (_anthropic.APITimeoutError, _anthropic.APIConnectionError) as e:
            if attempt < 3:
                wait = 15 * (2 ** attempt)
                print(f"⚠️ Anthropic timeout/connection エラー ({type(e).__name__})、{wait}秒後にリトライ ({attempt+1}/3)…")
                _time.sleep(wait)
            else:
                raise
        except Exception as e:
            return {
                "error": str(e), "overall_stance": "neutral", "priority_actions": [],
                "telegram_message": "⚠️ AI分析でエラーが発生しました",
                "risk_warnings": [], "opportunity_highlights": [], "weekly_theme": "-",
                "geopolitical_note": "",
            }

    return {
        "error": "max retries exceeded", "overall_stance": "neutral", "priority_actions": [],
        "telegram_message": "⚠️ API過負荷でAI分析失敗",
        "risk_warnings": [], "opportunity_highlights": [], "weekly_theme": "-",
        "geopolitical_note": "",
    }


# ── Phase 1 (2026-04-28): priority_actions post-filter ──────────────────────────
# 細切れリバランス抑制 + 連日重複抑制 + 決算 blackout + フリップ警告。
# LLM 出力をフィルタしてユーザー実行負荷と手数料コストを下げる。

_BUY_LIKE   = {"buy", "add", "dca", "margin_buy"}
_SELL_LIKE  = {"sell", "trim", "reduce", "take_profit", "stop_loss"}
_SHORT_LIKE = {"short", "short_sell"}
_COVER_LIKE = {"cover", "buy_to_cover"}


def _direction_of(action_type) -> str:
    t = str(action_type or "").lower()
    if t in _BUY_LIKE:
        return "buy"
    if t in _SELL_LIKE:
        return "sell"
    if t in _SHORT_LIKE:
        return "short"
    if t in _COVER_LIKE:
        return "cover"
    return "other"


def _load_recent_recommendations(days: int = 14) -> list:
    """直近 N 日の自分の推奨ログを返す。"""
    from datetime import timedelta
    log_path = BASE_DIR / "ai_recommendation_log.json"
    if not log_path.exists():
        return []
    try:
        entries = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return []
    except Exception:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for e in entries:
        try:
            ts = datetime.fromisoformat((e.get("as_of") or "").split("+")[0])
            if ts >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


def _load_cancelled_recommendation_keys() -> set[tuple[str, str, str]]:
    """action_state.json から cancelled の (ticker, YYYY-MM-DD, direction) を返す。"""
    cancelled_keys: set[tuple[str, str, str]] = set()
    try:
        _path = BASE_DIR / "action_state.json"
        if not _path.exists():
            return cancelled_keys
        _state = json.loads(_path.read_text(encoding="utf-8"))
        raw_actions = _state.get("actions", {}) if isinstance(_state, dict) else {}
        if isinstance(raw_actions, dict):
            entries = raw_actions.values()
        elif isinstance(raw_actions, list):
            entries = raw_actions
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "").lower() != "cancelled":
                continue
            tk = entry.get("ticker")
            day = (entry.get("recommended_at") or "")[:10]
            direction = _direction_of(entry.get("action_type") or entry.get("type"))
            if tk and day:
                cancelled_keys.add((tk, day, direction))
    except Exception:
        pass
    return cancelled_keys


def _recommendation_entry_is_cancelled(entry: dict, cancelled_keys: set[tuple[str, str, str]]) -> bool:
    if not isinstance(entry, dict):
        return False
    tk = entry.get("ticker")
    day = (entry.get("as_of") or "")[:10]
    direction = _direction_of(entry.get("type"))
    return bool(tk and day and (tk, day, direction) in cancelled_keys)


def _load_recommendation_execution_rows() -> tuple[list[dict], bool]:
    """Load the fill/order source of truth for RECENT_OWN_RECS labels."""
    path = BASE_DIR / "action_executions.json"
    if not path.exists():
        return [], False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], False
    rows = raw.get("executions") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return [], False
    return [row for row in rows if isinstance(row, dict)], True


def _load_recommendation_action_states() -> list[dict]:
    path = BASE_DIR / "action_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    actions = raw.get("actions") if isinstance(raw, dict) else {}
    if isinstance(actions, dict):
        rows = actions.values()
    elif isinstance(actions, list):
        rows = actions
    else:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _recommendation_row_matches(entry: dict, row: dict, *, state_row: bool = False) -> bool:
    if str(entry.get("ticker") or "") != str(row.get("ticker") or ""):
        return False
    entry_direction = _direction_of(entry.get("type"))
    row_direction = _direction_of(
        row.get("action_type") if state_row else (row.get("direction") or row.get("type"))
    )
    if not entry_direction or entry_direction != row_direction:
        return False

    entry_analysis_id = str(entry.get("analysis_id") or "")
    row_analysis_id = str(row.get("analysis_id") or "")
    if entry_analysis_id and row_analysis_id:
        return entry_analysis_id == row_analysis_id

    entry_action = str(entry.get("action") or "").strip()
    row_action = str(
        (row.get("action_detail") if state_row else row.get("action")) or ""
    ).strip()
    if entry_action and row_action:
        return entry_action == row_action

    entry_day = str(entry.get("as_of") or "")[:10]
    row_time = (
        row.get("recommended_at")
        if state_row
        else (row.get("executed_at_time") or row.get("saved_at"))
    )
    return bool(entry_day and str(row_time or "")[:10] == entry_day)


def _recommendation_state_label(
    entry: dict,
    *,
    execution_rows: list[dict],
    executions_available: bool,
    state_rows: list[dict],
) -> str:
    """Describe recommendation lifecycle without treating a recommendation as a fill."""
    if not executions_available:
        return "[状態不明・約定扱い禁止]"

    execution_matches = [
        row for row in execution_rows
        if _recommendation_row_matches(entry, row)
    ]
    # Same-day ticker/direction is only a fallback when it identifies one row.
    # Prefer an exact action/analysis match where multiple recommendations exist.
    if len(execution_matches) > 1:
        exact = [
            row for row in execution_matches
            if (
                str(entry.get("analysis_id") or "")
                and str(entry.get("analysis_id")) == str(row.get("analysis_id") or "")
            )
            or (
                str(entry.get("action") or "").strip()
                and str(entry.get("action") or "").strip() == str(row.get("action") or "").strip()
            )
        ]
        execution_matches = exact
    if len(execution_matches) == 1:
        row = execution_matches[0]
        status = str(row.get("status") or "").lower()
        day = str(row.get("executed_at_time") or row.get("saved_at") or "")[:10]
        short_day = day[5:] if len(day) >= 10 else day or "?"
        if status in {"executed", "partial", "filled", "done"}:
            return f"[約定済 {short_day}]"
        if status == "ordered":
            return f"[発注済・未約定 {short_day}]"

    state_matches = [
        row for row in state_rows
        if _recommendation_row_matches(entry, row, state_row=True)
    ]
    if len(state_matches) > 1:
        exact = [
            row for row in state_matches
            if (
                str(entry.get("analysis_id") or "")
                and str(entry.get("analysis_id")) == str(row.get("analysis_id") or "")
            )
            or (
                str(entry.get("action") or "").strip()
                and str(entry.get("action") or "").strip() == str(row.get("action_detail") or "").strip()
            )
        ]
        state_matches = exact
    if len(state_matches) == 1:
        status = str(state_matches[0].get("status") or "").lower()
        if status == "cancelled":
            return "[CANCELLED]"
        if status == "expired":
            return "[expired・未約定]"
        if status in {"filled", "executed"}:
            # action_state alone is not proof of a broker fill.
            return "[状態不整合・約定扱い禁止]"

    return "[推奨のみ・未約定]"


def _load_earnings_blackout(within_business_days: int = 5) -> set:
    """earnings_hedge_suggestions.json から決算 0〜N 営業日以内の銘柄集合を返す。"""
    eh_path = BASE_DIR / "earnings_hedge_suggestions.json"
    if not eh_path.exists():
        return set()
    try:
        data = json.loads(eh_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    blackout = set()
    for entry in list(data.get("suggestions") or []) + list(data.get("skipped") or []):
        bdays = entry.get("business_days", entry.get("bdays"))
        tk    = entry.get("ticker")
        if tk and bdays is not None:
            try:
                if 0 <= int(bdays) <= within_business_days:
                    blackout.add(tk)
            except Exception:
                continue
    return blackout


def _estimate_action_jpy(action: dict, holdings_price_map: dict, fx_rate: float) -> float:
    """
    priority_action の概算金額を JPY で推定する。
    継続積立 (毎日/毎月) は inf 扱い (filter されない)。推定不能なら -1。
    """
    import re
    # 明示的な amount_jpy があればそれを優先
    if action.get("amount_jpy"):
        try:
            return abs(float(action["amount_jpy"]))
        except Exception:
            pass
    hint = str(action.get("amount_hint") or "")
    body = str(action.get("action") or "")
    text = hint or body
    if not text:
        return -1.0
    # 継続フロー (一度設定すれば終わり) は filter から除外
    if any(k in text for k in ("毎日", "毎月", "毎週", "毎営業日", "自動積立", "DCA設定")):
        return float("inf")
    # ¥N万 / N万円 表記
    m = re.search(r"[¥￥]?\s*([\d][\d,]*\.?\d*)\s*(万円|万)", text)
    if m:
        return float(m.group(1).replace(",", "")) * 10_000
    # ¥NNN,NNN 表記
    m = re.search(r"[¥￥]\s*([\d][\d,]*)", text)
    if m:
        return float(m.group(1).replace(",", ""))
    ticker = str(action.get("ticker") or "").upper()
    # Investment trusts are specified in JPY, not NAV "units".  Treating
    # e.g. 100口 as 100 shares multiplied by a quoted NAV caused enormous
    # fictional notionals.  A quantity-only fund instruction is deliberately
    # unpriceable and the existing hard-cap path rejects it fail-closed.
    if ticker.startswith(("SLIM_", "IFREE_", "MNXACT", "NOMURA_")):
        return -1.0
    # N株/N口 表記 + ticker 価格
    m = re.search(r"([\d][\d,]*)\s*(?:株|口)", text)
    if m:
        try:
            shares  = float(m.group(1).replace(",", ""))
            ticker  = action.get("ticker") or ""
            info    = holdings_price_map.get(ticker, {})
            price   = _unit_price_for_notional(action, info)
            currency = action.get("currency") or info.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")
            if price > 0:
                return shares * price * (fx_rate if currency == "USD" else 1.0)
        except Exception:
            pass
    return -1.0


def _reindex_final_action_ranks(actions: list[dict]) -> list[dict]:
    """Give surviving recommendations contiguous display ranks.

    The model rank is retained as ``source_rank`` for auditability.  Ranking
    happens only after filtering, so a rejected #2 can never leave the user
    with a confusing #1, #3 board or Telegram sequence.
    """
    indexed = [(index, action) for index, action in enumerate(actions) if isinstance(action, dict)]

    def _rank_key(item: tuple[int, dict]) -> tuple[int, int]:
        index, action = item
        try:
            return int(action.get("rank")), index
        except (TypeError, ValueError):
            return 9999, index

    indexed.sort(key=_rank_key)
    ranked: list[dict] = []
    for display_rank, (_index, action) in enumerate(indexed, 1):
        source_rank = action.get("source_rank", action.get("rank"))
        if source_rank not in (None, ""):
            action["source_rank"] = source_rank
        action["rank"] = display_rank
        action["display_rank"] = display_rank
        ranked.append(action)
    return ranked


def _set_operational_stance(
    synthesis: dict,
    reason_counts: dict,
    executable_count: int,
    *,
    actions: list[dict] | None = None,
) -> None:
    """Keep model market stance separate from what may be acted on today."""
    if executable_count > 0:
        ready = [
            action for action in (actions or [])
            if isinstance(action, dict) and action.get("execution_readiness") == "ready"
        ]
        low_urgency_exits_only = bool(ready) and all(
            _direction_of(action.get("type")) == "sell"
            and str(action.get("urgency") or "medium").lower() == "low"
            for action in ready
        )
        if low_urgency_exits_only:
            state = {
                "code": "optional_exit_only",
                "label": "任意整理のみ",
                "reason": "安全ゲートを通過した候補は低緊急度の売却・トリムのみです。見送りも可能です",
            }
        else:
            state = {
                "code": "actionable",
                "label": "実行候補あり",
                "reason": "安全ゲートを通過した候補があります",
            }
    elif reason_counts.get("market_closed_reprice_required"):
        state = {
            "code": "await_market_reprice",
            "label": "休場明けの再評価待ち",
            "reason": "休場日に生成された指値は次の取引セッションで価格・板を更新してから再提案します",
        }
    elif any(str(code).startswith(("portfolio_snapshot", "technical_data")) for code in reason_counts):
        state = {
            "code": "await_data_refresh",
            "label": "データ更新待ち",
            "reason": "口座・保有またはテクニカルデータが古いため、新規発注を保留します",
        }
    elif reason_counts:
        state = {
            "code": "review_required",
            "label": "確認待ち",
            "reason": "市場スタンスとは別に、現在の候補は安全ゲートの確認が必要です",
        }
    else:
        state = {
            "code": "observe",
            "label": "観察継続",
            "reason": "実行候補はありません",
        }
    synthesis["operational_stance"] = state


def _unit_price_for_notional(action: dict, price_info: dict | None = None) -> float:
    """Return the per-share price used for notional risk checks."""
    price_info = price_info or {}

    def _as_positive_float(value) -> float:
        try:
            n = float(value)
            return n if n > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    atype = str(action.get("type") or action.get("action_type") or "").lower()
    if atype in {"buy", "add", "dca", "margin_buy"}:
        limit_price = _as_positive_float(action.get("limit_price"))
        if limit_price > 0:
            return limit_price
        for value in (
            action.get("decision_price"),
            action.get("price"),
            price_info.get("current_price"),
            price_info.get("price"),
        ):
            price = _as_positive_float(value)
            if price > 0:
                return price
        return 0.0

    for value in (
        price_info.get("current_price"),
        action.get("decision_price"),
        action.get("limit_price"),
        action.get("price"),
        price_info.get("price"),
    ):
        price = _as_positive_float(value)
        if price > 0:
            return price
    return 0.0


_NOTIONAL_EQUATION_RE = re.compile(
    r"[¥￥]\s*[\d,.]+\s*[×xX*]\s*[\d,]+\s*(?:株|口)?\s*=\s*[¥￥]\s*[\d,.]+"
)


def _normalize_notional_equation(action: dict, price_info: dict | None = None) -> dict:
    """Replace an LLM arithmetic equation with the deterministic order notional."""
    reason = str(action.get("reason") or "")
    match = _NOTIONAL_EQUATION_RE.search(reason)
    if not match:
        return action
    quantity = _parse_amount_hint_shares(action)
    unit_price = _unit_price_for_notional(action, price_info)
    if quantity <= 0 or unit_price <= 0:
        return action

    label = quantity_label_for_ticker(action.get("ticker"))
    price_text = f"{unit_price:,.4f}".rstrip("0").rstrip(".")
    corrected = f"¥{price_text}×{quantity}{label}=¥{unit_price * quantity:,.0f}"
    original = match.group(0)
    if original == corrected:
        return action
    updated = dict(action)
    updated["reason"] = reason[: match.start()] + corrected + reason[match.end() :]
    updated["notional_claim_corrected"] = True
    updated["notional_claim_original"] = original
    return updated


def _normalize_jpx_action_units(synthesis: dict) -> dict:
    """Canonicalize JPX symbols and keep quantity labels consistent everywhere."""
    if not isinstance(synthesis, dict):
        return synthesis
    actions = synthesis.get("priority_actions") or []
    if not isinstance(actions, list):
        return synthesis
    for action in actions:
        if not isinstance(action, dict):
            continue
        from instrument_metadata import canonical_execution_ticker
        ticker = canonical_execution_ticker(action.get("ticker"))
        action["ticker"] = ticker
        hint = action.get("amount_hint") or ""
        if not ticker.endswith(".T") or not isinstance(hint, str):
            continue
        match = re.search(r"(\d+)\s*(株|口)", hint)
        if not match:
            continue
        quantity = int(match.group(1))
        lot = trading_unit_for_ticker(ticker)
        label = quantity_label_for_ticker(ticker)
        action["execution_trading_unit"] = lot
        policy_final = bool(action.get("policy_size_final"))
        normalized_quantity = quantity
        if quantity % lot != 0 and not policy_final:
            rounded = max(lot, round(quantity / lot) * lot)
            normalized_quantity = rounded
            action.setdefault("_warnings", []).append(
                f"JPX売買単位({lot}{label})に自動調整: {quantity}→{rounded}{label}"
            )
        elif match.group(2) != label:
            action.setdefault("_warnings", []).append(
                f"JPX数量単位の表示を正規化: {quantity}{match.group(2)}→{quantity}{label}"
            )
        replacement = f"{normalized_quantity}{label}"
        action["amount_hint"] = re.sub(r"\d+\s*(?:株|口)", replacement, hint, count=1)
        body = str(action.get("action") or "")
        if body:
            action["action"] = re.sub(
                r"\d[\d,]*\s*(?:株|口)",
                replacement,
                body,
                count=1,
            )
    return synthesis


def _action_confidence_pct(action: dict) -> float:
    try:
        return float(action.get("confidence_pct") or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_ai_bounded_reason(action: dict, *fields: str) -> bool:
    keys = fields or ("ai_override_reason", "bounded_decision_reason")
    return any(str(action.get(key) or "").strip() for key in keys)


def _cap_bounded_action(
    action: dict,
    *,
    gate: str,
    cap_jpy: float,
    estimated_jpy: float,
    min_confidence: float,
) -> tuple[bool, str | None]:
    """Mark an AI-bounded action as capped, or return a fail-closed reason."""
    ticker = action.get("ticker") or "action"
    if _action_confidence_pct(action) < min_confidence:
        return False, (
            f"{gate}: {ticker} は AI bounded 条件の confidence "
            f"{_action_confidence_pct(action):.0f}% < {min_confidence:.0f}%"
        )
    if estimated_jpy < 0 or estimated_jpy == float("inf"):
        return False, f"{gate}: {ticker} は金額推定不能のため bounded cap を検証できない"
    if estimated_jpy > cap_jpy:
        return False, (
            f"{gate}: 推定 ¥{estimated_jpy:,.0f} が cap ¥{cap_jpy:,.0f} を超過"
        )
    action["ai_bounded_gate"] = gate
    action["provisional_decision"] = True
    action["cap_applied_jpy"] = round(cap_jpy, 0)
    action["estimated_action_jpy"] = round(estimated_jpy, 0)
    return True, None


def _load_tax_loss_harvest_tickers(min_loss_jpy: float = 30_000) -> set:
    """損出し候補の ticker セットを返す（Phase 3 #11: DCA/add との矛盾解消）。"""
    out: set = set()
    try:
        from tax_optimizer import analyze_loss_harvest
        r = analyze_loss_harvest(min_loss_jpy=min_loss_jpy)
        for c in (r.get("candidates") or []):
            tk = c.get("ticker")
            if tk:
                out.add(tk)
    except Exception:
        pass
    return out


def _build_consolidated_rebalance_context(positions_raw: list,
                                           total_jpy: float,
                                           fx_rate: float = 150.0) -> str:
    """集中銘柄（>10%）について「一括 trim プラン」を rebalance_planner で計算し、
    Opus に「細切れではなく、まとめて何株 trim するか」を明示する。
    Phase 2 — 細かいリバランス抑制。"""
    if not positions_raw or not total_jpy:
        return ""
    try:
        from rebalance_planner import compute_full_trim_plan, summarize_plans
    except Exception:
        return ""
    plans = []
    for p in positions_raw:
        try:
            tk = p.get("ticker") or ""
            if not tk or tk.startswith("CASH"):
                continue  # 現金は対象外
            v = float(p.get("value_jpy") or 0)
            current_pct = v / total_jpy if total_jpy > 0 else 0
            if current_pct < 0.10:
                continue  # 10% 未満は対象外（過集中のみ）
            price  = float(p.get("current_price") or p.get("price") or 0)
            currency = p.get("currency") or ("JPY" if tk.endswith(".T") else "USD")
            # 目標: 現在の半分 or 最低 5% のうち大きい方（heuristic）
            target_pct = max(0.05, current_pct * 0.5)
            plan = compute_full_trim_plan(
                ticker=tk, current_pct=current_pct, target_pct=target_pct,
                total_jpy=total_jpy, share_price=price,
                currency=currency, fx_rate=fx_rate,
            )
            plans.append(plan)
        except Exception:
            continue
    if not plans:
        return ""
    table = summarize_plans(plans)
    if not table:
        return ""
    lines = [
        "## 集中銘柄の一括 trim プラン（REBALANCE_PLANS — コード計算済み）",
        "※ 以下は rebalance_planner.compute_full_trim_plan の出力（手数料効率＋単元考慮）。",
        "※ priority_actions で trim を提案するときは、この「総株数 / 分割回数」に従って 1〜2 回でまとめること。毎日 1 株ずつの細切れ提案は禁止。",
        "",
        table,
    ]
    return "\n".join(lines)


def _execution_time_key(record: dict) -> str:
    return str(record.get("executed_at_time") or record.get("saved_at") or "")


def _drop_superseded_ordered_executions(rows: list[dict]) -> list[dict]:
    """Drop stale ordered rows after a later terminal fill or cancellation."""
    terminal_statuses = {"executed", "filled", "done", "cancelled", "canceled", "skip"}
    terminal_by_state: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("action_state_id") or "")
        if not sid:
            continue
        status = str(row.get("status") or "").lower()
        if status in terminal_statuses:
            terminal_by_state[sid] = max(terminal_by_state.get(sid, ""), _execution_time_key(row))

    if not terminal_by_state:
        return rows

    pruned: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            pruned.append(row)
            continue
        status = str(row.get("status") or "").lower()
        sid = str(row.get("action_state_id") or "")
        if status == "ordered" and sid and terminal_by_state.get(sid, "") >= _execution_time_key(row):
            continue
        pruned.append(row)
    return pruned


def _load_recent_execution_snapshot(
    days: int = 7,
    *,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return effective DONE_LIST rows and cross-ledger order conflicts."""
    p = BASE_DIR / "action_executions.json"
    if not p.exists():
        return [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    entries = data.get("executions") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    try:
        from order_intent_resolver import resolve_recent_order_intents
        return resolve_recent_order_intents(
            entries or [],
            action_state_path=BASE_DIR / "action_state.json",
            days=days,
            now=now,
        )
    except Exception:
        # The legacy fallback is intentionally conservative for unavailable
        # resolver code during mixed-version deployments.
        from datetime import timedelta
        reference = now or datetime.now()
        cutoff = reference.replace(tzinfo=None) - timedelta(days=days)
        out = []
        for e in entries or []:
            if (e.get("status") or "").lower() not in {"ordered", "executed", "filled", "done"}:
                continue
            try:
                ts = datetime.fromisoformat((e.get("saved_at") or e.get("executed_at_time") or "").split("+")[0])
                if ts >= cutoff:
                    out.append(e)
            except Exception:
                continue
        return _drop_superseded_ordered_executions(out), []


def _load_recent_executions(days: int = 7, *, now: datetime | None = None) -> list:
    """直近 N 日の有効な ordered / executed アクションを返す。"""
    effective, _conflicts = _load_recent_execution_snapshot(days=days, now=now)
    return effective


def _execution_direction(raw: str | None) -> str:
    """Normalize action_executions direction values to economic directions."""
    dr = str(raw or "").lower().strip()
    if dr in {"buy", "add", "dca", "margin_buy"}:
        return "buy"
    if dr in {"sell", "trim", "reduce", "take_profit", "stop_loss"}:
        return "sell"
    if dr in {"short", "short_sell"}:
        return "short"
    if dr in {"cover", "buy_to_cover"}:
        return "cover"
    return ""


def _recent_order_intents_by_direction(
    days: int = 7,
    *,
    now: datetime | None = None,
) -> dict[tuple[str, str], list[dict]]:
    """post-filter 用: (ticker, direction) ごとに直近発注/約定レコードを保持する。"""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for e in _load_recent_executions(days=days, now=now):
        if not isinstance(e, dict):
            continue
        tk = e.get("ticker")
        dr = _execution_direction(e.get("direction") or e.get("type"))
        if not tk or not dr:
            continue
        by_key.setdefault((tk, dr), []).append(dict(e))
    for rows in by_key.values():
        rows.sort(key=lambda x: x.get("saved_at") or x.get("executed_at_time") or "", reverse=True)
    return by_key


def _order_state_conflicts_by_direction(
    days: int = 7,
    *,
    now: datetime | None = None,
) -> dict[tuple[str, str], list[dict]]:
    """Linked ordered rows whose recommendation is terminal.

    They do not participate in DONE_LIST so the investment idea may be
    proposed again, but the proposal is review-only until broker status is
    explicitly reconciled.
    """
    _effective, conflicts = _load_recent_execution_snapshot(days=days, now=now)
    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in conflicts:
        ticker = row.get("ticker")
        direction = _execution_direction(row.get("direction") or row.get("type"))
        if ticker and direction:
            by_key.setdefault((ticker, direction), []).append(dict(row))
    for rows in by_key.values():
        rows.sort(key=_execution_time_key, reverse=True)
    return by_key


def _open_action_state_by_direction() -> dict[tuple[str, str], list[dict]]:
    """Open action_state entries keyed by economic direction for inverse-order checks."""
    p = BASE_DIR / "action_state.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw_actions = data.get("actions") if isinstance(data, dict) else {}
    if isinstance(raw_actions, dict):
        items = raw_actions.items()
    elif isinstance(raw_actions, list):
        items = [(str(i), row) for i, row in enumerate(raw_actions)]
    else:
        return {}

    by_key: dict[tuple[str, str], list[dict]] = {}
    for action_id, entry in items:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status not in {"pending", "placed"}:
            continue
        ticker = entry.get("ticker")
        direction = _execution_direction(entry.get("action_type") or entry.get("type"))
        if not ticker or direction not in {"buy", "sell", "short", "cover"}:
            continue
        row = dict(entry)
        row.setdefault("id", entry.get("id") or action_id)
        by_key.setdefault((ticker, direction), []).append(row)

    for rows in by_key.values():
        rows.sort(key=lambda x: x.get("recommended_at") or x.get("placed_at") or "", reverse=True)
    return by_key


def _order_intent_tunable_float(key: str, fallback: float) -> float:
    try:
        from tunable_params import get as _tp_get
        value = float(_tp_get(key, fallback))
        return value if math.isfinite(value) else fallback
    except Exception:
        return fallback


def _order_intent_tunable_int(key: str, fallback: int) -> int:
    try:
        from tunable_params import get as _tp_get
        return int(_tp_get(key, fallback))
    except Exception:
        return fallback


_EXECUTION_PLAN_GATE_MODES = {"off", "observe", "enforce"}


def _execution_plan_gate_mode() -> tuple[str, str | None]:
    """execution plan gate の動作モードを返す。(mode, warning)

    off = 分類しない / observe = 分類を注記のみ (既定) / enforce = 非実行候補を除外。
    enforce は tunable_params で明示設定されたときだけ有効。未知値・読込失敗は
    observe に縮退し warning を返す (gate が黙って enforce に化けない)。
    """
    try:
        from tunable_params import get as _tp_get
        raw = _tp_get("execution_plan_gate_mode", "observe")
    except Exception:
        return "observe", None
    mode = str(raw or "observe").strip().lower()
    if mode not in _EXECUTION_PLAN_GATE_MODES:
        return "observe", f"execution_plan_gate_mode_invalid: {raw!r} → observe に縮退"
    if mode == "enforce":
        try:
            require_raw = _tp_get("execution_plan_enforce_require_readiness", True)
            require_readiness = str(require_raw).strip().lower() not in {"false", "0", "off", "no"}
        except Exception:
            require_readiness = True
        if require_readiness:
            try:
                from execution_plan_observer import evaluate_enforce_readiness, load_observations

                readiness = evaluate_enforce_readiness(load_observations())
                if not readiness.get("ready_for_enforce"):
                    blockers = "; ".join(str(x) for x in (readiness.get("blockers") or [])[:3])
                    return "observe", f"execution_plan_enforce_not_ready: {blockers or 'readiness unavailable'}"
            except Exception as exc:
                return "observe", f"execution_plan_enforce_readiness_error: {exc}"
    return mode, None


def _format_done_list_for_prompt(days: int = 7) -> str:
    """直近の発注済み/約定済みアクションを Opus プロンプト向けに整形（DONE_LIST）。
    LLM が同銘柄 × 同方向の重複推奨を出さないように。"""
    execs = _load_recent_executions(days=days)
    if not execs:
        return ""
    by_ticker: dict = {}
    for e in execs:
        tk = e.get("ticker") or "?"
        by_ticker.setdefault(tk, []).append(e)
    conf_min = _order_intent_tunable_float("order_intent_material_confidence_pct", 75.0)
    rank_max = _order_intent_tunable_int("order_intent_material_rank_max", 2)
    multiplier = _order_intent_tunable_float("order_intent_material_multiplier", 1.25)
    lines = [f"## 直近{days}日の発注/約定済みアクション（DONE_LIST — open-order aware）"]
    lines.append(
        "※ 原則は同 ticker × direction の追加発注禁止。ただし active ordered が既存注文では不足し、"
        f"rank<={rank_max}・confidence>={conf_min:.0f}・target_notional_jpy>=既存注文の{multiplier:.2f}倍を満たす場合だけ、"
        "通常発注ではなく既存注文の変更候補として明示する。executed/filled/done は再提案禁止。"
    )
    for tk, items in sorted(by_ticker.items()):
        items_sorted = sorted(items, key=lambda x: x.get("saved_at") or "", reverse=True)[:3]
        parts = []
        for e in items_sorted:
            d  = (e.get("saved_at") or "")[:10]
            dr = e.get("direction") or "?"
            qy = e.get("quantity")
            st = e.get("status") or ""
            parts.append(f"{d}:{dr}{f' {qy}株' if qy else ''}({st})")
        lines.append(f"  {tk}: {' / '.join(parts)}")
    return "\n".join(lines)


def _done_set_by_direction(days: int = 7, *, now: datetime | None = None) -> set:
    """post-filter 用: (ticker, direction) のセットを返す。"""
    return set(_recent_order_intents_by_direction(days=days, now=now).keys())


def _order_intent_positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _order_intent_first_positive(*values) -> float | None:
    for value in values:
        number = _order_intent_positive_float(value)
        if number is not None:
            return number
    return None


def _order_intent_price(record: dict) -> float | None:
    """action_executions の注文価格。spec通り limit_price -> price -> decision_price。"""
    return _order_intent_first_positive(
        record.get("limit_price"),
        record.get("price"),
        record.get("decision_price"),
    )


def _order_intent_record_notional_jpy(record: dict, fx_rate: float) -> float | None:
    cash_delta = record.get("cash_delta")
    try:
        cash_delta_abs = abs(float(cash_delta)) if cash_delta is not None else None
    except (TypeError, ValueError):
        cash_delta_abs = None
    explicit = _order_intent_first_positive(
        record.get("notional_jpy"),
        record.get("estimated_notional_jpy"),
        record.get("amount_jpy"),
        cash_delta_abs,
    )
    if explicit is not None:
        return explicit
    quantity = _order_intent_positive_float(record.get("quantity"))
    price = _order_intent_price(record)
    if quantity is None or price is None:
        return None
    ticker = str(record.get("ticker") or "")
    currency = str(record.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")).upper()
    return quantity * price * (fx_rate if currency == "USD" else 1.0)


def _order_intent_recommended_notional_jpy(action: dict, estimated_action_jpy: float | None) -> float | None:
    if estimated_action_jpy is not None and math.isfinite(estimated_action_jpy) and estimated_action_jpy > 0:
        return estimated_action_jpy
    return _order_intent_first_positive(
        action.get("estimated_notional_jpy"),
        action.get("amount_jpy"),
        action.get("target_notional_jpy"),
        action.get("target_total_notional_jpy"),
    )


def _order_intent_target_notional_jpy(action: dict) -> float | None:
    return _order_intent_first_positive(
        action.get("target_notional_jpy"),
        action.get("target_total_notional_jpy"),
        action.get("desired_notional_jpy"),
    )


def _order_intent_rank(action: dict) -> int | None:
    value = action.get("rank")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or int(number) != number:
        return None
    return int(number)


def _order_intent_min_increment_jpy(action: dict, direction: str, portfolio_total: float) -> float:
    minimum = _order_intent_tunable_float("order_intent_min_increment_jpy", 100_000.0)
    atype = str(action.get("type") or "").lower()
    risk_reduction_sell = direction == "sell" and atype in {"sell", "trim", "reduce", "take_profit", "stop_loss"}
    if not risk_reduction_sell:
        pct = _order_intent_tunable_float("order_intent_min_increment_pct_of_portfolio", 0.0025)
        minimum = max(minimum, float(portfolio_total or 0) * pct)
    return minimum


def _order_intent_base_result(
    *,
    action: dict,
    decision: str,
    filter_rule: str | None = None,
    reason: str | None = None,
    existing: dict | None = None,
    existing_notional_jpy: float | None = None,
    recommended_notional_jpy: float | None = None,
    target_notional_jpy: float | None = None,
    incremental_notional_jpy: float | None = None,
    material_change: bool = False,
    material_change_reasons: list[str] | None = None,
    non_executable: bool = False,
) -> dict:
    result = {
        "order_intent_decision": decision,
    }
    if filter_rule:
        result["filter_rule"] = filter_rule
    if reason:
        result["non_executable_reason"] = reason
        if decision in {"blocked_duplicate_order", "skip_recently_executed"}:
            result["filtered_reason"] = reason
    if non_executable:
        result["non_executable"] = True
        result["execution_state"] = "not_ordered"
    if existing:
        result["existing_order_id"] = existing.get("id") or existing.get("action_state_id")
        result["existing_order_status"] = existing.get("status")
        result["existing_order_quantity"] = existing.get("quantity")
    if existing_notional_jpy is not None:
        result["existing_order_notional_jpy"] = round(existing_notional_jpy)
    if recommended_notional_jpy is not None:
        result["recommended_notional_jpy"] = round(recommended_notional_jpy)
    if target_notional_jpy is not None:
        result["target_notional_jpy"] = round(target_notional_jpy)
    if incremental_notional_jpy is not None:
        result["incremental_notional_jpy"] = round(incremental_notional_jpy)
    if material_change or material_change_reasons:
        result["material_change"] = bool(material_change)
        result["material_change_reasons"] = material_change_reasons or []
    try:
        recommended_quantity = _parse_amount_hint_shares(action)
        if recommended_quantity > 0:
            result["recommended_quantity"] = recommended_quantity
    except Exception:
        pass
    if existing and existing.get("quantity") is not None and target_notional_jpy is None:
        result["target_quantity"] = existing.get("quantity")
        result["executable_delta_quantity"] = 0
    return result


def _classify_order_intent(
    action: dict,
    recent_intents: dict[tuple[str, str], list[dict]] | None,
    *,
    portfolio_total: float,
    fx_rate: float = 150.0,
    estimated_action_jpy: float | None = None,
    done_days: int = 7,
) -> dict:
    """Classify same-direction recent order state without creating executable duplicates."""
    if not isinstance(action, dict):
        return {"order_intent_decision": "new_order"}

    ticker = action.get("ticker") or ""
    direction = _direction_of(action.get("type"))
    if not ticker or direction not in {"buy", "sell", "short", "cover"}:
        return _order_intent_base_result(
            action=action,
            decision="blocked_duplicate_order",
            filter_rule="blocked_duplicate_order",
            reason=f"blocked_duplicate_order: {ticker or 'action'} は type/direction が不明なため重複判定できない",
        )

    intents = list((recent_intents or {}).get((ticker, direction)) or [])
    if not intents:
        return {"order_intent_decision": "new_order"}

    ordered = [e for e in intents if str(e.get("status") or "").lower() == "ordered"]
    terminal = [
        e for e in intents
        if str(e.get("status") or "").lower() in {"executed", "filled", "done"}
    ]

    if not ordered:
        if terminal:
            return _order_intent_base_result(
                action=action,
                decision="skip_recently_executed",
                filter_rule="already_executed",
                reason=f"already_executed: {ticker} {direction} は直近 {done_days} 日に発注/約定済み（DONE_LIST）",
                existing=terminal[0],
            )
        return {"order_intent_decision": "new_order"}

    existing = ordered[0]
    recommended_notional = _order_intent_recommended_notional_jpy(action, estimated_action_jpy)
    existing_notional = _order_intent_record_notional_jpy(existing, fx_rate)
    if existing_notional is None or recommended_notional is None:
        return _order_intent_base_result(
            action=action,
            decision="blocked_duplicate_order",
            filter_rule="blocked_duplicate_order",
            reason=f"blocked_duplicate_order: {ticker} {direction} は既存注文または新規推奨の金額推定が不明",
            existing=existing,
            existing_notional_jpy=existing_notional,
            recommended_notional_jpy=recommended_notional,
        )

    if existing_notional >= recommended_notional * 0.8:
        return _order_intent_base_result(
            action=action,
            decision="keep_existing_order",
            filter_rule="existing_order_covers_intent",
            reason=(
                f"existing_order_covers_intent: {ticker} {direction} は既存注文 "
                f"¥{existing_notional:,.0f} が新規推奨 ¥{recommended_notional:,.0f} を概ね充足"
            ),
            existing=existing,
            existing_notional_jpy=existing_notional,
            recommended_notional_jpy=recommended_notional,
            target_notional_jpy=existing_notional,
            incremental_notional_jpy=0,
            material_change=False,
            material_change_reasons=[],
            non_executable=True,
        )

    target_notional = _order_intent_target_notional_jpy(action)
    confidence = _action_confidence_pct(action)
    rank = _order_intent_rank(action)
    conf_min = _order_intent_tunable_float("order_intent_material_confidence_pct", 75.0)
    rank_max = _order_intent_tunable_int("order_intent_material_rank_max", 2)
    multiplier = _order_intent_tunable_float("order_intent_material_multiplier", 1.25)
    if target_notional is None:
        return _order_intent_base_result(
            action=action,
            decision="blocked_duplicate_order",
            filter_rule="blocked_duplicate_order",
            reason=f"blocked_duplicate_order: {ticker} {direction} は既存注文不足だが target_notional_jpy が無く増額判定不能",
            existing=existing,
            existing_notional_jpy=existing_notional,
            recommended_notional_jpy=recommended_notional,
        )

    incremental = target_notional - existing_notional
    material_reasons = []
    if confidence >= conf_min:
        material_reasons.append(f"confidence>={conf_min:.0f}")
    if rank is not None and rank <= rank_max:
        material_reasons.append(f"rank<={rank_max}")
    if target_notional >= existing_notional * multiplier:
        material_reasons.append(f"target>={multiplier:.2f}x_existing")

    if confidence < conf_min or rank is None or rank > rank_max or target_notional < existing_notional * multiplier:
        return _order_intent_base_result(
            action=action,
            decision="blocked_duplicate_order",
            filter_rule="blocked_duplicate_order",
            reason=(
                f"blocked_duplicate_order: {ticker} {direction} は既存注文不足だが "
                "confidence/rank/target_notional の増額条件を満たさない"
            ),
            existing=existing,
            existing_notional_jpy=existing_notional,
            recommended_notional_jpy=recommended_notional,
            target_notional_jpy=target_notional,
            incremental_notional_jpy=incremental,
            material_change=False,
            material_change_reasons=material_reasons,
        )

    min_increment = _order_intent_min_increment_jpy(action, direction, portfolio_total)
    if incremental < min_increment:
        return _order_intent_base_result(
            action=action,
            decision="keep_existing_order",
            filter_rule="below_minimum_increment",
            reason=(
                f"below_minimum_increment: {ticker} {direction} の増額差分 "
                f"¥{incremental:,.0f} < 最小 ¥{min_increment:,.0f} のため既存注文維持"
            ),
            existing=existing,
            existing_notional_jpy=existing_notional,
            recommended_notional_jpy=recommended_notional,
            target_notional_jpy=target_notional,
            incremental_notional_jpy=incremental,
            material_change=False,
            material_change_reasons=material_reasons,
            non_executable=True,
        )

    material_reasons.append(f"incremental>=¥{min_increment:,.0f}")
    return _order_intent_base_result(
        action=action,
        decision="amend_existing_order",
        filter_rule="order_amendment_required",
        reason=(
            f"order_amendment_required: {ticker} {direction} は既存注文 "
            f"¥{existing_notional:,.0f} から目標 ¥{target_notional:,.0f} への変更候補"
        ),
        existing=existing,
        existing_notional_jpy=existing_notional,
        recommended_notional_jpy=recommended_notional,
        target_notional_jpy=target_notional,
        incremental_notional_jpy=incremental,
        material_change=True,
        material_change_reasons=material_reasons,
        non_executable=True,
    )


def _format_recent_own_recs_for_prompt(days: int = 14, max_entries: int = 30) -> str:
    """直近 N 日の自分の推奨履歴を Opus プロンプト向けに整形。
    LLM が自己矛盾（買い→売りフリップ）を回避できるように渡す。
    約定事実は action_executions.json のみを正本として状態を明記する。"""
    recs = _load_recent_recommendations(days=days)
    if not recs:
        return ""
    execution_rows, executions_available = _load_recommendation_execution_rows()
    state_rows = _load_recommendation_action_states()
    by_ticker: dict = {}
    for e in recs:
        tk = e.get("ticker") or "?"
        by_ticker.setdefault(tk, []).append(e)
    lines = [f"## 直近{days}日の自分の推奨履歴（自己矛盾チェック用 — RECENT_OWN_RECS）"]
    lines.append("※ 同一銘柄に逆方向（buy ↔ sell/trim）の推奨を出す場合は、市況の有意な変化を reason に明示する義務がある。")
    lines.append("※ これは推奨履歴であり、[約定済] 以外は未約定。推奨を「実行済み」「売却済み」と表現してはならない。約定の正本は action_executions.json のみ。")
    lines.append("※ [CANCELLED] はユーザーが明示的にキャンセル済み — 同一意図の再提案禁止（type を変えての回避も含む）。")
    count = 0
    for tk, entries in sorted(by_ticker.items()):
        entries_sorted = sorted(entries, key=lambda x: x.get("as_of") or "", reverse=True)[:5]
        items = []
        for e in entries_sorted:
            d = (e.get("as_of") or "")[:10]
            t = e.get("type") or "?"
            u = e.get("urgency") or ""
            marker = _recommendation_state_label(
                e,
                execution_rows=execution_rows,
                executions_available=executions_available,
                state_rows=state_rows,
            )
            items.append(f"{d}:{t}{('('+u+')') if u else ''} {marker}")
            count += 1
        lines.append(f"  {tk}: {' / '.join(items)}")
        if count >= max_entries:
            break
    return "\n".join(lines)


def _is_cumulative_buy_action(action: dict, atype_lc: str) -> bool:
    """定期/自動積立、または誤ったつみたて枠スポット買い提案かを判定する。"""
    src = str(action.get("source") or "").lower()
    if src == "dca_ladder":
        return False
    if atype_lc == "dca":
        return True

    text = " ".join(
        str(action.get(k) or "")
        for k in ("action", "reason", "amount_hint", "execution_note")
    )
    text_lc = text.lower()
    one_shot_markers = ("一括", "スポット", "一時買付", "一時買い", "単発")
    has_one_shot_marker = any(k in text for k in one_shot_markers)
    tsumitate_frame_markers = (
        "NISAつみたて",
        "つみたて投資枠",
        "つみたて枠",
        "積立投資枠",
    )
    growth_frame_markers = (
        "NISA成長",
        "成長投資枠",
        "成長枠",
    )
    has_tsumitate_frame = any(k in text for k in tsumitate_frame_markers)
    has_growth_frame = any(k in text for k in growth_frame_markers)
    if has_one_shot_marker:
        if has_tsumitate_frame and not has_growth_frame:
            return True
        return False

    recurring_markers = (
        "クレカ",
        "自動積立",
        "積立設定",
        "積立を設定",
        "積立開始",
        "積立を開始",
        "積立増額",
        "積立を増額",
        "月次積立",
        "定期積立",
        "毎月",
        "毎週",
        "毎日",
        "毎営業日",
        "monthly contribution",
        "dca設定",
    )
    if any(k in text for k in recurring_markers) or "monthly contribution" in text_lc:
        return True
    if ("積立" in text or "つみたて" in text) and not has_one_shot_marker:
        return True
    return False


def _non_executable_action_reason(action: dict) -> str | None:
    """Return a filter reason when an action explicitly says it should not be issued."""
    # Codex round6 #2: 上流 (rebalance_engine の degraded / NISA 保護等) が明示的に
    # executable=False / observe_only=True を立てた action は deterministic に除去する。
    # rebalance_medium は prompt に JSON 注入されるため、LLM が priority_actions へ
    # 写した場合でもここで実行候補から外す (status だけでは抑制にならない)。
    if action.get("executable") is False or action.get("observe_only") is True:
        ticker = action.get("ticker") or "action"
        why = action.get("suppressed_reason") or "executable=False/observe_only=True"
        return f"non_executable_flag: {ticker} は実行不可指定 ({why}) のため実行候補から除去"

    text = " ".join(
        str(action.get(k) or "")
        for k in ("action", "reason", "amount_hint", "execution_note")
    )
    text_lc = text.lower()
    try:
        confidence = float(action.get("confidence_pct") or 0)
    except Exception:
        confidence = 0.0

    zero_share = bool(re.search(r"(?<!\d)0\s*株", text)) or bool(re.search(r"\b0\s+shares?\b", text_lc))
    explicit_noop = any(
        marker in text
        for marker in ("除外", "出さない", "発注しない", "注文しない")
    ) or any(
        marker in text_lc
        for marker in ("do not issue", "do not order", "no action", "0 shares")
    ) or zero_share
    duplicate_order_note = ("注文中" in text or "既に" in text) and explicit_noop
    zero_confidence_noop = confidence <= 0 and explicit_noop

    if duplicate_order_note or zero_confidence_noop:
        ticker = action.get("ticker") or "action"
        return f"non_executable_action: {ticker} は既存注文/0株/除外指定のため実行候補から除去"
    return None


def _decision_boundary_class(reason: str) -> str:
    tag = str(reason or "").split(":", 1)[0]
    hard_prefixes = {
        "insider_restricted",
        "non_executable_flag",
        "non_executable_action",
        "disable_stop_loss_recommendations",
        "disable_cumulative_recommendations",
        "already_executed",
        "blocked_duplicate_order",
        "leverage_health",
        "unknown_action_type",
        "policy_engine_error",
        "post_filter_error",
        "policy__rule_ledger_integrity",
        "policy__rule_var_budget",
        "policy__rule_dd_stage",
        "policy__rule_leverage_block",
        "policy__rule_cvar_unstable",
        "policy__rule_vix_extreme",
        "policy_size_collapsed",
    }
    ai_bounded_prefixes = {
        "tax_loss_harvest_conflict",
        "earnings_blackout",
        "too_small",
        "rebalance_cooldown",
        "source_observe_only",
        "plan_wait_for_better_candidate",
        "plan_over_budget",
        "plan_unmatched_no_override",
    }
    if tag in hard_prefixes or tag.startswith("policy_rule_error"):
        return "hard_safety"
    if tag in ai_bounded_prefixes:
        return "ai_bounded_rejected"
    if tag.startswith("policy__rule_earnings_blackout"):
        return "ai_bounded_rejected"
    return "context_only"


def _build_decision_boundary_audit(
    kept: list,
    filtered: list,
    annotated: list,
    *,
    context_blocks: dict | None = None,
) -> dict:
    boundaries = [
        {"name": "insider_restrictions", "mode": "hard_safety"},
        {"name": "ledger_integrity", "mode": "hard_safety"},
        {"name": "var_dd_leverage", "mode": "hard_safety"},
        {"name": "unknown_action_type", "mode": "hard_safety"},
        {"name": "observe_only_sources", "mode": "ai_bounded"},
        {"name": "tax_loss_harvest_conflict", "mode": "ai_bounded"},
        {"name": "earnings_blackout", "mode": "ai_bounded"},
        {"name": "cooldown", "mode": "ai_bounded_annotation"},
        {"name": "too_small", "mode": "ai_bounded"},
        {"name": "execution_plan", "mode": "ai_bounded_or_context"},
        {"name": "display_only_information_lanes", "mode": "context_verdict_required"},
    ]
    promoted = []
    for action in kept:
        if not isinstance(action, dict):
            continue
        if action.get("provisional_decision") or action.get("ai_bounded_gate"):
            promoted.append({
                "ticker": action.get("ticker"),
                "type": action.get("type"),
                "gate": action.get("ai_bounded_gate") or "provisional_decision",
                "cap_applied_jpy": action.get("cap_applied_jpy"),
                "source_lane": action.get("source_lane"),
                "source_observe_only": bool(action.get("source_observe_only")),
            })
    rejected = []
    rejected_counts: dict[str, int] = {}
    for action in filtered:
        if not isinstance(action, dict):
            continue
        reason = str(action.get("filtered_reason") or "")
        cls = _decision_boundary_class(reason)
        rejected_counts[cls] = rejected_counts.get(cls, 0) + 1
        rejected.append({
            "ticker": action.get("ticker"),
            "type": action.get("type"),
            "class": cls,
            "reason": reason,
        })
    annotated_rows = [
        {
            "ticker": action.get("ticker"),
            "type": action.get("type"),
            "warning": action.get("cooldown_warning") or action.get("rebal_cooldown_warning"),
        }
        for action in annotated
        if isinstance(action, dict)
    ]
    return {
        "boundaries": boundaries,
        "promoted_count": len(promoted),
        "rejected_counts": rejected_counts,
        "context_blocks_present": {
            key: bool(value)
            for key, value in (context_blocks or {}).items()
            if key in {"ipo_watch", "news_topic", "social_topic", "geopolitical", "disclosure", "catalyst"}
        },
        "promoted": promoted,
        "rejected": rejected[:25],
        "annotated": annotated_rows[:25],
    }


def _ensure_information_lane_verdicts(synthesis: dict) -> dict:
    if not isinstance(synthesis, dict):
        return synthesis
    blocks = synthesis.get("context_blocks") if isinstance(synthesis.get("context_blocks"), dict) else {}
    verdicts = synthesis.get("information_lane_verdicts")
    if not isinstance(verdicts, list):
        verdicts = []
    covered = {
        str(v.get("lane"))
        for v in verdicts
        if isinstance(v, dict) and v.get("lane")
    }
    for lane, present in blocks.items():
        if lane == "alpha_modules" or not present or lane in covered:
            continue
        verdicts.append({
            "lane": lane,
            "verdict": "ignore",
            "verdict_reason": (
                "missing_verdict: LLM did not return an explicit "
                f"information_lane_verdict for {lane}"
            ),
        })
    synthesis["information_lane_verdicts"] = verdicts
    return synthesis


def _jp_disclosure_signal_strength(row: dict) -> float:
    values: list[float] = []
    try:
        values.append(abs(float(row.get("directional_score") or 0)) * float(
            row.get("directional_confidence") or 0
        ))
        values.append(abs(float(row.get("guidance_revision_pct") or 0)))
        values.append(abs(float(row.get("monthly_yoy_pct") or 0)))
        values.append(min(1.0, float(row.get("insider_cluster_score") or 0) / 3.0))
    except (TypeError, ValueError):
        pass
    if row.get("activist_flag") is True:
        values.append(1.0)
    if row.get("dilution_flag") is True:
        try:
            values.append(max(0.1, abs(float(row.get("dilution_pct") or 0.5))))
        except (TypeError, ValueError):
            values.append(0.5)
    if row.get("going_concern_flag") is True:
        values.append(1.0)
    return max(values or [0.0])


def _jp_buy_action_exists(synthesis: dict) -> bool:
    actions = synthesis.get("priority_actions") or []
    return any(
        isinstance(action, dict)
        and str(action.get("ticker") or "").endswith(".T")
        and str(action.get("type") or "").lower() in {"buy", "add", "dca", "margin_buy"}
        for action in actions
    )


def _ranked_observe_only_jp_disclosure_items(
    synthesis: dict,
    *,
    min_strength: float = 0.30,
    limit: int = 3,
) -> list[dict]:
    brief = synthesis.get("disclosure_brief") if isinstance(synthesis, dict) else {}
    if not isinstance(brief, dict):
        return []
    items = brief.get("items")
    if not isinstance(items, list):
        return []
    brief_observe_only = brief.get("observe_only") is True
    ranked: list[tuple[float, int, dict]] = []
    seen: set[str] = set()
    for idx, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip()
        if not ticker or ticker in seen:
            continue
        market = str(row.get("market") or "").upper()
        if not (ticker.endswith(".T") or market == "JP"):
            continue
        if not (brief_observe_only or row.get("observe_only") is True):
            continue
        strength = _jp_disclosure_signal_strength(row)
        if strength < min_strength:
            continue
        seen.add(ticker)
        ranked.append((-strength, idx, row))
    ranked.sort()
    return [row for _strength, _idx, row in ranked[:limit]]


def _disclosure_verdict_covers_ticker(verdict: dict, ticker: str) -> bool:
    if not isinstance(verdict, dict) or verdict.get("lane") != "disclosure":
        return False
    raw = str(verdict.get("ticker") or "")
    return ticker in {part for part in re.split(r"[/,\s]+", raw) if part}


def _annotate_jp_disclosure_observe_only_boundary(synthesis: dict) -> dict:
    if not isinstance(synthesis, dict):
        return synthesis
    items = _ranked_observe_only_jp_disclosure_items(synthesis)
    if not items:
        return synthesis

    verdicts = synthesis.get("information_lane_verdicts")
    if not isinstance(verdicts, list):
        verdicts = []
    for row in items:
        ticker = str(row.get("ticker") or "").strip()
        if any(_disclosure_verdict_covers_ticker(verdict, ticker) for verdict in verdicts):
            continue
        verdicts.append({
            "lane": "disclosure",
            "ticker": ticker,
            "verdict": "ignore",
            "verdict_reason": (
                "observe_only JP disclosure signal; certified/provisional action "
                "metadata is required before promotion to priority_actions"
            ),
            "adopted_as": "なし",
        })
    synthesis["information_lane_verdicts"] = verdicts

    if not _jp_buy_action_exists(synthesis):
        tickers = ",".join(str(row.get("ticker") or "").strip() for row in items)
        reason = (
            f"jp_disclosure_observe_only={len(items)} tickers={tickers} "
            "require certification or source_observe_only/provisional_decision metadata before actionization"
        )
        reasons = synthesis.get("jp_no_buy_rationale")
        if not isinstance(reasons, list):
            reasons = []
        if reason not in reasons:
            reasons.append(reason)
        synthesis["jp_no_buy_rationale"] = reasons
    return synthesis


def _compact_num(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n.is_integer():
        return str(int(n))
    return f"{n:.1f}".rstrip("0").rstrip(".")


def _augment_no_jp_buy_rationale(synthesis: dict, data: dict) -> dict:
    if not isinstance(synthesis, dict) or not isinstance(data, dict):
        return synthesis
    actions = synthesis.get("priority_actions") or []
    if _jp_buy_action_exists(synthesis):
        synthesis.pop("jp_no_buy_rationale", None)
        return synthesis

    reasons: list[str] = []
    sm = data.get("scenario_monitoring") or {}
    active = sm.get("active_scenarios") if isinstance(sm, dict) else None
    observe = sm.get("observe_only_scenarios") if isinstance(sm, dict) else None
    if active:
        reasons.append(f"scenario_active={len(active)} but no JP buy survived final gates")
    if observe:
        reasons.append(f"observe_only_scenarios={len(observe)} require capped provisional metadata before actionization")

    screening = data.get("screening") or {}
    if isinstance(screening, dict):
        jp_screen = screening.get("jp_screen_candidates") or [
            c for c in (screening.get("short_candidates") or [])
            if isinstance(c, dict) and str(c.get("ticker") or "").endswith(".T")
        ]
        jp_margin = [
            c for c in (screening.get("margin_long_candidates") or [])
            if isinstance(c, dict) and str(c.get("ticker") or "").endswith(".T")
        ]
        top_jp_candidate_tickers: list[str] = []
        if jp_screen or jp_margin:
            reasons.append(f"jp_screening_candidates={len(jp_screen)} jp_margin_candidates={len(jp_margin)} were comparison inputs, not forced buys")
            jp_screen_tickers = [
                str(c.get("ticker"))
                for c in jp_screen
                if isinstance(c, dict) and c.get("ticker")
            ][:6]
            jp_margin_tickers = [
                str(c.get("ticker"))
                for c in jp_margin
                if isinstance(c, dict) and c.get("ticker")
            ][:6]
            if jp_screen_tickers:
                reasons.append(f"jp_screening_tickers={','.join(jp_screen_tickers)}")
            if jp_margin_tickers:
                reasons.append(f"jp_margin_tickers={','.join(jp_margin_tickers)}")
            if jp_screen:
                top_screen = max(
                    [c for c in jp_screen if isinstance(c, dict)],
                    key=lambda c: float(c.get("score") or c.get("composite_score") or 0),
                    default=None,
                )
                if top_screen:
                    score = _compact_num(top_screen.get("score") or top_screen.get("composite_score") or 0)
                    ticker = str(top_screen.get("ticker") or "")
                    if ticker:
                        top_jp_candidate_tickers.append(ticker)
                    reasons.append(
                        f"top_jp_screen_candidate={top_screen.get('ticker')}:{top_screen.get('ai_signal', '?')}:{score}"
                    )
            if jp_margin:
                top_margin = max(
                    [c for c in jp_margin if isinstance(c, dict)],
                    key=lambda c: float(c.get("score") or c.get("composite_score") or 0),
                    default=None,
                )
                if top_margin:
                    score = _compact_num(top_margin.get("score") or top_margin.get("composite_score") or 0)
                    ticker = str(top_margin.get("ticker") or "")
                    if ticker and ticker not in top_jp_candidate_tickers:
                        top_jp_candidate_tickers.append(ticker)
                    reasons.append(f"top_jp_margin_candidate={top_margin.get('ticker')}:{score}")
        generated_jp_tickers: set[str] = set()
        for key in ("priority_actions", "raw_priority_actions", "post_policy_priority_actions"):
            for action in synthesis.get(key) or []:
                if not isinstance(action, dict):
                    continue
                ticker = str(action.get("ticker") or "")
                if ticker.endswith(".T"):
                    generated_jp_tickers.add(ticker)
        jp_rejected: list[str] = []
        for row in synthesis.get("_filtered_actions") or []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "")
            atype = str(row.get("type") or row.get("action_type") or "").lower()
            if not ticker.endswith(".T") or atype not in {"buy", "add", "dca", "margin_buy"}:
                continue
            generated_jp_tickers.add(ticker)
            reason_tag = str(row.get("filtered_reason") or "filtered").split(":", 1)[0]
            jp_rejected.append(f"{ticker}:{reason_tag}")
        if jp_rejected:
            reasons.append(f"post_filter_jp_rejected={','.join(jp_rejected[:6])}")
        not_emitted = [
            ticker for ticker in top_jp_candidate_tickers
            if ticker and ticker not in generated_jp_tickers
        ]
        if not_emitted:
            reasons.append(f"jp_candidates_not_emitted_by_synthesis={','.join(not_emitted[:6])}")
    non_jp_buys = [
        str(action.get("ticker"))
        for action in actions
        if isinstance(action, dict)
        and str(action.get("ticker") or "")
        and not str(action.get("ticker") or "").endswith(".T")
        and str(action.get("type") or action.get("action_type") or "").lower() in {"buy", "add", "dca", "margin_buy"}
    ][:6]
    if non_jp_buys:
        reasons.append(f"final_non_jp_buy_tickers={','.join(non_jp_buys)}")

    pf = synthesis.get("post_filter") if isinstance(synthesis.get("post_filter"), dict) else {}
    if pf.get("filtered_count"):
        reasons.append(f"post_filter_filtered={pf.get('summary') or {}}")
    policy = synthesis.get("policy_decision") if isinstance(synthesis.get("policy_decision"), dict) else {}
    if policy.get("rejected_count"):
        reasons.append(f"policy_rejected_count={policy.get('rejected_count')}")

    try:
        rb_path = BASE_DIR / "rebalance_report.json"
        if rb_path.exists():
            rb = json.loads(rb_path.read_text(encoding="utf-8"))
            cur = ((rb.get("currency") or {}).get("currencies") or {})
            if cur:
                reasons.append(f"rebalance_currency={cur}")
    except Exception:
        pass

    synthesis["jp_no_buy_rationale"] = reasons or ["no JP buy/add/dca/margin_buy action emitted by final synthesis"]
    if not actions and "no_action_rationale" not in synthesis:
        synthesis["no_action_rationale"] = "日本株 buy なし: " + "; ".join(synthesis["jp_no_buy_rationale"][:3])
    return synthesis


def _augment_no_margin_short_rationale(
    synthesis: dict,
    data: dict,
    *,
    margin_long_analysis: dict | None = None,
    short_selling_analysis: dict | None = None,
) -> dict:
    if not isinstance(synthesis, dict):
        return synthesis
    if not isinstance(data, dict):
        data = {}

    actions = synthesis.get("priority_actions") or []
    action_types = {
        str(action.get("type") or "").lower()
        for action in actions
        if isinstance(action, dict)
    }
    if "margin_buy" in action_types:
        synthesis.pop("margin_no_buy_rationale", None)
    else:
        reasons: list[str] = []
        screening = data.get("screening") if isinstance(data.get("screening"), dict) else {}
        ml_cands = screening.get("margin_long_candidates") or []
        if screening.get("margin_long_blocked") is True:
            reasons.append("margin_long_blocked=True")
        if isinstance(ml_cands, list):
            reasons.append(f"margin_long_candidates={len(ml_cands)}")
            tickers = [
                str(c.get("ticker"))
                for c in ml_cands
                if isinstance(c, dict) and c.get("ticker")
            ][:6]
            if tickers:
                reasons.append(f"margin_candidate_tickers={','.join(tickers)}")
        leverage = synthesis.get("leverage_health") if isinstance(synthesis.get("leverage_health"), dict) else {}
        if leverage:
            reasons.append(
                f"leverage_health.margin_buy_allowed={leverage.get('margin_buy_allowed', True)} "
                f"status={leverage.get('status', '?')}"
            )
        converted = [
            str(action.get("ticker"))
            for action in actions
            if isinstance(action, dict)
            and action.get("margin_buy_converted_to_buy") is True
            and action.get("ticker")
        ][:6]
        if converted:
            reasons.append(f"margin_buy_converted_to_cash_buy={','.join(converted)}")
        margin_candidate_tickers = {
            str(c.get("ticker"))
            for c in (screening.get("margin_long_candidates") or [])
            if isinstance(c, dict) and c.get("ticker")
        }
        adopted_as_cash = [
            str(action.get("ticker"))
            for action in actions
            if isinstance(action, dict)
            and str(action.get("ticker") or "") in margin_candidate_tickers
            and str(action.get("type") or action.get("action_type") or "").lower() in {"buy", "add", "dca"}
        ][:6]
        if adopted_as_cash:
            reasons.append(f"margin_candidate_adopted_as_cash_buy={','.join(adopted_as_cash)}")
        if isinstance(margin_long_analysis, dict):
            source = margin_long_analysis.get("_source") or margin_long_analysis.get("model_used")
            if source:
                reasons.append(f"margin_tier_source={source}")
            for note in margin_long_analysis.get("margin_actions") or []:
                if not isinstance(note, dict):
                    continue
                text_parts = [
                    str(note.get("action") or "").strip(),
                    str(note.get("reason") or "").strip(),
                ]
                text = " / ".join(part for part in text_parts if part)
                if text:
                    reasons.append(f"margin_tier_no_margin_reason={text[:160]}")
                    break
            tier_actions = margin_long_analysis.get("priority_actions") or []
            tier_candidates = []
            for action in tier_actions:
                if not isinstance(action, dict):
                    continue
                ticker = str(action.get("ticker") or "")
                if not ticker:
                    continue
                urgency = str(action.get("urgency") or "?")
                conf = _compact_num(action.get("confidence_pct") or action.get("confidence") or 0)
                tier_candidates.append(f"{ticker}:{urgency}:{conf}")
            if tier_candidates:
                reasons.append(f"margin_tier_candidates={','.join(tier_candidates[:6])}")
                final_margin_tickers = {
                    str(action.get("ticker"))
                    for action in actions
                    if isinstance(action, dict)
                    and str(action.get("type") or "").lower() == "margin_buy"
                    and action.get("ticker")
                }
                unadopted = [
                    item.split(":", 1)[0]
                    for item in tier_candidates
                    if item.split(":", 1)[0] not in final_margin_tickers
                ]
                if unadopted:
                    reasons.append(f"margin_tier_not_used_as_margin={','.join(unadopted[:6])}")
        margin_rejected = []
        for row in synthesis.get("_filtered_actions") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "").lower() != "margin_buy":
                continue
            ticker = str(row.get("ticker") or "?")
            reason_tag = str(row.get("filtered_reason") or "filtered").split(":", 1)[0]
            margin_rejected.append(f"{ticker}:{reason_tag}")
        if margin_rejected:
            reasons.append(f"post_filter_margin_rejected={','.join(margin_rejected[:6])}")
        if reasons:
            synthesis["margin_no_buy_rationale"] = reasons
        else:
            synthesis.pop("margin_no_buy_rationale", None)

    if "short" in action_types:
        synthesis.pop("short_no_action_rationale", None)
    else:
        reasons = []
        screening = data.get("screening") if isinstance(data.get("screening"), dict) else {}
        short_cands = screening.get("short_candidates") or []
        if isinstance(short_cands, list):
            reasons.append(f"short_candidates={len(short_cands)}")
            tickers = [
                str(c.get("ticker"))
                for c in short_cands
                if isinstance(c, dict) and c.get("ticker")
            ][:6]
            if tickers:
                reasons.append(f"short_candidate_tickers={','.join(tickers)}")
        meta = screening.get("short_candidates_meta")
        if not isinstance(meta, dict):
            meta = {}
        for key in ("scanned", "shortable_count", "vix_blocked"):
            if key in meta:
                reasons.append(f"{key}={meta.get(key)}")
        opportunities = synthesis.get("short_opportunities")
        if isinstance(opportunities, list):
            reasons.append(f"short_opportunities={len(opportunities)}")
        short_note = synthesis.get("short_not_recommended")
        if short_note:
            reasons.append(f"short_not_recommended={str(short_note)[:120]}")
        if isinstance(short_selling_analysis, dict):
            source = short_selling_analysis.get("_source") or short_selling_analysis.get("model_used")
            if source:
                reasons.append(f"short_tier_source={source}")
            tier_note = short_selling_analysis.get("short_not_recommended")
            if tier_note:
                reasons.append(f"short_tier_not_recommended={str(tier_note)[:120]}")
        short_rejected = []
        for row in synthesis.get("_filtered_actions") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "").lower() != "short":
                continue
            ticker = str(row.get("ticker") or "?")
            reason_tag = str(row.get("filtered_reason") or "filtered").split(":", 1)[0]
            short_rejected.append(f"{ticker}:{reason_tag}")
        if short_rejected:
            reasons.append(f"post_filter_short_rejected={','.join(short_rejected[:6])}")
        if reasons:
            synthesis["short_no_action_rationale"] = reasons
        else:
            synthesis.pop("short_no_action_rationale", None)
    return synthesis


def _normalized_investment_tier(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "long": "long",
        "medium": "medium",
        "short": "swing",
        "swing": "swing",
        "長期": "long",
        "中期": "medium",
        "短期": "swing",
    }
    return aliases.get(text, text)


def _account_matches(requested: object, actual: object) -> bool:
    req = str(requested or "").strip().lower()
    act = str(actual or "").strip().lower()
    if not req:
        return True
    if req == act:
        return True
    # ``特定`` and ``一般`` are executable broker account categories, not
    # interchangeable aliases.  Only an explicitly generic taxable label may
    # match both; a concrete order route must stay inside that account.
    if req in {"taxable", "課税", "課税口座"}:
        return "特定" in act or "一般" in act or "課税" in act
    if req in {"nisa", "nisa口座"}:
        return "nisa" in act
    if "nisa" in req or "nisa" in act:
        return req in act or act in req
    return req in act or act in req


def _holding_info_for_action(action: dict, holdings_price_map: dict) -> dict:
    """Resolve a ticker holding to the action's tier/account instead of last-write wins."""
    ticker = str(action.get("ticker") or "")
    aggregate = holdings_price_map.get(ticker) or {}
    lots = [row for row in aggregate.get("lots", []) if isinstance(row, dict)]
    if not lots:
        return aggregate

    requested_tier = _normalized_investment_tier(action.get("tier"))
    if requested_tier in {"", "all"}:
        requested_tier = ""
    requested_account = (
        action.get("execution_account")
        or action.get("target_account")
        or action.get("account")
        or action.get("account_type")
        or action.get("broker_account")
        or ""
    )
    try:
        from execution_safety import canonical_broker, canonical_owner

        requested_owner = canonical_owner(
            action.get("execution_owner") or action.get("target_owner") or action.get("owner")
        )
        requested_broker = canonical_broker(
            action.get("execution_broker") or action.get("target_broker") or action.get("broker")
        )
    except Exception:
        requested_owner = str(action.get("execution_owner") or action.get("owner") or "").strip().lower()
        requested_broker = str(action.get("execution_broker") or action.get("broker") or "").strip().lower()
    matched = lots
    if requested_tier:
        matched = [
            row for row in matched
            if _normalized_investment_tier(row.get("investment_type")) == requested_tier
        ]
    if requested_account:
        matched = [row for row in matched if _account_matches(requested_account, row.get("account"))]
    if requested_owner:
        matched = [row for row in matched if str(row.get("owner") or "") == requested_owner]
    if requested_broker:
        matched = [row for row in matched if str(row.get("broker") or "") == requested_broker]

    if not matched:
        unresolved = dict(aggregate)
        unresolved["holding_scope_unresolved"] = True
        return unresolved
    if len(matched) == 1:
        return dict(matched[0])

    # Multiple lots in the same explicitly selected scope are safe to
    # aggregate.  Multiple unscoped lots are intentionally marked ambiguous.
    selected = dict(matched[0])
    selected["shares"] = sum(float(row.get("shares") or 0) for row in matched)
    selected["value_jpy"] = sum(float(row.get("value_jpy") or 0) for row in matched)
    selected["lot_keys"] = [str(row.get("key") or "") for row in matched if row.get("key")]
    accounts = {str(row.get("account") or "") for row in matched}
    tiers = {_normalized_investment_tier(row.get("investment_type")) for row in matched}
    owners = {str(row.get("owner") or "") for row in matched if row.get("owner")}
    brokers = {str(row.get("broker") or "") for row in matched if row.get("broker")}
    if len(accounts) == 1:
        selected["account"] = next(iter(accounts))
    if len(tiers) == 1:
        selected["investment_type"] = next(iter(tiers))
    if len(owners) == 1:
        selected["owner"] = next(iter(owners))
    if len(brokers) == 1:
        selected["broker"] = next(iter(brokers))
    if (
        len(accounts) > 1
        or (len(tiers) > 1 and not requested_tier)
        or (not requested_tier and not requested_account)
        or (len(owners) > 1 and not requested_owner)
        or (len(brokers) > 1 and not requested_broker)
    ):
        selected["holding_scope_ambiguous"] = True
    return selected


def _bind_action_to_holding(action: dict, holdings_price_map: dict) -> tuple[dict, dict]:
    info = _holding_info_for_action(action, holdings_price_map)
    if not info:
        return action, info
    bound = dict(action)
    scope_resolved = not info.get("holding_scope_unresolved") and not info.get("holding_scope_ambiguous")
    if scope_resolved and info.get("account"):
        bound["execution_account"] = info["account"]
    if scope_resolved and info.get("investment_type"):
        bound["execution_investment_type"] = info["investment_type"]
    is_nisa_buy = (
        _direction_of(action.get("type") or action.get("action_type")) == "buy"
        and "nisa" in str(action.get("execution_account") or action.get("account") or "").lower()
    )
    owner_explicit = bool(action.get("execution_owner") or action.get("target_owner") or action.get("owner"))
    broker_explicit = bool(action.get("execution_broker") or action.get("target_broker") or action.get("broker"))
    nisa_route_complete = owner_explicit and broker_explicit
    if scope_resolved and info.get("owner") and (not is_nisa_buy or owner_explicit):
        bound["execution_owner"] = info["owner"]
    if scope_resolved and info.get("broker") and (not is_nisa_buy or broker_explicit):
        bound["execution_broker"] = info["broker"]
    lot_keys = info.get("lot_keys") or ([info.get("key")] if info.get("key") else [])
    if scope_resolved and lot_keys and (not is_nisa_buy or nisa_route_complete):
        bound["execution_position_keys"] = lot_keys
    elif scope_resolved and lot_keys and is_nisa_buy:
        # A same-ticker holding does not prove which person's NISA owns a new
        # order.  Do not expose a position key that contradicts route_missing.
        bound.pop("execution_position_keys", None)
        bound["execution_position_binding"] = "withheld_unresolved_nisa_route"
    elif not scope_resolved:
        bound.pop("execution_position_keys", None)
        bound["execution_position_binding"] = (
            "withheld_unresolved_holding_scope"
            if info.get("holding_scope_unresolved")
            else "withheld_ambiguous_holding_scope"
        )
    if info.get("holding_scope_ambiguous"):
        bound["holding_scope_ambiguous"] = True
    if info.get("holding_scope_unresolved"):
        bound["holding_scope_unresolved"] = True
    return bound, info


def _normalize_exit_action_against_holdings(action: dict, holdings_price_map: dict) -> dict:
    """Correct misleading full-exit wording when actual holdings exceed sell shares."""
    if not isinstance(action, dict):
        return action
    # These fields are deterministic portfolio facts.  Never trust a
    # model-supplied copy when deciding whether an exit fits the selected lot.
    action = dict(action)
    for field in (
        "holding_shares_before",
        "holding_shares_after",
        "requested_sell_quantity",
        "holding_quantity_exceeds_account",
        "holding_quantity_shortfall",
    ):
        action.pop(field, None)
    atype_lc = str(action.get("type") or action.get("action_type") or "").lower()
    if atype_lc not in {"sell", "trim", "take_profit", "reduce"}:
        return action
    ticker = action.get("ticker") or ""
    if not ticker:
        return action
    action, holding_info = _bind_action_to_holding(action, holdings_price_map)
    if holding_info.get("holding_scope_unresolved") or holding_info.get("holding_scope_ambiguous"):
        held = 0
    else:
        try:
            held = int(float(holding_info.get("shares") or 0))
        except Exception:
            held = 0
    sell_shares = _parse_amount_hint_shares(action)
    if held > 0:
        action["holding_shares_before"] = held
    if held > 0 and sell_shares > 0:
        action["requested_sell_quantity"] = sell_shares
        action["holding_shares_after"] = max(0, held - sell_shares)
        if sell_shares > held:
            action["holding_quantity_exceeds_account"] = True
            action["holding_quantity_shortfall"] = sell_shares - held
    if held <= 0 or sell_shares <= 0 or sell_shares >= held:
        return action

    text = str(action.get("action") or "")
    reason = str(action.get("reason") or "")
    if not any(marker in (text + reason) for marker in ("全数", "→0", "0株")):
        return action

    remaining = max(0, held - sell_shares)
    corrected = dict(action)
    corrected["action"] = text.replace("全数売却", "一部売却").replace("全数", "一部")
    corrected["action"] = re.sub(
        r"\d+\s*株保有\s*→\s*0\s*株",
        f"{held}株保有→{remaining}株",
        corrected["action"],
    )
    corrected["reason"] = reason.replace("一括処分", "一部利益確定").replace("全数", "一部")
    corrected["position_size_corrected"] = True
    note = f"保有数確認: 実保有{held}株に対し売却提案{sell_shares}株のため、全数売却ではなく一部トリム（残{remaining}株）として扱う"
    corrected["execution_note"] = (
        f"{corrected.get('execution_note')} / {note}" if corrected.get("execution_note") else note
    )
    return corrected


def _normalize_entry_action_against_holdings(action: dict, holdings_price_map: dict) -> dict:
    """Correct "new buy" wording when a position already exists."""
    if not isinstance(action, dict):
        return action
    atype_lc = str(action.get("type") or action.get("action_type") or "").lower()
    if atype_lc not in {"buy", "add", "dca", "margin_buy"}:
        return action
    ticker = action.get("ticker") or ""
    if not ticker:
        return action
    action, holding_info = _bind_action_to_holding(action, holdings_price_map)
    try:
        held = float(holding_info.get("shares") or 0)
    except Exception:
        held = 0.0
    if held <= 0:
        return action

    text = str(action.get("action") or "")
    reason = str(action.get("reason") or "")
    if "新規" not in (text + reason):
        return action

    corrected = dict(action)
    corrected["type"] = "add" if atype_lc == "buy" else action.get("type")
    corrected["action"] = text.replace("新規購入", "追加購入").replace("新規買い", "買い増し").replace("新規", "追加")
    corrected["reason"] = reason.replace("新規購入", "追加購入").replace("新規買い", "買い増し").replace("新規", "追加")
    corrected["position_size_corrected"] = True
    note = f"保有数確認: 既に{ticker}を{held:g}保有しているため、新規ではなく追加購入として扱う"
    corrected["execution_note"] = (
        f"{corrected.get('execution_note')} / {note}" if corrected.get("execution_note") else note
    )
    return corrected


def _format_earnings_blackout_for_prompt(within_business_days: int = 5) -> str:
    """決算 N 営業日以内の銘柄を Opus プロンプト向けに整形。
    buy/add/dca 推奨を出さないよう促す。"""
    blackout = _load_earnings_blackout(within_business_days=within_business_days)
    if not blackout:
        return ""
    lines = [f"## 決算 blackout（EARNINGS_BLACKOUT — 決算 0〜{within_business_days} 営業日以内）"]
    lines.append("※ 以下の銘柄に対する buy/add/dca 推奨は禁止。trim/hold は許可。")
    lines.append("  " + ", ".join(sorted(blackout)))
    return "\n".join(lines)


def _sync_guard_promoted_stance_text(synthesis: dict, *, original_stance: str, promoted_stance: str) -> None:
    """Keep user-facing stance headers aligned with deterministic stance promotion."""
    original_stance = str(original_stance or "").lower()
    promoted_stance = str(promoted_stance or "").lower()
    if not promoted_stance:
        return

    message = str(synthesis.get("telegram_message") or "")
    if message:
        lines = message.splitlines()
        if lines:
            stance_pattern = r"(defensive|neutral|moderately_aggressive|aggressive)"
            first = lines[0]
            updated = re.sub(rf"\(({stance_pattern})\)", f"({promoted_stance})", first, count=1)
            updated = re.sub(rf"stance=({stance_pattern})", f"stance={promoted_stance}", updated, count=1)
            if updated == first and original_stance and original_stance in first:
                updated = first.replace(original_stance, promoted_stance, 1)
            lines[0] = updated
            synthesis["telegram_message"] = "\n".join(lines)

    reason = str(synthesis.get("stance_reason") or "").strip()
    guard_note = (
        f"stance_guard: {original_stance or 'none'}→{promoted_stance} "
        "（実データのaggressive条件を満たし、有効なhard overrideなし）"
    )
    if guard_note not in reason:
        synthesis["stance_reason"] = f"{reason} / {guard_note}" if reason else guard_note


def _apply_stance_guard(synthesis: dict, data: dict, regime_bull_confirmed: bool) -> dict:
    """P0-4: aggressive 昇格条件を**実データ**で満たすのに、有効な hard override 無しで
    stance が格下げされている場合、aggressive へ決定論的に補正する。

    背景: 汚染メトリクス (excess α / CVaR) を口実にした不当な格下げ (prompt の override
    規則違反) を是正する。override の妥当性は LLM のテキストではなく実データで判定する
    (rule 3040 の defensive 強制条件 = VIX>30 / current_dd<=-8% / margin danger のみ)。
    """
    if not isinstance(synthesis, dict) or not isinstance(data, dict):
        return synthesis
    stance = str(synthesis.get("overall_stance") or "").lower()

    mm = data.get("market_meta", {}) or {}
    try:
        vix = float(mm.get("vix")) if mm.get("vix") is not None else None
    except (TypeError, ValueError):
        vix = None

    risk = data.get("risk", {}) or {}
    actual_dd = risk.get("actual_current_dd")  # percent, actual guard/NAV only; never synthetic parquet DD
    try:
        actual_dd = float(actual_dd) if actual_dd is not None else None
    except (TypeError, ValueError):
        actual_dd = None
    actual_dd_stage = risk.get("actual_dd_stage")

    leverage_health = synthesis.get("leverage_health") if isinstance(synthesis.get("leverage_health"), dict) else {}
    leverage_status = str(leverage_health.get("status") or "").lower() or None

    cash_pct = None
    try:
        cash = data.get("cash_info", {}) or {}
        tc = float(cash.get("total_cash_jpy") or cash.get("total_cash") or 0)
        pt = float(data.get("portfolio_total") or 0)
        if pt > 0:
            cash_pct = tc / pt * 100.0
    except (TypeError, ValueError):
        cash_pct = None

    inputs_complete = (
        vix is not None
        and cash_pct is not None
        and actual_dd is not None
        and leverage_status is not None
    )

    # aggressive 昇格条件 (prompt 3043 と整合)。入力欠損時は fail-safe で昇格しない。
    eligible = (
        inputs_complete
        and bool(regime_bull_confirmed)
        and (vix is not None and vix < 20)
        and (cash_pct is not None and cash_pct > 3.0)
    )
    # 実データで判定する hard override (rule 3040 の defensive 強制条件のみ)
    hard_override = (
        (vix is not None and vix > 30)
        or (actual_dd is not None and actual_dd <= -8.0)
        or (actual_dd_stage in {"block", "daily_block", "monthly_block", "stage_1", "stage_2", "stage_3"})
        or (leverage_status in ("deleverage", "emergency"))
    )

    if eligible and stance != "aggressive" and not hard_override:
        synthesis["stance_guard_applied"] = True
        synthesis["stance_guard_detail"] = {
            "original_stance": stance or None,
            "promoted_to": "aggressive",
            "reason": "aggressive 昇格条件を実データで充足し有効な override 無し（excess α/CVaR は override 根拠に不可）",
            "vix": vix,
            "actual_dd_pct": actual_dd,
            "actual_dd_stage": actual_dd_stage,
            "cash_pct": (round(cash_pct, 1) if cash_pct is not None else None),
            "leverage_status": leverage_status,
            "regime_bull_confirmed": bool(regime_bull_confirmed),
            "original_override_block": synthesis.get("aggressive_override_block"),
        }
        synthesis["overall_stance"] = "aggressive"
        _sync_guard_promoted_stance_text(
            synthesis,
            original_stance=stance,
            promoted_stance="aggressive",
        )
        synthesis["stance_guard_display_synced"] = True
        print(f"  🛡️ stance guard: {stance or 'none'} → aggressive（override 規則違反を実データで是正）")
    return synthesis


def _phase1_post_filter(
    synthesis: dict,
    portfolio_total: float,
    fx_rate: float = 150.0,
    positions: list | None = None,
    cash_info: dict | None = None,
    execution_plan: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """
    Phase 1 post-filter:
      (1) minimum amount: 推定 amount_jpy < total * 0.005 → deferred
      (2) cooldown:        直近 7 日に同 ticker × 同 direction → 除外
      (3) earnings blackout: buy/add/dca で 0 <= bdays <= 5 → 除外
      (4) flip warning:    直近 14 日に逆方向推奨があれば annotate（除去せず警告のみ）

    除去された actions は synthesis['_filtered_actions'] に保存（透明性）。
    """
    if not isinstance(synthesis, dict):
        return synthesis
    analysis_now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if analysis_now.tzinfo is None:
        analysis_now = analysis_now.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    # These settings also apply to policy-rejected rows below. Without this,
    # an action that is intrinsically non-executable can be shown as merely
    # policy-blocked when the policy gate rejects every action first.
    try:
        from tunable_params import get as _tp_get_filter
        _disable_sl_global = bool(_tp_get_filter("disable_stop_loss_recommendations", True))
        _disable_cumulative_global = bool(_tp_get_filter("disable_cumulative_recommendations", True))
    except Exception:
        _disable_sl_global = True
        _disable_cumulative_global = True

    def _intrinsic_filtered_reason(action: dict) -> str | None:
        from insider_restrictions import is_restricted_ticker
        ticker = action.get("ticker") or ""
        if is_restricted_ticker(ticker):
            return (
                f"insider_restricted: {ticker} is structurally excluded from "
                "AI/policy signal surfaces; use the insider-window B2 planner only"
            )
        reason = _non_executable_action_reason(action)
        if reason:
            return reason
        atype_lc = str(action.get("type") or "").lower()
        action_text = str(action.get("action") or "")
        if _disable_sl_global:
            is_sl_intent = (
                atype_lc == "stop_loss"
                or "逆指値" in action_text
                or "stop loss" in action_text.lower()
                or "stop-loss" in action_text.lower()
            )
            if is_sl_intent:
                ticker = action.get("ticker") or ""
                return (
                    f"disable_stop_loss_recommendations: {ticker} stop_loss / 逆指値推奨は "
                    "全体設定で禁止 (tunable_params: disable_stop_loss_recommendations=true)"
                )
        is_buy_side_action = _direction_of(action.get("type")) == "buy" or atype_lc in {"buy", "add", "dca", "margin_buy"}
        if _disable_cumulative_global and is_buy_side_action and _is_cumulative_buy_action(action, atype_lc):
            ticker = action.get("ticker") or ""
            return (
                f"disable_cumulative_recommendations: {ticker} 定期/自動積立アクションは "
                "broker で自動設定済み (tunable_params: disable_cumulative_recommendations=true)"
            )
        return None

    policy_rejected_rows = []
    for item in synthesis.get("policy_filtered_actions") or []:
        if not isinstance(item, dict) or not isinstance(item.get("action"), dict):
            continue
        row = dict(item["action"])
        rule = item.get("rule") or "policy"
        reason = item.get("reason") or ""
        policy_reason = f"policy_{rule}: {reason}"
        intrinsic_reason = _intrinsic_filtered_reason(row)
        if intrinsic_reason:
            row["filtered_reason"] = intrinsic_reason
            row["policy_filtered_reason"] = policy_reason
        else:
            row["filtered_reason"] = policy_reason
        policy_rejected_rows.append(row)

    actions = synthesis.get("priority_actions") or []
    raw_directions_by_ticker: dict[str, set[str]] = {}
    for raw_action in actions:
        if not isinstance(raw_action, dict) or not raw_action.get("ticker"):
            continue
        raw_direction = _direction_of(raw_action.get("type"))
        if raw_direction in {"buy", "sell", "short", "cover"}:
            raw_directions_by_ticker.setdefault(str(raw_action["ticker"]), set()).add(raw_direction)
    raw_opposite_tickers = {
        ticker
        for ticker, directions in raw_directions_by_ticker.items()
        if ({"buy", "sell"} <= directions or {"short", "cover"} <= directions)
    }
    if not isinstance(actions, list) or not actions:
        if policy_rejected_rows:
            synthesis["_filtered_actions"] = policy_rejected_rows
            reasons: dict = {}
            for f in policy_rejected_rows:
                tag = (f.get("filtered_reason") or "?").split(":", 1)[0]
                reasons[tag] = reasons.get(tag, 0) + 1
            synthesis["_filtered_action_summary"] = reasons
            synthesis["post_filter"] = {
                "input_count": 0,
                "kept_count": 0,
                "filtered_count": len(policy_rejected_rows),
                "annotated_count": 0,
                "summary": reasons,
                "annotated_summary": {},
                "all_actions_filtered": True,
                "policy_accepted_count": (
                    synthesis.get("policy_decision", {}).get("accepted_count")
                    if isinstance(synthesis.get("policy_decision"), dict) else None
                ),
            }
            synthesis["decision_boundary_audit"] = _build_decision_boundary_audit(
                [],
                policy_rejected_rows,
                [],
                context_blocks=synthesis.get("context_blocks") if isinstance(synthesis.get("context_blocks"), dict) else {},
            )
            refs = []
            for f in policy_rejected_rows[:5]:
                ticker = f.get("ticker") or ""
                body = str(f.get("action") or "")[:45]
                tag = str(f.get("filtered_reason") or "policy").split(":", 1)[0]
                refs.append(f"- {ticker} {body}（{tag}）")
            synthesis["telegram_message"] = (
                f"📊 新規実行アクションなし ({str(synthesis.get('overall_stance') or 'neutral')})\n"
                "Policy Engine が実行候補をブロック。参考候補は以下。\n"
                + "\n".join(refs)
            )[:1000]
        else:
            synthesis["decision_boundary_audit"] = _build_decision_boundary_audit(
                [],
                [],
                [],
                context_blocks=synthesis.get("context_blocks") if isinstance(synthesis.get("context_blocks"), dict) else {},
            )
        synthesis["decision_summary"] = {
            "candidate_count": len(policy_rejected_rows),
            "executable_count": 0,
            "review_count": 0,
            "filtered_count": len(policy_rejected_rows),
            "deferred_count": 0,
            "no_action_classification": "system_constraints" if policy_rejected_rows else "market_no_trade",
            "reason_counts": synthesis.get("_filtered_action_summary") or {},
            "count_conservation_ok": True,
        }
        _set_operational_stance(
            synthesis,
            synthesis["decision_summary"]["reason_counts"],
            executable_count=0,
        )
        return synthesis

    # tunable_params から動的に各クールダウン日数を取得
    try:
        from tunable_params import get as _tp_get_cd
        _cd_days = int(_tp_get_cd("cooldown_same_direction_days", 7))
        _ff_days = int(_tp_get_cd("flip_flop_window_days", 14))
        _eb_days2 = int(_tp_get_cd("earnings_blackout_days", 5))
        _done_days = int(_tp_get_cd("done_list_same_direction_days", 7))
    except Exception:
        _cd_days = 7
        _ff_days = 14
        _eb_days2 = 5
        _done_days = 7

    # stance 連動 runtime override (Option B-3 / 攻めモード時の動的緩和):
    # aggressive / moderately_aggressive のとき cooldown / blackout / 最低取引額を緩和。
    # rolling 24h の cooldown だと、前朝の未処理推奨が翌朝の全アクションを消すため、
    # 攻めモードでは「同一カレンダー日だけ重複抑制」に切り替える。
    _stance = str(synthesis.get("overall_stance") or "").lower()
    _is_aggressive = _stance in ("aggressive", "moderately_aggressive")
    _cooldown_same_day_only = False
    if _is_aggressive:
        _cd_days   = max(1, min(_cd_days,   1))   # クールダウン 最小 1 日に短縮
        _eb_days2  = max(1, min(_eb_days2,  1))   # 決算 blackout 最小 1 日に短縮
        _cooldown_same_day_only = True
        synthesis["_runtime_overrides"] = {
            "stance":              _stance,
            "cooldown_days":       _cd_days,
            "cooldown_scope":      "same_calendar_day",
            "done_list_days":      _done_days,
            "earnings_blackout_d": _eb_days2,
            "min_action_jpy_mult": 0.7,
            "note": "aggressive stance による runtime 緩和（Option B-3）",
        }

    recent_14d = _load_recent_recommendations(days=_ff_days)   # フリップフロップ検出用
    if _cooldown_same_day_only:
        _today_s = analysis_now.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
        recent_7d = [
            e for e in _load_recent_recommendations(days=max(2, _cd_days))
            if isinstance(e, dict) and str(e.get("as_of") or "")[:10] == _today_s
        ]
    else:
        recent_7d = _load_recent_recommendations(days=_cd_days)   # 連日重複抑制用
    cancelled_rec_keys = _load_cancelled_recommendation_keys()
    blackout   = _load_earnings_blackout(within_business_days=_eb_days2)
    def _with_optional_now(func, *, days: int):
        # Several downstream extensions/tests still provide the legacy
        # ``func(days=...)`` callable.  Preserve that interface while native
        # helpers use the injected analysis clock.
        try:
            return func(days=days, now=analysis_now)
        except TypeError as exc:
            if "unexpected keyword argument 'now'" not in str(exc):
                raise
            return func(days=days)

    done_set = _with_optional_now(_done_set_by_direction, days=_done_days)
    done_intents = _with_optional_now(_recent_order_intents_by_direction, days=_done_days)
    order_state_conflicts = _with_optional_now(_order_state_conflicts_by_direction, days=_done_days)
    open_state_intents = _open_action_state_by_direction()
    recent_execution_rows = _with_optional_now(_load_recent_executions, days=max(14, _ff_days))
    open_execution_intents: dict[tuple[str, str], list[dict]] = {}
    for execution_row in recent_execution_rows:
        if not isinstance(execution_row, dict):
            continue
        if str(execution_row.get("status") or "").lower() != "ordered":
            continue
        execution_ticker = str(execution_row.get("ticker") or "")
        execution_direction = _execution_direction(
            execution_row.get("direction") or execution_row.get("type")
        )
        if execution_ticker and execution_direction:
            open_execution_intents.setdefault(
                (execution_ticker, execution_direction), []
            ).append(dict(execution_row))
    # 全体統一: tunable_params.disable_stop_loss_recommendations=true なら
    # 全銘柄で stop_loss / 逆指値発注的アクションを post-filter で除去する。
    try:
        from tunable_params import get as _tp_get_lh
        _loss_min = float(_tp_get_lh("loss_harvest_min_jpy", 30_000))
    except Exception:
        _loss_min = 30_000.0
    loss_set   = _load_tax_loss_harvest_tickers(min_loss_jpy=_loss_min)  # 損出し候補

    # Phase 2: リバランスクールダウン（直近3営業日に trim/sell 実行があれば trim 推奨を抑制）
    rebal_cooldown_active = False
    rebal_cooldown_reason = ""
    try:
        from behavioral_guard import is_rebalance_in_cooldown as _rebal_cd
        # VIX を market_meta から拾う（指定なければ None でも OK）
        _vix = None
        try:
            mm_path = BASE_DIR / "vix_state.json"
            if mm_path.exists():
                _vix = float(json.loads(mm_path.read_text()).get("vix") or 0) or None
        except Exception:
            pass
        rebal_cooldown_active, rebal_cooldown_reason = _rebal_cd(vix=_vix)
    except Exception:
        pass

    holdings_price_map: dict = {}
    try:
        from execution_safety import canonical_broker, canonical_owner, load_nisa_profiles

        _nisa_raw_for_routes, _nisa_profiles_for_routes = load_nisa_profiles(BASE_DIR)
    except Exception:
        canonical_broker = lambda value: str(value or "").strip().lower()  # type: ignore
        canonical_owner = lambda value: str(value or "").strip().lower()  # type: ignore
        _nisa_profiles_for_routes = {}

    def _add_holding_lot(pos: dict) -> None:
        tk = pos.get("ticker")
        if not tk:
            return
        broker = canonical_broker(pos.get("broker") or pos.get("execution_broker"))
        owner = canonical_owner(pos.get("owner") or pos.get("execution_owner"))
        if broker and not owner:
            route_matches = [
                route_owner
                for route_owner, route_profile in _nisa_profiles_for_routes.items()
                if route_profile.get("execution_broker") == broker
            ]
            if len(route_matches) == 1:
                owner = route_matches[0]
        lot = {
            "key": pos.get("key"),
            "current_price": pos.get("current_price") or pos.get("price"),
            "currency": pos.get("currency"),
            "shares": float(pos.get("shares") or 0),
            "value_jpy": float(pos.get("value_jpy") or 0),
            "investment_type": pos.get("investment_type"),
            "account": pos.get("account"),
            "broker": broker,
            "owner": owner,
        }
        bucket = holdings_price_map.setdefault(tk, {
            "current_price": lot["current_price"],
            "currency": lot["currency"],
            "shares": 0.0,
            "value_jpy": 0.0,
            "lots": [],
        })
        bucket["shares"] += lot["shares"]
        bucket["value_jpy"] += lot["value_jpy"]
        bucket["lots"].append(lot)

    # 優先 1: 引数で渡された positions（data_gatherer の build_portfolio_snapshot 出力）
    if positions:
        for pos in positions:
            _add_holding_lot(pos)
    # 優先 2: holdings.json を直接読む（後方互換）
    if not holdings_price_map:
        try:
            h_path = BASE_DIR / "holdings.json"
            if h_path.exists():
                h_data = json.loads(h_path.read_text(encoding="utf-8"))
                # 旧形式: {"positions": [...]}
                pos_list = h_data.get("positions") or []
                # 新形式: top-level key が ticker
                if not pos_list and isinstance(h_data, dict):
                    pos_list = []
                    for k, v in h_data.items():
                        if isinstance(v, dict) and "shares" in v:
                            pos_list.append({**v, "key": k, "ticker": v.get("ticker") or k})
                for pos in pos_list:
                    tk = pos.get("ticker")
                    if tk:
                        sh = float(pos.get("shares") or 0)
                        ep = float(pos.get("entry_price") or pos.get("current_price") or 0)
                        cur = pos.get("currency") or ("JPY" if str(tk).endswith(".T") else "USD")
                        val_jpy = sh * ep * (fx_rate if cur == "USD" else 1.0)
                        _add_holding_lot({
                            **pos,
                            "current_price": pos.get("current_price") or ep,
                            "currency": cur,
                            "shares": sh,
                            "value_jpy": val_jpy,
                        })
        except Exception:
            pass

    past_dir_7d = {
        (e.get("ticker"), _direction_of(e.get("type")))
        for e in recent_7d
        if e.get("ticker") and not _recommendation_entry_is_cancelled(e, cancelled_rec_keys)
    }
    past_dir_14d_by_ticker: dict = {}
    for e in recent_14d:
        if _recommendation_entry_is_cancelled(e, cancelled_rec_keys):
            continue
        tk = e.get("ticker")
        if not tk:
            continue
        past_dir_14d_by_ticker.setdefault(tk, set()).add(_direction_of(e.get("type")))

    # tunable_params で動的調整可能（fallback で従来動作維持）
    try:
        from tunable_params import get as _tp_get
        _min_action_jpy = float(_tp_get("min_action_jpy", 150_000))
        # P2-1: 支配項 (portfolio×pct) も tunable 化。従来固定 0.005 で aggressive でも
        #       ¥147K 床が動かず、1株提案が恒常的に too_small で枯れていた。
        _min_action_pct = float(_tp_get("min_action_pct_of_portfolio", 0.005))
        # H2: 通常 buy/add/dca/margin_buy の単発金額ハードキャップ（AI上書き不可）。
        _max_single_action_pct = float(_tp_get("max_single_action_pct_of_portfolio", 0.05))
        _margin_cash_first_min_cash_pct = float(_tp_get("margin_buy_cash_first_min_cash_pct", 0.10))
        _margin_min_confidence_pct = float(_tp_get("margin_buy_min_confidence_pct", 80))
        _margin_min_score = float(_tp_get("margin_buy_min_score", 100))
        _margin_min_expected_return_pct = float(_tp_get("margin_buy_min_expected_return_pct_annual", 18))
    except Exception:
        _min_action_jpy = 150_000.0
        _min_action_pct = 0.005
        _max_single_action_pct = 0.05
        _margin_cash_first_min_cash_pct = 0.10
        _margin_min_confidence_pct = 80.0
        _margin_min_score = 100.0
        _margin_min_expected_return_pct = 18.0
    # stance 連動: aggressive のとき両閾値を緩和（Option B-3 + P2-1）
    if _is_aggressive:
        _min_action_jpy *= 0.7
        _min_action_pct *= 0.6
    threshold = max(float(portfolio_total or 0) * _min_action_pct, _min_action_jpy / 3)
    # H2: stance に関わらず固定（aggressiveでも単発金額の上限は緩めない）。
    max_single_action_cap_jpy = min(float(portfolio_total or 0) * _max_single_action_pct, 1_500_000.0)

    _CORE_ETF_TICKERS = {
        "GLD", "IAU", "SPY", "VOO", "VTI", "VT", "QQQ",
        "1306.T", "1321.T", "1489.T", "1540.T",
    }
    _FUND_PREFIXES = ("SLIM_", "IFREE_", "NOMURA_", "MNXACT")

    def _is_core_fund_or_etf(action: dict) -> bool:
        # core 枠 (¥150万) への昇格は決定論的な ticker 判定のみ。asset_class 等の
        # AI 出力テキストを見ると、モデルの自己申告でハードキャップが倍増してしまう。
        ticker = str(action.get("ticker") or "").upper()
        return ticker.startswith(_FUND_PREFIXES) or ticker in _CORE_ETF_TICKERS

    def _single_action_cap_profile(action: dict) -> dict:
        total = float(portfolio_total or 0)
        atype = str(action.get("type") or "").lower()
        tier = str(action.get("tier") or "").lower()
        source = str(action.get("source") or "").strip()
        if atype == "margin_buy":
            pct, abs_jpy, label = 0.015, 500_000.0, "margin"
        elif atype == "short":
            pct, abs_jpy, label = 0.010, 300_000.0, "short"
        elif atype in {"buy", "add"} and tier in {"short", "swing"}:
            pct, abs_jpy, label = 0.010, 300_000.0, "swing"
        elif atype == "dca" or source == "dca_ladder" or _is_core_fund_or_etf(action):
            pct, abs_jpy, label = _max_single_action_pct, 1_500_000.0, "core_or_dca"
        else:
            pct, abs_jpy, label = 0.025, 750_000.0, "individual"
        return {
            "cap_jpy": min(total * pct, abs_jpy),
            "pct": pct,
            "abs_jpy": abs_jpy,
            "label": label,
        }

    plan_gate_mode, plan_gate_mode_warning = _execution_plan_gate_mode()
    plan_observe_stats: dict = {"decisions": {}, "would_filter_count": 0}

    def _apply_execution_plan_gate(action: dict, estimated_jpy: float, cap_jpy: float) -> tuple[bool, str | None]:
        if plan_gate_mode == "off":
            return True, None
        # An active plan with zero items is meaningful: it represents zero
        # approved discretionary funding.  Treating that as a missing plan
        # would let new buys bypass the gate exactly when the user has chosen
        # not to deploy cash.  A deliberately disabled/missing plan retains
        # the historical fail-open behaviour so a planner outage cannot halt
        # every recommendation.
        if (
            not isinstance(execution_plan, dict)
            or str(execution_plan.get("status") or "").lower() in {"disabled", "missing"}
        ):
            return True, None
        direction = _direction_of(action.get("type"))
        if direction not in {"buy", "sell", "short", "cover"}:
            return True, None
        plan_action = dict(action)
        if estimated_jpy >= 0 and estimated_jpy != float("inf"):
            plan_action["estimated_notional_jpy"] = round(estimated_jpy)
        try:
            from execution_plan_engine import classify_candidate_against_plan
            decision = classify_candidate_against_plan(
                plan_action,
                execution_plan,
                h2_cap_jpy=round(cap_jpy),
            )
        except Exception as exc:
            action["execution_plan_error"] = str(exc)
            # observe mode is explicitly non-blocking, but enforce mode must not
            # silently bypass the plan gate when classification itself fails.
            if plan_gate_mode == "observe":
                action["execution_plan_gate_mode"] = "observe"
                action["execution_plan_observed_decision"] = "execution_plan_error"
                action["execution_plan_enforced"] = False
                action["execution_plan_would_filter"] = True
                plan_observe_stats["decisions"]["execution_plan_error"] = (
                    plan_observe_stats["decisions"].get("execution_plan_error", 0) + 1
                )
                plan_observe_stats["would_filter_count"] += 1
                return True, None
            return False, f"execution_plan_error: plan classification failed ({str(exc)[:200]})"

        plan_decision = decision.get("execution_plan_decision")
        if not plan_decision:
            return True, None

        if plan_gate_mode == "observe":
            # 観測のみ: enforce 用の execution_plan_decision とフィールドを分離し、
            # API/UI が「実際に除外された」と誤読しないようにする。候補は除外しない。
            action["execution_plan_gate_mode"] = "observe"
            action["execution_plan_observed_decision"] = plan_decision
            action["execution_plan_enforced"] = False
            action["execution_plan_would_filter"] = not bool(decision.get("executable"))
            if "plan_item_id" in decision:
                action["plan_item_id"] = decision.get("plan_item_id")
            for key in (
                "monthly_objective_id",
                "execution_plan_match_kind",
                "execution_plan_advisory_item_ids",
                "execution_plan_override",
                "override_reason",
                "budget_impact_jpy",
                "ai_bounded_gate",
                "cap_applied_jpy",
                "monthly_remaining_before_jpy",
                "monthly_remaining_after_jpy",
            ):
                if key in decision:
                    action[key] = decision.get(key)
            decisions = plan_observe_stats["decisions"]
            decisions[plan_decision] = decisions.get(plan_decision, 0) + 1
            if not decision.get("executable"):
                plan_observe_stats["would_filter_count"] += 1
            return True, None

        action["execution_plan_gate_mode"] = "enforce"
        action["execution_plan_decision"] = plan_decision
        for key in (
            "plan_item_id",
            "monthly_objective_id",
            "execution_plan_match_kind",
            "execution_plan_advisory_item_ids",
            "plan_remaining_before_jpy",
            "plan_remaining_after_jpy",
            "monthly_remaining_before_jpy",
            "monthly_remaining_after_jpy",
            "monthly_remaining_jpy",
            "execution_plan_override",
            "override_reason",
            "budget_impact_jpy",
        ):
            if key in decision:
                action[key] = decision.get(key)

        if decision.get("executable"):
            if decision.get("ai_bounded_gate"):
                action["ai_bounded_gate"] = decision.get("ai_bounded_gate")
                action["provisional_decision"] = True
                action["cap_applied_jpy"] = decision.get("cap_applied_jpy")
            return True, None

        ticker = action.get("ticker") or "?"
        if plan_decision == "plan_consumed_by_open_order":
            return False, (
                f"plan_consumed_by_open_order: {ticker} {direction} は "
                f"execution_plan item {decision.get('plan_item_id')} が既存注文/約定で消化済み"
            )
        if plan_decision == "plan_wait_for_better_candidate":
            return False, (
                f"plan_wait_for_better_candidate: {ticker} {direction} は "
                f"execution_plan item {decision.get('plan_item_id')} に合致するが "
                f"confidence/urgency が不足 (required_confidence={decision.get('required_confidence_pct')})"
            )
        if plan_decision == "plan_over_budget":
            return False, (
                f"plan_over_budget: {ticker} {direction} は execution_plan item "
                f"{decision.get('plan_item_id')} の残予算 ¥{float(decision.get('plan_remaining_jpy') or 0)/10000:.1f}万を超過"
            )
        if plan_decision == "blocked_by_existing_guard":
            return False, (
                f"execution_plan_existing_guard: {ticker} {direction} は "
                f"{decision.get('existing_guard')} が優先"
            )
        if plan_decision == "plan_unmatched_no_override":
            return False, (
                f"plan_unmatched_no_override: {ticker} {direction} は active execution_plan に合致せず、"
                "opportunistic override 条件も未達"
            )
        return False, f"{plan_decision}: execution_plan により非実行"

    def _attach_estimated_notional(action: dict) -> float:
        amt = _estimate_action_jpy(action, holdings_price_map, fx_rate)
        if amt >= 0 and amt != float("inf"):
            action["estimated_notional_jpy"] = round(amt)
        return amt

    def _cash_ratio_for_bump() -> float:
        ctx = cash_info if isinstance(cash_info, dict) else {}
        try:
            total_cash = float(ctx.get("total_cash_jpy") or ctx.get("total_cash") or 0)
            total = float(portfolio_total or 0)
            return total_cash / total if total > 0 else 0.0
        except Exception:
            return 0.0

    def _available_cash_for_currency(currency: str) -> float:
        ctx = cash_info if isinstance(cash_info, dict) else {}
        try:
            if currency == "USD":
                return float(ctx.get("usd_as_jpy") or 0)
            return float(ctx.get("jpy_cash") or ctx.get("cash_jpy") or 0)
        except Exception:
            return 0.0

    kabu_mini_verification_needed: list[dict] = []

    def _is_jp_cash_buy(action: dict) -> bool:
        ticker = str(action.get("ticker") or "")
        atype = str(action.get("type") or "").lower()
        return ticker.endswith(".T") and atype in {"buy", "add"}

    def _kabu_mini_requested_channel(action: dict) -> str:
        return str(action.get("execution_channel") or action.get("broker_channel") or "").strip()

    def _is_kabu_mini_cash_buy(action: dict) -> bool:
        if not _is_jp_cash_buy(action):
            return False
        try:
            from kabu_mini_eligibility import action_requests_kabu_mini, is_kabu_mini_eligible
            channel = _kabu_mini_requested_channel(action)
            return action_requests_kabu_mini(action) and is_kabu_mini_eligible(action.get("ticker"), channel=channel)
        except Exception:
            return False

    def _kabu_mini_requested_but_unconfirmed(action: dict) -> bool:
        if not _is_jp_cash_buy(action):
            return False
        try:
            from kabu_mini_eligibility import action_requests_kabu_mini, is_kabu_mini_eligible
            channel = _kabu_mini_requested_channel(action)
            return action_requests_kabu_mini(action) and not is_kabu_mini_eligible(action.get("ticker"), channel=channel)
        except Exception:
            return False

    def _mark_kabu_mini_verification_needed(action: dict, *, reason: str, estimated_jpy: float) -> None:
        if not _kabu_mini_requested_but_unconfirmed(action):
            return
        cap_profile = _single_action_cap_profile(action)
        channel = _kabu_mini_requested_channel(action)
        action["kabu_mini_eligibility_unknown"] = True
        action["kabu_mini_requested_channel"] = channel or None
        action["kabu_mini_verification_reason"] = (
            "kabu_mini requested but ticker/channel is not confirmed in "
            "data/kabu_mini_eligible.json"
        )
        try:
            from kabu_mini_eligibility import build_kabu_mini_verification_record
            record = build_kabu_mini_verification_record(
                action,
                reason=reason,
                estimated_jpy=estimated_jpy,
                threshold_jpy=threshold,
                max_single_action_cap_jpy=cap_profile["cap_jpy"],
            )
        except Exception:
            record = {
                "ticker": str(action.get("ticker") or ""),
                "requested_channel": channel or None,
                "action_type": str(action.get("type") or "").lower() or None,
                "amount_hint": action.get("amount_hint"),
                "reason": reason,
                "estimated_notional_jpy": round(estimated_jpy) if estimated_jpy >= 0 and math.isfinite(estimated_jpy) else None,
                "threshold_jpy": round(threshold),
                "max_single_action_cap_jpy": round(cap_profile["cap_jpy"]),
                "single_action_cap_class": cap_profile["label"],
                "source": "phase1_post_filter",
            }
        kabu_mini_verification_needed.append(record)

    def _entry_lot_size(action: dict) -> int:
        ticker = str(action.get("ticker") or "")
        if ticker.endswith(".T"):
            return 1 if _is_kabu_mini_cash_buy(action) else trading_unit_for_ticker(ticker)
        return 1

    def _unit_jpy_for_action(action: dict) -> float:
        ticker = str(action.get("ticker") or "")
        info = holdings_price_map.get(ticker, {}) or {}
        price = _unit_price_for_notional(action, info)
        if price <= 0:
            return 0.0
        currency = action.get("currency") or info.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")
        return price * (fx_rate if currency == "USD" else 1.0)

    def _rewrite_action_shares(action: dict, target_shares: int) -> None:
        label = quantity_label_for_ticker(action.get("ticker"))
        action["amount_hint"] = f"{target_shares}{label}"
        body = str(action.get("action") or "")
        if body:
            updated = re.sub(r"\d[\d,]*\s*(?:株|口)", f"{target_shares}{label}", body, count=1)
            if updated == body:
                updated = f"{body}（{target_shares}{label}へ調整）"
            action["action"] = updated

    def _maybe_resize_kabu_mini_over_cap(action: dict, estimated_jpy: float) -> float:
        if not _is_kabu_mini_cash_buy(action):
            return estimated_jpy
        cap_profile = _single_action_cap_profile(action)
        cap_jpy = cap_profile["cap_jpy"]
        if estimated_jpy < 0 or estimated_jpy == float("inf") or estimated_jpy <= cap_jpy:
            return estimated_jpy
        unit_jpy = _unit_jpy_for_action(action)
        if unit_jpy <= 0:
            return estimated_jpy
        current_shares = max(0, _parse_amount_hint_shares(action))
        if current_shares <= 0:
            return estimated_jpy
        min_shares = max(1, int(math.ceil(threshold / unit_jpy)))
        max_shares = int(math.floor(cap_jpy / unit_jpy))
        target_shares = min(current_shares, max_shares)
        if target_shares < min_shares or target_shares <= 0 or target_shares >= current_shares:
            return estimated_jpy
        resized_jpy = target_shares * unit_jpy
        old_hint = str(action.get("amount_hint") or "")
        _rewrite_action_shares(action, target_shares)
        action["execution_channel"] = action.get("execution_channel") or "rakuten_kabu_mini_open"
        action["jp_kabu_mini_resized"] = True
        action["jp_kabu_mini_resize_detail"] = (
            f"100株単位では推定 ¥{estimated_jpy/10000:.1f}万で単発上限 "
            f"¥{cap_jpy/10000:.0f}万（{cap_profile['label']}）を超えるため、"
            f"かぶミニ現物 {target_shares}株 約¥{resized_jpy/10000:.1f}万へ縮小"
        )
        action["estimated_notional_jpy"] = round(resized_jpy)
        action["execution_note"] = (
            f"{action.get('execution_note')} / {action['jp_kabu_mini_resize_detail']}"
            if action.get("execution_note") else action["jp_kabu_mini_resize_detail"]
        )
        if old_hint and old_hint != action["amount_hint"]:
            action.setdefault("_warnings", []).append(f"amount_hint auto-resized: {old_hint} → {action['amount_hint']}")
        return resized_jpy

    def _total_cash_available_jpy() -> float:
        ctx = cash_info if isinstance(cash_info, dict) else {}
        try:
            return float(ctx.get("total_cash_jpy") or ctx.get("total_cash") or 0)
        except Exception:
            return 0.0

    def _cash_can_cover_action(estimated_jpy: float) -> bool:
        if estimated_jpy < 0 or estimated_jpy == float("inf"):
            return False
        total = float(portfolio_total or 0)
        total_cash = _total_cash_available_jpy()
        cash_ratio = (total_cash / total) if total > 0 else 0.0
        return cash_ratio >= _margin_cash_first_min_cash_pct and total_cash >= estimated_jpy

    def _numeric_action_field(action: dict, *keys: str) -> float | None:
        for key in keys:
            try:
                value = action.get(key)
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _has_margin_buy_exception(action: dict) -> bool:
        if not _has_ai_bounded_reason(
            action,
            "margin_buy_reason",
            "margin_rationale",
            "leverage_reason",
            "leverage_rationale",
            "ai_margin_reason",
            "bounded_decision_reason",
        ):
            return False
        confidence = _action_confidence_pct(action)
        score = _numeric_action_field(action, "score", "screening_score", "margin_score", "source_score", "composite_score")
        expected_return = _numeric_action_field(
            action,
            "expected_return_pct_annual",
            "expected_return_annual_pct",
            "expected_return_pct",
        )
        return (
            confidence >= _margin_min_confidence_pct
            and score is not None and score >= _margin_min_score
            and expected_return is not None and expected_return >= _margin_min_expected_return_pct
        )

    def _convert_margin_buy_to_cash_buy(action: dict, estimated_jpy: float) -> dict:
        action["original_type"] = action.get("type") or "margin_buy"
        action["type"] = "buy"
        action["margin_buy_converted_to_buy"] = True
        action["margin_buy_conversion_reason"] = (
            f"cash_first: cash_ratio>={_margin_cash_first_min_cash_pct*100:.0f}% "
            f"かつ現金 ¥{_total_cash_available_jpy()/10000:.1f}万で推定 "
            f"¥{estimated_jpy/10000:.1f}万を賄えるため、信用買いを現物買いへ正規化。"
            "高conviction信用例外には専用理由・confidence・score・期待リターンが必要。"
        )
        body = str(action.get("action") or "")
        if body:
            updated = body.replace("信用買い", "現物買い").replace("信用買付", "現物買付")
            if updated == body:
                updated = f"{body}（現物買いへ正規化）"
            action["action"] = updated
        action["execution_note"] = (
            f"{action.get('execution_note')} / {action['margin_buy_conversion_reason']}"
            if action.get("execution_note") else action["margin_buy_conversion_reason"]
        )
        return action

    def _maybe_bump_small_buy(action: dict, estimated_jpy: float) -> float:
        if not (
            _is_aggressive
            and _cash_ratio_for_bump() > 0.10
            and 0 <= estimated_jpy < threshold
        ):
            return estimated_jpy
        atype = str(action.get("type") or "").lower()
        # AI sometimes emits margin_buy as a one-share "trial" even when that
        # sits just below the executable notional floor. After leverage_health
        # has allowed margin buys, normalize it to the smallest executable lot
        # instead of letting a safe high-conviction candidate die as too_small.
        if atype not in {"buy", "add", "margin_buy"} or _direction_of(atype) != "buy":
            return estimated_jpy
        confidence = _action_confidence_pct(action)
        rank = _order_intent_rank(action)
        urgency = str(action.get("urgency") or "").lower()
        conf_min = _order_intent_tunable_float("small_notional_bump_min_confidence_pct", 75.0)
        rank_max = _order_intent_tunable_int("small_notional_bump_rank_max", 3)
        blocked_reasons: list[str] = []
        if confidence < conf_min:
            blocked_reasons.append(f"confidence<{conf_min:.0f}")
        if rank is None or rank > rank_max:
            blocked_reasons.append(f"rank>{rank_max}" if rank is not None else "rank_missing")
        if urgency == "low":
            blocked_reasons.append("urgency=low")
        if blocked_reasons:
            action["small_notional_bump_blocked_reason"] = (
                "small_notional_bump_conviction_gate: " + ", ".join(blocked_reasons)
            )
            return estimated_jpy
        ticker = str(action.get("ticker") or "")
        unit_jpy = _unit_jpy_for_action(action)
        if unit_jpy <= 0:
            return estimated_jpy
        currency = action.get("currency") or (holdings_price_map.get(ticker, {}) or {}).get("currency") or ("JPY" if ticker.endswith(".T") else "USD")
        lot = _entry_lot_size(action)
        current_shares = max(0, _parse_amount_hint_shares(action))
        target_shares = int(math.ceil(threshold / unit_jpy / lot) * lot)
        target_shares = max(target_shares, lot)
        if target_shares <= current_shares:
            return estimated_jpy
        bumped_jpy = target_shares * unit_jpy
        available_jpy = _available_cash_for_currency(currency)
        max_bump_jpy = min(
            available_jpy if available_jpy > 0 else bumped_jpy,
            max(threshold * 3.0, float(portfolio_total or 0) * 0.01),
        )
        if bumped_jpy > max_bump_jpy:
            return estimated_jpy

        old_hint = str(action.get("amount_hint") or "")
        _rewrite_action_shares(action, target_shares)
        if _is_kabu_mini_cash_buy(action):
            action["execution_channel"] = action.get("execution_channel") or "rakuten_kabu_mini_open"
        action["small_notional_bumped"] = True
        action["small_notional_bump_detail"] = (
            f"aggressive cash>{10}%: 推定 ¥{estimated_jpy/10000:.1f}万 → "
            f"{target_shares}株 約¥{bumped_jpy/10000:.1f}万"
        )
        action["execution_note"] = (
            f"{action.get('execution_note')} / {action['small_notional_bump_detail']}"
            if action.get("execution_note") else action["small_notional_bump_detail"]
        )
        action["estimated_notional_jpy"] = round(bumped_jpy)
        if old_hint and old_hint != action["amount_hint"]:
            action.setdefault("_warnings", []).append(f"amount_hint auto-bumped: {old_hint} → {action['amount_hint']}")
        return bumped_jpy

    for _row in policy_rejected_rows:
        if isinstance(_row, dict):
            _attach_estimated_notional(_row)

    kept, filtered, deferred, annotated = [], list(policy_rejected_rows), [], []
    scenario_cap_used: dict[str, float] = {}
    for a in actions:
        if not isinstance(a, dict):
            kept.append(a); continue
        a = dict(a)
        try:
            from execution_safety import (
                classify_recent_opposite_execution,
                enrich_action_routing,
            )

            a = enrich_action_routing(a, base_dir=BASE_DIR)
        except Exception as exc:
            a["routing_resolution_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        ticker    = a.get("ticker") or ""
        direction = _direction_of(a.get("type"))
        atype_lc = str(a.get("type") or "").lower()
        a = _normalize_exit_action_against_holdings(a, holdings_price_map)
        a = _normalize_entry_action_against_holdings(a, holdings_price_map)
        direction = _direction_of(a.get("type"))
        atype_lc = str(a.get("type") or "").lower()

        amt = _attach_estimated_notional(a)
        a = _normalize_notional_equation(a, holdings_price_map.get(str(ticker), {}))

        if ticker in raw_opposite_tickers:
            a["opposite_intent_conflict"] = True
            a["opposite_intent_conflict_reason"] = (
                f"{ticker} は同一分析のフィルタ前候補に反対方向の売買意図が併存"
            )

        try:
            recent_opposite_guard = classify_recent_opposite_execution(
                a,
                recent_execution_rows,
                now=analysis_now,
            )
        except Exception as exc:
            recent_opposite_guard = {
                "level": "review",
                "code": "market_session_unresolved",
                "message": f"反対約定のセッション判定に失敗: {type(exc).__name__}: {str(exc)[:120]}",
            }
        if recent_opposite_guard:
            a["recent_opposite_execution_guard"] = recent_opposite_guard
            if recent_opposite_guard.get("level") == "warning":
                a["flip_warning"] = str(recent_opposite_guard.get("message") or "反対約定履歴あり")

        noop_reason = _intrinsic_filtered_reason(a)
        if noop_reason:
            a["filtered_reason"] = noop_reason
            filtered.append(a); continue

        # observe_only source の AI 昇格: 生 observe_only=True は上の intrinsic で落とす。
        # ここに来るのは source_observe_only=true / provisional_decision=true の昇格済み action だけ。
        if a.get("source_observe_only") is True or (
            a.get("provisional_decision") is True and str(a.get("source_lane") or "").strip()
        ):
            if not (
                a.get("provisional_decision") is True
                and _has_ai_bounded_reason(a, "ai_override_reason", "bounded_decision_reason")
                and str(a.get("source_lane") or "").strip()
            ):
                a["filtered_reason"] = (
                    f"source_observe_only: {ticker} は observe_only 由来だが "
                    "provisional_decision/source_lane/ai_override_reason が欠落"
                )
                filtered.append(a); continue
            base_cap = float(portfolio_total or 0) * 0.01
            scenario_id = str(a.get("scenario_id") or a.get("source_event_id") or "")
            if scenario_id:
                remaining = max(0.0, 600_000.0 - scenario_cap_used.get(scenario_id, 0.0))
                base_cap = min(base_cap, remaining)
            ok, cap_reason = _cap_bounded_action(
                a,
                gate="source_observe_only",
                cap_jpy=base_cap,
                estimated_jpy=amt,
                min_confidence=70,
            )
            if not ok:
                a["filtered_reason"] = cap_reason
                filtered.append(a); continue
            if scenario_id:
                scenario_cap_used[scenario_id] = scenario_cap_used.get(scenario_id, 0.0) + max(0.0, amt)

        # (3) earnings blackout — buy 系のみ
        if direction == "buy" and ticker in blackout:
            if (
                bool(a.get("earnings_event_trade") or a.get("policy_earnings_blackout_override"))
                and _has_ai_bounded_reason(a, "earnings_event_reason", "ai_override_reason", "bounded_decision_reason")
            ):
                ok, cap_reason = _cap_bounded_action(
                    a,
                    gate="earnings_blackout",
                    cap_jpy=float(portfolio_total or 0) * 0.005,
                    estimated_jpy=amt,
                    min_confidence=75,
                )
                if not ok:
                    a["filtered_reason"] = cap_reason
                    filtered.append(a); continue
            else:
                a["filtered_reason"] = (
                    f"earnings_blackout: {ticker} は決算 5 営業日以内。"
                    "earnings_event_trade と専用理由が無いため buy 推奨を抑制"
                )
                filtered.append(a); continue

        # (3.4) 推奨は terminal だが linked execution が ordered のままなら、
        # 再提案は可視化する一方で再発注は broker 状態確認まで止める。
        conflict_rows = order_state_conflicts.get((ticker, direction), [])
        if conflict_rows:
            existing = conflict_rows[0]
            a.update({
                "order_intent_decision": "stale_order_requires_confirmation",
                "filter_rule": "stale_order_requires_confirmation",
                "non_executable": True,
                "execution_state": "review",
                "execution_readiness": "review",
                "existing_order_id": existing.get("id"),
                "existing_order_status": existing.get("status"),
                "existing_order_quantity": existing.get("quantity"),
                "recommendation_status": existing.get("recommendation_status"),
                "non_executable_reason": (
                    f"stale_order_requires_confirmation: {ticker} {direction} は推奨状態が "
                    f"{existing.get('recommendation_status') or 'terminal'} だが、発注台帳 "
                    f"{existing.get('id') or '?'} が ordered のまま。証券会社で取消/約定を確認するまで再発注不可"
                ),
            })
            deferred.append(a); continue

        # (3.5) DONE_LIST — 直近 7 日に同 ticker × 同 direction が ordered/executed 済み
        if ticker and direction in ("buy", "sell", "short", "cover") and (ticker, direction) in done_set:
            intent_rows = done_intents.get((ticker, direction)) or []
            if not intent_rows:
                a["filtered_reason"] = f"already_executed: {ticker} {direction} は直近 7 日に発注/約定済み（DONE_LIST）"
                filtered.append(a); continue
            intent_decision = _classify_order_intent(
                a,
                done_intents,
                portfolio_total=portfolio_total,
                fx_rate=fx_rate,
                estimated_action_jpy=amt,
                done_days=_done_days,
            )
            decision = intent_decision.get("order_intent_decision")
            if decision == "new_order":
                pass
            elif decision in {"keep_existing_order", "amend_existing_order"}:
                a.update(intent_decision)
                deferred.append(a); continue
            else:
                a.update(intent_decision)
                a["filtered_reason"] = intent_decision.get("filtered_reason") or (
                    f"already_executed: {ticker} {direction} は直近 7 日に発注/約定済み（DONE_LIST）"
                )
                filtered.append(a); continue

        # (3.6) 未完了の反対方向 action_state がある場合は fail-closed。
        # 例: stale pending trim が残ったまま add を出すと、ユーザーには売買反転に見える。
        if ticker and direction in ("buy", "sell", "short", "cover"):
            opposite = {
                "buy": "sell",
                "sell": "buy",
                "short": "cover",
                "cover": "short",
            }.get(direction)
            opposite_rows = []
            if opposite:
                opposite_rows.extend(open_state_intents.get((ticker, opposite), []))
                opposite_rows.extend(open_execution_intents.get((ticker, opposite), []))
            if opposite_rows:
                existing = opposite_rows[0]
                try:
                    from execution_safety import routing_owner

                    action_owner = routing_owner(a)
                    existing_owner = routing_owner(existing)
                except Exception:
                    action_owner = str(a.get("execution_owner") or "")
                    existing_owner = str(existing.get("execution_owner") or "")
                if action_owner and existing_owner and action_owner != existing_owner:
                    a["cross_owner_opposite_action"] = True
                    a["cross_owner_opposite_order_id"] = existing.get("id")
                    a["cross_owner_opposite_owner"] = existing_owner
                    existing = None
                if existing is None:
                    pass
                else:
                    existing_id = existing.get("id") or "?"
                    existing_status = existing.get("status") or "open"
                    existing_type = existing.get("action_type") or existing.get("type") or opposite
                    a["filtered_reason"] = (
                        f"opposite_open_action: {ticker} {direction} は未完了の反対方向 "
                        f"{existing_type}({existing_status}, id={existing_id}) と矛盾。"
                        "既存アクションを filled/cancelled/expired に同期してから再評価"
                    )
                    a["opposite_open_action_id"] = existing_id
                    a["opposite_open_action_status"] = existing_status
                    a["opposite_open_action_type"] = existing_type
                    filtered.append(a); continue

        # (3.7) tax-loss harvest 矛盾解消: 損出し候補に buy/add/dca を出すのは矛盾
        if direction == "buy" and ticker in loss_set:
            if _has_ai_bounded_reason(a, "tax_override_reason", "ai_override_reason", "bounded_decision_reason"):
                ok, cap_reason = _cap_bounded_action(
                    a,
                    gate="tax_loss_harvest_conflict",
                    cap_jpy=float(portfolio_total or 0) * 0.005,
                    estimated_jpy=amt,
                    min_confidence=75,
                )
                if not ok:
                    a["filtered_reason"] = cap_reason
                    filtered.append(a); continue
            else:
                a["filtered_reason"] = (
                    f"tax_loss_harvest_conflict: {ticker} は損出し候補（含み損 ¥{_loss_min:,.0f}+）。"
                    "buy/add/dca と矛盾するため除去。AI が税効果を上回る理由を専用フィールドで示す必要あり。"
                )
                filtered.append(a); continue

        # (3.8) 全体統一: disable_stop_loss_recommendations=true なら
        # 全銘柄の stop_loss / 逆指値発注的アクションを除去（type 偽装も含む）
        if _disable_sl_global:
            _action_text = str(a.get("action") or "")
            _is_sl_intent = (
                atype_lc == "stop_loss"
                or "逆指値" in _action_text
                or "stop loss" in _action_text.lower()
                or "stop-loss" in _action_text.lower()
            )
            if _is_sl_intent:
                a["filtered_reason"] = (
                    f"disable_stop_loss_recommendations: {ticker} stop_loss / 逆指値推奨は "
                    f"全体設定で禁止 (tunable_params: disable_stop_loss_recommendations=true)"
                )
                filtered.append(a); continue

        # (3.9) 全体統一: disable_cumulative_recommendations=true なら
        # 買い側の定期/自動積立アクション（type=dca / 自動積立/クレカ/毎月 文言）を除去。
        # 「NISAつみたて枠で一括/スポット買い」は制度上ミスリードなので除去。
        # sell/trim の理由説明に「持株会」「月次積立は継続」が出ても除去しない。
        _is_buy_side_action = direction == "buy" or atype_lc in {"buy", "add", "dca", "margin_buy"}
        if _disable_cumulative_global and _is_buy_side_action:
            if _is_cumulative_buy_action(a, atype_lc):
                a["filtered_reason"] = (
                    f"disable_cumulative_recommendations: {ticker} 定期/自動積立アクションは "
                    f"broker で自動設定済み (tunable_params: disable_cumulative_recommendations=true)"
                )
                filtered.append(a); continue

        # (3.10) 6 soft constraint 強制（urgency 1段下げ程度の soft enforcement）
        # long_max_single_pct / medium_max_single_pct: 既存ポジ追加 buy で上限超過なら urgency 下げ
        try:
            from tunable_params import get as _tp_sc
            _long_max  = _tp_sc("long_max_single_pct", None)
            _med_max   = _tp_sc("medium_max_single_pct", None)
            _news_thr  = _tp_sc("news_score_threshold", None)
        except Exception:
            _long_max = _med_max = _news_thr = None
        # 銘柄保有比率を holdings_price_map から取得して比較
        if atype_lc in ("buy", "add", "dca") and ticker:
            _pos_value = float(_holding_info_for_action(a, holdings_price_map).get("value_jpy") or 0)
            _pt_total  = float(portfolio_total or 0)
            _ratio_pct = (_pos_value / _pt_total * 100) if _pt_total > 0 else 0
            _tier_lc   = str(a.get("tier", "")).lower()
            _exceed = False
            if _tier_lc == "long" and _long_max is not None and _ratio_pct >= float(_long_max):
                _exceed = True
                _reason_pct = _long_max
            elif _tier_lc == "medium" and _med_max is not None and _ratio_pct >= float(_med_max):
                _exceed = True
                _reason_pct = _med_max
            else:
                _reason_pct = None
            if _exceed:
                _orig_urg = str(a.get("urgency", "medium")).lower()
                _new_urg  = {"high": "medium", "medium": "low"}.get(_orig_urg, _orig_urg)
                a["urgency"] = _new_urg
                a.setdefault("_soft_downgrades", []).append(
                    f"single_max_pct: {ticker} {_ratio_pct:.1f}% ≥ {_reason_pct}% 上限 → urgency {_orig_urg}→{_new_urg}"
                )
        # news_score_threshold: 低 score ニュース起点の action は urgency 下げ
        if _news_thr is not None:
            _ns = a.get("news_score") or a.get("source_news_score")
            try:
                if _ns is not None and float(_ns) < float(_news_thr):
                    _orig_urg = str(a.get("urgency", "medium")).lower()
                    _new_urg  = {"high": "medium", "medium": "low"}.get(_orig_urg, _orig_urg)
                    a["urgency"] = _new_urg
                    a.setdefault("_soft_downgrades", []).append(
                        f"news_score: {_ns} < {_news_thr} 閾値 → urgency {_orig_urg}→{_new_urg}"
                    )
            except Exception:
                pass

        # (3.11) stance 連動: aggressive で confidence≥60 の short tier buy は urgency 上げ
        if _is_aggressive and atype_lc in ("buy", "add"):
            _conf  = a.get("confidence_pct") or 0
            _tier  = str(a.get("tier", "")).lower()
            try:
                if int(_conf) >= 60 and _tier in ("short", "swing"):
                    _orig_urg = str(a.get("urgency", "low")).lower()
                    _new_urg  = {"low": "medium", "medium": "high"}.get(_orig_urg, _orig_urg)
                    if _new_urg != _orig_urg:
                        a["urgency"] = _new_urg
                        a.setdefault("_soft_upgrades", []).append(
                            f"aggressive_stance: short tier conf {_conf}% → urgency {_orig_urg}→{_new_urg}"
                        )
            except Exception:
                pass

        # (3.12) leverage_health 連動: VIX 上昇で deleverage 必要時は新規 buy / margin_buy 抑制
        try:
            _lh_state = synthesis.get("leverage_health") or {}
            if isinstance(_lh_state, dict):
                _new_buy_ok = _lh_state.get("new_buy_allowed", True)
                _margin_ok  = _lh_state.get("margin_buy_allowed", True)
                if not _new_buy_ok and atype_lc in ("buy", "add", "dca", "margin_buy"):
                    a["filtered_reason"] = (
                        f"leverage_health.{_lh_state.get('status','?')}: "
                        f"current={_lh_state.get('current_leverage')}x cap={_lh_state.get('leverage_cap')}x "
                        f"({_lh_state.get('action','?')}) → 新規 buy 抑制"
                    )
                    filtered.append(a); continue
                if not _margin_ok and atype_lc == "margin_buy":
                    a["filtered_reason"] = (
                        f"leverage_health: margin_buy_allowed=False "
                        f"(VIX={_lh_state.get('vix','?')}, status={_lh_state.get('status','?')}) → 信用買い禁止"
                    )
                    filtered.append(a); continue
        except Exception:
            pass

        # (3.13) cash-first credit discipline:
        # 現金で賄える通常の margin_buy は、AI の銘柄判断を残しつつ現物 buy に正規化する。
        # 信用買いを維持するには、専用理由に加えて confidence/score/期待リターンが
        # tunable な高conviction閾値を満たす必要がある。
        if atype_lc == "margin_buy" and _cash_can_cover_action(amt) and not _has_margin_buy_exception(a):
            a = _convert_margin_buy_to_cash_buy(a, amt)
            direction = _direction_of(a.get("type"))
            atype_lc = str(a.get("type") or "").lower()

        # (3.6) リバランスクールダウン: trim/sell/rebalance を 3営業日に1回までに絞る
        if rebal_cooldown_active and atype_lc in {"trim", "rebalance", "sell", "take_profit"}:
            note = f"rebalance_cooldown: {rebal_cooldown_reason}（警告のみ。重複発注でなければAI判断を維持）"
            a["rebal_cooldown_warning"] = note
            a["execution_note"] = (
                f"{a.get('execution_note')} / {note}" if a.get("execution_note") else note
            )
            annotated.append(a)

        # (1) 最低取引額（少額ポジションの整理は免除）
        amt = _maybe_resize_kabu_mini_over_cap(a, amt)
        amt = _maybe_bump_small_buy(a, amt)
        if 0 <= amt < threshold:
            # 出口免除: 少額ポジション（評価額 < small_position_threshold_jpy）の sell/trim/stop_loss/take_profit は通す
            # → 「TXN 1株保有を売れない」永久ホールド問題を解消
            try:
                _small_pos_jpy = float(_tp_get("small_position_threshold_jpy", 300_000))
            except Exception:
                _small_pos_jpy = 300_000.0
            is_exit = atype_lc in {"sell", "trim", "stop_loss", "take_profit"}
            pos_info = _holding_info_for_action(a, holdings_price_map)
            pos_value_jpy = float(pos_info.get("value_jpy") or 0)
            is_small_position = 0 < pos_value_jpy < _small_pos_jpy

            if is_exit and is_small_position:
                a["small_position_exit"] = True  # トレース用マーカー
            elif amt >= threshold * 0.9:
                quantity = _parse_amount_hint_shares(a)
                minimum_quantity = None
                if quantity > 0 and amt > 0:
                    per_share = amt / quantity
                    if per_share > 0:
                        minimum_quantity = math.ceil(threshold / per_share)
                a.update({
                    "order_intent_decision": "near_minimum_notional",
                    "filter_rule": "near_minimum_notional",
                    "non_executable": True,
                    "execution_state": "review",
                    "execution_readiness": "review",
                    "minimum_executable_quantity": minimum_quantity,
                    "recommended_notional_jpy": round(amt),
                    "minimum_notional_jpy": round(threshold),
                    "non_executable_reason": (
                        f"near_minimum_notional: 推定 ¥{amt/10000:.1f}万は最小 "
                        f"¥{threshold/10000:.0f}万の90%以上だが未達。自動増額条件を満たさないため要確認"
                    ),
                })
                deferred.append(a); continue
            elif (
                a.get("small_notional_exception") is True
                and _has_ai_bounded_reason(a, "small_notional_exception_reason", "ai_override_reason", "bounded_decision_reason")
            ):
                ok, cap_reason = _cap_bounded_action(
                    a,
                    gate="too_small",
                    cap_jpy=threshold,
                    estimated_jpy=amt,
                    min_confidence=70,
                )
                if not ok:
                    a["filtered_reason"] = cap_reason
                    filtered.append(a); continue
            else:
                a["filtered_reason"] = (
                    f"too_small: 推定 ¥{amt/10000:.1f}万 < 最小 ¥{threshold/10000:.0f}万 "
                    f"(細切れリバランス抑制)。AI が small_notional_exception と専用理由を示せば例外可"
                )
                _mark_kabu_mini_verification_needed(a, reason="too_small", estimated_jpy=amt)
                filtered.append(a); continue

        # (1b) 最大取引額（単発ハードキャップ・AI上書き不可。H2）
        # 既存の絶対金額capはsource_observe_only/earnings_blackout/tax_loss_harvest_conflict
        # の3特殊ゲートのみに掛かり、通常のbuy/add/dca/margin_buyには上限が無かった。
        # ここはAIの明示理由による上書きを許さないfail-closedなhard capとして実装する
        # （上書きを許すと「結局プロンプト注意書きに頼る」構造に戻ってしまうため）。
        # continuous DCA (amt=inf) は対象外。amt<0 (金額推定不能) は通常buy系では
        # 検証不能として fail-closed で reject する。
        if atype_lc in {"buy", "add", "dca", "margin_buy", "short"} and amt != float("inf"):
            cap_profile = _single_action_cap_profile(a)
            cap_jpy = cap_profile["cap_jpy"]
            if amt < 0:
                a["filtered_reason"] = (
                    "max_single_action_cap: 金額推定不能 (amount_hint/action を解析できず "
                    f"単発上限 ¥{cap_jpy/10000:.0f}万 "
                    f"({cap_profile['label']}: portfolio_total×{cap_profile['pct']*100:.1f}%, "
                    f"絶対上限 ¥{cap_profile['abs_jpy']/10000:.0f}万) との比較ができない) "
                    "のため fail-closed で reject。AI上書き不可。"
                )
                _mark_kabu_mini_verification_needed(a, reason="amount_unparseable", estimated_jpy=amt)
                filtered.append(a); continue
            if amt > cap_jpy:
                a["filtered_reason"] = (
                    f"max_single_action_cap: 推定 ¥{amt/10000:.1f}万 が単発上限 "
                    f"¥{cap_jpy/10000:.0f}万 "
                    f"({cap_profile['label']}: portfolio_total×{cap_profile['pct']*100:.1f}%, "
                    f"絶対上限 ¥{cap_profile['abs_jpy']/10000:.0f}万) を超過。"
                    "AI上書き不可（hard cap）。"
                )
                a["single_action_cap_class"] = cap_profile["label"]
                a["single_action_cap_jpy"] = round(cap_jpy)
                _mark_kabu_mini_verification_needed(a, reason="max_single_action_cap", estimated_jpy=amt)
                filtered.append(a); continue

        # (1c) execution_plan — 週次/月次計画との整合。
        # H2 hard cap / DONE_LIST / blackout / tax など既存ガードを緩めないため、
        # ここは単発上限チェック後にのみ実行する。
        if atype_lc in {"buy", "add", "dca", "margin_buy", "sell", "trim", "reduce", "short", "cover"}:
            cap_profile_for_plan = _single_action_cap_profile(a)
            ok_plan, plan_reason = _apply_execution_plan_gate(a, amt, cap_profile_for_plan["cap_jpy"])
            if not ok_plan:
                a["filtered_reason"] = plan_reason or "execution_plan: non-executable by plan"
                filtered.append(a); continue

        # (2) cooldown — 直近 7 日同方向
        if ticker and direction in ("buy", "sell", "short", "cover") and (ticker, direction) in past_dir_7d:
            if _cooldown_same_day_only:
                cd_text = "本日すでに推奨済み"
            else:
                cd_text = f"直近 {_cd_days} 日に推奨済み"
            note = f"cooldown: {ticker} {direction} は{cd_text}（重複抑制・非表示にはしない）"
            a["cooldown_warning"] = note
            a["cooldown_duplicate"] = True
            a["execution_note"] = (
                f"{a.get('execution_note')} / {note}" if a.get("execution_note") else note
            )
            annotated.append(a)
            kept.append(a)
            continue

        # (4) flip warning
        if direction in ("buy", "sell", "short", "cover"):
            opposite = {
                "buy": "sell",
                "sell": "buy",
                "short": "cover",
                "cover": "short",
            }.get(direction)
            if opposite in past_dir_14d_by_ticker.get(ticker, set()):
                a["flip_warning"] = (
                    f"⚠️ 直近 14 日に {ticker} の {opposite} 推奨履歴あり。"
                    "判断の自己矛盾を必ず検証すること（市況の有意な変化が説明できれば許容）。"
                )

        kept.append(a)

    # A single synthesis must not silently recommend both buying and selling
    # the same ticker in the same (or unknown) account scope.  Opposite actions
    # are permitted only when both sides resolve to distinct explicit scopes,
    # e.g. Long/NISA buy versus Medium/taxable trim.
    by_ticker: dict[str, list[dict]] = {}
    for action in kept:
        if isinstance(action, dict) and action.get("ticker"):
            by_ticker.setdefault(str(action["ticker"]), []).append(action)

    opposite_conflicts: set[int] = set()
    for ticker, ticker_actions in by_ticker.items():
        buys = [a for a in ticker_actions if _direction_of(a.get("type")) == "buy"]
        sells = [a for a in ticker_actions if _direction_of(a.get("type")) == "sell"]
        for buy_action in buys:
            for sell_action in sells:
                buy_scope = (
                    str(buy_action.get("execution_owner") or "").strip().lower(),
                    str(buy_action.get("execution_broker") or "").strip().lower(),
                    str(buy_action.get("execution_account") or "").strip().lower(),
                    _normalized_investment_tier(
                        buy_action.get("execution_investment_type") or buy_action.get("tier")
                    ),
                )
                sell_scope = (
                    str(sell_action.get("execution_owner") or "").strip().lower(),
                    str(sell_action.get("execution_broker") or "").strip().lower(),
                    str(sell_action.get("execution_account") or "").strip().lower(),
                    _normalized_investment_tier(
                        sell_action.get("execution_investment_type") or sell_action.get("tier")
                    ),
                )
                scopes_explicit = bool(any(buy_scope) and any(sell_scope))
                if not scopes_explicit or buy_scope == sell_scope:
                    opposite_conflicts.update({id(buy_action), id(sell_action)})
                    continue
                for action, own_scope, other_scope in (
                    (buy_action, buy_scope, sell_scope),
                    (sell_action, sell_scope, buy_scope),
                ):
                    action["cross_scope_opposite_action"] = True
                    action["opposite_action_scope"] = {
                        "ticker": ticker,
                        "own": {
                            "owner": own_scope[0], "broker": own_scope[1],
                            "account": own_scope[2], "tier": own_scope[3],
                        },
                        "other": {
                            "owner": other_scope[0], "broker": other_scope[1],
                            "account": other_scope[2], "tier": other_scope[3],
                        },
                    }

    if opposite_conflicts:
        non_conflicting: list[dict] = []
        for action in kept:
            if isinstance(action, dict) and id(action) in opposite_conflicts:
                action["filtered_reason"] = (
                    f"same_analysis_opposite_conflict: {action.get('ticker') or '?'} の買いと売りが "
                    "同一または未特定の口座・運用ティアで同時提案されたため両方を停止"
                )
                action["same_analysis_opposite_conflict"] = True
                filtered.append(action)
            else:
                non_conflicting.append(action)
        kept = non_conflicting

    # The per-action classifier above deliberately does not mutate plan
    # remaining amounts.  Allocate only the final kept set now, after all
    # unrelated guards have completed, so rejected candidates cannot consume a
    # shared plan pool and several valid candidates cannot overbook it.
    plan_batch_report: dict = {"applied": False, "accepted_count": 0, "over_budget_count": 0}
    if plan_gate_mode != "off" and isinstance(execution_plan, dict) and execution_plan.get("items") and kept:
        controlled = {
            "plan_new_order",
            "opportunistic_override",
            "scenario_playbook_bounded",
        }
        try:
            from execution_plan_engine import allocate_candidate_batch_against_plan

            batch_results = allocate_candidate_batch_against_plan(kept, execution_plan)
            if len(batch_results) != len(kept):
                raise RuntimeError("execution-plan batch result length mismatch")
            plan_batch_report["applied"] = True
        except Exception as exc:
            batch_results = []
            plan_batch_report["error"] = str(exc)
            # Enforce mode treats allocator failures just like classifier
            # failures: plan-controlled orders do not bypass the safety gate.
            for action in kept:
                static_decision = str(
                    action.get("execution_plan_decision")
                    or action.get("execution_plan_observed_decision")
                    or ""
                )
                if static_decision not in controlled:
                    batch_results.append({"applicable": False})
                else:
                    batch_results.append({
                        "applicable": True,
                        "executable": False,
                        "execution_plan_decision": "execution_plan_error",
                        "reason": "final execution-plan batch allocation failed",
                    })

        batch_kept: list[dict] = []
        for action, batch in zip(kept, batch_results):
            if not isinstance(batch, dict) or not batch.get("applicable"):
                batch_kept.append(action)
                continue
            batch_decision = str(batch.get("execution_plan_decision") or "")
            is_executable = bool(batch.get("executable"))
            if plan_gate_mode == "observe":
                action["execution_plan_batch_observed_decision"] = batch_decision
                action["execution_plan_batch_would_filter"] = not is_executable
                if not is_executable:
                    action["execution_plan_would_filter"] = True
                    plan_observe_stats["would_filter_count"] += 1
                    plan_batch_report["over_budget_count"] += 1
                else:
                    plan_batch_report["accepted_count"] += 1
                for key, value in batch.items():
                    if key not in {"applicable", "executable", "reason", "execution_plan_decision"} and value is not None:
                        action[f"execution_plan_batch_{key}"] = value
                batch_kept.append(action)
                continue

            action["execution_plan_batch_decision"] = batch_decision
            if is_executable:
                plan_batch_report["accepted_count"] += 1
                for key, value in batch.items():
                    if key not in {"applicable", "executable", "reason", "execution_plan_decision"} and value is not None:
                        action[key] = value
                batch_kept.append(action)
                continue

            plan_batch_report["over_budget_count"] += 1
            action["execution_plan_decision"] = batch_decision or "plan_over_budget"
            action["filtered_reason"] = (
                f"{action['execution_plan_decision']}: {action.get('ticker') or '?'} は "
                f"最終候補集合で実行計画の残枠を超過 ({batch.get('reason') or 'batch allocation'})"
            )
            filtered.append(action)
        kept = batch_kept

    try:
        from execution_readiness import apply_execution_readiness
        apply_execution_readiness(kept, base_dir=BASE_DIR, now=analysis_now)
    except Exception as exc:
        for action in kept:
            action["execution_readiness"] = "review"
            action.setdefault("execution_block_reasons", []).append({
                "code": "execution_readiness_error",
                "message": f"実行可否判定に失敗: {type(exc).__name__}: {str(exc)[:160]}",
            })

    # The model's original rank is audit data.  The actionable/review board
    # needs contiguous ranks after all deterministic filters have run.
    kept = _reindex_final_action_ranks(kept)
    synthesis["priority_actions"]  = kept
    synthesis["_filtered_actions"] = filtered
    if deferred:
        synthesis["order_intent_deferred_actions"] = deferred
    else:
        synthesis.pop("order_intent_deferred_actions", None)
    synthesis["_annotated_actions"] = annotated
    if kabu_mini_verification_needed:
        synthesis["kabu_mini_verification_needed"] = kabu_mini_verification_needed
    else:
        synthesis.pop("kabu_mini_verification_needed", None)

    reasons: dict = {}
    for f in filtered:
        tag = (f.get("filtered_reason") or "?").split(":")[0]
        reasons[tag] = reasons.get(tag, 0) + 1
    deferred_reasons: dict = {}
    for f in deferred:
        tag = f.get("filter_rule") or f.get("order_intent_decision") or "deferred"
        deferred_reasons[tag] = deferred_reasons.get(tag, 0) + 1
    synthesis["post_filter"] = {
        "input_count":    len(actions),
        "kept_count":     len(kept),
        "filtered_count": len(filtered),
        "deferred_count": len(deferred),
        "annotated_count": len(annotated),
        "summary":        reasons,
        "deferred_summary": deferred_reasons,
        "annotated_summary": {"cooldown": len(annotated)} if annotated else {},
        "all_actions_filtered": bool(actions and not kept and (filtered or deferred)),
        "cooldown_scope": "same_calendar_day" if _cooldown_same_day_only else f"rolling_{_cd_days}d",
        "policy_accepted_count": (
            synthesis.get("policy_decision", {}).get("accepted_count")
            if isinstance(synthesis.get("policy_decision"), dict) else None
        ),
        "kabu_mini_verification_needed_count": len(kabu_mini_verification_needed),
    }
    plan_gate_report: dict = {"mode": plan_gate_mode}
    plan_consumption = (
        execution_plan.get("consumption_summary")
        if isinstance(execution_plan, dict) and isinstance(execution_plan.get("consumption_summary"), dict)
        else None
    )
    has_monthly_ledger = isinstance(plan_consumption, dict) and "monthly_consumed_jpy" in plan_consumption

    def _plan_nonnegative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    plan_gate_report["monthly_attribution"] = {
        "available": has_monthly_ledger,
        "unattributed_count": (
            _plan_nonnegative_int(plan_consumption.get("unattributed_monthly_total_count"))
            if has_monthly_ledger else 0
        ),
        "unattributed_notional_jpy": (
            _plan_nonnegative_int(plan_consumption.get("unattributed_monthly_total_notional_jpy"))
            if has_monthly_ledger else 0
        ),
    }
    if plan_gate_mode_warning:
        plan_gate_report["warning"] = plan_gate_mode_warning
    if plan_observe_stats["decisions"]:
        plan_gate_report["observed_decisions"] = plan_observe_stats["decisions"]
        plan_gate_report["would_filter_count"] = plan_observe_stats["would_filter_count"]
    if plan_batch_report["applied"] or plan_batch_report.get("error"):
        plan_gate_report["batch_allocation"] = plan_batch_report
    synthesis["post_filter"]["execution_plan_gate"] = plan_gate_report

    reason_counts = dict(reasons)
    for action in deferred:
        code = str(action.get("filter_rule") or action.get("order_intent_decision") or "review")
        reason_counts[code] = reason_counts.get(code, 0) + 1
    for action in kept:
        if action.get("execution_readiness") == "ready":
            continue
        for row in action.get("execution_block_reasons") or []:
            code = str(row.get("code") or "execution_review") if isinstance(row, dict) else "execution_review"
            reason_counts[code] = reason_counts.get(code, 0) + 1
    executable_count = sum(1 for action in kept if action.get("execution_readiness") == "ready")
    readiness_review_count = sum(1 for action in kept if action.get("execution_readiness") != "ready")
    review_count = readiness_review_count + len(deferred)
    if executable_count > 0:
        no_action_classification = None
    elif not actions:
        no_action_classification = "market_no_trade"
    elif any(code.startswith("portfolio_snapshot") or code.startswith("technical_data") for code in reason_counts):
        no_action_classification = "data_unavailable"
    elif reason_counts.get("stale_order_requires_confirmation") and len(reason_counts) == 1:
        no_action_classification = "state_reconciliation_required"
    else:
        no_action_classification = "system_constraints"
    _set_operational_stance(synthesis, reason_counts, executable_count, actions=kept)
    synthesis["decision_summary"] = {
        "candidate_count": len(actions),
        "executable_count": executable_count,
        "review_count": review_count,
        "filtered_count": len(filtered),
        "deferred_count": len(deferred),
        "no_action_classification": no_action_classification,
        "reason_counts": reason_counts,
        "count_conservation_ok": len(actions) == len(kept) + len(filtered) + len(deferred),
    }
    if kabu_mini_verification_needed:
        try:
            from kabu_mini_eligibility import record_kabu_mini_verification_needed
            record_kabu_mini_verification_needed(kabu_mini_verification_needed)
            synthesis.pop("kabu_mini_verification_write_error", None)
        except Exception as exc:
            synthesis["kabu_mini_verification_write_error"] = str(exc)
    synthesis["decision_boundary_audit"] = _build_decision_boundary_audit(
        kept,
        filtered,
        annotated,
        context_blocks=synthesis.get("context_blocks") if isinstance(synthesis.get("context_blocks"), dict) else {},
    )

    if filtered or deferred:
        if reasons:
            synthesis["_filtered_action_summary"] = reasons
        else:
            synthesis.pop("_filtered_action_summary", None)
        if deferred:
            synthesis["_deferred_action_summary"] = deferred_reasons
        else:
            synthesis.pop("_deferred_action_summary", None)
        if not kept:
            summary_parts = []
            if reasons:
                summary_parts.extend(f"{k}={v}" for k, v in sorted(reasons.items()))
            if deferred_reasons:
                summary_parts.extend(f"deferred:{k}={v}" for k, v in sorted(deferred_reasons.items()))
            synthesis["no_action_rationale"] = (
                f"AI提案 {len(filtered) + len(deferred)} 件は全て post-filter で除去/保留されました: "
                + ", ".join(summary_parts)
            )
            _pa = synthesis["post_filter"].get("policy_accepted_count") or 0
            if _pa > 0:
                synthesis["post_filter"]["warning"] = (
                    f"Policy Engine accepted {_pa} 件を post-filter が全除去/保留。"
                    "状態同期または cooldown 設計を確認してください。"
                )
        else:
            synthesis.pop("no_action_rationale", None)
        if filtered:
            print(f"  🛡️ Phase1 post-filter: {len(filtered)}件除去 reasons={reasons}, 残り {len(kept)}件")
        if deferred:
            print(f"  🅿️ Phase1 post-filter: {len(deferred)}件保留 reasons={deferred_reasons}, 残り {len(kept)}件")
    else:
        synthesis.pop("_filtered_action_summary", None)
        synthesis.pop("_deferred_action_summary", None)
        synthesis.pop("no_action_rationale", None)

    if actions and executable_count == 0:
        summary_text = ", ".join(f"{key}={value}" for key, value in sorted(reason_counts.items()))
        synthesis["no_action_rationale"] = (
            f"実行可能アクション0件 ({no_action_classification})。候補{len(actions)}件の内訳: {summary_text}"
        )

    if annotated:
        print(f"  📝 Phase1 post-filter: cooldown {len(annotated)}件は注釈のみ（非表示化しない）")

    # ── telegram_message を kept actions に合わせて再構築 ──────────────────────
    # Opus が生成した telegram_message はフィルタ前の actions を参照しているため、
    # フィルタ後の kept actions で上書きしてヘッダーと個別メッセージを一致させる。
    executable_actions = [a for a in kept if a.get("execution_readiness") == "ready"]
    if executable_actions:
        _urgency_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        # LLM の既存1行目にはフィルタ前候補が列挙されることがあるため再利用しない。
        # スタンスだけを決定論的に再構築し、発注対象は ready actions に限定する。
        operational = synthesis.get("operational_stance") or {}
        op_label = str(operational.get("label") or "") if isinstance(operational, dict) else ""
        first_line = f"📊 stance={_stance or 'neutral'}" + (f" / operation={op_label}" if op_label else "")
        action_lines = []
        for i, a in enumerate(executable_actions[:7], 1):
            if not isinstance(a, dict):
                continue
            urg    = _urgency_icon.get(str(a.get("urgency", "medium")).lower(), "🟡")
            ticker = a.get("ticker") or ""
            body   = (a.get("action") or "").strip()
            # ticker が本文冒頭に重複している場合は剥がす
            if ticker and body.upper().startswith(ticker.upper()):
                body = body[len(ticker):].lstrip(" ・:：")
            hint = a.get("amount_hint") or ""
            line = f"{i}. {urg} {ticker} {body}"
            if hint:
                line += f"（{hint}）"
            action_lines.append(line)
        filtered_lines = []
        for f in (filtered + deferred + [a for a in kept if a not in executable_actions])[:5]:
            if not isinstance(f, dict):
                continue
            ticker = f.get("ticker") or ""
            body = (f.get("action") or "").strip()
            if ticker and body.upper().startswith(ticker.upper()):
                body = body[len(ticker):].lstrip(" ・:：")
            readiness = str(f.get("execution_readiness") or "")
            block_reasons = f.get("execution_block_reasons") or []
            first_block = block_reasons[0] if block_reasons else {}
            reason_tag = str(
                f.get("filtered_reason")
                or f.get("filter_rule")
                or f.get("order_intent_decision")
                or (first_block.get("code") if isinstance(first_block, dict) else "")
                or readiness
                or "filtered"
            ).split(":", 1)[0]
            filtered_lines.append(f"- {ticker} {body[:45]}（{reason_tag}）")
        new_msg = (first_line + "\n\n" if first_line else "") + "\n".join(action_lines)
        if filtered_lines:
            new_msg += "\n\n参考候補（実行除外）\n" + "\n".join(filtered_lines)
        synthesis["telegram_message"] = new_msg[:1000]
        synthesis["telegram_message_scope"] = "ready_only"
        print(f"  📱 telegram_message を ready {len(executable_actions)}件 actions から再構築")
    elif filtered or deferred or kept:
        rationale = synthesis.get("no_action_rationale") or (
            "AI提案は全て post-filter で除去/保留されました"
        )
        scope = synthesis.get("post_filter", {}).get("cooldown_scope")
        suffix = f" / scope={scope}" if scope else ""
        summary = synthesis.get("_filtered_action_summary") or {}
        if isinstance(summary, dict) and summary and set(summary) == {"policy__rule_ledger_integrity"}:
            rationale = (
                "台帳整合性エラーのため、AI候補は全て実行停止。"
                "保有・現金台帳の照合が終わるまで発注しないでください。"
            )
        synthesis["telegram_message"] = (
            f"📊 実行アクション 0件 ({_stance or 'neutral'})\n"
            f"{rationale}{suffix}\n"
            "参考候補はJSON/UIに保存済みですが、Telegram上では発注対象として扱いません。"
        )[:500]
        synthesis["telegram_message_scope"] = "ready_only"
        print("  📱 telegram_message を no-action 用に再構築")

    return synthesis


# ── AI推奨事後検証ログ ───────────────────────────────────

def _parse_amount_hint_shares(action: dict) -> int:
    text = " ".join(str(action.get(k, "")) for k in ("amount_hint", "action"))
    m = re.search(r"(\d+)\s*(?:株|口)", text)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _risk_mode_from_stance(stance: str | None) -> str:
    s = (stance or "").lower()
    if s in {"aggressive", "moderately_aggressive"}:
        return "aggressive"
    if s == "defensive":
        return "defensive"
    return "neutral"


def _portfolio_decision_state_from_synthesis(synthesis: dict) -> str:
    final_actions = synthesis.get("final_priority_actions") or synthesis.get("priority_actions") or []
    raw_actions = synthesis.get("raw_priority_actions") or []
    if isinstance(final_actions, list) and len(final_actions) > 0:
        return "action_taken"
    policy = synthesis.get("policy_decision") or {}
    if isinstance(policy, dict) and (
        policy.get("failed_closed")
        or (policy.get("rejected_count", 0) and not policy.get("accepted_count", 0))
    ):
        return "risk_blocked"
    if isinstance(raw_actions, list) and len(raw_actions) > 0:
        return "cash_retained"
    return "no_valid_candidates"


def _scenario_rows_for_log() -> list[dict]:
    state = load_json(BASE_DIR / "scenario_state.json", {}) or {}
    scenarios = state.get("scenarios") if isinstance(state, dict) else None
    if isinstance(scenarios, dict):
        iterable = scenarios.items()
    elif isinstance(scenarios, list):
        iterable = ((sc.get("id", sc.get("name", "?")), sc) for sc in scenarios if isinstance(sc, dict))
    else:
        return []
    rows = []
    for key, sc in iterable:
        if not isinstance(sc, dict):
            continue
        if sc.get("status") not in {"active", "watching"}:
            continue
        rows.append({
            "key": key,
            "name": sc.get("name"),
            "readiness": sc.get("readiness"),
            "status": sc.get("status"),
        })
    return rows


def _count_filtered_reasons(synthesis: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("policy_filtered_actions", "_filtered_actions", "_degraded_filtered_actions"):
        for item in synthesis.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            reason = item.get("rule") or item.get("filtered_reason") or item.get("reason") or key
            reason = str(reason).split(":", 1)[0][:80]
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _write_runtime_observability_logs(
    synthesis: dict,
    data: dict,
    *,
    analysis_id: str | None = None,
    fsync: bool = True,
) -> dict:
    """Append runtime observability rows from the final analyzer output.

    Catalyst hypotheses are written by ``catalyst_layer.run``. This function
    covers the main analyzer hooks: final-decider attribution, portfolio-level
    daily decision, and sell/trim recommendation events.
    """
    if not isinstance(synthesis, dict):
        return {"written": 0, "errors": ["synthesis_not_dict"]}
    try:
        from almanac.observability.candidate_extractor import final_action_hypothesis_identity
        from almanac.observability.ids import new_analysis_id, new_row_id
        from almanac.observability.logs import (
            write_agent_attribution,
            write_portfolio_decision,
            write_sell_decision,
        )
    except Exception as exc:
        return {"written": 0, "errors": [f"import_failed: {exc}"]}

    analysis_id = analysis_id or new_analysis_id()
    analysis_date = datetime.now().strftime("%Y-%m-%d")
    final_actions = synthesis.get("final_priority_actions") or synthesis.get("priority_actions") or []
    raw_actions = synthesis.get("raw_priority_actions") or []
    if not isinstance(final_actions, list):
        final_actions = []
    if not isinstance(raw_actions, list):
        raw_actions = []

    errors: list[str] = []
    written = 0

    cash_info = data.get("cash_info") or {}
    total_assets = float(data.get("portfolio_total") or 0.0)
    total_cash = float(cash_info.get("total_cash_jpy") or cash_info.get("total_cash") or 0.0)
    cash_ratio = max(0.0, min(1.0, total_cash / total_assets)) if total_assets > 0 else 0.0
    try:
        write_portfolio_decision(
            BASE_DIR / "portfolio_decision_log.jsonl",
            analysis_date=analysis_date,
            analysis_id=analysis_id,
            portfolio_decision_state=_portfolio_decision_state_from_synthesis(synthesis),
            risk_mode=_risk_mode_from_stance(synthesis.get("overall_stance")),
            cash_ratio=cash_ratio,
            total_assets_jpy=total_assets,
            active_scenarios=_scenario_rows_for_log(),
            generated_candidates=len(raw_actions),
            injected_candidates=len(raw_actions),
            adopted_candidates=len(final_actions),
            rejected_count_by_reason=_count_filtered_reasons(synthesis),
            opus_no_buy_reason=synthesis.get("no_action_rationale") if not final_actions else None,
            cash_critic_triggered=bool(synthesis.get("cash_critic_triggered")),
            benchmark_return_today=0.0,
            portfolio_return_today=0.0,
            opportunity_cost_today_bps=0.0,
            fsync=fsync,
        )
        written += 1
    except Exception as exc:
        errors.append(f"portfolio_decision: {exc}")

    for action in final_actions:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "")
        atype = str(action.get("type") or action.get("action_type") or "")
        if not ticker or not atype:
            continue
        identity = final_action_hypothesis_identity(action)
        if identity is None:
            continue
        hypothesis_id = str(identity["hypothesis_id"])
        hypothesis_type = str(identity["hypothesis_type"])
        horizon = int(identity["horizon_days"])
        canonical_atype = str(identity["action_type"])
        try:
            write_agent_attribution(
                BASE_DIR / "agent_attribution_log.jsonl",
                hypothesis_id=hypothesis_id,
                analysis_id=analysis_id,
                analysis_date=analysis_date,
                ticker=str(identity["ticker"]),
                hypothesis_type=hypothesis_type,
                time_horizon_days=horizon,
                agent="opus_final",
                role="final_decider",
                stance="support",
                confidence_pct=action.get("confidence_pct") if isinstance(action.get("confidence_pct"), int) else None,
                reason=action.get("reason"),
                final_candidate_status="adopted",
                fsync=fsync,
            )
            written += 1
        except Exception as exc:
            errors.append(f"agent_attribution:{ticker}: {exc}")

        if canonical_atype in {"sell", "trim"}:
            try:
                write_sell_decision(
                    BASE_DIR / "sell_decision_log.jsonl",
                    sell_decision_id=new_row_id(),
                    hypothesis_id=hypothesis_id,
                    ticker=ticker,
                    action_type=canonical_atype,
                    shares_recommended=_parse_amount_hint_shares(action),
                    price_at_recommend=float(action.get("decision_price") or action.get("limit_price") or 0.0),
                    reason=str(action.get("reason") or ""),
                    conviction_at_sell=int(action.get("confidence_pct") or 0),
                    benchmark_basket=["SPY"],
                    benchmark_weights=[1.0],
                    execution_state="not_ordered",
                    context_blocks=synthesis.get("context_blocks") or {},
                    narrative_context_present=bool(
                        (synthesis.get("context_blocks") or {}).get("ipo_watch")
                        or (synthesis.get("context_blocks") or {}).get("news_topic")
                        or (synthesis.get("context_blocks") or {}).get("social_topic")
                        or (synthesis.get("context_blocks") or {}).get("geopolitical")
                    ),
                    fsync=fsync,
                )
                written += 1
            except Exception as exc:
                errors.append(f"sell_decision:{ticker}: {exc}")

    return {"written": written, "errors": errors, "analysis_id": analysis_id}


def _log_red_team_verdicts(synthesis: dict) -> None:
    """RedTeam攻撃案へのOpus採否(adopt/partial/reject)を red_team_ledger に記録する。

    攻めバックログ2026-07 項目2: 特に reject は現状どこにも残らず、
    「止めたのが正しかったか」を事後検証する手段が無かった。
    synthesis["red_team_verdict"] はプロンプトで要求されるがこれまで
    未使用だったフィールド。失敗しても本分析フローは止めない。
    """
    verdicts = synthesis.get("red_team_verdict")
    if not isinstance(verdicts, list) or not verdicts:
        return
    try:
        from red_team_ledger import record_verdict
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            ticker = v.get("ticker")
            action = v.get("action")
            verdict = v.get("verdict")
            if not ticker or not action or verdict not in ("adopt", "partial", "reject"):
                continue
            try:
                record_verdict(
                    ticker=str(ticker),
                    action=str(action),
                    verdict=str(verdict),
                    verdict_reason=str(v.get("verdict_reason") or ""),
                    model="opus_synthesis",
                )
            except Exception:
                continue
    except Exception:
        pass


def _log_recommendations(synthesis: dict, market_meta: dict) -> None:
    """
    AI推奨アクションを推奨時の市場データとともに保存する。
    事後検証（何日後に推奨が正しかったか）に使用。

    保存先: ai_recommendation_log.json（最大 100 件ローテーション）
    """
    from utils import atomic_write_json

    log_path = BASE_DIR / "ai_recommendation_log.json"
    MAX_ENTRIES = 500

    existing: list = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []

    actions = synthesis.get("priority_actions", [])
    as_of   = datetime.now().isoformat()

    for a in actions[:7]:  # 上位 7 件まで記録
        ticker = a.get("ticker")
        if not ticker:
            continue

        # 推奨時の価格を yfinance から取得（ベストエフォート）
        price_at_rec = None
        if not is_pseudo_market_ticker(ticker):
            try:
                import yfinance as yf
                price_at_rec = round(float(yf.Ticker(ticker).fast_info['lastPrice']), 2)
            except Exception:
                pass

        entry = {
            "as_of":           as_of,
            "ticker":          ticker,
            "tier":            a.get("tier"),
            "type":            a.get("type"),
            "urgency":         a.get("urgency"),
            "action":          a.get("action", "")[:200],
            "reason":          a.get("reason", "")[:200],
            "amount_hint":     a.get("amount_hint", ""),
            "confidence_pct":  a.get("confidence_pct"),
            "return_20d_rank": a.get("return_20d_rank"),
            "analysis_id":     a.get("analysis_id") or synthesis.get("analysis_id"),
            "execution_account": a.get("execution_account"),
            "execution_owner": a.get("execution_owner"),
            "execution_broker": a.get("execution_broker"),
            "execution_position_keys": a.get("execution_position_keys") or [],
            "price_at_rec":    price_at_rec,
            "vix_at_rec":      market_meta.get("vix"),
            "us10y_at_rec":    market_meta.get("us10y_yield", {}).get("value"),
            "stance":          synthesis.get("overall_stance"),
            "verified":        False,         # 事後検証時に True に更新
            "verified_at":     None,
            "price_verified":  None,
            "outcome_pct":     None,
        }
        existing.append(entry)

    # 最大 MAX_ENTRIES 件を保持
    if len(existing) > MAX_ENTRIES:
        existing = existing[-MAX_ENTRIES:]

    atomic_write_json(log_path, existing)
    print(f"  📝 推奨ログ保存: {len(actions[:7])}件 → ai_recommendation_log.json")
    # 発注状態管理: stop_loss / sell (high urgency) を追跡
    try:
        from action_state_tracker import record_recommendations as _record_acts
        _n_tracked = _record_acts(actions[:7], source="opus")
        if _n_tracked:
            print(f"  📌 発注追跡登録: {_n_tracked}件 → action_state.json")
    except Exception:
        pass


def _ensure_scenario_state_fresh(base_dir: Path = BASE_DIR, evaluator=None, *, force: bool = False) -> bool:
    """scenario_state.json が scenario_playbook.json / scenario_engine.py より古い(or 不在)
    なら deterministic に再生成する。Returns True if refreshed.

    portfolio_analyst.py --force は scenario_state を再生成しないため、playbook を編集しても
    古い required_signals / readiness が AI 分析に入り得る(war_end の required_signals が
    stale で残った事例)。mtime 比較で stale を検知し evaluate_scenarios() で更新する。
    """
    state_p = base_dir / "scenario_state.json"
    play_p = base_dir / "scenario_playbook.json"
    engine_p = base_dir / "scenario_engine.py"
    state_mtime = state_p.stat().st_mtime if state_p.exists() else -1.0
    input_paths = (
        play_p,
        engine_p,
        base_dir / "vix_state.json",
        base_dir / "geopolitical_state.json",
        base_dir / "technical_state.json",
        base_dir / "macro_state.json",
        base_dir / "regime_state.json",
        base_dir / "market_snapshot.json",
    )
    src_mtime = max((path.stat().st_mtime if path.exists() else 0.0) for path in input_paths)
    if force or state_mtime < src_mtime:
        if evaluator is None:
            import scenario_engine as _scen
            evaluator = _scen.evaluate_scenarios
        evaluator()
        return True
    return False


def _refresh_execution_plan_state(
    *,
    base_dir: Path = BASE_DIR,
    generator=None,
    now: datetime | None = None,
) -> dict:
    """Regenerate execution_plan_state.json before a fresh AI analysis.

    If the planner itself fails, replace the artifact with an empty disabled
    state so a stale previous plan cannot silently suppress today's actions.
    Existing hard guards still apply even when this planning layer is disabled.
    """
    if _env_bool("ALMANAC_SKIP_EXECUTION_PLAN_REFRESH", False):
        return {"ok": False, "skipped": True, "reason": "env_skip"}

    now = now or datetime.now()
    if generator is None:
        from execution_plan_engine import generate_execution_plan
        generator = generate_execution_plan

    try:
        plan = generator(base_dir=base_dir, now=now, write=True)
        items = plan.get("items", []) if isinstance(plan, dict) else []
        summary = plan.get("consumption_summary", {}) if isinstance(plan, dict) else {}
        return {
            "ok": True,
            "items": len(items) if isinstance(items, list) else None,
            "remaining_normal_jpy": summary.get("remaining_normal_jpy") if isinstance(summary, dict) else None,
            "remaining_opportunity_jpy": summary.get("remaining_opportunity_jpy") if isinstance(summary, dict) else None,
        }
    except Exception as exc:
        try:
            from utils import atomic_write_json

            today = now.date()
            weekday = today.weekday()
            week_start = today - timedelta(days=weekday)
            week_end = week_start + timedelta(days=6)
            atomic_write_json(
                base_dir / "execution_plan_state.json",
                {
                    "schema_version": "1.0",
                    "as_of": now.astimezone().isoformat(timespec="seconds") if now.tzinfo else now.isoformat(timespec="seconds"),
                    "horizon": {
                        "month": today.strftime("%Y-%m"),
                        "week_start": week_start.isoformat(),
                        "week_end": week_end.isoformat(),
                    },
                    "status": "disabled",
                    "source_versions": {},
                    "budgets": {},
                    "consumption_summary": {},
                    "items": [],
                    "no_action_rationale": [
                        "execution_plan refresh failed; disabled for this analysis to avoid stale-plan suppression"
                    ],
                    "warnings": [f"execution_plan_refresh_failed: {type(exc).__name__}: {str(exc)[:300]}"],
                    "generated_by": "analyst._refresh_execution_plan_state",
                },
            )
        except Exception:
            pass
        return {"ok": False, "error": str(exc)[:500]}


def _quarantine_post_filter_failure(synthesis: dict, error: Exception | str) -> int:
    """post-filter が例外で完了しなかったときの fail-closed 隔離。

    Policy Engine 呼び出し部 (policy_engine_error) と同じ思想: 後段ガード
    (H2 cap / DONE_LIST / execution_plan 等) を評価できなかった候補を
    実行可能なまま残さない。priority_actions を空にし、候補は
    _filtered_actions へ non_executable で隔離する (post_filter_rejected
    として stage log に載り、record_recommendations / Telegram 実行リスト /
    実行ボタンには載らない)。
    """
    if not isinstance(synthesis, dict):
        return 0
    blocked = [a for a in (synthesis.get("priority_actions") or []) if isinstance(a, dict)]
    reason = f"post_filter_error: post-filter が例外で完了せず fail-closed ({str(error)[:200]})"
    quarantined = []
    for a in blocked:
        row = dict(a)
        row["filtered_reason"] = reason
        row["filter_rule"] = "post_filter_error"
        row["non_executable"] = True
        row["execution_state"] = "not_ordered"
        quarantined.append(row)
    synthesis["priority_actions"] = []
    if isinstance(synthesis.get("_filtered_actions"), list):
        synthesis["_filtered_actions"].extend(quarantined)
    else:
        synthesis["_filtered_actions"] = quarantined
    synthesis["post_filter_error"] = str(error)[:500]
    post_filter = synthesis.get("post_filter")
    if not isinstance(post_filter, dict):
        post_filter = {}
        synthesis["post_filter"] = post_filter
    post_filter["error"] = str(error)[:200]
    post_filter["fail_closed"] = True
    post_filter["kept_count"] = 0
    synthesis["no_action_rationale"] = (
        f"post-filter が例外で完了しなかったため、fail-closed で候補 {len(quarantined)} 件を全て実行保留にしました"
    )
    warn = "⚠️ post-filter 障害により本日の実行候補は全て保留（fail-closed）"
    message = str(synthesis.get("telegram_message") or "")
    synthesis["telegram_message"] = (f"{warn}\n\n{message}" if message else warn)[:1000]
    return len(quarantined)


# ── Scenario playbook deterministic injection (2026-07-07) ──

_PLAYBOOK_INJECT_BUY_TYPES = {"buy"}
_PLAYBOOK_REPROPOSE_DAYS = 7          # 同一 ticker×buy の再提案間隔
# 1回の分析で注入する合計名目の上限 (総資産比)。japan_standalone_bull の
# ¥500k×2銘柄 (≈3.4%) が単独で通るサイズにし、max_single_action_pct (5%) と揃える。
_PLAYBOOK_INJECT_TOTAL_CAP_PCT = 0.05


def _inject_playbook_actions(synthesis: dict, data: dict) -> dict:
    """active/partial シナリオの phase_1 buy を priority_actions へ決定論注入する。

    設計 (fail-open — 注入失敗は AI 案のみで続行、安全側の欠落はゲートが担保):
      - 対象: scenario_monitoring.active_scenarios (= enabled_for_decision 済) のうち
        status が active (scale 1.0) / partial (scale 0.5)。phase_1 の buy のみ。
        phase_2 以降は confirmation_required 前提のため注入しない。
      - 除外: 既に priority_actions に同 ticker が居る / insider_restricted /
        直近 7 日以内に同 ticker の buy が**実行済み/発注中** (executed/ordered) /
        JP 銘柄で jp_equity_ex_employer_pct が目標到達済み。
        ※抑制は執行ベース。推奨が出ただけで未執行なら、シナリオ継続中は
        毎回再提案する (2026-07-07 ユーザー指示 — 推奨は出たが執行されず消える問題への対策)。
      - サイズ: allocation_jpy × scale (USD は FX 換算)。1回の注入合計は総資産の 3% まで
        (priority high → readiness 降順で消化)。
      - 注入後は通常の policy gate / post_filter / 単発上限を全て通る。
    """
    injected: list = []
    skipped: list = []

    # Reserved attestation is owned by this deterministic injector.  Strip any
    # model-supplied copy before deciding which rows to append, so an LLM cannot
    # self-declare the dedicated execution-plan override.
    for existing in synthesis.get("priority_actions", []) or []:
        if not isinstance(existing, dict):
            continue
        existing.pop("playbook_gate", None)
        existing.pop("playbook_injected", None)

    sm = data.get("scenario_monitoring") or {}
    scenarios = [
        sc for sc in (sm.get("active_scenarios") or [])
        if isinstance(sc, dict) and sc.get("status") in ("active", "partial")
    ]
    if not scenarios:
        return {"injected": injected, "skipped": skipped}
    _prio_rank = {"high": 0, "medium": 1, "low": 2}
    scenarios.sort(key=lambda s: (_prio_rank.get(str(s.get("priority")), 1),
                                  -float(s.get("readiness_pct") or 0)))

    playbook = load_json(BASE_DIR / "scenario_playbook.json", {}) or {}
    playbook_by_id = {
        str(sc.get("id")): sc for sc in playbook.get("scenarios", []) if isinstance(sc, dict)
    }

    try:
        restricted = set((load_json(BASE_DIR / "insider_restricted.json", {}) or {}).get("tickers") or [])
    except Exception:
        restricted = set()

    # 直近 N 日に buy を実行済み/発注中の ticker (執行ベースの再提案抑制)。
    # 推奨ログではなく action_executions を見る — 推奨が出ただけで未執行の銘柄は
    # 抑制せず再提案し続ける (執行されるか、シナリオが落ちるまで)。
    recent_buy_tickers: set = set()
    try:
        _cutoff = (datetime.now() - timedelta(days=_PLAYBOOK_REPROPOSE_DAYS)).isoformat()
        _exec_raw = load_json(BASE_DIR / "action_executions.json", {}) or {}
        for e in _exec_raw.get("executions", []) or []:
            if not isinstance(e, dict):
                continue
            if str(e.get("direction") or "").lower() == "buy" \
               and str(e.get("status") or "").lower() in ("executed", "ordered") \
               and str(e.get("saved_at") or "") >= _cutoff:
                recent_buy_tickers.add(str(e.get("ticker") or ""))
    except Exception:
        recent_buy_tickers = set()

    existing_tickers = {
        str(a.get("ticker") or "")
        for a in synthesis.get("priority_actions", []) if isinstance(a, dict)
    }

    portfolio_total = float(data.get("portfolio_total") or 0)
    total_cap_jpy = portfolio_total * _PLAYBOOK_INJECT_TOTAL_CAP_PCT
    jp_exp = data.get("jp_exposure") or {}
    jp_pct = jp_exp.get("jp_equity_ex_employer_pct")
    jp_target = jp_exp.get("target_pct")

    try:
        from utils import get_fx_rate_cached
        fx_rate, _ = get_fx_rate_cached(account_json_path=BASE_DIR / "account.json")
        fx_rate = float(fx_rate)
    except Exception:
        fx_rate = 150.0

    used_jpy = 0.0
    for sc in scenarios:
        sid = str(sc.get("id") or "")
        raw_scale = sc.get("allocation_scale")
        scale = float(raw_scale) if raw_scale is not None else (
            1.0 if sc.get("status") == "active" else 0.5
        )
        pb = playbook_by_id.get(sid) or {}
        phase1 = ((pb.get("actions") or {}).get("phase_1") or {})
        if scale <= 0:
            for entry in phase1.get("buy") or []:
                if isinstance(entry, dict) and entry.get("ticker"):
                    skipped.append({
                        "scenario_id": sid,
                        "ticker": str(entry.get("ticker")),
                        "reason": "allocation_scale_zero",
                    })
            continue
        for entry in phase1.get("buy") or []:
            if not isinstance(entry, dict):
                continue
            ticker = str(entry.get("ticker") or "")
            if not ticker:
                continue

            def _skip(reason: str):
                skipped.append({"scenario_id": sid, "ticker": ticker, "reason": reason})

            if ticker in restricted:
                _skip("insider_restricted"); continue
            if ticker in existing_tickers:
                _skip("already_in_priority_actions"); continue
            if ticker in recent_buy_tickers:
                _skip(f"buy 実行/発注済みが直近{_PLAYBOOK_REPROPOSE_DAYS}日以内に存在"); continue
            if ticker.endswith(".T"):
                if not isinstance(jp_pct, (int, float)) or not isinstance(jp_target, (int, float)):
                    _skip("JP動的目標データ不足"); continue
                if jp_pct >= jp_target:
                    _skip(f"jp_equity_ex_employer {jp_pct:.1f}% >= 目標 {jp_target:.0f}%"); continue

            if entry.get("allocation_jpy") is not None:
                amt_jpy = float(entry.get("allocation_jpy") or 0) * scale
            elif entry.get("allocation_usd") is not None:
                amt_jpy = float(entry.get("allocation_usd") or 0) * fx_rate * scale
            else:
                _skip("allocation 未定義"); continue
            if amt_jpy <= 0:
                _skip("allocation 0 以下"); continue
            if used_jpy + amt_jpy > total_cap_jpy:
                _skip(f"注入合計上限 {_PLAYBOOK_INJECT_TOTAL_CAP_PCT*100:.0f}% 超過"); continue

            used_after_jpy = used_jpy + amt_jpy

            action = {
                "type": "buy",
                "ticker": ticker,
                "tier": "Long" if ticker.endswith(".T") else "Swing",
                "urgency": "medium",
                "confidence_pct": 70 if sc.get("status") == "active" else 62,
                "amount_hint": f"¥{int(round(amt_jpy)):,}",
                "action": (
                    f"楽天証券で {ticker} を ¥{int(round(amt_jpy)):,} 目安に買付"
                    f"（シナリオ: {sc.get('name', sid)} {sc.get('status')}）"
                ),
                "reason": (
                    f"[scenario_playbook:{sid}] {sc.get('name', sid)} "
                    f"{str(sc.get('status')).upper()} readiness{sc.get('readiness_pct')}%"
                    f"{' × ' + str(scale) if scale < 1.0 else ''}: "
                    f"{str(entry.get('reason') or '')[:120]}"
                ),
                "source": "scenario_playbook",
                "scenario_id": sid,
                "playbook_injected": True,
                "allocation_scale": scale,
                "playbook_gate": {
                    "version": 1,
                    "attested": True,
                    "scenario_status": str(sc.get("status") or ""),
                    "entry_cap_jpy": round(amt_jpy),
                    "run_cap_jpy": round(total_cap_jpy),
                    "run_used_after_jpy": round(used_after_jpy),
                    "jp_target_check_applicable": ticker.endswith(".T"),
                    "jp_target_check_passed": (
                        bool(jp_pct < jp_target) if ticker.endswith(".T") else True
                    ),
                },
            }
            synthesis["priority_actions"].append(action)
            existing_tickers.add(ticker)
            injected.append({k: action[k] for k in
                             ("ticker", "type", "amount_hint", "scenario_id", "allocation_scale")})
            used_jpy = used_after_jpy

    return {"injected": injected, "skipped": skipped,
            "used_jpy": round(used_jpy), "cap_jpy": round(total_cap_jpy)}


# ── メインエントリー ─────────────────────────────────────

def run_analysis(force: bool = False) -> dict:
    """
    フル AI 分析を実行してキャッシュに保存。
    force=False の場合、有効なキャッシュがあればそれを返す。
    """
    try:
        from utils import load_environment_secrets
        load_environment_secrets()
    except Exception as _secret_e:
        print(f"  ⚠️ secrets 読み込みスキップ: {_secret_e}")

    if not force and is_cache_valid():
        print("✅ キャッシュが有効です（スキップ）")
        write_progress(8, 8, "✅ 分析完了（キャッシュ利用）", "有効な既存分析結果を読み込みました")
        return get_cached()

    try:
        if _ensure_macro_event_state_fresh():
            print("  🔄 macro_event_state.json 再生成 (CPI/FOMC/雇用統計)")
    except Exception as _me:
        # The readiness gate treats a missing/stale calendar as review.  Do not
        # silently interpret refresh failure as "no important events".
        print(f"  ⚠️ macro event calendar 更新失敗: {_me}")

    try:
        if _ensure_technical_state_fresh():
            print("  🔄 technical_state.json 再生成 (分析前の鮮度保証)")
    except Exception as _te:
        print(f"  ⚠️ technical_state 鮮度保証スキップ: {_te}")

    try:
        if _ensure_news_candidates_fresh():
            print("  🔄 news_signal_candidates.json 再生成 (分析前の鮮度保証)")
    except Exception as _ne:
        print(f"  ⚠️ news_signal_candidates 鮮度保証スキップ: {_ne}")

    try:
        from vix_tracker import get_vix_context as _get_vix_context
        _vix_refresh = _get_vix_context()
        if _vix_refresh.get("source") == "stale_cache":
            print("  ⚠️ vix_state 更新失敗: stale cache（requiredシグナルはfail-closed）")
        else:
            print(f"  🔄 vix_state確認: source={_vix_refresh.get('source', 'unknown')}")
    except Exception as _ve:
        print(f"  ⚠️ vix_state 更新失敗: {_ve}")

    # Phase 0: action_state 自動クリーンアップ（再提案ループ防止）
    # - pending: 30営業日超 → expired
    # - placed:  10営業日超 unfilled → expired
    # - ordered execution: 自動取消せず stale warning のみ
    # 6762.T 逆指値¥2,286 のような「placed のまま filled されず AI が再提案し続ける」
    # 病的ループを根本から断つ。
    try:
        from action_state_tracker import auto_cleanup as _auto_cleanup
        _cleanup = _auto_cleanup(pending_max_days=30, placed_max_days=10, ordered_max_days=10)
        if _cleanup.get("total_expired", 0) > 0:
            print(
                f"  🧹 自動クリーンアップ: "
                f"pending {_cleanup['expired_pending']}件 / "
                f"placed {_cleanup['expired_placed']}件 / "
                f"ordered {_cleanup.get('expired_ordered',0)}件 → expired/cancelled"
            )
    except Exception as _ce:
        print(f"  ⚠️ action_state クリーンアップ失敗: {_ce}")

    # Phase 1A: 過去推奨の事後検証（yfinance一括取得、APIコストなし）
    _accuracy_context = ""
    try:
        from recommendation_verifier import verify_recommendations, format_accuracy_context as _fmt_acc
        _acc_stats = verify_recommendations()
        _accuracy_context = _fmt_acc(_acc_stats)
        if _accuracy_context:
            print(f"  📊 推奨精度コンテキスト注入: {_acc_stats.get('total_verified', 0)}件検証済み")
    except Exception as _ve:
        print(f"  ⚠️ 推奨検証スキップ: {_ve}")

    # Phase 1B: DCA ラダーの当日評価を分析前に1回だけ実行する。
    # Codex re-review #5: 実 LaunchAgent は 06:00 だが DCA cron は 07:25/17:05 のため、
    # 朝分析は前日17:05の stale な bottom_fishing_signals.json を読んでしまう。
    # freshness gate は stale 注入を防ぐが「当日候補の供給」はできない。
    # ここで当日未評価なら dry_run 評価して当日候補を生成し、cron への依存を断つ。
    try:
        import drawdown_dca_engine as _dca_eng
        _dca_file = BASE_DIR / "bottom_fishing_signals.json"
        _today_iso = datetime.now().strftime("%Y-%m-%d")
        _need_eval = True
        if _dca_file.exists():
            try:
                _sig = json.loads(_dca_file.read_text(encoding="utf-8"))
                _fresh = _sig.get("freshness_date") or str(_sig.get("evaluated_at") or "")[:10]
                _need_eval = (_fresh != _today_iso)
            except Exception:
                _need_eval = True
        if _need_eval:
            _bd = _dca_eng._estimate_cash_breakdown()
            _sig = _dca_eng.generate_ladder_signals(
                cash_jpy=_bd.get("total_jpy"), dry_run=True, cash_breakdown=_bd
            )
            _dca_eng.persist(_sig)
            print(f"  🩸 DCA 当日評価実行: active_tranche={_sig.get('active_tranche')} (cron非依存)")
    except Exception as _dce:
        print(f"  ⚠️ DCA 当日評価スキップ: {_dce}")

    # Phase 1B-2: 実行計画をAI分析直前に再生成する。
    # 06:00 LaunchAgent / Web refresh / 手動 --force のどの入口でも、
    # gather_data() が古い execution_plan_state.json を読まないようにする。
    _plan_refresh = _refresh_execution_plan_state()
    if _plan_refresh.get("ok"):
        print(
            "  🧭 execution_plan_state.json 再生成: "
            f"items={_plan_refresh.get('items')} "
            f"remaining_normal=¥{int(_plan_refresh.get('remaining_normal_jpy') or 0):,} "
            f"opportunity=¥{int(_plan_refresh.get('remaining_opportunity_jpy') or 0):,}"
        )
    elif _plan_refresh.get("skipped"):
        print(f"  ⚠️ execution_plan refresh スキップ: {_plan_refresh.get('reason')}")
    else:
        print(f"  ⚠️ execution_plan refresh 失敗: {_plan_refresh.get('error')}")

    # Phase 1C: scenario_state.json の鮮度保証(deterministic refresh、stale防止)。
    try:
        if _ensure_scenario_state_fresh(force=True):
            print("  🔄 scenario_state.json 再生成 (分析時点の市場スナップショットを固定)")
    except Exception as _se:
        print(f"  ⚠️ scenario_state 鮮度保証スキップ: {_se}")

    write_progress(0, 8, "📊 データ収集開始", "ポートフォリオ・シグナル・レジーム情報を読み込み中")
    print("📊 データ収集中…")
    data = gather_data()

    # ガード状態を注入（ガードレール違反時にSonnet/Opusに伝達）
    _guard_path = BASE_DIR / "guard_state.json"
    if _guard_path.exists():
        try:
            data["guard_state"] = json.loads(_guard_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # データ品質チェック: 重要フィールドの欠損を警告
    _missing_keys = [k for k in ("market_meta", "regime", "positions", "scenario") if not data.get(k)]
    if _missing_keys:
        print(f"⚠️ gather_data() 欠損フィールド: {_missing_keys} — 分析精度が低下する可能性があります")
    if not data.get("market_meta", {}).get("vix"):
        print("⚠️ VIXデータ未取得 — レジーム判定・VIXスケーリングが機能しません")
    if not data.get("positions"):
        print("⚠️ ポジションデータ未取得 — 分析を中断します")

    age = data.get("signals_age_hours")
    if age and age > 24:
        print(f"⚠️  シグナルが {age:.0f}時間前 のデータです（analyzer.py を実行してください）")

    # Medium 層ドリフト計算
    try:
        from rebalance_engine import calculate_medium_drift
        import portfolio_manager as pm
        snap = pm.build_portfolio_snapshot()
        data["rebalance_medium"] = calculate_medium_drift(snap)
    except Exception:
        data["rebalance_medium"] = {}

    shared_ctx = _build_shared_market_context(data)

    # FinCon Verbal Reinforcement: 過去の投資信念をShared Contextに注入
    beliefs = _load_beliefs()
    if beliefs:
        beliefs_ctx = _format_beliefs_context(beliefs)
        shared_ctx = shared_ctx + "\n\n" + beliefs_ctx
        print(f"  🧠 投資信念 {len(beliefs)}件をコンテキストに注入")

    _TIER_WORKERS = max(1, min(4, _env_int("ALMANAC_TIER_MAX_WORKERS", 2)))
    _TIER_CALL_TIMEOUT = _tier_llm_timeout_seconds()
    write_progress(5, 8, f"🤖 ティア分析中（並列{_TIER_WORKERS}本）",
                   "Long/Medium/ShortはSonnet、信用買い/空売りは設定モデル（現行DeepSeek V4 Pro）で一次判断")
    print(f"🤖 Sonnet×3 + book-aware×2 分析開始 (max_workers={_TIER_WORKERS}, llm_timeout={_TIER_CALL_TIMEOUT:.0f}s)…")

    # Phase 2B: レジーム合意フラグを計算（BL VIXスケーリング強化用）
    _regime_d   = data.get("regime", {})
    _vix_val_ra = float(data.get("market_meta", {}).get("vix") or 20.0)
    _regime_bull_confirmed = sum([
        "強気" in _regime_d.get("regime", ""),
        (float(_regime_d.get("macro_score") or 5)) >= 6,
        _vix_val_ra < 20,
        bool(_regime_d.get("spy_above", True)),
    ]) >= 3

    # Phase 1: 一次判断を並列実行（Red Team は後段で起動）
    # Red Team を一次判断出力（high_return_*）参照可能にするため、明示的に直列化。
    # レイテンシ +10〜20s 増えるが、Red Team の銘柄レベル洞察品質が向上する。
    # max_workers を絞るため、キュー待ちも含めた全体 timeout は
    # 1 wave あたりの LLM timeout × wave 数 + バッファで計算する。
    _TIER_COUNT = 5
    _TIER_WAVES = (_TIER_COUNT + _TIER_WORKERS - 1) // _TIER_WORKERS
    _TIER_TIMEOUT = _env_float(
        "ALMANAC_TIER_ANALYSIS_TIMEOUT_SECONDS",
        _TIER_CALL_TIMEOUT * _TIER_WAVES + 30.0,
    )

    def _safe_result(future, name: str) -> dict:
        try:
            return future.result(timeout=_TIER_TIMEOUT)
        except Exception as _e:
            print(f"  ⚠️ {name} タイムアウト/エラー ({type(_e).__name__}): {_e}")
            return {"error": str(_e), "health": "caution", "summary": "タイムアウト",
                    "priority_actions": [], "hold_notes": []}

    _executor = ThreadPoolExecutor(max_workers=_TIER_WORKERS)
    try:
        futures = {
            "Long分析":       _executor.submit(_self_consistent_long,     data, shared_ctx),
            "Medium分析":     _executor.submit(_analyze_medium,           data, shared_ctx),
            "Swing分析":      _executor.submit(_analyze_short_positions,  data, shared_ctx),
            "MarginLong分析": _executor.submit(_analyze_margin_long,      data, shared_ctx),
            "ShortSell分析":  _executor.submit(_analyze_short_selling,    data, shared_ctx),
        }
        from concurrent.futures import wait as _cf_wait
        done, not_done = _cf_wait(futures.values(), timeout=_TIER_TIMEOUT)

        def _collect_tier_result(name: str) -> dict:
            fut = futures[name]
            if fut not in done:
                print(f"  ⚠️ {name} タイムアウト: {_TIER_TIMEOUT}s 超過")
                return {
                    "error": f"{name} timeout after {_TIER_TIMEOUT}s",
                    "health": "caution",
                    "summary": "タイムアウト",
                    "priority_actions": [],
                    "hold_notes": [],
                }
            return _safe_result(fut, name)

        long_analysis            = _collect_tier_result("Long分析")
        medium_analysis          = _collect_tier_result("Medium分析")
        short_positions_analysis = _collect_tier_result("Swing分析")
        margin_long_analysis     = _collect_tier_result("MarginLong分析")
        short_selling_analysis   = _collect_tier_result("ShortSell分析")

        for fut in not_done:
            fut.cancel()
    finally:
        try:
            _executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            _executor.shutdown(wait=False)

    _degraded_info = _build_degraded_mode_info({
        "Long分析": long_analysis,
        "Medium分析": medium_analysis,
        "Swing分析": short_positions_analysis,
        "MarginLong分析": margin_long_analysis,
        "ShortSell分析": short_selling_analysis,
    })
    _degraded_context = _format_degraded_mode_context(_degraded_info)
    if _degraded_info.get("enabled"):
        print(f"  ⚠️ DEGRADED MODE: {_degraded_info.get('reason')}")

    # Phase 1.5: Sonnet ハイリターン候補を抽出して tier_hints を組み立て
    _tier_hints = {
        "long":   (long_analysis.get("high_return_opportunity")
                   if isinstance(long_analysis, dict) else None),
        "medium": (medium_analysis.get("medium_high_return_strategy")
                   if isinstance(medium_analysis, dict) else None),
        "short":  (short_positions_analysis.get("high_risk_high_return")
                   if isinstance(short_positions_analysis, dict) else None),
    }
    print(
        f"  🎯 tier_hints: long={'✓' if _tier_hints['long'] else '×'} "
        f"medium={'✓' if _tier_hints['medium'] else '×'} "
        f"short={'✓' if _tier_hints['short'] else '×'}"
    )

    # Phase 2: Red Team を tier_hints 付きで起動
    redteam_analysis = _analyze_redteam_multi(
        data, shared_ctx, beliefs, tier_hints=_tier_hints
    )

    # DeepSeek系スキーマをhealth/priority_actionsに正規化（Opus一貫性のため）
    if isinstance(margin_long_analysis, dict):
        margin_long_analysis.setdefault("priority_actions", [])
        margin_long_analysis.setdefault("health", "good")
    if isinstance(short_selling_analysis, dict):
        short_selling_analysis.setdefault("health", short_selling_analysis.get("margin_health", "safe"))
        short_selling_analysis.setdefault("priority_actions", short_selling_analysis.get("margin_actions", []))

    # ── クーリング期間強制フィルタ（コードによる後処理）──────────────
    # 「実際に買いが執行された」銘柄に限定してクーリングを適用する。
    # holding_days ベースのフィルタはポジションインポート・手動入力・entry_date 更新で
    # 誤発動するため廃止。action_executions.json の status=ordered/executed/filled/done を参照。
    _COOLING_DAYS = 14
    _COOLING_LOSS_THRESHOLD = -0.10
    _COOLING_BLOCK_TYPES = {"trim", "sell", "stop_loss"}
    _pos_map = {p.get("ticker"): p for p in data.get("positions", [])}

    # 直近 _COOLING_DAYS 日に買い執行記録のある銘柄セットを構築
    _recently_bought: set[str] = {
        e["ticker"]
        for e in _load_recent_executions(days=_COOLING_DAYS)
        if (e.get("direction") or "").lower() in {"buy", "add", "dca", "margin_buy"}
        and e.get("ticker")
    }

    def _apply_cooling_filter(analysis: dict) -> None:
        if not isinstance(analysis, dict):
            return
        actions = analysis.get("priority_actions", [])
        if not isinstance(actions, list):
            return
        kept, moved = [], []
        for a in actions:
            ticker = a.get("ticker", "")
            atype  = str(a.get("type", "")).lower()
            pos    = _pos_map.get(ticker, {})
            hdays  = pos.get("holding_days")   # 表示用（フィルタ条件には使わない）
            upct   = pos.get("unrealized_pct") or 0.0
            if (atype in _COOLING_BLOCK_TYPES
                    and ticker in _recently_bought
                    and upct > _COOLING_LOSS_THRESHOLD):
                days_txt = f"保有{hdays}日・" if hdays is not None else ""
                moved.append(
                    f"{ticker}（{days_txt}直近{_COOLING_DAYS}日以内に買い執行済み・"
                    f"クーリング期間中のため{atype}を保留。含み損-10%超で除外対象外）"
                )
                print(f"  🛡️ クーリングフィルタ: {ticker} {atype} を除去（買い執行済み・{days_txt}クーリング中）")
            else:
                kept.append(a)
        analysis["priority_actions"] = kept
        if moved:
            notes = analysis.setdefault("hold_notes", [])
            if isinstance(notes, list):
                notes.extend(moved)

    for _tier_analysis in [long_analysis, medium_analysis, short_positions_analysis]:
        _apply_cooling_filter(_tier_analysis)
    # ────────────────────────────────────────────────────────────────

    # ── セーフティネット: 一次判断がティア別フィールドを欠落させた時の自動補完 ──
    # Bug 経験: 共有 tool schema の制約で margin_long_picks / short_opportunities /
    # margin_actions がしばしば欠落。重要な信用買い・空売り機会の埋没を防ぐ。
    try:
        # (1) margin_long_analysis.margin_long_picks の補完
        ml_cands = data.get("screening", {}).get("margin_long_candidates", []) or []
        ml_blocked = bool(data.get("screening", {}).get("margin_long_blocked", False))
        if isinstance(margin_long_analysis, dict):
            mlp = margin_long_analysis.get("margin_long_picks") or []
            if (not mlp) and ml_cands and not ml_blocked:
                top3 = sorted(ml_cands, key=lambda c: -(c.get("score") or 0))[:3]
                auto_picks = []
                for c in top3:
                    sc = c.get("score") or 0
                    urg = "high" if sc >= 120 else ("medium" if sc >= 100 else "low")
                    auto_picks.append({
                        "ticker": c.get("ticker"),
                        "strategy": c.get("strategy"),
                        "reason": (
                            f"score={sc} RSI{c.get('rsi','?')} "
                            f"MA50比{c.get('ma50_dev','?')}% 出来高{c.get('volume_ratio','?')}倍"
                            f" | 自動補完（DeepSeek信用買いスキップ検知）"
                        ),
                        "stop_loss_pct": c.get("stop_loss_pct"),
                        "urgency": urg,
                        "_auto_filled": True,
                    })
                margin_long_analysis["margin_long_picks"] = auto_picks
                print(f"  🛡️ margin_long_picks を自動補完: {[p['ticker'] for p in auto_picks]}")

        # (2) short_selling_analysis の空売り機会・margin_actions の補完
        ss_cands = data.get("screening", {}).get("short_candidates", []) or []
        if isinstance(short_selling_analysis, dict):
            so = short_selling_analysis.get("short_opportunities") or []
            if (not so) and ss_cands:
                _ma = data.get("margin", {}) if isinstance(data.get("margin"), dict) else {}
                _mr = _ma.get("maintenance_ratio")
                _blocked_margin = isinstance(_mr, (int, float)) and _mr < 130
                auto_so = []
                auto_ma = []
                for c in ss_cands[:5]:
                    tk = c.get("ticker", "?")
                    rsi = c.get("rsi")
                    dev = c.get("pct_from_ma50")
                    price = c.get("price")
                    tier = c.get("tier", "?")
                    strength = c.get("strength", "?")
                    reason_txt = c.get("reason", "")
                    urg = "high" if strength in ("strong", "very_strong") else ("medium" if strength == "medium" else "low")
                    auto_so.append({
                        "ticker": tk,
                        "urgency": urg,
                        "entry_zone": f"${price:.2f}圏" if isinstance(price, (int, float)) else "現値近辺",
                        "stop_loss":  "直近高値+3%",
                        "target_price": "目標はRSI50回帰 or MA50復帰",
                        "risk_reward": "1:2目安（要検証）",
                        "catalyst":   f"{tier}/{strength} | {reason_txt}",
                        "reason":     f"RSI={rsi} MA50比{dev}% | 自動補完（DeepSeek空売りスキップ検知）",
                        "confidence_pct": 50,
                        "return_20d_rank": "middle",
                        "_auto_filled": True,
                    })
                    if _blocked_margin:
                        auto_ma.append({
                            "urgency": "high",
                            "action":  f"{tk}空売り新規建てを保留（維持率{_mr:.1f}%で130%未満）",
                            "reason":  "新規空売り禁止ラインを下回る。維持率改善まで監視のみ。",
                        })
                short_selling_analysis["short_opportunities"] = auto_so
                if auto_ma:
                    existing_ma = short_selling_analysis.get("margin_actions") or []
                    short_selling_analysis["margin_actions"] = existing_ma + auto_ma
                print(f"  🛡️ short_opportunities を自動補完: {[s['ticker'] for s in auto_so]}")
    except Exception as _e:
        print(f"  ⚠️ 自動補完エラー（スキップ）: {_e}")
    # ────────────────────────────────────────────────────────────────

    print("  ✅ Sonnet×3 + book-aware×2 + Red Team 分析完了")

    # Phase 1B: エージェント間不一致スコアリング
    _disagreement_context = _compute_disagreement(
        long_analysis, medium_analysis, short_positions_analysis,
        margin_long_analysis, short_selling_analysis)
    if _disagreement_context:
        print("  🔍 エージェント間不一致スコア生成完了")

    # Phase 1C: データ鮮度スコアリング
    _data_freshness_context = _compute_data_freshness()

    # 整理 #5 + P0-2: TWR / benchmark / excess α を AI に注入 (objective.md +200bps 受入れ基準)。
    # 汚染NAV(clean_since前)・短い窓(min_clean_days未満)・benchmark stale・cash_flow台帳未整備の
    # いずれかなら excess α を数値で出さず「データ不足」縮退し、stance/alpha hurdle の根拠に使わせない。
    _twr_context = ""
    try:
        from nav_recorder import modified_dietz_twr as _twr
        from config_clean_baseline import (
            clamp_date_from as _clamp,
            clean_nav_since_iso as _cs_iso,
            min_clean_days as _mcd,
        )
        from datetime import datetime as _dt, timedelta as _td
        _today = _dt.now().date().isoformat()
        _cs = _cs_iso()
        _min_days = _mcd()
        _90d_ago  = _clamp((_dt.now().date() - _td(days=90)).isoformat())
        _ytd_from = _clamp(f"{_dt.now().year}-01-01")

        # cash_flow 台帳の健全性（積立を controlled-out できるか）。未整備なら excess α を出さない。
        _cf_ok = False
        _cf_reason = "cash_flow_status_unavailable"
        try:
            from nav_recorder import cash_flow_ledger_status as _cfs
            _st = _cfs(date_from=_cs, date_to=_today)
            _cf_ok = bool(_st.get("ok"))
            _cf_reason = _st.get("reason") or _cf_reason
        except Exception:
            pass

        _r90  = _twr(date_from=_90d_ago, date_to=_today, clean_since=_cs, min_clean_days=_min_days)
        _rYTD = _twr(date_from=_ytd_from, date_to=_today, clean_since=_cs, min_clean_days=_min_days)
        _lines = [
            "## 📈 Time-Weighted Return vs Benchmark (objective.md 受入れ基準: +200bps)",
            f"※ 信頼できる NAV 起点 = {_cs}。これ以前(バグ修正前)の汚染データは測定から除外。",
        ]
        _any_confirmed_excess = False
        for _label, _r in (("直近90日", _r90), ("年初来", _rYTD)):
            if _r.get("error"):
                _lines.append(f"- {_label}: ⚠️ データ不足 — {_r['error']}")
                continue
            _twr_pct   = _r.get("twr_pct")
            _bench     = _r.get("benchmark_twr_pct")
            _excess    = _r.get("excess_return_pct")
            _confirmed = bool(_r.get("confirmed"))
            _win = f"({_r.get('v_start_date')}→{_r.get('v_end_date')}, 実{_r.get('period_days_actual')}日)"
            if _excess is not None and _confirmed and _cf_ok:
                _any_confirmed_excess = True
                _lines.append(
                    f"- {_label} {_win}: portfolio TWR {_twr_pct:+.2f}% / benchmark {_bench:+.2f}% / "
                    f"**excess α {_excess:+.2f}%**"
                )
            else:
                _why = _r.get("excess_suppressed_reason") or (f"cash_flow:{_cf_reason}" if not _cf_ok else "unconfirmed")
                _twr_disp = f"{_twr_pct:+.2f}%" if _twr_pct is not None else "n/a"
                _lines.append(
                    f"- {_label} {_win}: TWR {_twr_disp}（**excess α は未確定 → 判断根拠に使わない**: {_why}）"
                )
        # 行動指針: confirmed な excess がある時のみ alpha hurdle 強化を指示
        if _any_confirmed_excess:
            _lines.append(
                "→ 直近 excess α が負なら「期待 alpha < 50bps の候補は採用しない」を強める。"
                " 12ヶ月で +200bps 未達なら新規 alpha 提案より hold_notes の精度向上を優先すること。"
            )
        else:
            _lines.append(
                "→ クリーン履歴/cash_flow台帳が不十分なため excess α は未確定。"
                " **excess α・CVaR を stance 格下げや採用基準の根拠にしてはならない**（VIX/regime/DD の実値で判断すること）。"
            )
        _twr_context = "\n".join(_lines)
    except Exception as _e:
        print(f"  ⚠️ TWR context skip: {_e}")

    # BLビュー抽出: Sonnetティア分析から銘柄別リターン予測を生成してbl_views.jsonに保存
    try:
        _vix_val = float(data.get("market_meta", {}).get("vix") or 20.0)
        _extract_bl_views(long_analysis, medium_analysis, short_positions_analysis, vix=_vix_val, regime_bull=_regime_bull_confirmed)
    except Exception as _e:
        print(f"  ⚠️ BLビュー抽出エラー（スキップ）: {_e}")

    # Phase 1D: DeepSeek-R1 Judge（一次判断クロスバリデーション）
    _judge_context = ""
    try:
        print("  ⚖️ DeepSeek-R1 Judge 実行中…")
        _judge_context = _judge_sonnet_outputs(
            long_analysis, medium_analysis,
            short_positions_analysis, margin_long_analysis,
            short_selling_analysis,
            redteam_analysis)
    except Exception as _e:
        print(f"  ⚠️ Judge エラー（スキップ）: {_e}")

    # P2-2: Extended Thinking は tool_choice=force と併用不可のため synthesis では無効
    #       (thinking_fired=False)。実態に合わせて表記を修正。
    write_progress(7, 8, "🏆 最終戦略合成中 (Opus 合成)",
                   "5判断（Sonnet×3 + book-aware×2）・Webニュース・生ポジションデータ・過去履歴をOpusが統合して最終判断")
    print("🏆 最終戦略合成中 (Opus 合成)…")

    # ── Opus 合成に screening 候補を直接可視化 ──
    # Sonnet が WATCH-BULLISH 候補を hold_notes に退避させてしまうバグ対策として、
    # Opus に screen_candidates / margin_long_candidates / short_candidates を生データで渡す。
    _screen_lines: list = []
    _screen_sources = data.get("screening", {}).get("screen_sources", []) or []
    if _screen_sources:
        _screen_lines.append("## 📊 スクリーニング入力の鮮度")
        for src in _screen_sources[:5]:
            if not isinstance(src, dict):
                continue
            _screen_lines.append(
                f"- {src.get('source')}: {src.get('timestamp') or 'timestampなし'} "
                f"candidates={src.get('candidate_count', '?')} / screened={src.get('total_screened', '?')} "
                f"included={src.get('included', '?')}"
            )
    _sc = data.get("screen_candidates", []) or []
    _watch_bullish = []
    _buy_signals = []
    for c in _sc:
        if not isinstance(c, dict):
            continue
        sig = str(c.get("ai_signal", "")).upper()
        conf = c.get("ai_confidence") or 0
        score = float(c.get("score") or 0)
        if sig == "BUY" and conf >= 60:
            _buy_signals.append(c)
        elif sig == "WATCH" and _screen_candidate_has_bullish_support(c) and score >= 25:
            _watch_bullish.append(c)
    if _buy_signals or _watch_bullish:
        _screen_lines.append("## 📊 短期スクリーニング候補（生データ・Swing採用検討）")
        if _buy_signals:
            _screen_lines.append("### BUYシグナル（強気レジーム下は type=\"buy\" tier=\"Swing\" で採用）")
            for c in _buy_signals[:5]:
                _screen_lines.append(
                    f"- {c.get('ticker')}: {c.get('strategy','?')} conf{c.get('ai_confidence')}% "
                    f"score={c.get('score')} RSI{c.get('rsi','?')} 1m{c.get('mom_1m','?')}%"
                )
        if _watch_bullish:
            _screen_lines.append("### WATCH+強気BULLISH（score≥25・条件付エントリー候補）")
            _screen_lines.append("→ 強気レジーム時は試験エントリー候補として検討する（採用基準は期待 alpha ≥ 50bps）。件数ノルマ・最低 1 件採用の強制はしない。")
            for c in _watch_bullish[:5]:
                db = c.get("ai_debate") or {}
                bull_reason = (db.get("bull") or "")[:80] if isinstance(db.get("bull"), str) else ""
                _screen_lines.append(
                    f"- {c.get('ticker')}: {c.get('strategy','?')} score={c.get('score')} "
                    f"RSI{c.get('rsi','?')} 1m{c.get('mom_1m','?')}% | bull: {bull_reason}"
                )
    _jp_sc = data.get("screening", {}).get("jp_screen_candidates", []) or [
        c for c in _sc if isinstance(c, dict) and str(c.get("ticker") or "").endswith(".T")
    ]
    if _jp_sc:
        _screen_lines.append("")
        _screen_lines.append("### 日本株の短期スクリーニング候補（forced buyではなく比較対象）")
        _screen_lines.append("→ score/出来高/過熱度を米国株候補と同じ土俵で比較し、期待 alpha ≥ 50bps の場合のみ採用。")
        _jp_ranked = sorted(_jp_sc, key=lambda x: -(x.get("score") or 0))
        for c in _jp_ranked[:6]:
            db = c.get("ai_debate") or {}
            bull_reason = (db.get("bull") or db.get("bull_view") or "")[:80] if isinstance(db, dict) else ""
            _screen_lines.append(
                f"- {c.get('ticker')}: {c.get('strategy','?')} signal={c.get('ai_signal','?')} "
                f"conf{c.get('ai_confidence','?')}% score={c.get('score','?')} "
                f"RSI{c.get('rsi','?')} 1m{c.get('mom_1m','?')}% source={c.get('screen_source','?')} "
                f"| bull: {bull_reason}"
            )
    # 信用買い候補
    _ml = data.get("screening", {}).get("margin_long_candidates", []) or []
    _ml_blocked = bool(data.get("screening", {}).get("margin_long_blocked", False))
    if _ml and not _ml_blocked:
        _screen_lines.append("")
        _screen_lines.append("## 📊 信用買い候補（margin_long_analysis 採用検討）")
        _screen_lines.append("→ 信用買い一次判断を踏まえ、margin_health が safe かつ stance が neutral 以上なら type=\"margin_buy\" または現金優先の type=\"buy\" tier=\"Medium\" で採用可能。")
        _ml_ranked = sorted(_ml, key=lambda x: -(x.get("score") or 0))
        for c in _ml_ranked[:5]:
            _screen_lines.append(
                f"- {c.get('ticker')}: {c.get('strategy','?')} score={c.get('score')} "
                f"RSI{c.get('rsi','?')} MA50比{c.get('pct_from_ma50', c.get('ma50_dev','?'))}% "
                f"出来高{c.get('vol_ratio', c.get('volume_ratio','?'))}倍"
            )
        _jp_ml = [c for c in _ml_ranked if str(c.get("ticker") or "").endswith(".T")]
        if _jp_ml:
            _screen_lines.append("### 日本株の信用買い候補（forced buyではなく比較対象）")
            for c in _jp_ml[:5]:
                _screen_lines.append(
                    f"- {c.get('ticker')}: {c.get('strategy','?')} score={c.get('score')} "
                    f"composite={c.get('composite_score','?')} RSI{c.get('rsi','?')} "
                    f"MA50比{c.get('pct_from_ma50', c.get('ma50_dev','?'))}%"
                )
    # 空売り候補
    _ss = data.get("screening", {}).get("short_candidates", []) or []
    if _ss:
        _screen_lines.append("")
        _screen_lines.append("## 📊 空売り候補（Short_Selling short_opportunities 採用検討）")
        for c in _ss[:5]:
            _screen_lines.append(
                f"- {c.get('ticker')}: tier={c.get('tier')} strength={c.get('strength')} "
                f"RSI{c.get('rsi','?')} MA50比{c.get('pct_from_ma50','?')}% | {(c.get('reason') or '')[:60]}"
            )
    _screening_context = "\n".join(_screen_lines) if _screen_lines else ""
    _ipo_watch_context = _fmt_ipo_watch_context(data.get("ipo_watch"))

    # ── DCA ラダー context 構築（bottom_fishing_signals.json から読み込み）──
    _dca_context = ""
    try:
        _dca_file = BASE_DIR / "bottom_fishing_signals.json" if 'BASE_DIR' in globals() else None
        if _dca_file is None:
            from pathlib import Path as _P
            _dca_file = _P(__file__).parent.parent / "bottom_fishing_signals.json"
        if _dca_file.exists():
            _dca_sig = json.loads(_dca_file.read_text(encoding="utf-8"))
            _active = _dca_sig.get("active_tranche")
            # F5: freshness gate — 当日評価でなければ古い active tranche を
            # プロンプトへ注入しない。stale な発火候補を「今日の推奨」として
            # 採用させないため。freshness_date が無い旧 snapshot は evaluated_at の
            # 日付で代替判定する。
            _dca_fresh = _dca_sig.get("freshness_date") or (str(_dca_sig.get("evaluated_at") or "")[:10])
            _today_iso = datetime.now().strftime("%Y-%m-%d")
            _dca_is_fresh = (_dca_fresh == _today_iso)
            if _active and not _dca_is_fresh:
                _dca_context = (
                    "## 🩸 底打ち買い下がりシグナル（DCA ラダー）\n"
                    f"- active_tranche={_active} だが評価日 {_dca_fresh} が当日でないため stale。\n"
                    "→ 当日再評価まで DCA 発火候補を priority_actions に採用しないこと。"
                )
                _active = None  # 以降の注入をスキップ
            if _active:
                _dd = _dca_sig.get("dd", {}) or {}
                _panic = _dca_sig.get("panic", {}) or {}
                _vix_ex = _dca_sig.get("vix_extract", {}) or {}
                _buys = _dca_sig.get("recommended_buys", []) or []
                _reasons = _dca_sig.get("tranche_reasons", []) or []
                _lines = [
                    "## 🩸 底打ち買い下がりシグナル（DCA ラダー）",
                    f"### Active Tranche: {_active}",
                    f"- Portfolio DD (from peak): {_dd.get('dd_from_peak')} (current ¥{_dd.get('current_value_jpy'):,.0f})" if _dd.get('current_value_jpy') else f"- Portfolio DD: {_dd.get('dd_from_peak')}",
                    f"- VIX: {_vix_ex.get('level')} (5d peak decay: {_vix_ex.get('decay_from_peak_5d_pct')}%)",
                    f"- Fear&Greed: {_panic.get('fear_greed')}",
                    f"- Put/Call: {_panic.get('put_call')}",
                    f"- HY OAS: {_panic.get('hy_oas_bps')} bps",
                    f"- 発動根拠: {'; '.join(_reasons)}",
                    "### Recommended buys (tranche 指定):",
                ]
                for _b in _buys:
                    _ccy = _b.get("currency", "")
                    _df = _b.get("deferred_jpy") or 0
                    _warn = f" ⚠️通貨別cash不足(繰延¥{_df:,.0f})" if _df > 0 else ""
                    _lines.append(
                        f"  - {_b.get('ticker')}({_ccy}): 投入¥{_b.get('target_jpy',0):,.0f} "
                        f"/要求¥{_b.get('requested_jpy', _b.get('target_jpy',0)):,.0f} "
                        f"urgency={_b.get('urgency','high')}{_warn}"
                    )
                _lines.append(
                    "→ 上記 Recommended buys は DCA ラダー機構の発火候補。期待 alpha が手数料・税後 50bps 以上 "
                    "かつ Policy Engine (ex-ante VaR / DD stage) で gating されない場合のみ "
                    "type=\"dca\" source=\"dca_ladder\" として priority_actions に採用すること。"
                    "投入額(target_jpy)は通貨別残高で clip 済み。繰延(deferred_jpy)分は FX 振替を"
                    "明示承認しない限り発注しないこと。"
                )
                _lines.append(
                    "→ regime=bear / Red Team v2 が弱気警告を出している場合は、DCA ラダーよりリスク制約を優先する。"
                    "「DCA は弱気警告に勝つ」というルールは廃止。"
                )
                _dca_context = "\n".join(_lines)
    except Exception as _e:
        print(f"  ⚠️ DCA context load skip: {_e}")

    # ── ニュース材料深掘り context (DeepSeek) ──
    _news_topic_context = ""
    try:
        from pathlib import Path as _P
        import sys as _sys
        _root = _P(__file__).parent.parent
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        from news_topic_analyzer import format_for_prompt as _news_fmt   # type: ignore
        _news_topic_context = _news_fmt(max_entries=10) or ""
    except Exception as _e:
        print(f"  ⚠️ news_topic context skip: {_e}")

    # ── SNS 話題銘柄深掘り context (DeepSeek) ──
    _social_topic_context = ""
    try:
        from social_topic_analyzer import format_for_prompt as _social_fmt  # type: ignore
        _social_topic_context = _social_fmt(max_entries=8) or ""
    except Exception as _e:
        print(f"  ⚠️ social_topic context skip: {_e}")

    # ── Part E α 獲得モジュール群 (6 module) ──
    _alpha_blocks: list[str] = []
    _alpha_loaders = [
        ("insider",         "insider_tracker",           "format_for_prompt", {}),
        ("overnight_gap",   "overnight_gap_scanner",     "format_for_prompt", {}),
        ("pair_trade",      "pair_screener",             "format_for_prompt", {}),
        ("squeeze",         "squeeze_detector",          "format_for_prompt", {}),
        ("earnings_hedge",  "earnings_proximity_manager","format_for_prompt", {}),
        ("leveraged_decay", "leveraged_decay_monitor",   "format_for_prompt", {}),
    ]
    for _name, _mod, _fn, _kw in _alpha_loaders:
        try:
            _m = __import__(_mod, fromlist=[_fn])
            _block = getattr(_m, _fn)(**_kw) or ""
            if _block.strip():
                _alpha_blocks.append(_block.strip())
        except Exception as _e:
            print(f"  ⚠️ alpha[{_name}] load skip: {_e}")
    _alpha_context = ("\n\n".join(_alpha_blocks)).strip()
    if _alpha_context:
        print(f"  ✓ alpha context: {len(_alpha_blocks)} module(s) active ({sum(len(b) for b in _alpha_blocks)} chars)")

    # risk_parity / vol_target 参考値を synthesis へ（portfolio_optimizer 出力）
    try:
        _opt_path = _root / "portfolio_optimization.json"
        if _opt_path.exists():
            import json as _json
            _opt = _json.loads(_opt_path.read_text(encoding="utf-8"))
            _rp = _opt.get("risk_parity") or {}
            _vt = _opt.get("vol_target") or {}
            if _rp.get("clamped_weights") or _vt.get("scale") is not None:
                _rp_lines = ["## ⚖️ Risk Parity / Vol Target 参考重み", ""]
                if _rp.get("clamped_weights"):
                    cw = _rp["clamped_weights"]
                    _rp_lines.append(f"- Tier Weight (inv-vol): Long {cw.get('long',0)*100:.1f}% / Medium {cw.get('medium',0)*100:.1f}% / Swing {cw.get('swing',0)*100:.1f}%")
                if _vt.get("scale") is not None:
                    _rp_lines.append(f"- Vol Target Scale: ×{_vt.get('scale',1.0):.3f} (regime={_vt.get('regime','normal')}, predicted_vol={_vt.get('predicted_vol',0):.3f})")
                _rp_lines.append("")
                _rp_lines.append("→ vol_scaled_weights を priority_actions の amount_hint に反映。"
                                 "risk_parity clamped_weights は tier 配分目標として使用。")
                _alpha_context = (_alpha_context + "\n\n" + "\n".join(_rp_lines)).strip() if _alpha_context else "\n".join(_rp_lines)
    except Exception as _e:
        print(f"  ⚠️ optimizer context skip: {_e}")

    try:
        synthesis = _synthesize(
            long_analysis, medium_analysis,
            short_positions_analysis, margin_long_analysis, short_selling_analysis,
            data["portfolio_total"], data["scenario"], data["risk"],
            data["market_meta"], data["news"], data.get("earnings", {}),
            data.get("backtest_summary", []),
            data.get("cash_info", {}),
            data.get("pending_orders", []),
            positions_raw=data.get("positions", []),
            portfolio_integrity=data.get("portfolio_integrity"),
            tax_context=data.get("tax_context"),
            espp_context=data.get("espp_context"),
            scenario_monitoring=data.get("scenario_monitoring"),
            disagreement_context=_disagreement_context,
            data_freshness_context=_data_freshness_context,
            accuracy_context=_accuracy_context,
            judge_context=_judge_context,
            redteam_context=f"## ⚔️ Red Team攻撃的仮説（要評決）\n{json.dumps(redteam_analysis, ensure_ascii=False)}\n→ 全件を red_team_verdict に採否記載すること。全件 reject も valid な結論（最低 1 件採用などのノルマは無し）。期待 alpha が手数料・税後 50bps 以上の adopt/partial のみを priority_actions に変換すること。" if redteam_analysis.get("attacks") else "",
            screening_context=_screening_context,
            dca_context=_dca_context,
            ipo_watch_context=_ipo_watch_context,
            news_topic_context=_news_topic_context,
            social_topic_context=_social_topic_context,
            alpha_context=_alpha_context,
            twr_context=_twr_context,
            degraded_context=_degraded_context,
            currency_breakdown_whole=data.get("currency_breakdown_whole"),
            currency_breakdown_long=data.get("currency_breakdown_long"),
            current_currency_policy=data.get("current_currency_policy"),
        )
        if _is_synthesis_failure(synthesis):
            raise RuntimeError(synthesis.get("error") or "final synthesis failed")
    except Exception as _synth_err:
        import traceback as _tb
        _trace = _tb.format_exc()
        print(f"⛔ synthesis 例外 ({type(_synth_err).__name__}): {_synth_err} — キャッシュ保存を中止")
        print(f"  📋 traceback:\n{_trace}")
        # 永続化: synthesis_error_log.txt に時系列で蓄積（後から原因調査できるよう）
        try:
            from pathlib import Path as _P
            _log = _P(__file__).parent.parent / "synthesis_error_log.txt"
            with _log.open("a", encoding="utf-8") as _f:
                _f.write(f"\n=== {datetime.now().isoformat()} ===\n{_trace}\n")
        except Exception:
            pass
        _err_txt = str(_synth_err)
        _is_transient_api_error = (
            "529" in _err_txt
            or "Overloaded" in _err_txt
            or "500" in _err_txt
            or "Internal server error" in _err_txt
            or "api_error" in _err_txt
        )
        if _is_transient_api_error:
            _override_env = "ALMANAC_MODEL_OVERRIDE_FINAL_SYNTHESIS"
            _old_override = os.environ.get(_override_env)
            try:
                _last_fallback_err: Exception | None = None
                for _fallback_model_key in ("sonnet", "haiku"):
                    try:
                        print(
                            "  ↪️ Opus transient API error のため final_synthesis を "
                            f"{_fallback_model_key} に一時降格して再試行"
                        )
                        os.environ[_override_env] = _fallback_model_key
                        synthesis = _synthesize(
                            long_analysis, medium_analysis,
                            short_positions_analysis, margin_long_analysis, short_selling_analysis,
                            data["portfolio_total"], data["scenario"], data["risk"],
                            data["market_meta"], data["news"], data.get("earnings", {}),
                            data.get("backtest_summary", []),
                            data.get("cash_info", {}),
                            data.get("pending_orders", []),
                            positions_raw=data.get("positions", []),
                            portfolio_integrity=data.get("portfolio_integrity"),
                            tax_context=data.get("tax_context"),
                            espp_context=data.get("espp_context"),
                            scenario_monitoring=data.get("scenario_monitoring"),
                            disagreement_context=_disagreement_context,
                            data_freshness_context=_data_freshness_context,
                            accuracy_context=_accuracy_context,
                            judge_context=_judge_context,
                            redteam_context=f"## ⚔️ Red Team攻撃的仮説（要評決）\n{json.dumps(redteam_analysis, ensure_ascii=False)}\n→ 全件を red_team_verdict に採否記載すること。全件 reject も valid な結論（最低 1 件採用などのノルマは無し）。期待 alpha が手数料・税後 50bps 以上の adopt/partial のみを priority_actions に変換すること。" if redteam_analysis.get("attacks") else "",
                            screening_context=_screening_context,
                            dca_context=_dca_context,
                            ipo_watch_context=_ipo_watch_context,
                            news_topic_context=_news_topic_context,
                            social_topic_context=_social_topic_context,
                            alpha_context=_alpha_context,
                            twr_context=_twr_context,
                            degraded_context=_degraded_context,
                            currency_breakdown_whole=data.get("currency_breakdown_whole"),
                            currency_breakdown_long=data.get("currency_breakdown_long"),
                            current_currency_policy=data.get("current_currency_policy"),
                        )
                        if _is_synthesis_failure(synthesis):
                            raise RuntimeError(synthesis.get("error") or "fallback synthesis failed")
                        synthesis.setdefault(
                            "synthesis_fallback",
                            f"{_fallback_model_key}_after_opus_transient_api_error",
                        )
                        print(f"  ✅ {_fallback_model_key} fallback synthesis 成功")
                        break
                    except Exception as _fallback_err:
                        _last_fallback_err = _fallback_err
                        print(f"  ⚠️ {_fallback_model_key} fallback synthesis failed: {_fallback_err}")
                else:
                    write_progress(
                        8, 8,
                        "❌ 分析失敗（最終合成）",
                        f"final synthesis failed; cache not updated: {_last_fallback_err}",
                    )
                    raise RuntimeError(
                        f"final synthesis failed; cache not updated: {_last_fallback_err}"
                    ) from _last_fallback_err
            finally:
                if _old_override is None:
                    os.environ.pop(_override_env, None)
                else:
                    os.environ[_override_env] = _old_override
        else:
            write_progress(
                8, 8,
                "❌ 分析失敗（最終合成）",
                f"final synthesis failed; cache not updated: {_synth_err}",
            )
            raise RuntimeError(
                f"final synthesis failed; cache not updated: {_synth_err}"
            ) from _synth_err

    if isinstance(synthesis, dict):
        _apply_degraded_mode(synthesis, _degraded_info)
        synthesis["currency_basis_context"] = {
            "whole_portfolio": data.get("currency_breakdown_whole") or data.get("currency_breakdown", {}),
            "long_tier": data.get("currency_breakdown_long", {}),
            "target_basis": "long_tier",
            "note": "whole_portfolioは表示用、currency target/rebalance判定はlong_tier専用",
        }
        geo_state = data.get("geopolitical_state") or data.get("geopolitical") or {}
        _ctx_blocks = synthesis.get("context_blocks") if isinstance(synthesis.get("context_blocks"), dict) else {}
        synthesis["context_blocks"] = {
            **_ctx_blocks,
            "ipo_watch": bool(_ipo_watch_context.strip()),
            "news_topic": bool(_news_topic_context.strip()),
            "social_topic": bool(_social_topic_context.strip()),
            "geopolitical": bool(geo_state),
            "catalyst": bool(_ctx_blocks.get("catalyst")),
            "alpha_modules": len(_alpha_blocks),
        }
        _ensure_information_lane_verdicts(synthesis)

        # 2026-07 AI動的外貨比率: AI の currency_target_recommendation を currency_policy で
        # 検証し、valid なら state/log に保存する (次回 rebalance が採用)。自動発注はしない。
        # 無効/期限切れ/自信不足/basis不一致は不採用 (log には残るが state 更新せず)。
        # 急変クランプの基準は「現在有効な目標」なので resolve で baseline を取得する。
        try:
            import currency_policy
            from rebalance_engine import CURRENCY_TARGETS as _STATIC_CCY
            _ccy_rec = synthesis.get("currency_target_recommendation")
            if isinstance(_ccy_rec, dict) and _ccy_rec:
                _baseline_targets, _ = currency_policy.resolve_effective_targets(static=_STATIC_CCY)
                _ccy_res = currency_policy.ingest(_ccy_rec, current_targets=_baseline_targets)
                # 監査用に採否を synthesis へ反映 (UI/履歴で参照可能・自動適用はしない)。
                synthesis["currency_policy_verdict"] = {
                    "actionable": _ccy_res["actionable"],
                    "verdict": _ccy_res["verdict"],
                    "reason": _ccy_res["reason"],
                    "clamped": _ccy_res["clamped"],
                    "basis": "long_tier",
                }
                print(f"  💱 currency policy: {_ccy_res['verdict']} (actionable={_ccy_res['actionable']})")
        except Exception as _ccy_e:
            print(f"  ⚠️ currency policy ingest 失敗: {_ccy_e}")

    # Fix G (2026-04-20): synthesis に market_meta_snapshot を強制パススルー。
    # Opus が埋め忘れた場合のセーフティネット＋履歴比較 (XAI) 用に必ず格納する。
    # Fix 7B (2026-04-24): nightly_recheck.py の差分判定用に spy/qqq/usdjpy も含める。
    if isinstance(synthesis, dict):
        _mm = data.get("market_meta", {}) or {}
        _y10 = _mm.get("us10y_yield")
        _y10_val = _y10.get("value") if isinstance(_y10, dict) else _y10

        # SPY/QQQ/USDJPY のライブ取得（差分比較の基礎値として永続化）
        _spy_price = _mm.get("spy_price")
        _qqq_price = _mm.get("qqq_price")
        _usdjpy    = _mm.get("usdjpy")
        if _spy_price is None or _qqq_price is None or _usdjpy is None:
            try:
                import yfinance as _yf  # type: ignore
                for _sym, _key in (("SPY", "_spy_price"), ("QQQ", "_qqq_price"), ("JPY=X", "_usdjpy")):
                    try:
                        _hist = _yf.Ticker(_sym).history(period="1d")
                        if not _hist.empty:
                            _val = float(_hist["Close"].iloc[-1])
                            if _key == "_spy_price" and _spy_price is None:
                                _spy_price = _val
                            elif _key == "_qqq_price" and _qqq_price is None:
                                _qqq_price = _val
                            elif _key == "_usdjpy" and _usdjpy is None:
                                _usdjpy = _val
                    except Exception:
                        pass
            except Exception:
                pass

        _snapshot = {
            "vix":                _mm.get("vix"),
            "vix_level":          _mm.get("vix_level"),
            "us10y_yield":        _y10_val,
            "yield_curve_status": _mm.get("yield_curve_status"),
            "spy_price":          _spy_price,
            "qqq_price":          _qqq_price,
            "usdjpy":             _usdjpy,
        }
        # 既に Opus が埋めていた場合はマージ（Opus の値を優先）
        existing = synthesis.get("market_meta_snapshot") or {}
        if isinstance(existing, dict):
            for k, v in _snapshot.items():
                existing.setdefault(k, v)
            synthesis["market_meta_snapshot"] = existing
        else:
            synthesis["market_meta_snapshot"] = _snapshot

        # Option B-3: leverage_health snapshot をパススルー（UI / 履歴で参照可能に）
        try:
            from behavioral_guard import evaluate_leverage_health as _elh_pt
            _lh_pt = _elh_pt(
                portfolio_total_jpy=float(data.get("portfolio_total") or 0),
                vix=_mm.get("vix"),
            )
            synthesis["leverage_health"]    = _lh_pt
            synthesis["current_leverage"]   = _lh_pt.get("current_leverage")
        except Exception as _le:
            print(f"  ⚠️ leverage_health snapshot 失敗: {_le}")

    # ── hold をアクションから分離（セーフティネット）──
    # P0-7: margin_buy をプロンプトで使う設計なので VALID にも含める（leverage_health gate は line 3098- が担当）
    VALID_ACTION_TYPES = {"buy", "sell", "rebalance", "trim", "reduce", "dca", "stop_loss", "take_profit", "short", "cover", "add", "margin_buy"}
    if isinstance(synthesis, dict) and "priority_actions" in synthesis:
        actions = synthesis["priority_actions"]
        if isinstance(actions, list):
            hold_items = [a for a in actions if str(a.get("type", "")).lower() not in VALID_ACTION_TYPES]
            real_actions = [a for a in actions if str(a.get("type", "")).lower() in VALID_ACTION_TYPES]
            synthesis["priority_actions"] = real_actions
            # hold_notes に統合
            existing_holds = synthesis.get("hold_notes", [])
            if isinstance(existing_holds, list):
                for h in hold_items:
                    existing_holds.append(f"{h.get('ticker','')}: {h.get('action','')}")
                synthesis["hold_notes"] = existing_holds

    # action_stage_log: ランごとの識別子を生成
    _asl_analysis_id = None
    _asl_scenario_key = (data.get("scenario") or {}).get("key", "") if isinstance(data, dict) else ""
    _asl_regime = ((data.get("regime") or {}).get("regime", "") if isinstance(data, dict) else "")
    _asl_dd_stage = ((data.get("risk") or {}).get("actual_dd_stage", "") if isinstance(data, dict) else "")
    _asl_leverage = ((synthesis.get("leverage_health") or {}).get("status", "") if isinstance(synthesis, dict) else "")
    _asl_as_of = datetime.now().isoformat()
    try:
        _asl_fx = float(((data.get("cash_info") or {}).get("fx_rate_usdjpy")) or ((data.get("market_meta") or {}).get("usdjpy")) or 150.0)
    except Exception:
        _asl_fx = 150.0
    _asl_price_map: dict = {}
    try:
        for _pos in (data.get("positions") or []):
            if not isinstance(_pos, dict):
                continue
            _tk = _pos.get("ticker")
            if _tk:
                _asl_price_map[_tk] = {
                    "current_price": _pos.get("current_price") or _pos.get("price"),
                    "currency": _pos.get("currency"),
                    "shares": _pos.get("shares") or 0,
                    "value_jpy": _pos.get("value_jpy") or 0,
                }
    except Exception:
        _asl_price_map = {}

    def _asl_with_estimates(actions: list[dict]) -> list[dict]:
        out = []
        for _a in actions or []:
            if not isinstance(_a, dict):
                continue
            _row = dict(_a)
            if _row.get("estimated_notional_jpy") is None:
                try:
                    _est = _estimate_action_jpy(_row, _asl_price_map, _asl_fx)
                    if _est >= 0 and _est != float("inf"):
                        _row["estimated_notional_jpy"] = round(_est)
                except Exception:
                    pass
            out.append(_row)
        return out
    try:
        # F7: action_stage_log と既存 observability ログで同一 run を join できるよう、
        # ID は almanac.observability.ids.new_analysis_id() に一本化する。
        # (action_stage_log.new_analysis_id は後方互換 fallback として残す)
        from action_stage_log import log_opus_raw as _asl_opus
        try:
            from almanac.observability.ids import new_analysis_id as _asl_new_id
        except Exception:
            from action_stage_log import new_analysis_id as _asl_new_id
        _asl_analysis_id = _asl_new_id()
        synthesis["analysis_id"] = _asl_analysis_id
        for _action in synthesis.get("priority_actions") or []:
            if isinstance(_action, dict):
                _action["analysis_id"] = _asl_analysis_id
    except Exception:
        pass

    # F3: tier_generated ステージを記録（各一次ティアが生成したアクション）。
    # 同一 analysis_id で opus_raw / policy / final と join できる。
    if _asl_analysis_id:
        try:
            from action_stage_log import log_tier_generated as _asl_tier
            # Codex re-review #4: margin_long_picks / short_opportunities は type を
            # 持たないため、ログ前に方向 (margin_buy / short) を付与する。
            # そのままだと tier_generated が unknown/neutral になり方向集計が壊れる。
            _tier_map = {
                "Long":       (long_analysis, "priority_actions", None),
                "Medium":     (medium_analysis, "priority_actions", None),
                "Swing":      (short_positions_analysis, "priority_actions", None),
                "MarginLong": (margin_long_analysis, "margin_long_picks", "margin_buy"),
                "ShortSell":  (short_selling_analysis, "short_opportunities", "short"),
            }
            for _tname, (_tres, _akey, _default_type) in _tier_map.items():
                if isinstance(_tres, dict):
                    _tacts = _tres.get(_akey) or _tres.get("priority_actions") or []
                    if isinstance(_tacts, list) and _tacts:
                        # type 欠落の候補に default_type を補完（コピーで非破壊）
                        _enriched = []
                        for _a in _tacts:
                            if not isinstance(_a, dict):
                                continue
                            if _default_type and not _a.get("type"):
                                _a = {**_a, "type": _default_type}
                            _enriched.append(_a)
                        _asl_tier(
                            analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                            tier_name=_tname, actions=_asl_with_estimates(_enriched),
                            scenario_key=_asl_scenario_key, regime=_asl_regime,
                            actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
                        )
        except Exception:
            pass

    if isinstance(synthesis, dict) and isinstance(synthesis.get("priority_actions"), list):
        # Audit split: keep model/degraded output before deterministic policy gates.
        # priority_actions remains the backward-compatible final field and is
        # overwritten by subsequent policy/post-filter stages.
        synthesis["priority_actions"] = _asl_with_estimates(synthesis.get("priority_actions", []))
        synthesis["raw_priority_actions"] = [dict(a) for a in synthesis.get("priority_actions", [])]
        # action_stage_log: opus_raw ステージを記録
        if _asl_analysis_id:
            try:
                _asl_opus(
                    analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                    actions=synthesis["raw_priority_actions"],
                    scenario_key=_asl_scenario_key, regime=_asl_regime,
                    actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
                )
            except Exception:
                pass

    # ── DCA signals snapshot を synthesis に保存 ──
    # 当日評価かどうかを freshness_date で確認し、古い場合は stale フラグを立てる
    try:
        _dca_snap_path = BASE_DIR / "bottom_fishing_signals.json"
        if _dca_snap_path.exists():
            _dca_snap = json.loads(_dca_snap_path.read_text(encoding="utf-8"))
            _dca_fresh_date = _dca_snap.get("freshness_date") or _dca_snap.get("evaluated_at", "")[:10]
            _today_iso = datetime.now().date().isoformat()
            synthesis["dca_signals"] = {
                "active_tranche":     _dca_snap.get("active_tranche"),
                "recommended_buys":   _dca_snap.get("recommended_buys", []),
                "evaluated_at":       _dca_snap.get("evaluated_at"),
                "freshness_date":     _dca_fresh_date,
                "is_fresh":           (_dca_fresh_date == _today_iso),
            }
    except Exception:
        pass

    # ── Scenario playbook deterministic injection (2026-07-07) ──
    # active/partial かつ enabled_for_decision のシナリオ phase_1 buy を、Opus の emit
    # 裁量に依存せず priority_actions へ注入する。後段の policy gate / post_filter /
    # 単発上限は全て通過させる (安全ゲートのバイパスなし)。
    # 背景: japan_standalone_bull が 3 週間 active でも synthesis が 1489.T/1306.T を
    # 一度も emit せず、シナリオ→発注候補の接続が確率的だった。
    if isinstance(synthesis, dict) and isinstance(synthesis.get("priority_actions"), list):
        try:
            _inj_result = _inject_playbook_actions(synthesis, data)
            if _inj_result.get("injected") or _inj_result.get("skipped"):
                synthesis["playbook_injection"] = _inj_result
            if _inj_result.get("injected"):
                print(f"  🎯 playbook 注入: {len(_inj_result['injected'])}件 "
                      f"({', '.join(a.get('ticker','?') for a in _inj_result['injected'])})")
        except Exception as _inj_e:
            print(f"  ⚠️ playbook 注入 skip (fail-open で AI 案のみ): {_inj_e}")

    # ── P1-17/P1-21: Deterministic Policy Engine gate ──
    # AI の priority_actions を ex-ante 制約 (VaR / DD stage / leverage / earnings / VIX / freshness)
    # で deterministic にフィルタする。プロンプト依頼ではなくコード側で執行する。
    if isinstance(synthesis, dict) and isinstance(synthesis.get("priority_actions"), list):
        try:
            from policy_engine import apply_policy_gate, build_context_from_synthesis_inputs

            # freshness を数値で再計算 (analyst の _compute_data_freshness は string なので別計算)
            _freshness_score = _extract_data_freshness_score(_data_freshness_context or "")

            # earnings blackout のティッカー一覧を抽出。
            # NOTE: 以前は _synthesize() のローカル変数 earnings_blackout_ctx を
            #   `"earnings_blackout_ctx" in dir()` ガード越しに参照していたが、ここは
            #   別関数 run_analysis() のスコープで当該変数が存在せずガードが常に False、
            #   _blackout_tickers が常に [] になり policy engine の決算ゲートへ届かなかった。
            #   blackout 銘柄 set を返す既存ヘルパーを直接呼び、確実に配線する。
            _blackout_tickers = []
            try:
                _blackout_tickers = sorted(_load_earnings_blackout(within_business_days=5))
            except Exception:
                _blackout_tickers = []

            _policy_macro = dict(data.get("market_meta") or {}) if isinstance(data, dict) else {}
            if isinstance(data, dict):
                _policy_macro["scenario_key"] = (data.get("scenario") or {}).get("key")
                _policy_macro["regime"] = (data.get("regime") or {}).get("regime")
                _policy_macro["regime_bull_confirmed"] = bool(_regime_bull_confirmed)

            _policy_risk = dict(data.get("risk") or {}) if isinstance(data, dict) else {}
            _guard_state = data.get("guard_state") if isinstance(data, dict) else None
            if isinstance(_guard_state, dict):
                for _key in ("allow_dca_tranche", "dca_active_tranche", "trading_allowed"):
                    if _key in _guard_state:
                        _policy_risk[_key] = _guard_state.get(_key)

            _pe_ctx = build_context_from_synthesis_inputs(
                risk            = _policy_risk,
                macro           = _policy_macro,
                leverage_health = synthesis.get("leverage_health"),
                freshness_score = _freshness_score,
                earnings_blackout_tickers = _blackout_tickers,
                portfolio_integrity = data.get("portfolio_integrity") if isinstance(data, dict) else None,
            )
            _pe_decision = apply_policy_gate(synthesis["priority_actions"], _pe_ctx)

            # 反映: accepted で priority_actions を置換、rejected / modified を監査用に保存
            synthesis["priority_actions"] = _pe_decision.accepted
            synthesis["post_policy_priority_actions"] = [
                dict(a) for a in _pe_decision.accepted if isinstance(a, dict)
            ]
            synthesis["policy_filtered_actions"] = [
                item for item in (_pe_decision.rejected + _pe_decision.modified)
                if isinstance(item, dict)
            ]
            synthesis["policy_decision"]  = _pe_decision.as_dict()

            # rejected を hold_notes にも記録 (UI から見える形で)
            _existing_holds = synthesis.get("hold_notes", [])
            if isinstance(_existing_holds, list):
                for _rj in _pe_decision.rejected:
                    _act = _rj.get("action", {})
                    _existing_holds.append(
                        f"{_act.get('ticker','')}: policy reject — {_rj.get('reason','')}"
                    )
                synthesis["hold_notes"] = _existing_holds

            print(f"  ✓ Policy Engine: accepted={len(_pe_decision.accepted)} "
                  f"rejected={len(_pe_decision.rejected)} modified={len(_pe_decision.modified)}")
            # action_stage_log: policy_accepted / policy_rejected を記録
            if _asl_analysis_id:
                try:
                    from action_stage_log import log_policy_decision as _asl_policy
                    _asl_policy(
                        analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                        accepted=_pe_decision.accepted,
                        rejected=_pe_decision.rejected,
                        scenario_key=_asl_scenario_key, regime=_asl_regime,
                        actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
                    )
                except Exception:
                    pass
        except Exception as _pe_err:
            # Policy Engine は安全装置なので fail-open しない。
            # 制約評価に失敗した状態で AI 提案を通すと、静かに壊れる資産システムになる。
            _blocked = synthesis.get("priority_actions") or []
            synthesis["priority_actions"] = []
            synthesis["post_policy_priority_actions"] = []
            synthesis["policy_filtered_actions"] = [
                {
                    "rule": "policy_engine_error",
                    "reason": f"Policy Engine gate failed closed: {_pe_err}",
                    "action": a,
                }
                for a in _blocked if isinstance(a, dict)
            ]
            synthesis["policy_decision"] = {
                "accepted": [],
                "rejected": synthesis["policy_filtered_actions"],
                "modified": [],
                "accepted_count": 0,
                "rejected_count": len([a for a in _blocked if isinstance(a, dict)]),
                "modified_count": 0,
                "error": str(_pe_err),
                "failed_closed": True,
            }
            _existing_holds = synthesis.get("hold_notes", [])
            if isinstance(_existing_holds, list):
                _existing_holds.append(f"Policy Engine failure: all priority_actions blocked ({_pe_err})")
                synthesis["hold_notes"] = _existing_holds
            print(f"  🔴 Policy Engine gate failed closed: {_pe_err}")

    # 日本株 .T 銘柄の amount_hint を銘柄別JPX売買単位に自動丸め。
    # ETFまで一律100株にすると1489(1口)・1306(10口)を過大発注してしまう。
    _normalize_jpx_action_units(synthesis)

    # P0-4: stance guard — 汚染メトリクス由来の不当な aggressive 格下げを実データで是正。
    #       _phase1_post_filter の _is_aggressive 判定より前に適用する。
    try:
        _apply_stance_guard(synthesis, data, _regime_bull_confirmed)
    except Exception as _sg_e:
        print(f"  ⚠️ stance guard エラー（スキップ）: {_sg_e}")

    # Phase 1 (2026-04-28): post-filter — 細切れ抑制 / cooldown / earnings blackout / flip 警告
    try:
        _fx = 150.0
        try:
            from utils import get_fx_rate_cached
            _fx, _ = get_fx_rate_cached()
        except Exception:
            pass
        _phase1_post_filter(
            synthesis,
            float(data.get("portfolio_total") or 0),
            fx_rate=_fx,
            positions=data.get("positions"),
            cash_info=data.get("cash_info"),
            execution_plan=data.get("execution_plan"),
        )
    except Exception as _pe:
        _pf_quarantined = _quarantine_post_filter_failure(synthesis, _pe)
        print(f"  ⚠️ Phase1 post-filter エラー → fail-closed で {_pf_quarantined} 件を実行保留に隔離: {_pe}")
    if isinstance(synthesis, dict):
        try:
            from execution_plan_observer import record_observation as _record_plan_observation

            _plan_observation = _record_plan_observation(
                synthesis,
                analysis_id=_asl_analysis_id,
                as_of=_asl_as_of,
            )
            synthesis["execution_plan_observation"] = _plan_observation
            _pf = synthesis.get("post_filter")
            if isinstance(_pf, dict):
                _gate = _pf.get("execution_plan_gate")
                if isinstance(_gate, dict):
                    _gate["readiness"] = _plan_observation.get("readiness")
        except Exception as _plan_obs_e:
            synthesis["execution_plan_observation"] = {
                "recorded": False,
                "error": str(_plan_obs_e),
            }
            print(f"  ⚠️ execution plan observe 記録エラー（分析は継続）: {_plan_obs_e}")
    try:
        _augment_no_jp_buy_rationale(synthesis, data)
    except Exception as _jp_e:
        print(f"  ⚠️ JP no-buy rationale エラー（スキップ）: {_jp_e}")
    try:
        _augment_no_margin_short_rationale(
            synthesis,
            data,
            margin_long_analysis=margin_long_analysis,
            short_selling_analysis=short_selling_analysis,
        )
    except Exception as _ms_e:
        print(f"  ⚠️ margin/short no-action rationale エラー（スキップ）: {_ms_e}")

    if isinstance(synthesis, dict):
        _annotate_us_holiday_actions(synthesis)

    if isinstance(synthesis, dict) and isinstance(synthesis.get("priority_actions"), list):
        synthesis["final_priority_actions"] = [
            dict(a) for a in synthesis.get("priority_actions", []) if isinstance(a, dict)
        ]
        try:
            # F7: action_stage_log と同じ analysis_id を渡して両ログを join 可能にする。
            synthesis["observability_write"] = _write_runtime_observability_logs(
                synthesis, data, analysis_id=_asl_analysis_id
            )
        except Exception as _obs_e:
            synthesis["observability_write"] = {"written": 0, "errors": [str(_obs_e)]}
            print(f"  ⚠️ observability log write failed: {_obs_e}")
    if isinstance(synthesis, dict):
        try:
            from brief_disclosures import yesterday_disclosure_signals
            synthesis["disclosure_brief"] = {
                "label": "未検証・観測のみ",
                "observe_only": True,
                "items": yesterday_disclosure_signals(limit=5),
            }
        except Exception as _brief_e:
            synthesis["disclosure_brief"] = {
                "label": "未検証・観測のみ",
                "observe_only": True,
                "items": [],
                "error": str(_brief_e),
            }
        try:
            blocks = synthesis.setdefault("context_blocks", {})
            if isinstance(blocks, dict):
                blocks["disclosure"] = bool((synthesis.get("disclosure_brief") or {}).get("items"))
            _annotate_jp_disclosure_observe_only_boundary(synthesis)
            _ensure_information_lane_verdicts(synthesis)
            audit = synthesis.get("decision_boundary_audit")
            if isinstance(audit, dict) and isinstance(synthesis.get("context_blocks"), dict):
                audit["context_blocks_present"] = {
                    key: bool(value)
                    for key, value in synthesis["context_blocks"].items()
                    if key in {"ipo_watch", "news_topic", "social_topic", "geopolitical", "disclosure", "catalyst"}
                }
        except Exception:
            pass

    result = {
        "as_of":             datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scenario_key":      data["scenario"].get("key", "NEUTRAL"),
        "portfolio_total":   data["portfolio_total"],
        "currency_breakdown": data.get("currency_breakdown", {}),
        "currency_breakdowns": {
            "whole_portfolio": data.get("currency_breakdown_whole") or data.get("currency_breakdown", {}),
            "long_tier": data.get("currency_breakdown_long", {}),
        },
        "signals_age_hours": data.get("signals_age_hours"),
        "long_analysis":               long_analysis,
        "medium_analysis":             medium_analysis,
        "short_positions_analysis":    short_positions_analysis,
        "margin_long_analysis":        margin_long_analysis,
        "short_selling_analysis":      short_selling_analysis,
        "synthesis":                   synthesis,
        "redteam":                     redteam_analysis,
    }

    save_cache(result)

    # AI推奨を事後検証ログに記録
    _log_recommendations(synthesis, data["market_meta"])

    # RedTeam攻撃案への採否(adopt/partial/reject)を事後検証ログに記録
    _log_red_team_verdicts(synthesis)

    # action_stage_log: 最終 priority_actions を post_filter_final として記録
    if _asl_analysis_id:
        try:
            from action_stage_log import (
                log_post_filter_final as _asl_final,
                log_post_filter_deferred as _asl_deferred,
                log_post_filter_rejected as _asl_rejected,
            )
            _asl_final(
                analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                actions=synthesis.get("priority_actions", []),
                scenario_key=_asl_scenario_key, regime=_asl_regime,
                actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
            )
            _asl_rejected(
                analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                actions=synthesis.get("_filtered_actions", []),
                scenario_key=_asl_scenario_key, regime=_asl_regime,
                actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
            )
            _asl_deferred(
                analysis_id=_asl_analysis_id, as_of=_asl_as_of,
                actions=synthesis.get("order_intent_deferred_actions", []),
                scenario_key=_asl_scenario_key, regime=_asl_regime,
                actual_dd_stage=_asl_dd_stage, leverage_status=_asl_leverage,
            )
        except Exception:
            pass

    # FinCon信念更新: Opus合成結果から投資信念をエピソード的に更新
    try:
        _update_beliefs(synthesis)
    except Exception as _e:
        print(f"  ⚠️ 投資信念更新エラー（スキップ）: {_e}")

    write_progress(8, 8, "✅ 分析完了", f"{CACHE_PATH.name} に保存しました")
    print(f"✅ 分析完了 → {CACHE_PATH}")
    return result


_INDICATOR_WORDS = r'(?:VIX|RSI|MACD|VaR|β|beta|score|スコア|信頼度|確率|勝率|win_rate|EV|GARCH|ATR|σ|シャープ|sharpe|総資産)'

_SANITIZE_PATTERNS = [
    # 指標ワード＋数値・金額（ラベルごと丸ごと削除）。
    # 例: 'VIX=20.3', 'VaR ¥500,000', '信頼度 85%', '総資産¥10,000,000'
    re.compile(rf'{_INDICATOR_WORDS}\s*[=:＝]?\s*[¥$]?\s*[+\-]?\d[\d,，.]*\s*[万千億]?\s*[円%]?', re.IGNORECASE),
    # MA 乖離・MA50比など
    re.compile(r'MA\d+\s*(?:比|乖離|から)\s*[+\-]?\d+(?:\.\d+)?%'),
    # 円金額（カンマ区切り、¥/円どちらでも）— 単独で残った金額の掃除
    re.compile(r'¥\s*\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?'),
    re.compile(r'\d{1,3}(?:[,，]\d{3})+\s*円'),
    re.compile(r'\b\d+(?:\.\d+)?\s*[万千億]円'),
    # ドル金額
    re.compile(r'\$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?'),
]
# 連続する記号・空白・句読点を1つに縮約するための後処理
_SANITIZE_CLEANUP = re.compile(r'\s*(?:[\(（]\s*[\)）])|\s+(?=[、。\.,])|\s{2,}')


def _sanitize_for_telegram(s: str) -> str:
    """
    Telegram 本文から計算式・指標値・金額を除去して結論文だけにする。

    残すもの: ティッカー、定性的な日本語結論、利確/損切り目標などの相対％
      （'+5%利確' 'RCL +5%' は数値が結論の一部なので残す。後段のクリーンアップで
        前後の不自然な記号だけ除去する）
    消すもの: VIX/RSI/score/VaR の指標、円・ドル金額、信頼度・確率の数値統計
    """
    if not s:
        return ""
    out = s
    for pat in _SANITIZE_PATTERNS:
        out = pat.sub("", out)
    # 残った不自然な空括弧 () （） や連続空白を整える
    out = re.sub(r'[\(（]\s*[\)）]', '', out)
    out = re.sub(r'\s*[／/、,]\s*[／/、,]+', '、', out)
    out = re.sub(r'\s{2,}', ' ', out)
    out = re.sub(r'^\s*[、。,\.\-/／]+\s*', '', out)
    return out.strip(" 　/／、,\n")


def _format_order_price(value, ticker: str = "") -> str:
    """Telegram 用に注文価格を短く表示する。"""
    if value is None or value == "":
        return ""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return str(value)
    if ticker.endswith(".T"):
        return f"¥{price:,.0f}"
    if abs(price) >= 100:
        return f"${price:,.2f}"
    return f"${price:.2f}"


def _format_order_instruction(action: dict) -> str:
    """priority_action の執行方式を Telegram に載せるための1行に整形する。"""
    if not isinstance(action, dict):
        return ""
    if action.get("no_trade_zone"):
        reason = str(action.get("skip_reason") or "推定コストが期待値を上回るため見送り")
        return _trim_plain(f"発注見送り: {reason}", 140)

    ticker = str(action.get("ticker") or "")
    order_type = str(action.get("order_type") or "").strip().lower()
    limit_price = action.get("limit_price")
    band = action.get("limit_price_band")
    expiry = action.get("expiry_minutes")
    decision_price = action.get("decision_price")

    if not any([order_type, limit_price is not None, band, expiry, decision_price is not None]):
        return ""

    labels = {
        "market": "成行",
        "limit": "指値",
        "stop": "逆指値",
        "stop_limit": "逆指値",
    }
    parts = [labels.get(order_type, order_type or "注文方式未指定")]

    if order_type in {"limit", "stop", "stop_limit"} or limit_price is not None or band:
        if isinstance(band, dict) and (band.get("low") is not None or band.get("high") is not None):
            low = _format_order_price(band.get("low"), ticker)
            high = _format_order_price(band.get("high"), ticker)
            if low and high:
                parts.append(f"{low}〜{high}")
            elif low or high:
                parts.append(low or high)
        elif limit_price is not None:
            parts.append(_format_order_price(limit_price, ticker))

    if expiry not in (None, ""):
        try:
            parts.append(f"有効{int(expiry)}分")
        except (TypeError, ValueError):
            parts.append(f"有効{expiry}")

    if decision_price not in (None, ""):
        price = _format_order_price(decision_price, ticker)
        if price:
            parts.append(f"判断値{price}")

    return " / ".join(p for p in parts if p)


def _trim_plain(s: str, limit: int) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for ch in ("。", "．", ". ", "、", " "):
        idx = cut.rfind(ch)
        if idx > limit // 4:
            return cut[:idx + (1 if ch in ("。", "．") else 0)] + "…"
    return cut + "…"


def send_to_telegram(result: dict) -> bool:
    """
    合成結果を Telegram に分割送信（ヘッダー + 1 アクション 1 メッセージ）。
    Fix 4+5 (2026-04-24): 旧実装は全内容を 1 メッセージに詰めて 4096 文字で切れていた。
    actions[:3] → actions[:15] に拡大し、Web UI 側（slice(0,20)）と件数基準を揃える。
    2026-05-03: 本文から計算式・指標値・金額を sanitizer で除去（結論のみ送信）。
    """
    import html as _tg_html
    import time as _tg_time
    from alert import send_telegram as _send

    def _trim(s: str, limit: int) -> str:
        s = s or ""
        if len(s) <= limit:
            return s
        cut = s[:limit]
        for ch in ("。", "．", ". ", "、", " "):
            idx = cut.rfind(ch)
            if idx > limit // 4:
                return cut[:idx + (1 if ch in ("。", "．") else 0)] + "…"
        return cut + "…"

    def _clean(s: str, limit: int) -> str:
        return _tg_html.escape(_trim(_sanitize_for_telegram(s or ""), limit), quote=False)

    def _safe_plain(s: object, limit: int) -> str:
        return _tg_html.escape(_trim(str(s or ""), limit), quote=False)

    def _send_checked(text: str, label: str) -> None:
        # ``None`` remains accepted for simple test doubles and legacy wrappers;
        # the production transport returns an explicit bool.
        if _send(text) is False:
            raise RuntimeError(f"Telegram {label} returned False")

    try:
        synthesis = result.get("synthesis", {}) or {}
        msg       = synthesis.get("telegram_message", "") or ""
        if synthesis.get("telegram_message_scope") != "ready_only":
            # Old caches and raw LLM output may mention candidates removed by
            # post-filter.  Never replay that text as an execution brief.
            stance = str(synthesis.get("overall_stance") or "neutral")
            rationale = str(synthesis.get("no_action_rationale") or "")
            msg = f"📊 stance={stance}"
            if rationale:
                msg += f"\n{rationale}"
        theme     = synthesis.get("weekly_theme", "") or ""
        raw_actions = synthesis.get("priority_actions", []) or []
        actions   = [
            a for a in raw_actions
            if isinstance(a, dict)
            and a.get("non_executable") is not True
            and a.get("execution_readiness") == "ready"
        ]
        review_count = sum(
            1 for a in raw_actions
            if isinstance(a, dict) and a.get("execution_readiness") != "ready"
        )

        # ── ヘッダー（1 メッセージ目）── stance + telegram_message + theme ──
        header  = f"🧠 <b>AI Portfolio Brief</b> — {_safe_plain(result.get('as_of', ''), 40)}\n\n"
        if not actions:
            header += "🚫 実行アクション: 0件\n"
        else:
            header += f"✅ 実行可能アクション: {len(actions)}件\n"
        if review_count:
            header += f"⚠️ 要確認・停止候補: {review_count}件（個別発注通知は送信しません）\n"
        header += f"{_clean(msg, 400)}\n"
        if theme:
            header += f"\n📌 {_clean(theme, 120)}"
        _send_checked(header[:3900], "header")
        _tg_time.sleep(0.4)

        from brief_disclosures import format_brief_section, yesterday_disclosure_signals
        disclosure_section = format_brief_section(yesterday_disclosure_signals(limit=5))
        if disclosure_section:
            _send_checked(_safe_plain(disclosure_section, 3900), "disclosure brief")
            _tg_time.sleep(0.4)

        # ── アクション 1 件 1 メッセージ（最大 15 件）──
        urgency_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        sent_actions = 0
        for i, a in enumerate(actions[:15], 1):
            if not isinstance(a, dict):
                continue
            icon   = urgency_icon.get(str(a.get("urgency", "medium")).lower(), "🟡")
            ticker = a.get("ticker") or ""
            body   = a.get("action") or ""
            reason = a.get("reason") or ""
            hint   = a.get("amount_hint") or a.get("price_target") or ""
            tier   = a.get("tier") or ""
            order_line = _format_order_instruction(a)
            execution_reason = str(a.get("execution_reason") or "").strip()

            # 本文冒頭にティッカーが重複している場合は剥がす
            if ticker and isinstance(body, str) and body.upper().startswith(str(ticker).upper()):
                body = body[len(ticker):].lstrip(" ・:：")

            tier_suffix = f" [{_safe_plain(tier, 40)}]" if tier else ""
            text  = f"{icon} <b>#{i} {_safe_plain(ticker, 40)}</b>{tier_suffix}\n"
            text += f"{_clean(body, 220)}\n"
            if hint:
                text += f"💰 {_clean(str(hint), 80)}\n"
            if order_line:
                text += f"📋 注文: {_safe_plain(order_line, 240)}\n"
            if execution_reason:
                text += f"⚙️ {_safe_plain(_trim_plain(execution_reason, 160), 180)}\n"
            text += f"📝 {_clean(reason, 500)}"
            _send_checked(text[:3900], f"action #{i} {ticker}")
            sent_actions += 1
            # Telegram Bot API は 30 msg/sec。0.5 秒は安全マージン。
            _tg_time.sleep(0.5)

        print(f"✅ Telegram送信完了: ヘッダー + アクション {sent_actions} 件")
        return True
    except Exception as e:
        print(f"❌ Telegram送信エラー: {e}")
        return False
