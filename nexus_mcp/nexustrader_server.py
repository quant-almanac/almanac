"""
ALMANAC MCP サーバー
Claude Code / Claude.ai から ALMANAC のデータを直接参照できる。

使用例（Claude.ai のチャット）:
  「今日のポートフォリオのリスクは？」
  「CRWVをどうすべきか教えて」
  「現在のガードレール状態は？」
"""

import json
import sys
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent

# FastMCP（mcp パッケージの高レベル API）
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ALMANAC")


def _read_json(filename: str) -> dict:
    path = BASE_DIR / filename
    if not path.exists():
        return {"error": f"{filename} が見つかりません"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}


# ── ツール定義 ──────────────────────────────────────────

@mcp.tool()
def get_portfolio() -> str:
    """現在の保有ポジション一覧を返す（ティア別分類・損益含む）"""
    data = _read_json("holdings.json")
    if "error" in data:
        return data["error"]
    holdings = data if isinstance(data, list) else data.get("holdings", [])
    summary = []
    for h in holdings:
        ticker = h.get("ticker", "?")
        name = h.get("name", "")
        tier = h.get("investment_type", "?")
        value = h.get("value_jpy", 0)
        pnl = h.get("unrealized_pct", 0)
        summary.append(f"{ticker}({name}) [{tier}] ¥{value:,.0f} / {pnl:+.1f}%")
    return "\n".join(summary) if summary else "保有なし"


@mcp.tool()
def get_guard_status() -> str:
    """リスクガードレール状態（取引可否・本日損益・月次損益）を返す"""
    data = _read_json("guard_state.json")
    if "error" in data:
        return data["error"]
    trading = "✅ 取引OK" if data.get("trading_allowed") else "🚫 取引停止"
    entry = "✅ 新規OK" if data.get("new_entry_allowed") else "⛔ 新規禁止"
    daily = data.get("daily_pnl_pct", 0)
    monthly = data.get("monthly_pnl_pct", 0)
    portfolio = data.get("portfolio_value", 0)
    alerts_raw = data.get("alerts", [])
    # alerts may be list of str or list of dict
    alerts_strs = []
    for a in alerts_raw:
        if isinstance(a, dict):
            alerts_strs.append(a.get("message") or a.get("text") or str(a))
        else:
            alerts_strs.append(str(a))
    result = f"""ガードレール状態: {trading} / {entry}
本日損益: {daily:+.2f}%
月次損益: {monthly:+.2f}%
総資産: ¥{portfolio:,.0f}
アラート: {', '.join(alerts_strs) if alerts_strs else 'なし'}
更新: {data.get('last_updated', '?')}"""
    return result


@mcp.tool()
def get_signals() -> str:
    """直近のトレーディングシグナル（エントリー価格・ターゲット・ストップロス）を返す"""
    data = _read_json("signals_log.json")
    if "error" in data:
        return data["error"]
    signals = data if isinstance(data, dict) else {}
    if not signals:
        return "シグナルなし"
    lines = []
    for ticker, sig in signals.items():
        score = sig.get("score", 0)
        entry = sig.get("entry_price", "?")
        target = sig.get("target_price", "?")
        stop = sig.get("stop_loss", "?")
        reason = sig.get("reason", "")
        lines.append(f"{ticker}: スコア{score:.1f} エントリー{entry} → ターゲット{target} / SL{stop}\n  {reason}")
    return "\n".join(lines)


@mcp.tool()
def get_macro() -> str:
    """マクロ経済指標（FRED: FF金利・10年債・CPI・失業率）を返す"""
    data = _read_json("macro_state.json")
    if "error" in data:
        return data["error"]
    fed = data.get("fed_rate")
    y10 = data.get("yield_10y")
    cpi = data.get("cpi_yoy")
    unemp = data.get("unemp_rate")
    spread = data.get("yield_spread")
    inverted = data.get("yield_inverted", False)
    yield_status = "⚠️ 逆イールド" if inverted else "✓ 順イールド"
    return f"""マクロ指標（FRED）:
FF金利: {fed}%
10年債利回り: {y10}%
CPI前年比: {cpi}%
失業率: {unemp}%
イールドスプレッド: {spread:+.2f}% {yield_status}
キャッシュ: {data.get('cached_at', '?')}"""


@mcp.tool()
def get_briefing() -> str:
    """最新の正式AI分析を簡潔に返す。"""
    data = _read_json("ai_portfolio_analysis.json")
    if "error" in data:
        return data["error"]
    synthesis = data.get("synthesis") or {}
    actions = [
        f"{row.get('ticker')}: {row.get('action') or row.get('type')}"
        for row in (synthesis.get("priority_actions") or [])
        if isinstance(row, dict)
    ]
    return f"""【{data.get('as_of', '?')} AI分析】
スタンス: {synthesis.get('overall_stance', '?')}
要約: {synthesis.get('morning_brief_headline') or synthesis.get('stance_reason', '?')}
推奨アクション: {', '.join(actions) if actions else 'なし'}
リスクアラート: {' / '.join(str(x) for x in (synthesis.get('risk_warnings') or [])) or 'なし'}"""


@mcp.tool()
def get_regime() -> str:
    """現在のマーケットレジーム（SPY/日経の200MA位置）を返す"""
    data = _read_json("regime_state.json")
    if "error" in data:
        return data["error"]
    spy = "✅ 200MA上" if data.get("spy_above") else "❌ 200MA下"
    nk = "✅ 200MA上" if data.get("nk_above") else "❌ 200MA下"
    updated = data.get("updated", "?")
    # レジーム判定
    if data.get("spy_above") and data.get("nk_above"):
        regime = "BULL（強気）"
    elif not data.get("spy_above") and not data.get("nk_above"):
        regime = "BEAR（弱気）"
    else:
        regime = "NEUTRAL（中立）"
    return f"レジーム: {regime}\nSPY: {spy}\n日経225: {nk}\n更新: {updated}"


@mcp.tool()
def get_ai_analysis() -> str:
    """最新のAIポートフォリオ分析（ティア別分析・総合判断）を返す"""
    data = _read_json("ai_portfolio_analysis.json")
    if "error" in data:
        return data["error"]
    synthesis = data.get("synthesis", {})
    if not synthesis:
        return "AI分析データなし（portfolio_analyst.py を実行してください）"
    stance = synthesis.get("overall_stance", "?")
    headline = synthesis.get("morning_brief_headline", "?")
    actions = synthesis.get("priority_actions", [])
    warnings = synthesis.get("risk_warnings", [])
    top_actions = "\n".join([
        f"  {i+1}. [{a.get('urgency','?')}] {a.get('action','?')}"
        for i, a in enumerate(actions[:3])
    ])
    return f"""AIスタンス: {stance}
ヘッドライン: {headline}
優先アクション:
{top_actions}
リスク警告: {', '.join(warnings[:2]) if warnings else 'なし'}
生成: {data.get('as_of', '?')}"""


@mcp.tool()
def get_short_candidates() -> str:
    """空売り候補銘柄（RSI・MA乖離率・理由）を返す"""
    data = _read_json("short_candidates.json")
    if "error" in data:
        return data["error"]
    candidates = data if isinstance(data, list) else data.get("candidates", [])
    if not candidates:
        return "空売り候補なし"
    lines = []
    for c in candidates[:5]:
        ticker = c.get("ticker", "?")
        rsi = c.get("rsi", "?")
        ma_pct = c.get("ma50_pct", "?")
        reason = c.get("reason", "")
        lines.append(f"{ticker}: RSI{rsi} MA乖離{ma_pct}% - {reason}")
    return "\n".join(lines)


@mcp.tool()
def get_nisa_summary() -> str:
    """NISA口座の状況（積立進捗・生涯枠残高）を返す"""
    data = _read_json("nisa_portfolio.json")
    if "error" in data:
        return data["error"]
    result = []
    for person in ["husband", "wife"]:
        p = data.get(person, {})
        if not p:
            continue
        name = "メイン" if person == "husband" else "サブ口座"
        broker = p.get("broker", "?")
        ts_used = p.get("tsumitate_used_this_year", 0)
        ts_limit = p.get("tsumitate_limit_annual", 0)
        lifetime_used = p.get("lifetime_used_estimate", 0)
        lifetime_limit = p.get("lifetime_limit", 0)
        result.append(
            f"[{name} / {broker}]\n"
            f"  積立: ¥{ts_used:,.0f} / ¥{ts_limit:,.0f} ({ts_used/ts_limit*100:.0f}%)\n"
            f"  生涯枠: ¥{lifetime_used:,.0f} / ¥{lifetime_limit:,.0f}"
        )
    return "\n".join(result) if result else "NISAデータなし"


if __name__ == "__main__":
    mcp.run()
