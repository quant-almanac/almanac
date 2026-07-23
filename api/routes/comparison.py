"""
GET /api/upgrade-comparison — AIアップグレード バックテスト比較レポート
"""
import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


@router.get("/api/upgrade-comparison")
async def get_upgrade_comparison():
    """
    Black-Litterman / Max Sharpe / Min CVaR の2年バックテスト比較結果を返す。
    reports/upgrade_comparison.json が存在しない場合は空レスポンスを返す。
    """
    path = BASE_DIR / "reports" / "upgrade_comparison.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": str(e), "comparison": {}}
    return {
        "comparison": {},
        "message": "バックテスト未実行。python backtest_comparison.py を実行してください。",
        "period": None,
        "generated": None,
    }


@router.get("/api/bl-views")
async def get_bl_views():
    """BL LLMビュー（銘柄別リターン期待値・分散）を返す"""
    path = BASE_DIR / "bl_views.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": str(e), "views": {}}
    return {"views": {}, "message": "bl_views.json 未生成。run_analysis() 実行後に生成されます。"}


@router.get("/api/agent-beliefs")
async def get_agent_beliefs():
    """FinCon エージェント投資信念ストアを返す"""
    path = BASE_DIR / "beliefs" / "agent_beliefs.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": str(e), "beliefs": []}
    return {"beliefs": [], "message": "beliefs/agent_beliefs.json 未生成"}


@router.get("/api/ai-upgrades")
async def get_ai_upgrades():
    """
    Phase 2 AI アップグレードデータをまとめて返す。
    - regime_consensus: HMM/macro_score/VIX/SPY-MA50 の4指標合意分析
    - bl_views: VIX連動スケーリング済みBLビュー
    - beliefs: FinCon エージェント投資信念（conviction_score付き）
    """
    result: dict = {}

    # ── Red Team attacks ────────────────────────────────────
    try:
        from analyst import CACHE_PATH, get_cached
        cached = get_cached() or {}
        result["redteam"] = cached.get("redteam") or {"attacks": [], "underutilized": []}
    except Exception as e:
        result["redteam"] = {"attacks": [], "underutilized": [], "error": str(e)}

    # ── BL views ───────────────────────────────────────────
    bl_path = BASE_DIR / "bl_views.json"
    try:
        result["bl_views"] = json.loads(bl_path.read_text(encoding="utf-8")) if bl_path.exists() else {}
    except Exception as e:
        result["bl_views"] = {"error": str(e)}

    # ── Agent beliefs ───────────────────────────────────────
    beliefs_path = BASE_DIR / "beliefs" / "agent_beliefs.json"
    try:
        result["beliefs"] = json.loads(beliefs_path.read_text(encoding="utf-8")) if beliefs_path.exists() else {"beliefs": []}
    except Exception as e:
        result["beliefs"] = {"error": str(e), "beliefs": []}

    # ── Regime consensus ────────────────────────────────────
    try:
        regime_path  = BASE_DIR / "regime_state.json"
        vix_path     = BASE_DIR / "vix_state.json"
        macro_path   = BASE_DIR / "macro_state.json"

        regime    = json.loads(regime_path.read_text(encoding="utf-8")) if regime_path.exists() else {}
        vix_state = json.loads(vix_path.read_text(encoding="utf-8")) if vix_path.exists() else {}

        # VIX取得（vix_state > regime）
        vix = float(vix_state.get("level") or regime.get("vix") or 20)

        hmm_regime  = regime.get("regime", "")
        macro_score = float(regime.get("macro_score") or 5)
        spy_above   = bool(regime.get("spy_above", True))

        hmm_sig   = "bull" if "強気" in hmm_regime else ("bear" if "弱気" in hmm_regime else "neutral")
        macro_sig = "bull" if macro_score >= 6 else ("bear" if macro_score <= 3 else "neutral")
        vix_sig   = "bull" if vix < 20 else ("bear" if vix >= 30 else "neutral")
        spy_sig   = "bull" if spy_above else "bear"

        signals   = [hmm_sig, macro_sig, vix_sig, spy_sig]
        bull_cnt  = signals.count("bull")
        bear_cnt  = signals.count("bear")
        max_agree = max(bull_cnt, bear_cnt, signals.count("neutral"))
        confidence = {4: 1.0, 3: 0.75, 2: 0.5}.get(max_agree, 0.25)
        direction  = "強気" if bull_cnt > bear_cnt else ("弱気" if bear_cnt > bull_cnt else "中立")

        # VIXスケール説明
        vix_scale = "×0.8(低VIX)" if vix < 20 else ("×1.0" if vix < 30 else ("×1.3(高VIX)" if vix < 40 else "×1.5(パニック)"))

        result["regime_consensus"] = {
            "hmm_regime":   hmm_regime,
            "macro_score":  macro_score,
            "vix":          round(vix, 1),
            "vix_scale":    vix_scale,
            "spy_above":    spy_above,
            "signals": {
                "hmm":   hmm_sig,
                "macro": macro_sig,
                "vix":   vix_sig,
                "spy":   spy_sig,
            },
            "bull_count":  bull_cnt,
            "bear_count":  bear_cnt,
            "confidence":  confidence,
            "direction":   direction,
            "conflicted":  confidence < 0.6,
        }
    except Exception as e:
        result["regime_consensus"] = {"error": str(e)}

    return result
