"""
シナリオベース戦略マネージャー
4つの相場シナリオ（BULL/NEUTRAL/BEAR/CRASH）に基づき、
現在の市場状況を評価して推奨戦略・リスク機会をJSON出力する。
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

BASE_DIR = Path(__file__).parent

SCENARIOS = {
    "BULL": {
        "name": "強気相場",
        "icon": "🚀",
        "color": "#22C55E",
        "description": "S&P500・日経ともに50日MAより上。リスクオン。",
        "cash_ratio_target": 0,
        "long_bias": True,
        "short_allowed": False,
        "leverage_allowed": True,
        "actions": [
            "攻めモードでは現金比率を0〜3%まで圧縮",
            "Longコアポジションを維持・積み増し",
            "Mediumポジションは利益確定ラインを引き上げ",
            "新規エントリーはモメンタム銘柄優先",
            "信用買いはVIX連動レバレッジ上限内で限定活用",
            "持株会は継続",
        ],
        "opportunity": {
            "medium_risk": ["セクターETF（半導体・AI関連）", "成長グロース株"],
            "high_risk": ["レバレッジETF（FANG+等）", "モメンタム個別株"],
        },
        "crisis_protocol": [],
    },
    "NEUTRAL": {
        "name": "中立相場",
        "icon": "⚖️",
        "color": "#6366F1",
        "description": "混在シグナル。S&P500またはNK225の一方が50日MA未満。",
        "cash_ratio_target": 15,
        "long_bias": True,
        "short_allowed": True,
        "leverage_allowed": False,
        "actions": [
            "現金比率を10〜15%に維持",
            "LongコアはDCA継続（SLIM系・インデックス）",
            "Mediumポジションは高値でトリム",
            "空売りスクリーニングで超割高銘柄を監視",
            "リバランスの好機（NISA成長枠活用）",
        ],
        "opportunity": {
            "medium_risk": ["割安バリュー株", "欧州ETF（EWG・IEV）", "ポーランド（EPOL）"],
            "high_risk": ["空売り候補（RSI80+・MA50比+20%）", "オプション活用"],
        },
        "crisis_protocol": [],
    },
    "BEAR": {
        "name": "弱気相場",
        "icon": "🐻",
        "color": "#F59E0B",
        "description": "S&P500・日経ともに50日MA未満。リスクオフ傾向。",
        "cash_ratio_target": 30,
        "long_bias": False,
        "short_allowed": True,
        "leverage_allowed": False,
        "actions": [
            "現金比率を25〜35%に引き上げ",
            "MediumポジションはStop-Lossで整理",
            "Longコアは損切りなし・DCA停止",
            "空売り候補を積極スクリーニング",
            "ゴールド（GLD）へのシフトを検討",
            "新規エントリー原則禁止",
        ],
        "opportunity": {
            "medium_risk": ["ゴールド（GLD）・コモディティ", "債券ETF", "インバース型ETF"],
            "high_risk": ["空売り（レジームB_弱気・C_弱気判定銘柄）"],
        },
        "crisis_protocol": [],
    },
    "CRASH": {
        "name": "クラッシュ/危機",
        "icon": "🚨",
        "color": "#EF4444",
        "description": "急落・地政学リスク・金融危機。守りを最優先。",
        "cash_ratio_target": 50,
        "long_bias": False,
        "short_allowed": True,
        "leverage_allowed": False,  # 底値打ち確認後でも原則禁止（手動オーバーライド時のみ許可）
        "actions": [
            "現金比率を40〜60%まで引き上げ",
            "Mediumポジションを全て売却",
            "Longコアはドローダウン-40%でDCA再開検討",
            "信用買いは全て解消",
            "VIX > 40で底値シグナル監視開始",
            "底値確認後：レバレッジETF・空売り利益で再エントリー",
            "持株会の売却を検討（コスト回収後）",
        ],
        "opportunity": {
            "medium_risk": ["ゴールド・短期国債", "ディフェンシブ（ヘルスケア・生活必需品）"],
            "high_risk": [
                "底値打ち確認後：NVDA/AVGO大量買い増し",
                "レバレッジETF（QQQ3x相当）",
                "底値圏での空売り利確→ロング転換",
                "超割安バリュー株の仕込み",
            ],
        },
        "crisis_protocol": [
            "1. VIX > 30: 新規禁止・現金比率35%へ",
            "2. VIX > 40: 現金50%へ・空売りポジション追加",
            "3. 主要指数-20%: 底値スクリーニング開始",
            "4. VIX < 30 + 主要指数反転: Long再構築フェーズ",
        ],
    },
}


def _load_regime() -> dict:
    path = BASE_DIR / "regime_state.json"
    fallback = None
    if path.exists():
        with open(path, encoding="utf-8") as f:
            fallback = json.load(f)
        if not _is_regime_stale(fallback):
            fallback["_source"] = "regime_state.json"
            return fallback

    screen_regime = _load_screen_market_regime()
    if screen_regime:
        return screen_regime
    if fallback:
        fallback["_source"] = "regime_state.json"
        fallback["_stale"] = True
        return fallback
    return {"spy_above": True, "nk_above": True, "_source": "default"}


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s[:16], "%Y-%m-%d %H:%M"),
    ):
        try:
            return parser(value)
        except Exception:
            continue
    return None


def _is_regime_stale(regime: dict, max_age_hours: float = 24.0) -> bool:
    ts = _parse_dt(str(regime.get("updated") or regime.get("cached_at") or ""))
    if ts is None:
        return False
    return (datetime.now() - ts).total_seconds() / 3600 > max_age_hours


def _load_screen_market_regime(max_age_hours: float = 24.0) -> dict | None:
    path = BASE_DIR / "screen_results.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = _parse_dt(str(data.get("timestamp") or data.get("generated_at") or ""))
        if ts is not None and (datetime.now() - ts).total_seconds() / 3600 > max_age_hours:
            return None
        mm = data.get("market_meta") or {}
        spy_label = mm.get("sp500")
        nk_label = mm.get("nikkei")
        if spy_label not in ("上", "下") or nk_label not in ("上", "下"):
            return None
        return {
            "spy_above": spy_label == "上",
            "nk_above": nk_label == "上",
            "updated": data.get("timestamp") or data.get("generated_at") or "",
            "regime": "A_強気" if spy_label == "上" and nk_label == "上" else "",
            "_source": "screen_results.json",
        }
    except Exception:
        return None


def _load_guard() -> dict:
    path = BASE_DIR / "guard_state.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _load_briefing() -> dict:
    path = BASE_DIR / "ai_portfolio_analysis.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        synthesis = data.get("synthesis") or {}
        return {
            "summary": synthesis.get("morning_brief_headline") or synthesis.get("stance_reason") or "",
            "risk_alert": " / ".join(str(x) for x in (synthesis.get("risk_warnings") or [])[:3]),
            "opportunity": synthesis.get("opportunity") or synthesis.get("optimization_insight") or "",
        }
    return {}


def _load_short_candidates() -> list:
    path = BASE_DIR / "short_candidates.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("candidates", [])
    return []


def _tunable_value(key: str, fallback):
    try:
        from tunable_params import get as _tp_get
        return _tp_get(key, fallback)
    except Exception:
        return fallback


def _apply_tunable_cash_target(scenario_key: str, scenario: dict) -> dict:
    scenario = dict(scenario)
    fallback = scenario.get("cash_ratio_target", 0)
    key_map = {
        "BULL": "target_cash_pct_aggressive",
        "NEUTRAL": "target_cash_pct_neutral",
        "BEAR": "target_cash_pct_defensive",
        "CRASH": "target_cash_pct_defensive",
    }
    key = key_map.get(scenario_key)
    if key:
        scenario["cash_ratio_target"] = _tunable_value(key, fallback)
    return scenario


def _load_long_term_candidates() -> list:
    path = BASE_DIR / "long_term_screen_results.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("candidates", [])
    return []


def detect_scenario(regime: dict, guard: dict) -> str:
    """
    現在の市場状況からシナリオを判定する。
    - CRASH: daily_pnl < -5% or monthly_pnl < -10%
    - BULL:  spy_above and nk_above
    - BEAR:  not spy_above and not nk_above
    - NEUTRAL: その他
    """
    daily_pnl = guard.get("daily_pnl_pct", 0) or 0
    monthly_pnl = guard.get("monthly_pnl_pct", 0) or 0
    spy_above = regime.get("spy_above", True)
    nk_above = regime.get("nk_above", True)

    if daily_pnl < -5 or monthly_pnl < -10:
        return "CRASH"
    if spy_above and nk_above:
        return "BULL"
    if not spy_above and not nk_above:
        return "BEAR"
    return "NEUTRAL"


def get_strategy() -> dict:
    """
    現在の戦略サマリーを返す。
    最新の正式AI分析の要約も統合する。
    """
    regime = _load_regime()
    guard = _load_guard()
    briefing = _load_briefing()
    short_candidates = _load_short_candidates()
    long_candidates = _load_long_term_candidates()

    scenario_key = detect_scenario(regime, guard)
    scenario = _apply_tunable_cash_target(scenario_key, SCENARIOS[scenario_key])

    # 高リスクハイリターン機会をまとめる
    high_return_ops = []
    if short_candidates:
        top_shorts = short_candidates[:3]
        for c in top_shorts:
            high_return_ops.append({
                "type": "short",
                "ticker": c.get("ticker"),
                "reason": c.get("reason", ""),
                "rsi": c.get("rsi"),
                "icon": "📉",
            })
    if long_candidates and scenario_key in ("BULL", "NEUTRAL"):
        top_longs = long_candidates[:3]
        for c in top_longs:
            high_return_ops.append({
                "type": "long_screen",
                "ticker": c.get("ticker"),
                "reason": f"スコア {c.get('score', 0):.0f}点",
                "sector": c.get("sector"),
                "icon": "📈",
            })

    return {
        "scenario": scenario_key,
        "scenario_name": scenario["name"],
        "scenario_icon": scenario["icon"],
        "scenario_color": scenario["color"],
        "scenario_description": scenario["description"],
        "cash_ratio_target": scenario["cash_ratio_target"],
        "long_bias": scenario["long_bias"],
        "short_allowed": scenario["short_allowed"],
        "leverage_allowed": scenario["leverage_allowed"],
        "actions": scenario["actions"],
        "opportunity": scenario["opportunity"],
        "crisis_protocol": scenario.get("crisis_protocol", []),
        "high_return_opportunities": high_return_ops,
        "regime": {
            "spy_above": regime.get("spy_above"),
            "nk_above": regime.get("nk_above"),
            "updated": regime.get("updated", ""),
            "source": regime.get("_source", ""),
            "stale": regime.get("_stale", False),
        },
        "briefing_summary": briefing.get("summary", ""),
        "risk_alert": briefing.get("risk_alert", ""),
        "opportunity_note": briefing.get("opportunity", ""),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


if __name__ == "__main__":
    import sys
    result = get_strategy()
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"現在のシナリオ: {result['scenario_icon']} {result['scenario_name']}")
        print(f"現金比率目標: {result['cash_ratio_target']}%")
        print(f"推奨アクション:")
        for a in result["actions"]:
            print(f"  • {a}")
        if result["high_return_opportunities"]:
            print(f"\n高リターン機会:")
            for op in result["high_return_opportunities"]:
                print(f"  {op['icon']} {op.get('ticker')} — {op.get('reason')}")
