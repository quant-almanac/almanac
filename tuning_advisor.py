"""
チューニングパラメータの AI 推奨値生成モジュール

現在の市場状況・ポートフォリオ状況・直近 30 日の post-filter 統計を
Claude（デフォルト Sonnet、role="tuning_advisor" で model_router 解決）に投入し、
各 tunable パラメータの推奨値・根拠を JSON で取得する。

外部 API（POST /api/tuning/ai-recommend）から呼ばれる。
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import tunable_params as tp

BASE_DIR = Path(__file__).parent


def _recent_post_filter_stats(days: int = 30) -> dict:
    """直近 action_stage_log から post-filter の除外/保留理由を集計する。"""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    counts = {
        "recent_too_small_count": 0,
        "recent_cooldown_count": 0,
        "recent_blackout_count": 0,
        "recent_already_executed_count": 0,
        "recent_tax_loss_conflict_count": 0,
        "recent_deferred_count": 0,
    }
    try:
        from action_stage_log import read_entries
        entries = read_entries(
            since_iso=since,
            stages=["post_filter_rejected", "post_filter_deferred"],
        )
    except Exception:
        return counts

    for entry in entries:
        stage = entry.get("stage")
        if stage == "post_filter_deferred":
            counts["recent_deferred_count"] += 1
        rule = str(entry.get("filter_rule") or entry.get("order_intent_decision") or "").lower()
        reason = str(entry.get("filtered_reason") or "").lower()
        blob = f"{rule} {reason}"
        if "too_small" in blob:
            counts["recent_too_small_count"] += 1
        if "cooldown" in blob:
            counts["recent_cooldown_count"] += 1
        if "earnings_blackout" in blob:
            counts["recent_blackout_count"] += 1
        if "already_executed" in blob:
            counts["recent_already_executed_count"] += 1
        if "tax_loss_harvest_conflict" in blob:
            counts["recent_tax_loss_conflict_count"] += 1
    return counts


def _load_market_context() -> dict:
    """現在の市場状況を集める（VIX / レジーム / マクロ / DD / portfolio 構成）"""
    ctx = {
        "as_of": datetime.now().isoformat(timespec="minutes"),
    }

    # レジーム
    try:
        regime_path = BASE_DIR / "regime_state.json"
        if regime_path.exists():
            r = json.loads(regime_path.read_text())
            ctx["regime"] = r.get("regime", "?")
            ctx["macro_score"] = r.get("macro_score")
            ctx["spy_above_ma50"] = r.get("spy_above")
            ctx["nikkei_above_ma50"] = r.get("nk_above")
    except Exception:
        pass

    # VIX (実構造: vix_state.json["vix"]["level"])
    try:
        vix_path = BASE_DIR / "vix_state.json"
        if vix_path.exists():
            v = json.loads(vix_path.read_text())
            vix_blob = v.get("vix") if isinstance(v.get("vix"), dict) else None
            if vix_blob:
                lv = vix_blob.get("level")
                if isinstance(lv, (int, float)):
                    ctx["vix"] = float(lv)
                ctx["vix_classification"] = vix_blob.get("classification")
                ctx["vix_change_5d"] = vix_blob.get("change_5d")
            else:
                # フォールバック: トップレベルにある場合
                for k in ("level", "value"):
                    val = v.get(k)
                    if isinstance(val, (int, float)):
                        ctx["vix"] = float(val)
                        break
    except Exception:
        pass

    # マクロ（FRED キャッシュ）
    try:
        macro_path = BASE_DIR / "macro_state.json"
        if macro_path.exists():
            m = json.loads(macro_path.read_text())
            ctx["fed_rate"] = m.get("fed_rate", {}).get("value")
            ctx["us10y"] = m.get("us10y_yield", {}).get("value")
            ctx["cpi_yoy"] = m.get("cpi_yoy", {}).get("value")
            ctx["unemployment"] = m.get("unemployment", {}).get("value")
    except Exception:
        pass

    # ポートフォリオ
    try:
        from portfolio_manager import build_portfolio_snapshot
        snap = build_portfolio_snapshot()
        total = float(snap.get("total_jpy") or 0)
        ctx["portfolio_total_jpy"] = total
        # cash_total_jpy already includes USD cash translated to JPY.  The old
        # cash_jpy + USD calculation counted USD cash twice.
        cash = float(snap.get("cash_total_jpy", snap.get("cash_jpy", 0)) or 0)
        ctx["cash_ratio_pct"] = round(cash / total * 100, 1) if total > 0 else None
        # セクター集中度（最大）
        sectors = snap.get("sector_breakdown", {})
        if sectors:
            top = max(sectors.items(), key=lambda x: x[1].get("ratio", 0))
            ctx["top_sector"] = top[0]
            ctx["top_sector_pct"] = round(top[1].get("ratio", 0) * 100, 1)
    except Exception:
        pass

    # ガード状態
    try:
        guard_path = BASE_DIR / "guard_state.json"
        if guard_path.exists():
            g = json.loads(guard_path.read_text())
            ctx["new_entry_allowed"] = g.get("new_entry_allowed", True)
            ctx["daily_pnl_pct"] = g.get("daily_pnl_pct")
            ctx["monthly_pnl_pct"] = g.get("monthly_pnl_pct")
            ctx["consecutive_positive_days"] = g.get("consecutive_positive_days", 0)
    except Exception:
        pass

    # 直近 30 日の post-filter 統計。文字列ログではなく action_stage_log を正とする。
    ctx.update(_recent_post_filter_stats(days=30))

    return ctx


def _build_prompt(market_ctx: dict, params: dict) -> tuple[str, str]:
    """system + user prompt を返す"""
    system = """あなたは ALMANAC（量的投資システム）のチューニング・アドバイザーです。
現在の市場・ポートフォリオ状況を踏まえ、各チューニングパラメータの最適な推奨値を JSON で返してください。

判断方針:
- 攻撃局面（VIX<20 / レジームA_強気 / DD 健全 / 連勝中）→ 閾値を緩めて機会を取りに行く
  例: already_executed/deferred が多い時は DONE_LIST/ordered 仕様をレビューし、too_small が多い時だけ min_action_jpy や min_action_pct_of_portfolio の引き下げを検討する
- 守備局面（VIX>30 / レジームC_弱気 / DD 大 / 連敗中）→ 閾値を厳しくして防御
  例: daily_loss_limit_pct を緩める（早期発動を抑える）/ rebalance_cooldown_days を伸ばす
- 各値はパラメータの min/max レンジ内に必ず収めること
- 不明・確信のないパラメータは現在値をそのまま recommended に入れ rationale に「変更不要」と記述すること
- **全パラメータを必ず返すこと**（省略禁止）。変更不要でも recommended に現在値をセットして返す

出力形式（JSON のみ、他は不要）:
{
  "recommendations": [
    {"key": "min_action_jpy", "recommended": 200000, "rationale": "VIX 18 強気局面で大型エントリー優先"},
    {"key": "recovery_window_days", "recommended": 5, "rationale": "変更不要: 現在値が適切"},
    ...
  ]
}"""

    # パラメータリストをコンパクトに
    param_lines = []
    for key, meta in params.items():
        param_lines.append(
            f"- {key}: 現在値 {meta.get('value')}, レンジ [{meta.get('min')} - {meta.get('max')}], "
            f"単位 {meta.get('unit')}, 説明: {meta.get('desc', '')[:80]}"
        )

    user = f"""## 現在の市場・ポートフォリオ状況
```json
{json.dumps(market_ctx, ensure_ascii=False, indent=2, default=str)}
```

## 全チューニングパラメータ
{chr(10).join(param_lines)}

上記を踏まえ、各パラメータの推奨値を返してください。
**全パラメータを必ず返すこと（省略禁止）**。変更不要の場合も recommended に現在値をセットし、rationale に「変更不要」と記述すること。
JSON 形式のみで返答してください（コードブロックなし）。"""

    return system, user


def generate_recommendations(
    keys: list[str] | None = None,
    market_context: dict | None = None,
) -> dict:
    """
    AI 推奨値を生成し、tunable_params.json の各エントリの ai_recommended に保存。
    レスポンスは UI 表示用に整形。
    """
    market_ctx = market_context if market_context is not None else _load_market_context()
    all_params = tp.list_all()
    if keys is not None:
        requested = list(dict.fromkeys(keys))
        all_params = {key: all_params[key] for key in requested if key in all_params}
    if not all_params:
        return {"error": "no tunable params loaded", "recommendations": []}

    system, user = _build_prompt(market_ctx, all_params)

    # Claude 呼び出し
    try:
        from analyst.llm_client import call_claude
        # Opus は重いので Sonnet で十分（pure JSON 出力）
        raw = call_claude(
            system=system,
            user=user,
            model="claude-sonnet-5",
            max_tokens=4096,
            use_tool=False,
            temperature=0.3,
        )
    except Exception as e:
        return {"error": f"Claude 呼び出し失敗: {e}", "recommendations": []}

    # JSON 抽出
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    # 前後のコードフェンスや日本語コメントを掃除
    import re
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"error": "JSON 抽出失敗", "raw": text[:500], "recommendations": []}

    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"error": f"JSON パース失敗: {e}", "raw": text[:500], "recommendations": []}

    recs = parsed.get("recommendations", [])
    saved: list = []
    recommendation_state: list[dict] = []
    for r in recs:
        key = r.get("key")
        rec_value = r.get("recommended")
        rationale = r.get("rationale", "")
        if not key or rec_value is None:
            continue
        try:
            current = all_params.get(key, {}).get("value")
            normalized = {
                "key": key,
                "current": current,
                "recommended": rec_value,
                "delta_pct": _delta_pct(current, rec_value),
                "rationale": rationale,
            }
            saved.append(normalized)
            recommendation_state.append(normalized)
        except Exception as e:
            saved.append({"key": key, "error": str(e)})

    if recommendation_state:
        tp.set_ai_recommendations(recommendation_state)

    return {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "market_context": market_ctx,
        "recommendations": saved,
        "total_proposed": len(saved),
    }


def load_market_context() -> dict:
    """Public, side-effect-free context collector used by Auto Tune."""
    return _load_market_context()


def _delta_pct(old, new) -> float | None:
    """変化率を％で返す"""
    try:
        o, n = float(old), float(new)
        if abs(o) < 1e-9:
            return None
        return round((n - o) / abs(o) * 100, 1)
    except Exception:
        return None


if __name__ == "__main__":
    import json as _j
    out = generate_recommendations()
    print(_j.dumps(out, ensure_ascii=False, indent=2))
