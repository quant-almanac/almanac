"""
model_router.py — ALMANAC の LLM モデル一元ルーティング

全 LLM 呼び出しは `get_model(role)` 経由にすることで、
- モデル昇降格（Opus 4.6 → 4.7 等）を一箇所で管理
- 予算モード（eco/normal/premium）でモデル自動降格
- A/B テスト・カナリアデプロイの容易化

使い方:
    from model_router import get_model, resolve_adapter
    model_id = get_model("final_synthesis")  # "claude-opus-4-8"
    adapter  = resolve_adapter("red_team_1") # "deepseek" → llm_adapters.call_deepseek

環境変数:
    ALMANAC_BUDGET_MODE = {eco, normal, premium}
        - eco:     Opus → Sonnet, Sonnet → Haiku, 外部モデル→haiku
        - normal:  ROLE_ROUTING そのまま（デフォルト）
        - premium: Sonnet → Opus（Red Team/話題分析は据え置き）
    ALMANAC_MODEL_OVERRIDE_<ROLE> = <model_key>
        - 個別 role を一時的に上書き（例: ALMANAC_MODEL_OVERRIDE_FINAL_SYNTHESIS=sonnet）
    旧 ALMANAC_* は移行期間の fallback として読む。
"""
from __future__ import annotations

from typing import Literal

from almanac.runtime_config import get_env

__all__ = [
    "MODEL_REGISTRY",
    "ROLE_ROUTING",
    "get_model",
    "get_model_key",
    "resolve_adapter",
    "list_roles",
    "is_anthropic",
]

BudgetMode = Literal["eco", "normal", "premium"]

# ─────────────────────────────────────────────────────────────
# モデル ID レジストリ（ベンダー非依存の key → ベンダー ID）
# ─────────────────────────────────────────────────────────────
MODEL_REGISTRY: dict[str, str] = {
    # Anthropic
    # Opus 4.8: 4.7 と同価格 ($5/$25 per M)・API 互換 (破壊的変更なし) で能力向上
    "opus":         "claude-opus-4-8",
    "sonnet":       "claude-sonnet-5",
    "haiku":        "claude-haiku-4-5-20251001",

    # External (OpenAI 互換 or google-genai)
    # NOTE: DeepSeek "deepseek-chat" alias は最新非推論モデル（V4 リリース後は自動的に V4-flash 系に追従）。
    # 明示的に V4-flash を指定して、将来の alias 変更による意図せぬ切替を防止。
    # V4-flash: $0.14 input / $0.28 output per M tokens（V3 比 ~50% 安価）
    "deepseek":     "deepseek-v4-flash",          # DeepSeek V4 flash chat
    "deepseek_pro": "deepseek-v4-pro",             # DeepSeek V4 Pro (正式モデル名 2026-05 確認)
    "deepseek_r":   "deepseek-reasoner",          # DeepSeek R1 推論モデル（V4 reasoner 出たら差替）
    "qwen":         "qwen2.5-72b-instruct",       # DashScope OpenAI 互換
    "gemini_flash": "gemini-flash-latest",        # google-genai (最新 flash stable alias)
}

# ベンダー判定：Anthropic SDK で叩くか、llm_adapters.py の外部 adapter で叩くか
_ANTHROPIC_KEYS = frozenset({"opus", "sonnet", "haiku"})

def is_anthropic(model_key: str) -> bool:
    """model_key が Anthropic SDK で扱うモデルか。"""
    return model_key in _ANTHROPIC_KEYS


# ─────────────────────────────────────────────────────────────
# Role → Model Key のマッピング
# ─────────────────────────────────────────────────────────────
ROLE_ROUTING: dict[str, str] = {
    # ── Anthropic (Opus / Sonnet / Haiku) ───────────────────
    "final_synthesis":         "opus",     # 最重要：全ティア合成 + Extended Thinking
    "dca_tranche_selector":    "opus",     # DCA 発動時の tranche 銘柄選定（高頻度ではない）

    "tier_analysis_long":      "sonnet",
    "tier_analysis_medium":    "sonnet",
    "tier_analysis_short":     "sonnet",

    # 信用買い・空売りは DeepSeek V4 Pro が一次判断し、最終 Opus が採否を統合する。
    "tier_analysis_margin_long": "deepseek_pro",
    "tier_analysis_shortsell":   "deepseek_pro",

    # ── スクリーナー専用ハーネス（S6 ハーネス再設計、2026-04-26）──
    # screener.py / long_term_screener.py の Sonnet×3 ディベートを DeepSeek V4-flash 単一コールに置換。
    # Stage1: predebate (deepseek) で 3 視点を内部生成 → Stage2: BUY 上位のみ Sonnet で第二意見。
    "screener_predebate":         "deepseek",   # NEW: 内部 3 視点予選
    "screener_second_opinion":    "sonnet",     # NEW: BUY 上位 3 件確認
    "long_term_predebate":        "deepseek",   # NEW: 90 銘柄予選
    "long_term_thesis":           "sonnet",     # 維持（テーゼ生成、Batch API）

    "screener_deepdive":       "sonnet",   # 旧 alias（screener_second_opinion と等価）。後方互換のため残置。
    "decision_support":        "sonnet",   # 同上

    "chat":                    "haiku",
    "delta_monitor":           "haiku",    # analyzer.py --delta-only 差分監視

    # ── 外部モデル ─────────────────────────────────────────
    "red_team_1":              "deepseek",      # 技術分析寄り
    "red_team_2":              "qwen",          # 中国・新興市場視点
    "red_team_3":              "gemini_flash",  # マクロ寄り

    "news_topic_deepdive":     "deepseek",
    "news_topic_fallback":     "qwen",

    "social_topic_deepdive":   "deepseek",
    "social_topic_fallback":   "qwen",
}


# ─────────────────────────────────────────────────────────────
# 予算モード別ダウングレードテーブル
# ─────────────────────────────────────────────────────────────
_ECO_DOWNGRADE: dict[str, str] = {
    "opus":   "sonnet",
    "sonnet": "haiku",
}

_PREMIUM_UPGRADE: dict[str, str] = {
    "sonnet": "opus",
    # Haiku / 外部モデルは据え置き（予算意図に反するため）
}


def _current_budget_mode() -> BudgetMode:
    mode = (get_env("ALMANAC_BUDGET_MODE", "normal") or "normal").lower()
    if mode in ("eco", "premium"):
        return mode  # type: ignore[return-value]
    return "normal"


def get_model_key(role: str) -> str:
    """
    role → model_key（ベンダー非依存キー）を解決する。
    環境変数 `ALMANAC_MODEL_OVERRIDE_<ROLE>` が優先。
    次に ALMANAC_BUDGET_MODE でダウングレード/アップグレード。
    """
    role_key = role.upper()
    override = get_env(f"ALMANAC_MODEL_OVERRIDE_{role_key}")
    if override and override in MODEL_REGISTRY:
        return override

    base = ROLE_ROUTING.get(role)
    if base is None:
        # 未登録 role は sonnet に fallback（安全側）
        return "sonnet"

    mode = _current_budget_mode()
    if mode == "eco":
        return _ECO_DOWNGRADE.get(base, base)
    if mode == "premium":
        return _PREMIUM_UPGRADE.get(base, base)
    return base


def get_model(role: str) -> str:
    """
    role → ベンダー固有の model_id（例: "claude-opus-4-8"）を返す。
    既存の `client.messages.create(model=...)` 呼び出しで直接使える。
    """
    key = get_model_key(role)
    return MODEL_REGISTRY[key]


def resolve_adapter(role: str) -> str:
    """
    role → adapter 種別を返す。
    戻り値: "anthropic" | "deepseek" | "qwen" | "gemini_flash" | "deepseek_r"
    呼び出し側は文字列で switch するか、llm_adapters の dispatch を使う。

    NOTE: deepseek_pro / deepseek_r は同じ DeepSeek API エンドポイントを使うため、
    adapter は "deepseek" に統一（model_id だけ差し替え）。
    """
    key = get_model_key(role)
    if is_anthropic(key):
        return "anthropic"
    # DeepSeek 系（flash / pro / reasoner）は全て "deepseek" adapter に集約
    if key in ("deepseek", "deepseek_pro", "deepseek_r"):
        return "deepseek"
    return key  # qwen / gemini_flash など


def list_roles() -> list[str]:
    """登録されている全 role のリスト（テスト・デバッグ用）"""
    return list(ROLE_ROUTING.keys())


# ─────────────────────────────────────────────────────────────
# CLI（診断用）
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        rows = []
        for role in list_roles():
            key = get_model_key(role)
            rows.append({
                "role":      role,
                "model_key": key,
                "model_id":  MODEL_REGISTRY[key],
                "adapter":   resolve_adapter(role),
            })
        print(json.dumps({
            "budget_mode": _current_budget_mode(),
            "routing":     rows,
        }, ensure_ascii=False, indent=2))
    else:
        print("Usage: python model_router.py show")
