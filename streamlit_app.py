"""
ALMANAC v4.0 - Streamlit ダッシュボード
全口座統合管理・リスク管理・持株会・クレカ積立・短期トレード
"""

import streamlit as st
import pandas as pd
import numpy as np
import json
import os
import time
import anthropic
import plotly.graph_objects as go
from datetime import datetime, date
from pathlib import Path

# ============================================================
# ページ設定（最初に実行）
# ============================================================

st.set_page_config(
    page_title='ALMANAC v4.0',
    page_icon='📈',
    layout='wide',
    initial_sidebar_state='expanded',
)

# ============================================================
# モジュールインポート
# ============================================================

BASE_DIR = Path(__file__).parent
AI_EXPLAIN_MODEL = 'claude-haiku-4-5-20251001'


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_ai_explain_usage(
    *,
    started: float,
    section_label: str,
    system: str,
    user_msg: str,
    response=None,
    status: str = 'ok',
    error: Exception | None = None,
) -> None:
    usage = getattr(response, 'usage', None)
    row = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'role': 'streamlit_ai_explain',
        'model': AI_EXPLAIN_MODEL,
        'use_tool': False,
        'max_tokens': 400,
        'elapsed_sec': round(time.monotonic() - started, 2),
        'prompt_chars': len(system) + len(user_msg),
        'section_label': section_label,
        'status': status,
    }
    if response is not None:
        row.update({
            'stop_reason': getattr(response, 'stop_reason', None),
            'content_types': [getattr(block, 'type', None) for block in getattr(response, 'content', [])],
            'input_tokens': getattr(usage, 'input_tokens', None),
            'output_tokens': getattr(usage, 'output_tokens', None),
        })
    if error is not None:
        row.update({
            'error_type': type(error).__name__,
            'error': str(error)[:500],
        })
    _append_llm_call_log(row)

def _safe_import(module_name):
    try:
        import importlib
        return importlib.import_module(module_name)
    except ImportError as e:
        return None

risk_engine     = _safe_import('risk_engine')
espp_mgr      = _safe_import('espp_plan_manager')
cc_mgr          = _safe_import('credit_card_investment')
portfolio_mgr   = _safe_import('portfolio_manager')
rebalance_eng   = _safe_import('rebalance_engine')
tax_opt         = _safe_import('tax_optimizer')
port_opt        = _safe_import('portfolio_optimizer')
lt_screener     = _safe_import('long_term_screener')
short_scr       = _safe_import('short_screener')
margin_mgr      = _safe_import('margin_manager')
decision_sup    = _safe_import('decision_support')
ollama_chat     = _safe_import('ollama_chat')

# ============================================================
# データロード関数
# ============================================================

@st.cache_data(ttl=300)   # 5分キャッシュ
def load_portfolio_json() -> dict:
    path = BASE_DIR / 'portfolio.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


@st.cache_data(ttl=300)
def load_holdings() -> dict:
    path = BASE_DIR / 'holdings.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


@st.cache_data(ttl=300)
def load_account() -> dict:
    path = BASE_DIR / 'account.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'balance': 0, 'risk_per_trade': 0.1}


@st.cache_data(ttl=600)
def load_trade_history() -> pd.DataFrame:
    path = BASE_DIR / 'trade_history.csv'
    if path.exists():
        try:
            df = pd.read_csv(path, parse_dates=['date'] if 'date' in pd.read_csv(path, nrows=1).columns else [])
            return df
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


@st.cache_data(ttl=300)
def load_briefing() -> dict:
    path = BASE_DIR / 'ai_portfolio_analysis.json'
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            synthesis = data.get('synthesis') or {}
            return {
                'generated_at': data.get('as_of'),
                'summary': synthesis.get('morning_brief_headline') or synthesis.get('stance_reason') or '',
                'market_comment': synthesis.get('overall_stance') or '',
                'actions': [
                    f"{row.get('ticker')}: {row.get('action') or row.get('type')}"
                    for row in (synthesis.get('priority_actions') or []) if isinstance(row, dict)
                ],
                'risk_alert': ' / '.join(str(x) for x in (synthesis.get('risk_warnings') or [])[:3]),
                'opportunity': synthesis.get('opportunity') or synthesis.get('optimization_insight') or '',
            }
        except Exception:
            return {}
    return {}


@st.cache_data(ttl=60)
def load_signals_log() -> dict:
    path = BASE_DIR / 'signals_log.json'
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


@st.cache_data(ttl=300)
def fetch_current_price(ticker: str) -> float | None:
    """yfinance で現在価格を取得（5分キャッシュ）"""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period='5d')
        if hist.empty:
            return None
        return float(hist['Close'].iloc[-1])
    except Exception:
        return None


@st.cache_data(ttl=300)
def load_screen_results() -> list:
    path = BASE_DIR / 'screen_results.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # dict形式（candidates キーあり）とリスト形式の両方に対応
        if isinstance(data, dict):
            return data.get('candidates', [])
        return data if isinstance(data, list) else []
    return []


@st.cache_data(ttl=60)
def load_regime_state() -> dict:
    path = BASE_DIR / 'regime_state.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def get_portfolio_total() -> float:
    """ポートフォリオ総額を返す。可能な限り portfolio_manager の正本スナップショットを使う。"""
    if portfolio_mgr:
        try:
            try:
                snapshot = portfolio_mgr.build_portfolio_snapshot(fetch_missing_sectors=False)
            except TypeError:
                snapshot = portfolio_mgr.build_portfolio_snapshot()
            total = float((snapshot or {}).get('total_jpy') or 0)
            if total > 0:
                return total
        except Exception:
            pass

    account = load_account()
    holdings = load_holdings()
    fx = _account_fx_rate(account)
    cash = _account_total_cash_jpy(account)
    positions = sum(
        _holding_value_jpy(key, info, fx)
        for key, info in holdings.items()
        if isinstance(info, dict)
    )
    return cash + positions


def _account_total_cash_jpy(account: dict) -> float:
    """account.json の保存済み派生値ではなく、残高とFXから現金合計を再計算する。"""
    try:
        jpy = float(account.get('balance', 0) or 0)
        usd = float(account.get('usd_balance', 0) or 0)
        fx = _account_fx_rate(account)
        return jpy + usd * fx
    except (TypeError, ValueError):
        return float(account.get('total_cash', account.get('balance', 0)) or 0)


def _account_fx_rate(account: dict) -> float:
    try:
        return float(account.get('fx_rate_usdjpy', 150.0) or 150.0)
    except (TypeError, ValueError):
        return 150.0


def _holding_value_jpy(key: str, info: dict, fx_rate: float) -> float:
    if key in {'CASH_JPY', 'CASH_USD'}:
        return 0.0
    try:
        shares = float(info.get('shares', 0) or 0)
        price = float(info.get('current_nav') or info.get('entry_price', 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    currency = str(info.get('currency', 'JPY') or 'JPY').upper()
    is_fund = bool(info.get('unit')) and currency != 'USD'
    value = shares * price / 10000 if is_fund else shares * price
    if currency == 'USD':
        value *= fx_rate
    return value


def _session_portfolio_total() -> float:
    total = st.session_state.get('portfolio_total')
    if total is None:
        total = get_portfolio_total()
        st.session_state['portfolio_total'] = total
    return float(total or 0)


# ============================================================
# カスタム CSS
# ============================================================

def inject_css():
    st.markdown('''
<style>
/* ======================================================
   ALMANAC v4.0 — AI-Native Morning Brief Theme
   Research-based: navy-black base + indigo-violet AI glow
   ====================================================== */

/* Google Fonts */
@import url('https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300;0,14..32,400;0,14..32,500;0,14..32,600;0,14..32,700;0,14..32,800&display=swap');

:root {
  /* ── Backgrounds ── */
  --bg:          #0B0E14;
  --bg-surface:  #161922;
  --bg-hover:    #1C1F2B;
  --bg-elevated: #1E2230;
  --bg-input:    #0F1219;

  /* ── Text ── */
  --text:        #E8ECF1;
  --text-sub:    #9BA1B0;
  --text-dim:    #5C6370;

  /* ── AI Accent (indigo-violet) ── */
  --ai:          #6366F1;
  --ai-hover:    #818CF8;
  --ai-glow:     rgba(99,102,241,0.18);
  --ai-violet:   #8B5CF6;
  --ai-border:   rgba(99,102,241,0.4);

  /* ── Financial semantic ── */
  --green:       #22C55E;
  --green-bg:    rgba(34,197,94,0.12);
  --green-dim:   #16a34a;
  --red:         #EF4444;
  --red-bg:      rgba(239,68,68,0.12);
  --amber:       #F59E0B;
  --amber-bg:    rgba(245,158,11,0.12);
  --blue:        #3B82F6;
  --blue-bg:     rgba(59,130,246,0.12);

  /* ── Borders ── */
  --border:      #1E2230;
  --border-hi:   #2D3A5E;

  /* Shorthands kept for legacy classes */
  --bg-card:     #161922;
  --bg-card2:    #0F1219;
  --text-dim-legacy: #5C6370;
}

/* ===== ベースフォント ===== */
body, .stApp, p, div, span, label, button {
  font-family: 'Inter', 'Noto Sans JP', system-ui, sans-serif !important;
}

/* ===== 数字はタブラー数字（金融UIの必須） ===== */
.tabular { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1, 'zero' 1; }

/* ===== Streamlit ヘッダー非表示・余白調整 ===== */
header[data-testid="stHeader"] { display: none !important; }
#MainMenu { display: none !important; }
footer { display: none !important; }
.block-container { padding: 1.5rem 2rem 2rem !important; max-width: 1400px !important; }
[data-testid="stSidebar"] { background: #0F1219 !important; }
[data-testid="stSidebar"] > div { padding: 1rem !important; }

/* ===== 汎用サーフェスカード ===== */
.nt-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 10px;
}
.nt-card-title {
  font-size: 0.68rem; font-weight: 700; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--text-dim); margin-bottom: 5px;
}
.nt-card-value { font-size: 1.35rem; font-weight: 700; color: var(--text); line-height: 1.2; }
.nt-card-delta { font-size: 0.78rem; margin-top: 3px; }
.nt-card-delta.pos { color: var(--green); }
.nt-card-delta.neg { color: var(--red); }

/* ===== AI 決定カード（インジゴグロー） ===== */
.ai-decision-card {
  background: rgba(17, 25, 40, 0.8);
  backdrop-filter: blur(16px) saturate(160%);
  border: 1px solid var(--ai-border);
  border-radius: 14px;
  padding: 18px 20px;
  margin-bottom: 14px;
  box-shadow: 0 0 24px var(--ai-glow), inset 0 1px 0 rgba(255,255,255,0.04);
  position: relative;
  overflow: hidden;
}
.ai-decision-card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, var(--ai) 0%, var(--ai-violet) 100%);
}
.ai-card-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
}
.ai-sparkle { font-size: 1rem; }
.ai-label {
  font-size: 0.62rem; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--ai-hover);
}
.ai-badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 999px;
  font-size: 0.62rem; font-weight: 700; letter-spacing: 0.06em;
}
.ai-badge.high   { background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }
.ai-badge.medium { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
.ai-badge.low    { background: rgba(239,68,68,0.15);  color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
.ai-card-headline {
  font-size: 1.05rem; font-weight: 700; color: var(--text);
  line-height: 1.35; margin-bottom: 10px;
}
.ai-card-metrics {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 8px; margin-bottom: 12px;
}
.ai-metric {
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 8px; padding: 8px 10px; text-align: center;
}
.ai-metric-lbl { font-size: 0.62rem; color: var(--text-dim); margin-bottom: 3px; }
.ai-metric-val { font-size: 0.95rem; font-weight: 700; }
.ai-metric-val.up { color: var(--green); }
.ai-metric-val.dn { color: var(--red); }
.ai-metric-val.neutral { color: var(--text-sub); }
.ai-card-reason {
  font-size: 0.82rem; color: var(--text-sub); line-height: 1.6;
  margin-bottom: 12px; padding: 10px 12px;
  background: rgba(255,255,255,0.02); border-radius: 8px;
  border-left: 2px solid var(--ai-border);
}
.ai-card-actions { display: flex; gap: 8px; }
.ai-btn-primary {
  background: var(--ai); color: white; border: none;
  border-radius: 7px; padding: 7px 16px;
  font-size: 0.8rem; font-weight: 600; cursor: pointer;
}
.ai-btn-secondary {
  background: transparent; color: var(--text-sub);
  border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 14px; font-size: 0.8rem; cursor: pointer;
}
.ai-timestamp { font-size: 0.65rem; color: var(--text-dim); margin-left: auto; }

/* ===== スケルトンローディング ===== */
.skeleton {
  background: linear-gradient(90deg, var(--bg-surface) 25%, var(--bg-hover) 50%, var(--bg-surface) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}
@keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
.skeleton-card {
  background: rgba(17,25,40,0.8);
  border: 1px solid var(--ai-border);
  border-radius: 14px; padding: 18px 20px; margin-bottom: 14px;
  box-shadow: 0 0 24px var(--ai-glow);
}

/* ===== ヒーローバー ===== */
.hero-bar {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px 24px;
  margin-bottom: 20px;
  display: flex; align-items: center; gap: 24px; flex-wrap: wrap;
}
.hero-label {
  font-size: 0.65rem; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--text-dim); margin-bottom: 4px;
}
.hero-value {
  font-size: 2.2rem; font-weight: 800; color: var(--text);
  letter-spacing: -0.025em; line-height: 1;
  font-variant-numeric: tabular-nums;
}
.hero-delta { font-size: 1rem; font-weight: 600; margin-top: 4px; }
.hero-delta.pos { color: var(--green); }
.hero-delta.neg { color: var(--red); }
.hero-divider {
  width: 1px; height: 48px; background: var(--border);
  flex-shrink: 0;
}
.hero-kpi { flex: 0 0 auto; }
.hero-live {
  margin-left: auto; display: flex; align-items: center; gap: 6px;
  font-size: 0.72rem; color: var(--text-dim);
}
.live-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--green);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.85)} }

/* ===== KPIグリッド ===== */
.kpi-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 10px; margin-bottom: 20px;
}
.kpi-card {
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px;
}
.kpi-label { font-size: 0.66rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--text-dim); margin-bottom: 5px; }
.kpi-value { font-size: 1.25rem; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
.kpi-sub   { font-size: 0.76rem; margin-top: 3px; color: var(--text-dim); }
.kpi-sub.pos { color: var(--green); }
.kpi-sub.neg { color: var(--red); }

/* ===== レジームバッジ ===== */
.regime-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 6px;
  font-size: 0.88rem; font-weight: 700;
}
.regime-A { background: var(--green-bg); color: #4ade80; border: 1px solid rgba(34,197,94,0.35); }
.regime-B { background: var(--amber-bg); color: #fbbf24; border: 1px solid rgba(245,158,11,0.35); }
.regime-C { background: var(--red-bg);   color: #f87171; border: 1px solid rgba(239,68,68,0.35); }

/* ===== トップナビ（水平タブ代替） ===== */
.top-nav {
  display: flex; align-items: center; gap: 2px;
  padding: 4px; background: var(--bg-surface);
  border: 1px solid var(--border); border-radius: 12px;
  margin-bottom: 20px;
}
.top-nav-item {
  flex: 1; padding: 8px 12px; border-radius: 9px;
  font-size: 0.82rem; font-weight: 600; text-align: center;
  color: var(--text-dim); cursor: pointer; transition: all 0.12s;
  white-space: nowrap;
}
.top-nav-item:hover { background: var(--bg-hover); color: var(--text); }
.top-nav-item.active {
  background: var(--ai-glow);
  color: var(--ai-hover);
  box-shadow: 0 0 12px var(--ai-glow);
}

/* ===== サイドバー（ミニマル） ===== */
.sb-brand { padding: 8px 0 16px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.sb-brand-name { font-size: 1.1rem; font-weight: 800; color: var(--text); }
.sb-brand-sub { font-size: 0.65rem; color: var(--text-dim); letter-spacing: 0.08em; margin-top: 2px; }
.sb-label { font-size: 0.6rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-dim); margin: 14px 0 6px; }
.sb-section-title { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-dim); margin: 14px 0 6px; }

/* ===== ステータス行 ===== */
.status-row {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 12px; border-radius: 9px; margin-bottom: 8px;
  background: var(--bg-hover);
}
.status-icon { font-size: 1rem; flex-shrink: 0; }
.status-label { font-size: 0.84rem; color: var(--text-sub); flex: 1; }
.status-val { font-size: 0.84rem; font-weight: 700; }
.status-val.ok    { color: var(--green); }
.status-val.warn  { color: var(--amber); }
.status-val.alert { color: var(--red); }

/* ===== リスク/チャンスバナー ===== */
.home-risk-card {
  background: var(--red-bg); border: 1px solid rgba(239,68,68,0.3);
  border-radius: 10px; padding: 12px 16px; font-size: 0.84rem; color: #fca5a5; margin-bottom: 10px;
}
.home-oppo-card {
  background: var(--blue-bg); border: 1px solid rgba(59,130,246,0.3);
  border-radius: 10px; padding: 12px 16px; font-size: 0.84rem; color: #93c5fd; margin-bottom: 10px;
}
.home-brief {
  background: var(--bg-hover); border: 1px solid var(--border);
  border-left: 3px solid var(--ai-violet); border-radius: 12px; padding: 16px 18px; margin-bottom: 12px;
}
.home-brief-text { font-size: 0.88rem; color: var(--text-sub); line-height: 1.7; }
.home-brief-market { font-size: 0.82rem; color: var(--text-dim); margin-top: 8px; }

/* ===== チャットパネル ===== */
.chat-panel-header { font-size: 0.92rem; font-weight: 700; color: var(--text); margin-bottom: 8px; display:flex; align-items:center; gap:8px; }
.chat-backend-badge { font-size: 0.6rem; background: rgba(99,102,241,0.1); border: 1px solid var(--ai-border); border-radius: 4px; padding: 2px 7px; color: var(--ai-hover); font-weight: 600; }

/* ===== AI フロー図（既存タブ用に残す） ===== */
.ai-flow { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:16px 0 6px; }
.ai-node { background:var(--bg-surface); border:1.5px solid var(--border); border-radius:10px; padding:10px 14px; text-align:center; min-width:108px; }
.ai-node.auto   { border-color: var(--blue); }
.ai-node.sonnet { border-color: var(--ai-violet); }
.ai-node.opus   { border-color: var(--green); }
.ai-node.output { border-color: var(--amber); }
.ai-node-label  { font-size:0.6rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; margin-bottom:3px; }
.ai-node.auto .ai-node-label { color:var(--blue); }
.ai-node.sonnet .ai-node-label { color:var(--ai-violet); }
.ai-node.opus .ai-node-label { color:var(--green); }
.ai-node.output .ai-node-label { color:var(--amber); }
.ai-node-name { font-size:0.8rem; font-weight:600; color:var(--text); white-space:nowrap; }
.ai-node-sub  { font-size:0.65rem; color:var(--text-dim); margin-top:2px; }
.ai-arrow { color:var(--text-dim); font-size:1rem; flex-shrink:0; }
.ai-group { display:flex; flex-direction:column; gap:5px; }

/* ===== その他の継承スタイル ===== */
.guardrail-banner { background: var(--amber-bg); border: 1px solid rgba(245,158,11,0.3); border-radius: 8px; padding: 10px 14px; font-size: 0.78rem; color: var(--amber); margin-top: 10px; }
.home-hero { background: var(--bg-surface); border: 1px solid var(--border); border-top: 3px solid var(--green); border-radius: 12px; padding: 16px 20px 14px; margin-bottom: 16px; }
.home-hero-top { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
.home-hero-brand { font-size:1rem; font-weight:800; color:var(--text); }
.home-hero-assets { font-size:2rem; font-weight:800; color:var(--text); letter-spacing:-0.01em; line-height:1; }
.home-hero-pnl { font-size:0.9rem; font-weight:600; margin-top:4px; }
.home-hero-pnl.pos { color:var(--green); } .home-hero-pnl.neg { color:var(--red); }
.home-hero-live { display:flex; align-items:center; gap:5px; font-size:0.72rem; color:var(--text-dim); }
.home-hero-dot { width:7px; height:7px; border-radius:50%; background:var(--green); display:inline-block; }
.home-hero-date { font-size:0.72rem; color:var(--text-dim); }
.home-panel { background:var(--bg-surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
.home-panel-title { font-size:0.66rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:var(--text-dim); margin-bottom:12px; display:flex; align-items:center; gap:6px; }
.home-panel-title::before { content:''; display:inline-block; width:3px; height:12px; border-radius:2px; background:var(--ai); }
.home-status-row { display:flex; align-items:center; gap:8px; padding:8px 10px; border-radius:7px; margin-bottom:8px; background:var(--bg-hover); }
.home-status-label { font-size:0.82rem; color:var(--text-sub); flex:1; }
.home-status-val { font-size:0.85rem; font-weight:700; }
.status-ok { color:var(--green); } .status-warn { color:var(--amber); } .status-alert { color:var(--red); }
.home-sig-ticker { font-size:1.4rem; font-weight:800; color:var(--green); }
.home-sig-score { font-size:0.8rem; color:var(--amber); font-weight:700; }
.home-sig-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; margin:10px 0; }
.home-sig-item { text-align:center; background:var(--bg-hover); border-radius:7px; padding:7px 4px; }
.home-sig-lbl { font-size:0.62rem; color:var(--text-dim); margin-bottom:2px; }
.home-sig-val { font-size:0.88rem; font-weight:700; color:var(--text); }
.home-sig-val.up { color:var(--green); } .home-sig-val.dn { color:var(--red); }
.home-sig-rr { font-size:0.75rem; color:var(--text-dim); margin-top:6px; }
.home-act-item { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; border-radius:8px; margin-bottom:8px; background:var(--bg-hover); border:1px solid var(--border); }
.home-act-num { min-width:22px; height:22px; border-radius:50%; background:var(--ai); color:white; font-size:0.65rem; font-weight:800; display:flex; align-items:center; justify-content:center; flex-shrink:0; margin-top:1px; }
.home-act-text { font-size:0.86rem; color:var(--text); line-height:1.45; }

/* ===== カスタムトップナビ ボタンスタイル ===== */
/* _render_top_nav() の st.button をタブ風に見せる */
[data-testid="stHorizontalBlock"] [data-testid="stBaseButton-secondary"] {
  background: #161922 !important;
  color: #C4C9D4 !important;
  border: 1px solid #1E2230 !important;
  border-radius: 8px !important;
  font-size: 0.84rem !important;
  font-weight: 600 !important;
  padding: 8px 4px !important;
}
[data-testid="stHorizontalBlock"] [data-testid="stBaseButton-secondary"]:hover {
  background: #1C1F2B !important;
  color: #E8ECF1 !important;
  border-color: #2D3A5E !important;
}
[data-testid="stHorizontalBlock"] [data-testid="stBaseButton-primary"] {
  background: #6366F1 !important;
  color: #fff !important;
  border: none !important;
  border-radius: 8px !important;
  font-size: 0.84rem !important;
  font-weight: 700 !important;
}
</style>
''', unsafe_allow_html=True)


# ============================================================
# Plotly チャートヘルパー（アニメーション付き）
# ============================================================

def _make_gauge(value: float, max_val: float, title: str,
                green_end: float = None, amber_end: float = None,
                suffix: str = '', height: int = 160,
                steps: list = None) -> go.Figure:
    """インジゴ色ゲージチャート（transition 800ms アニメーション）"""
    if steps is None:
        if green_end is None:
            green_end = max_val * 0.5
        if amber_end is None:
            amber_end = max_val * 0.75
        steps = [
            {'range': [0, green_end],          'color': 'rgba(34,197,94,0.13)'},
            {'range': [green_end, amber_end],   'color': 'rgba(245,158,11,0.13)'},
            {'range': [amber_end, max_val],     'color': 'rgba(239,68,68,0.13)'},
        ]
    fig = go.Figure(go.Indicator(
        mode='gauge+number',
        value=value,
        number={'suffix': suffix,
                'font': {'size': 20, 'color': '#E8ECF1'},
                'valueformat': '.1f'},
        title={'text': title, 'font': {'size': 10, 'color': '#9BA1B0'}},
        gauge={
            'axis': {'range': [0, max_val], 'tickcolor': '#5C6370',
                     'tickfont': {'size': 8, 'color': '#5C6370'}, 'nticks': 4},
            'bar': {'color': '#6366F1', 'thickness': 0.28},
            'bgcolor': '#0F1219',
            'bordercolor': '#1E2230',
            'steps': steps,
        },
    ))
    fig.update_layout(
        paper_bgcolor='#161922',
        font_color='#9BA1B0',
        margin=dict(l=20, r=20, t=42, b=8),
        height=height,
        transition={'duration': 800, 'easing': 'cubic-in-out'},
    )
    return fig


def _make_indicator(value: float, title: str,
                    suffix: str = '%', ref: float = 0.0,
                    height: int = 155) -> go.Figure:
    """数値＋デルタ インジケーター（transition 600ms アニメーション）"""
    color = '#22C55E' if value >= ref else '#EF4444'
    fig = go.Figure(go.Indicator(
        mode='number+delta',
        value=value,
        delta={
            'reference': ref,
            'valueformat': '.2f',
            'suffix': suffix,
            'increasing': {'color': '#22C55E'},
            'decreasing': {'color': '#EF4444'},
        },
        number={
            'suffix': suffix,
            'font': {'size': 28, 'color': color},
            'valueformat': '+.2f',
        },
        title={'text': title, 'font': {'size': 11, 'color': '#9BA1B0'}},
    ))
    fig.update_layout(
        paper_bgcolor='#161922',
        font_color='#9BA1B0',
        margin=dict(l=10, r=10, t=32, b=8),
        height=height,
        transition={'duration': 600, 'easing': 'cubic-in-out'},
    )
    return fig


# ============================================================
# 共通 AI 解説ウィジェット
# ============================================================

def _render_ai_explain(section_label: str, context: dict, key: str,
                       figures: list = None):
    """✦ AI が解説 ボタン + Haiku ストリーミング + アニメーションチャート"""
    btn_key   = f'ai_explain_btn_{key}'
    state_key = f'ai_explain_text_{key}'

    col_btn, _ = st.columns([1, 4])
    with col_btn:
        if st.button('✦ AI が解説', key=btn_key, use_container_width=True):
            st.session_state[state_key] = '__loading__'

    if st.session_state.get(state_key) == '__loading__':
        placeholder = st.empty()
        system = f"""あなたはALMANAC AIアシスタントです。
{section_label}の現在のデータを見て、ユーザーに向けて日本語で解説してください。
- 150〜200字程度、箇条書き不要、自然な文章で
- 具体的な数値を引用
- 注目点と注意点を1つずつ含める
- 末尾に「投資判断の最終責任はご自身にあります」を添える"""
        user_msg = f"現在の{section_label}データ:\n{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"
        full_text = ''
        started = time.monotonic()
        try:
            client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
            with client.messages.stream(
                model=AI_EXPLAIN_MODEL,
                max_tokens=400,
                system=system,
                messages=[{'role': 'user', 'content': user_msg}],
            ) as stream:
                for chunk in stream.text_stream:
                    full_text += chunk
                    placeholder.markdown(f'''
<div class="ai-decision-card">
  <div class="ai-card-header">
    <span class="ai-sparkle">✦</span>
    <span class="ai-label">AI 解説 · {section_label}</span>
    <span class="ai-badge high">● LIVE</span>
  </div>
	  <div style="font-size:0.88rem;color:var(--text-sub);line-height:1.75;">{full_text}▌</div>
	</div>''', unsafe_allow_html=True)
                final_message = stream.get_final_message() if hasattr(stream, 'get_final_message') else None
            _log_ai_explain_usage(
                started=started,
                section_label=section_label,
                system=system,
                user_msg=user_msg,
                response=final_message,
            )
            st.session_state[state_key] = full_text
            st.rerun()   # ← チャートを即座に表示するため再レンダリング
        except Exception as e:
            _log_ai_explain_usage(
                started=started,
                section_label=section_label,
                system=system,
                user_msg=user_msg,
                status='error',
                error=e,
            )
            st.session_state[state_key] = f'エラー: {e}'

    elif st.session_state.get(state_key):
        text = st.session_state[state_key]
        st.markdown(f'''
<div class="ai-decision-card">
  <div class="ai-card-header">
    <span class="ai-sparkle">✦</span>
    <span class="ai-label">AI 解説 · {section_label}</span>
    <span class="ai-badge high">✓ Haiku</span>
  </div>
  <div style="font-size:0.88rem;color:var(--text-sub);line-height:1.75;">{text}</div>
</div>''', unsafe_allow_html=True)

        # ── アニメーションチャートをテキストの直下に表示 ──
        if figures:
            _fcols = st.columns(len(figures))
            for _i, (_fc, _fig) in enumerate(zip(_fcols, figures)):
                with _fc:
                    st.plotly_chart(_fig, use_container_width=True,
                                    key=f'ai_fig_{key}_{_i}')

        if st.button('🔄 再解説', key=f'ai_explain_retry_{key}'):
            st.session_state[state_key] = '__loading__'
            st.rerun()


# ============================================================
# サイドバー
# ============================================================

def render_sidebar():
    with st.sidebar:
        # ===== ブランドヘッダー =====
        st.markdown('''
<div style="padding:12px 4px 10px;">
  <div style="font-size:1.2rem; font-weight:800; color:#f1f5f9; letter-spacing:0.04em;">
    ALMANAC <span style="color:#3b82f6; font-size:0.9rem;">v4.0</span>
  </div>
  <div style="display:flex; align-items:center; gap:6px; margin-top:4px;">
    <span style="width:7px;height:7px;border-radius:50%;background:#22c55e;display:inline-block;"></span>
    <span style="font-size:0.7rem; color:#64748b; font-weight:600; letter-spacing:0.08em;">PORTFOLIO COMMAND CENTER</span>
  </div>
</div>
''', unsafe_allow_html=True)
        st.divider()

        # ===== レジームバッジ（コンパクト） =====
        regime = load_regime_state()
        spy_above = bool(regime.get('spy_above', False))
        nk_above  = bool(regime.get('nk_above', False))
        if spy_above and nk_above:
            r_label, r_cls, r_desc = 'A_強気', 'regime-A', '全戦略有効・積極姿勢'
        elif not spy_above and not nk_above:
            r_label, r_cls, r_desc = 'C_弱気', 'regime-C', '守りモード・新規買い禁止'
        else:
            r_label, r_cls, r_desc = 'B_中立', 'regime-B', '慎重運用・逆張り中心'
        r_updated = regime.get('updated', '–')
        st.markdown(f'''
<div style="margin-bottom:4px;">
  <div class="sb-label">相場レジーム</div>
  <span class="regime-badge {r_cls}">{r_label}</span>
  <div style="font-size:0.72rem; color:#64748b; margin-top:5px;">{r_desc}</div>
  <div style="font-size:0.62rem; color:#475569; margin-top:2px;">更新: {r_updated}</div>
</div>
''', unsafe_allow_html=True)
        st.divider()

        # ===== ポートフォリオ総額 =====
        st.markdown('<div class="sb-label">ポートフォリオ総額</div>', unsafe_allow_html=True)
        portfolio_total = int(_session_portfolio_total())
        portfolio_total = st.number_input(
            '総額（円）', min_value=0, max_value=1_000_000_000,
            value=portfolio_total, step=100_000, format='%d',
            key='portfolio_total_input', label_visibility='collapsed',
        )
        st.session_state['portfolio_total'] = portfolio_total
        st.markdown(
            f'<div style="font-size:1.1rem; font-weight:700; color:#f1f5f9; margin-top:2px;">¥{portfolio_total/10000:.0f}<span style="font-size:0.75rem; color:#64748b; margin-left:4px;">万円</span></div>',
            unsafe_allow_html=True)
        st.divider()

        st.divider()

        # ===== チャットトグル =====
        chat_open = st.session_state.get('chat_open', False)
        new_val = st.toggle('💬 AIチャットパネル', value=chat_open, key='sb_chat_toggle')
        if new_val != chat_open:
            st.session_state['chat_open'] = new_val
            st.rerun()

        st.divider()

        # ===== データ更新 =====
        if st.button('🔄 データ更新', use_container_width=True, key='sb_refresh'):
            st.cache_data.clear()
            st.rerun()
        st.markdown(
            f'<div style="font-size:0.68rem; color:#475569; text-align:center; margin-top:4px;">'
            f'最終更新: {datetime.now().strftime("%H:%M:%S")}</div>',
            unsafe_allow_html=True)


# ============================================================
# KPI 行（常時表示）
# ============================================================

def render_kpi_row():
    """ダッシュボード上部のKPIメトリクス行を表示する。"""
    portfolio_total = _session_portfolio_total()
    account         = load_account()
    trade_df        = load_trade_history()

    # P&L 計算（簡易）
    today_pnl    = 0.0
    today_pnl_pct = 0.0
    mtd_return   = 0.0
    ytd_return   = 0.0
    sharpe       = 0.0
    max_dd       = 0.0
    var_jpy      = 0

    if not trade_df.empty:
        date_col = next((c for c in trade_df.columns if 'date' in c.lower()), None)
        pnl_col  = next((c for c in trade_df.columns if 'pnl' in c.lower() or 'profit' in c.lower()), None)
        if date_col and pnl_col:
            trade_df[date_col] = pd.to_datetime(trade_df[date_col], errors='coerce')
            today_trades = trade_df[trade_df[date_col].dt.date == date.today()]
            today_pnl = float(today_trades[pnl_col].sum()) if not today_trades.empty else 0.0
            today_pnl_pct = today_pnl / portfolio_total if portfolio_total > 0 else 0.0

            # MTD
            this_month = datetime.now().replace(day=1)
            mtd_trades = trade_df[trade_df[date_col] >= this_month]
            mtd_return = float(mtd_trades[pnl_col].sum()) / portfolio_total if portfolio_total > 0 else 0.0

            # YTD
            this_year = datetime.now().replace(month=1, day=1)
            ytd_trades = trade_df[trade_df[date_col] >= this_year]
            ytd_return = float(ytd_trades[pnl_col].sum()) / portfolio_total if portfolio_total > 0 else 0.0

    # VaR（リスクエンジン）
    var_result = None
    if risk_engine and not trade_df.empty:
        pnl_col = next((c for c in trade_df.columns if 'pnl' in c.lower() or 'profit' in c.lower()), None)
        if pnl_col:
            returns = trade_df[pnl_col] / portfolio_total
            returns = returns.dropna()
            if len(returns) >= 20:
                var_result = risk_engine.calculate_var_cornish_fisher(returns, 0.95, portfolio_total)
                var_jpy    = var_result.get('var_jpy', 0)
                dd_result  = risk_engine.calculate_drawdown(returns)
                max_dd     = dd_result.get('current_dd', 0.0)
                perf       = risk_engine.calculate_performance_metrics(returns)
                sharpe     = perf.get('sharpe_12m', 0.0)

    # 表示（カードスタイル）
    balance  = _account_total_cash_jpy(account)
    pnl_cls  = 'pos' if today_pnl >= 0 else 'neg'
    pnl_sign = '+' if today_pnl >= 0 else ''
    mtd_cls  = 'pos' if mtd_return >= 0 else 'neg'
    ytd_cls  = 'pos' if ytd_return >= 0 else 'neg'
    dd_cls   = 'neg' if max_dd < -0.10 else 'pos'

    kpi_html = f'''
<div style="display:grid; grid-template-columns:repeat(8,1fr); gap:10px; margin-bottom:4px;">
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">本日 P&L</div>
    <div class="nt-card-value" style="font-size:1.15rem;">¥{today_pnl:+,.0f}</div>
    <div class="nt-card-delta {pnl_cls}">{pnl_sign}{today_pnl_pct*100:.2f}%</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">MTD</div>
    <div class="nt-card-value" style="font-size:1.15rem;">{mtd_return*100:+.2f}%</div>
    <div class="nt-card-delta {mtd_cls}">月次リターン</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">YTD</div>
    <div class="nt-card-value" style="font-size:1.15rem;">{ytd_return*100:+.2f}%</div>
    <div class="nt-card-delta {ytd_cls}">年次リターン</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">Sharpe (12M)</div>
    <div class="nt-card-value" style="font-size:1.15rem;">{sharpe:.2f}</div>
    <div class="nt-card-delta" style="color:#94a3b8;">リスク調整後</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">ドローダウン</div>
    <div class="nt-card-value" style="font-size:1.15rem;">{max_dd*100:.1f}%</div>
    <div class="nt-card-delta {dd_cls}">現在DD</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">VaR (95%)</div>
    <div class="nt-card-value" style="font-size:1.15rem;">{"¥{:,.0f}".format(var_jpy) if var_jpy else "N/A"}</div>
    <div class="nt-card-delta neg">1日最大損失</div>
  </div>
  <div class="nt-card" style="padding:14px 12px; border-color:#3b82f6;">
    <div class="nt-card-title">総資産</div>
    <div class="nt-card-value" style="font-size:1.15rem; color:#60a5fa;">¥{portfolio_total/10000:.0f}万</div>
    <div class="nt-card-delta" style="color:#94a3b8;">全口座合計</div>
  </div>
  <div class="nt-card" style="padding:14px 12px;">
    <div class="nt-card-title">現金</div>
    <div class="nt-card-value" style="font-size:1.15rem;">¥{balance/10000:.0f}万</div>
    <div class="nt-card-delta" style="color:#94a3b8;">待機資金</div>
  </div>
</div>
'''
    st.markdown(kpi_html, unsafe_allow_html=True)

    # ドローダウン警告バナー
    if max_dd <= -0.35:
        st.error('🚨 ドローダウン -35%: 全現金化を強く推奨します')
    elif max_dd <= -0.25:
        st.warning('⚠️ ドローダウン -25%: 全ポジション 50% 縮小を推奨します')


# ============================================================
# タブ 1: ポートフォリオ総覧
# ============================================================

def render_tab_portfolio():
    st.subheader('ポートフォリオ総覧')

    # ポートフォリオスナップショット（portfolio_manager使用）
    snapshot = None
    if portfolio_mgr:
        with st.spinner('ポートフォリオデータを取得中...'):
            try:
                snapshot = portfolio_mgr.build_portfolio_snapshot()
            except Exception as e:
                st.warning(f'スナップショット取得エラー: {e}')

    portfolio_total = _session_portfolio_total()

    if snapshot:
        total_jpy = snapshot.get('total_jpy', portfolio_total)
        st.session_state['portfolio_total'] = total_jpy

        # ---- 概要メトリクス ----
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric('総資産', f'¥{total_jpy/10000:.0f}万')
        with col2:
            st.metric('現金', f'¥{snapshot["cash_jpy"]/10000:.0f}万')
        with col3:
            st.metric('USD/JPY', f'{snapshot["fx_rate"]:.2f}')
        with col4:
            st.metric('ポジション数', len(snapshot['positions']))

        st.divider()

        # ---- ポジションテーブル ----
        positions = snapshot.get('positions', [])
        if positions:
            type_label = {'short': '短期', 'medium': '中期', 'long': '長期'}
            rows = []
            for p in positions:
                rows.append({
                    '銘柄':     p['key'],
                    '名称':     p['name'],
                    '区分':     type_label.get(p['investment_type'], p['investment_type']),
                    '口座':     p['account'],
                    '評価額':   f'¥{p["value_jpy"]:,.0f}',
                    '含み損益': f'¥{p["unrealized_jpy"]:+,.0f}',
                    '損益率':   f'{p["unrealized_pct"]*100:+.1f}%',
                    '比率':     f'{p["value_jpy"]/total_jpy*100:.1f}%' if total_jpy > 0 else '-',
                    'セクター': p['sector'],
                })
            df_pos = pd.DataFrame(rows)
            # 含み損益でカラー表示
            st.dataframe(df_pos, use_container_width=True, hide_index=True)

        # ── ポートフォリオ解説用チャート ──
        _cb   = snapshot.get('currency_breakdown', {})
        _sb   = snapshot.get('sector_breakdown', {})
        _usd_ratio  = _cb.get('USD', {}).get('ratio', 0) * 100
        _tech_ratio = _sb.get('Technology', {}).get('ratio', 0) * 100
        _fig_port_usd = _make_gauge(
            _usd_ratio, 100.0, 'USD 配分 %',
            steps=[
                {'range': [0,    60],  'color': 'rgba(245,158,11,0.13)'},
                {'range': [60,   70],  'color': 'rgba(34,197,94,0.18)'},
                {'range': [70,   100], 'color': 'rgba(239,68,68,0.13)'},
            ],
            suffix='%',
        )
        _fig_port_tech = _make_gauge(
            _tech_ratio, 60.0, 'テック比率 %',
            green_end=30.0, amber_end=45.0, suffix='%',
        )
        _render_ai_explain(
            section_label='ポートフォリオ総覧',
            context={
                '総資産': f'¥{total_jpy/10000:.0f}万円',
                '現金': f'¥{snapshot.get("cash_jpy", 0)/10000:.0f}万円',
                'USD/JPY': snapshot.get('fx_rate', 0),
                'ポジション数': len(snapshot.get('positions', [])),
                '通貨配分': {k: f'{v["ratio"]*100:.1f}%' for k, v in _cb.items()},
                'セクター配分上位5': {k: f'{v["ratio"]*100:.1f}%' for k, v in list(_sb.items())[:5]},
                'リバランス要否': bool(portfolio_mgr and portfolio_mgr.get_rebalance_triggers(snapshot)),
            },
            key='portfolio',
            figures=[_fig_port_usd, _fig_port_tech],
        )
        st.divider()

        # ---- 通貨配分・セクター配分 ----
        col1, col2 = st.columns(2)
        with col1:
            st.markdown('**通貨配分（目標: USD 60-70% / JPY 30-40%）**')
            cb = snapshot.get('currency_breakdown', {})
            cur_rows = []
            for ccy, vals in cb.items():
                ratio = vals['ratio'] * 100
                target_min, target_max = {'USD': (60, 70), 'JPY': (30, 40)}.get(ccy, (0, 100))
                status = '✅' if target_min <= ratio <= target_max else '⚠️'
                cur_rows.append({
                    '通貨': ccy,
                    '評価額': f'¥{vals["value_jpy"]/10000:.0f}万',
                    '比率': f'{ratio:.1f}%',
                    '目標': f'{target_min}-{target_max}%',
                    '状態': status,
                })
            st.dataframe(pd.DataFrame(cur_rows), use_container_width=True, hide_index=True)
            if cb:
                pie_labels = list(cb.keys())
                pie_values = [cb[k]['ratio'] * 100 for k in pie_labels]
                pie_colors = ['#6366F1', '#22C55E', '#F59E0B', '#EF4444', '#3B82F6'][:len(pie_labels)]
                fig_pie = go.Figure(go.Pie(
                    labels=pie_labels,
                    values=pie_values,
                    hole=0.55,
                    marker=dict(colors=pie_colors, line=dict(color='#0F1219', width=2)),
                    textinfo='label+percent',
                    textfont=dict(size=11, color='#E8ECF1'),
                ))
                fig_pie.update_layout(
                    paper_bgcolor='#161922',
                    plot_bgcolor='#161922',
                    font_color='#9BA1B0',
                    margin=dict(l=0, r=0, t=10, b=0),
                    height=200,
                    showlegend=False,
                    transition={'duration': 800, 'easing': 'cubic-in-out'},
                )
                st.plotly_chart(fig_pie, use_container_width=True, key='port_pie')

        with col2:
            st.markdown('**セクター配分（上位5件）**')
            sb = snapshot.get('sector_breakdown', {})
            sec_rows = []
            for sector, vals in list(sb.items())[:8]:
                ratio = vals['ratio'] * 100
                sec_rows.append({
                    'セクター': sector,
                    '評価額': f'¥{vals["value_jpy"]/10000:.0f}万',
                    '比率': f'{ratio:.1f}%',
                })
            if sec_rows:
                st.dataframe(pd.DataFrame(sec_rows), use_container_width=True, hide_index=True)
            top5 = list(sb.items())[:5]
            if top5:
                bar_labels = [s for s, _ in top5]
                bar_values = [v['ratio'] * 100 for _, v in top5]
                fig_bar = go.Figure(go.Bar(
                    x=bar_values,
                    y=bar_labels,
                    orientation='h',
                    marker=dict(
                        color=bar_values,
                        colorscale=[[0, '#1E2230'], [1, '#6366F1']],
                        line=dict(color='#0F1219', width=1),
                    ),
                    text=[f'{v:.1f}%' for v in bar_values],
                    textposition='outside',
                    textfont=dict(color='#9BA1B0', size=10),
                ))
                fig_bar.update_layout(
                    paper_bgcolor='#161922',
                    plot_bgcolor='#0F1219',
                    font_color='#9BA1B0',
                    margin=dict(l=0, r=40, t=10, b=0),
                    height=200,
                    xaxis=dict(showgrid=False, visible=False),
                    yaxis=dict(gridcolor='#1E2230', color='#9BA1B0', autorange='reversed'),
                    transition={'duration': 800, 'easing': 'cubic-in-out'},
                )
                st.plotly_chart(fig_bar, use_container_width=True, key='port_bar')

        # ---- テック集中解消進捗 ----
        tech_vals = snapshot['sector_breakdown'].get('Technology', {})
        tech_ratio = tech_vals.get('ratio', 0)
        st.markdown('**テック集中解消進捗（目標: 30%）**')
        progress = max(0.0, min(1.0, 1 - (tech_ratio - 0.30) / max(tech_ratio - 0.30, 0.001)))
        st.progress(progress)
        delta_color = 'inverse' if tech_ratio > 0.35 else 'normal'
        st.caption(f'テック比率: {tech_ratio*100:.1f}% → 目標: 30%（5-7年・自然解消）')
        if tech_ratio > 0.35:
            st.warning(f'⚠️ テック比率が{tech_ratio*100:.0f}%と集中しています。新規購入はテック以外を優先してください。')

        # ---- リバランストリガー ----
        triggers = portfolio_mgr.get_rebalance_triggers(snapshot)
        if triggers:
            st.divider()
            st.markdown('**リバランストリガー**')
            for t in triggers:
                icon = {'critical': '🔴', 'warning': '⚠️', 'info': 'ℹ️'}.get(t.get('level', 'info'), 'ℹ️')
                st.write(f'{icon} {t["message"]}')

        st.caption(f'データ取得日時: {snapshot.get("as_of", "不明")}')

    else:
        # フォールバック: holdings.jsonから簡易表示
        fx = _account_fx_rate(load_account())
        holdings = load_holdings()
        if holdings:
            rows = []
            for ticker, info in holdings.items():
                if not isinstance(info, dict) or ticker in {'CASH_JPY', 'CASH_USD'}:
                    continue
                shares   = info.get('shares', 0)
                entry_px = info.get('entry_price', 0)
                value    = _holding_value_jpy(ticker, info, fx)
                rows.append({
                    '銘柄':     ticker,
                    '名称':     info.get('name', ticker),
                    '株数':     shares,
                    '取得単価': f'¥{entry_px:,.0f}',
                    '評価額':   f'¥{value:,.0f}',
                    '口座':     info.get('account', '-'),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info('保有ポジションがありません。holdings.json を確認してください。')


# ============================================================
# タブ 2: リスク管理
# ============================================================

def render_tab_risk():
    st.subheader('リスク管理')

    portfolio_total = _session_portfolio_total()
    trade_df        = load_trade_history()

    if risk_engine is None:
        st.error('risk_engine.py の読み込みに失敗しました')
        return

    pnl_col  = next((c for c in trade_df.columns if 'pnl' in c.lower() or 'profit' in c.lower()), None) if not trade_df.empty else None
    date_col = next((c for c in trade_df.columns if 'date' in c.lower()), None) if not trade_df.empty else None

    has_returns = (pnl_col is not None and len(trade_df) >= 20)
    returns_series = None

    if has_returns:
        returns_series = (trade_df[pnl_col] / portfolio_total).dropna()

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown('**VaR / CVaR（Cornish-Fisher補正）**')
        if has_returns and returns_series is not None:
            for conf in [0.95, 0.99]:
                var_r = risk_engine.calculate_var_cornish_fisher(returns_series, conf, portfolio_total)
                cvar_r = risk_engine.calculate_cvar(returns_series, conf, portfolio_total)
                st.metric(
                    f'VaR ({int(conf*100)}%)',
                    f'¥{var_r.get("var_jpy", 0):,.0f}',
                    f'{var_r.get("var_pct", 0)*100:.2f}%',
                )
                st.metric(
                    f'CVaR ({int(conf*100)}%)',
                    f'¥{cvar_r.get("cvar_jpy", 0):,.0f}',
                    f'{cvar_r.get("cvar_pct", 0)*100:.2f}%',
                )
                st.caption(
                    f'歪度: {var_r.get("skewness", 0):.3f} / '
                    f'尖度: {var_r.get("kurtosis", 0):.3f} / '
                    f'CF補正: {var_r.get("cf_adjustment", 0)*100:.3f}%'
                )
        else:
            st.info('取引履歴が20件以上になるとVaRを計算します')
            st.metric('VaR(95%) 健全水準（非公開規模）', '非公開')

    with col2:
        st.markdown('**ドローダウン**')
        if has_returns and returns_series is not None:
            dd_result = risk_engine.calculate_drawdown(returns_series)
            current_dd = dd_result['current_dd']
            max_dd     = dd_result['max_dd']
            alert      = dd_result['alert_level']

            color = {'normal': '🟢', 'warning': '🟡', 'critical': '🔴'}.get(alert, '⚪')
            st.metric('現在DD', f'{current_dd*100:.1f}%', delta_color='inverse')
            st.metric('最大DD', f'{max_dd*100:.1f}%')
            st.write(f'{color} {dd_result["action"]}')

            # ドローダウン時系列チャート
            dd_series = dd_result['drawdown_series']
            if date_col and len(dd_series) == len(trade_df):
                chart_df = pd.DataFrame({
                    'date': pd.to_datetime(trade_df[date_col], errors='coerce').values,
                    'drawdown': dd_series.values * 100,
                }).dropna()
                if not chart_df.empty:
                    fig_dd = go.Figure()
                    fig_dd.add_trace(go.Scatter(
                        x=chart_df['date'],
                        y=chart_df['drawdown'],
                        fill='tozeroy',
                        fillcolor='rgba(239,68,68,0.15)',
                        line=dict(color='#EF4444', width=2),
                        name='ドローダウン',
                    ))
                    fig_dd.update_layout(
                        paper_bgcolor='#161922',
                        plot_bgcolor='#0F1219',
                        font_color='#9BA1B0',
                        margin=dict(l=0, r=0, t=10, b=0),
                        height=200,
                        showlegend=False,
                        transition={'duration': 800, 'easing': 'cubic-in-out'},
                        xaxis=dict(showgrid=False, color='#5C6370'),
                        yaxis=dict(gridcolor='#1E2230', color='#5C6370', tickformat='.1f', ticksuffix='%'),
                    )
                    st.plotly_chart(fig_dd, use_container_width=True, key='risk_dd')
        else:
            st.info('取引履歴が蓄積されるとドローダウンを表示します')
            st.metric('-25%到達', '全ポジション50%縮小アラート')
            st.metric('-35%到達', '全現金化推奨アラート')

    with col3:
        st.markdown('**行動ガードレール**')
        guardrails = risk_engine.evaluate_behavioral_guardrails(
            daily_pnl_pct=0.0,
            monthly_pnl_pct=0.0,
            active_trades=len(load_holdings()),
            short_positions=0,
        )
        status_icon = '🟢' if guardrails['trading_allowed'] else '🔴'
        st.write(f'{status_icon} 取引ステータス: {"正常" if guardrails["trading_allowed"] else "停止"}')
        entry_icon = '🟢' if guardrails['new_entry_allowed'] else '🟡'
        st.write(f'{entry_icon} 新規エントリー: {"可能" if guardrails["new_entry_allowed"] else "禁止"}')
        if guardrails['alerts']:
            for alert in guardrails['alerts']:
                level = alert['level']
                icon  = {'critical': '🔴', 'warning': '🟡', 'info': 'ℹ️'}.get(level, '⚪')
                st.write(f'{icon} {alert["message"]}')

    try:
        _risk_ctx = {
            '総資産': f'¥{portfolio_total/10000:.0f}万円',
            'レジーム': load_regime_state().get('regime', '不明'),
            'ガードレール_取引OK': guardrails['trading_allowed'],
            'ガードレール_新規OK': guardrails['new_entry_allowed'],
        }
        _fig_risk_var = None
        _fig_risk_dd  = None
        if has_returns and returns_series is not None:
            _v95 = risk_engine.calculate_var_cornish_fisher(returns_series, 0.95, portfolio_total)
            _c95 = risk_engine.calculate_cvar(returns_series, 0.95, portfolio_total)
            _dd  = risk_engine.calculate_drawdown(returns_series)
            _var_pct = abs(_v95.get('var_pct', 0) * 100)
            _dd_pct  = abs(_dd.get('current_dd', 0) * 100)
            _risk_ctx.update({
                'VaR_95%': f'¥{_v95.get("var_jpy", 0):,.0f}',
                'CVaR_95%': f'¥{_c95.get("cvar_jpy", 0):,.0f}',
                '最大DD': f'{_dd.get("max_dd", 0)*100:.1f}%',
                '現在DD': f'{_dd_pct:.1f}%',
            })
            _fig_risk_var = _make_gauge(
                _var_pct, 5.0, 'VaR 95% (対資産 %)',
                green_end=2.0, amber_end=3.5, suffix='%',
            )
            _fig_risk_dd = _make_gauge(
                _dd_pct, 35.0, '現在ドローダウン %',
                green_end=10.0, amber_end=25.0, suffix='%',
            )
        else:
            _risk_ctx['VaR'] = 'データ不足（20件以上で計算）'
    except Exception:
        _risk_ctx = {'総資産': f'¥{portfolio_total/10000:.0f}万円', 'ステータス': '計算中'}
        _fig_risk_var = None
        _fig_risk_dd  = None
    _risk_figs = [f for f in [_fig_risk_var, _fig_risk_dd] if f is not None]
    _render_ai_explain(section_label='リスク管理', context=_risk_ctx, key='risk',
                       figures=_risk_figs or None)

    st.divider()

    # ストレステスト
    st.markdown('**ストレステスト**')
    positions = {}
    holdings  = load_holdings()
    fx = _account_fx_rate(load_account())
    for ticker, info in holdings.items():
        if isinstance(info, dict) and info.get('investment_type') != 'cash' and not str(ticker).startswith('CASH_'):
            positions[ticker] = {
                'value_jpy': _holding_value_jpy(ticker, info, fx),
                'currency':  str(info.get('currency', 'JPY') or 'JPY').upper(),
            }

    if positions:
        stress = risk_engine.run_stress_test(positions, portfolio_total)
        stress_rows = []
        for name, result in stress.items():
            stress_rows.append({
                'シナリオ':  name,
                '損失額':    f'¥{result["loss_jpy"]:,.0f}',
                '損失率':    f'{result["loss_pct"]*100:.1f}%',
                '評価':      {'normal': '🟢 安全', 'warning': '🟡 注意', 'critical': '🔴 危険'}.get(result['survival'], ''),
            })
        st.dataframe(pd.DataFrame(stress_rows), use_container_width=True, hide_index=True)
    else:
        st.info('保有ポジションがあるとストレステストを実行できます')

    # HMMレジーム（トレード履歴があれば）
    st.divider()
    st.markdown('**HMMレジーム状態**')
    if has_returns and returns_series is not None and len(returns_series) >= 60:
        regime_result = risk_engine.detect_regime_hmm(returns_series)
        if 'error' not in regime_result:
            col_a, col_b = st.columns(2)
            with col_a:
                label   = regime_result['current_label']
                icon    = {'Bull': '🟢', 'Neutral': '🟡', 'Bear': '🔴'}.get(label, '⚪')
                st.metric('現在の市場状態（HMM）', f'{icon} {label}')
            with col_b:
                probs = regime_result['state_probs']
                for state, prob in probs.items():
                    icon = {'Bull': '🟢', 'Neutral': '🟡', 'Bear': '🔴'}.get(state, '⚪')
                    st.write(f'{icon} {state}: {prob*100:.1f}%')
        else:
            st.info(regime_result.get('error', 'HMM計算エラー'))
    else:
        regime = load_regime_state()
        if regime:
            current = regime.get('regime', '不明')
            label_map = {'A_強気': '🟢 A_強気', 'B_中立': '🟡 B_中立', 'C_弱気': '🔴 C_弱気'}
            st.info(f'現在のレジーム（スクリーナー判定）: {label_map.get(current, current)}')
        else:
            st.info('60件以上のリターン系列でHMMレジーム検知が使用可能になります')


# ============================================================
# タブ 3: 短期トレード
# ============================================================

def _signal_status(entry: float, current: float | None, target: float, stop: float) -> tuple[str, str, float]:
    """シグナルのステータスと色を返す (label, css_color, progress 0-1)"""
    if current is None:
        return '価格取得中', '#475569', 0.5
    pct = (current - entry) / entry * 100 if entry > 0 else 0
    if current >= target:
        return f'🎯 目標達成 (+{pct:.1f}%)', '#00c896', 1.0
    if current <= stop:
        return f'🛑 損切りライン ({pct:.1f}%)', '#ef4444', 0.0
    if pct >= 0:
        # entry → current の進捗（0→targetまで）
        progress = (current - entry) / (target - entry) if target > entry else 0.5
        return f'▲ 上昇中 (+{pct:.1f}%)', '#4ade80', max(0.0, min(1.0, progress))
    else:
        # 下落中（entryからstopの距離に対する位置）
        progress = (current - stop) / (entry - stop) if entry > stop else 0.5
        return f'▼ 下落中 ({pct:.1f}%)', '#f87171', max(0.0, min(1.0, progress))


def render_tab_short_trade():
    st.subheader('短期トレード')

    # ===== AIシグナル =====
    signals = load_signals_log()

    col_hd, col_btn = st.columns([4, 1])
    with col_hd:
        st.markdown(f'''
<div style="display:flex; align-items:center; gap:12px;">
  <span style="font-size:1rem; font-weight:700; color:#e2e8f0;">AI シグナル</span>
  <span style="font-size:0.72rem; color:#94a3b8; background:#1a2035; border:1px solid #2d3a5e;
    border-radius:999px; padding:2px 10px;">
    Sonnet×3討論 → Opus最終判断 → Telegram通知済み
  </span>
  <span style="font-size:0.72rem; color:#60a5fa;">{len(signals)} 件</span>
</div>
''', unsafe_allow_html=True)
    with col_btn:
        if st.button('🤖 今すぐ分析実行', use_container_width=True, type='primary'):
            with st.spinner('analyzer.py 実行中（1〜3分）...'):
                try:
                    import subprocess, sys
                    result = subprocess.run(
                        [str(BASE_DIR / 'venv/bin/python'), str(BASE_DIR / 'analyzer.py')],
                        capture_output=True, text=True, timeout=300,
                        env={**os.environ},
                    )
                    if result.returncode == 0:
                        st.success('分析完了。シグナルはTelegramに送信されました。')
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f'エラー:\n{result.stderr[-500:]}')
                except Exception as e:
                    st.error(f'実行エラー: {e}')

    if not signals:
        st.info('シグナルがまだありません。「今すぐ分析実行」を押すか、平日 8:00/17:00 の自動実行をお待ちください。')
    else:
        # シグナルを日付降順でソート
        sorted_signals = sorted(
            signals.items(),
            key=lambda x: x[1].get('signal_date', ''),
            reverse=True,
        )

        for ticker, sig in sorted_signals:
            entry  = float(sig.get('entry_price') or 0)
            target = float(sig.get('target_price') or 0)
            stop   = float(sig.get('stop_loss') or 0)
            score  = int(sig.get('score') or 0)
            stars  = '★' * score + '☆' * (5 - score)
            sig_date = sig.get('signal_date', '不明')
            holding  = sig.get('holding_period', '不明')
            reason   = sig.get('reason', '')

            # 現在価格取得（キャッシュ）
            current = fetch_current_price(ticker)
            status_label, status_color, progress = _signal_status(entry, current, target, stop)

            # 上値余地 / 下値リスク
            upside  = (target - entry) / entry * 100 if entry > 0 else 0
            downside= (entry - stop)   / entry * 100 if entry > 0 else 0
            rr      = upside / downside if downside > 0 else 0

            current_str = f'${current:,.2f}' if current else '取得中...'

            # カード HTML
            st.markdown(f'''
<div class="nt-card" style="border-left: 4px solid {status_color}; margin-bottom:14px;">
  <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
    <div>
      <span style="font-size:1.15rem; font-weight:800; color:#e2e8f0;">{ticker}</span>
      <span style="font-size:0.75rem; color:#94a3b8; margin-left:10px;">{sig_date} &nbsp;|&nbsp; 保有期間: {holding}</span>
    </div>
    <div style="display:flex; align-items:center; gap:10px;">
      <span style="color:#f59e0b; font-size:0.9rem; letter-spacing:1px;">{stars}</span>
      <span style="font-size:0.75rem; font-weight:700; padding:3px 10px; border-radius:999px;
        background:#0f1a2a; border:1px solid {status_color}; color:{status_color};">{status_label}</span>
    </div>
  </div>

  <div style="display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:14px 0 10px;">
    <div>
      <div class="nt-card-title">現在値</div>
      <div style="font-size:1.05rem; font-weight:700; color:#60a5fa;">{current_str}</div>
    </div>
    <div>
      <div class="nt-card-title">エントリー</div>
      <div style="font-size:1.05rem; font-weight:700; color:#e2e8f0;">${entry:,.2f}</div>
    </div>
    <div>
      <div class="nt-card-title">目標株価</div>
      <div style="font-size:1.05rem; font-weight:700; color:#00c896;">${target:,.2f} (+{upside:.1f}%)</div>
    </div>
    <div>
      <div class="nt-card-title">損切りライン</div>
      <div style="font-size:1.05rem; font-weight:700; color:#ef4444;">${stop:,.2f} (-{downside:.1f}%)</div>
    </div>
    <div>
      <div class="nt-card-title">リスクリワード</div>
      <div style="font-size:1.05rem; font-weight:700; color:{"#00c896" if rr >= 2 else "#f59e0b" if rr >= 1.5 else "#ef4444"};">{rr:.1f}倍</div>
    </div>
  </div>

  <div style="background:#0d1520; border-radius:6px; height:6px; margin-bottom:10px; overflow:hidden;">
    <div style="height:100%; width:{progress*100:.0f}%; background:linear-gradient(90deg,{status_color}88,{status_color}); border-radius:6px; transition:width 0.3s;"></div>
  </div>
  <div style="display:flex; justify-content:space-between; font-size:0.65rem; color:#475569; margin-bottom:8px;">
    <span>損切 ${stop:,.2f}</span><span>エントリー ${entry:,.2f}</span><span>目標 ${target:,.2f}</span>
  </div>

  <div style="font-size:0.78rem; color:#94a3b8; line-height:1.6; border-top:1px solid #2d3a5e; padding-top:8px;">
    💡 {reason}
  </div>
</div>
''', unsafe_allow_html=True)

    if signals:
        top3 = [
            {
                'ticker': t,
                'entry': float(s.get('entry_price') or 0),
                'target': float(s.get('target_price') or 0),
                'stop': float(s.get('stop_loss') or 0),
                'score': int(s.get('score') or 0),
                'date': s.get('signal_date', ''),
            }
            for t, s in list(signals.items())[:3]
        ]
        # ── 短期トレード解説用チャート ──
        _top_sig = top3[0] if top3 else {}
        _sig_score = float(_top_sig.get('score', 0))
        _sig_entry = float(_top_sig.get('entry', 0))
        _sig_target= float(_top_sig.get('target', 0))
        _sig_stop  = float(_top_sig.get('stop', 0))
        _sig_rr    = ((_sig_target - _sig_entry) / (_sig_entry - _sig_stop)
                      if _sig_entry > _sig_stop > 0 else 0.0)
        _fig_sig_score = _make_gauge(
            _sig_score, 5.0, f'シグナルスコア ({_top_sig.get("ticker","–")})',
            green_end=3.5, amber_end=4.5, suffix='pt',
        )
        _fig_sig_rr = _make_indicator(
            _sig_rr, 'リスクリワード比', suffix='倍', ref=2.0,
        )
        _render_ai_explain(
            section_label='AIシグナル / 短期トレード',
            context={'シグナル上位3件': top3, 'レジーム': load_regime_state().get('regime', '不明')},
            key='short_trade',
            figures=[_fig_sig_score, _fig_sig_rr],
        )

    st.divider()

    # ===== スクリーニング候補（生データ） =====
    with st.expander('スクリーニング候補（生データ）', expanded=False):
        col_sc1, col_sc2 = st.columns([3, 1])
        with col_sc1:
            st.caption('直近の screener.py 実行結果。上記 AI シグナルはこの候補を通過したもの。')
        with col_sc2:
            if st.button('スクリーニング実行', key='scr_btn'):
                with st.spinner('スクリーニング中...'):
                    try:
                        import subprocess, sys
                        r = subprocess.run(
                            [str(BASE_DIR / 'venv/bin/python'), str(BASE_DIR / 'screener.py')],
                            capture_output=True, text=True, timeout=180, env={**os.environ},
                        )
                        if r.returncode == 0:
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(r.stderr[-300:])
                    except Exception as e:
                        st.error(str(e))

        results = load_screen_results()
        if results:
            rows = []
            for r in results:
                rows.append({
                    '銘柄':      r.get('ticker', '-'),
                    '戦略':      r.get('strategy', '-'),
                    'RSI':       r.get('rsi', '-'),
                    '出来高比':  r.get('volume_ratio', '-'),
                    '1M騰落':   f"{r.get('mom_1m', 0):+.1f}%" if r.get('mom_1m') is not None else '-',
                    '理由':      r.get('reason', '-')[:40],
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info('スクリーニング結果なし')

    # ===== 保有中ポジション =====
    with st.expander('保有中ポジション', expanded=False):
        holdings = load_holdings()
        fx = _account_fx_rate(load_account())
        skip = {'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'IFREE_FANGPLUS', 'NOMURA_SEMI', 'AVGO_特定', 'AVGO_一般'}
        rows = []
        for ticker, info in holdings.items():
            if not isinstance(info, dict) or ticker in skip:
                continue
            entry_px = info.get('entry_price', 0)
            shares   = info.get('shares', 0)
            currency = info.get('currency', 'JPY')
            sym      = '$' if currency == 'USD' else '¥'
            rows.append({
                '銘柄':     ticker,
                '口座種別': info.get('investment_type', '-'),
                '株数':     shares,
                '取得単価': f'{sym}{entry_px:,.2f}',
                '評価額(JPY)': f'¥{_holding_value_jpy(ticker, info, fx):,.0f}',
                '取得日':   info.get('entry_date', '-'),
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info('保有中ポジションなし')

    # ===== レジームパラメータ =====
    with st.expander('現在のレジームパラメータ', expanded=False):
        regime = load_regime_state()
        if regime:
            st.json(regime)
        else:
            st.info('regime_state.json が見つかりません')


# ============================================================
# タブ 4: 持株会
# ============================================================

def render_tab_espp():
    st.subheader('持株会管理（9999.T）')

    if espp_mgr is None:
        st.error('espp_plan_manager.py の読み込みに失敗しました')
        return

    portfolio_total = _session_portfolio_total()
    dashboard_data  = espp_mgr.get_dashboard_data(portfolio_total)

    # メトリクス行
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric('保有株数', f'{dashboard_data["current_shares"]:.2f}株')
        st.metric('現在株価', f'¥{dashboard_data["current_price"]:,.0f}' if dashboard_data['current_price'] else 'N/A')
    with col2:
        st.metric('評価額', f'¥{dashboard_data["current_value"]:,.0f}')
        pnl     = dashboard_data['unrealized_pnl']
        pnl_pct = dashboard_data['unrealized_pnl_pct']
        st.metric('含み損益', f'¥{pnl:+,.0f}', f'{pnl_pct*100:+.2f}%', delta_color='normal' if pnl >= 0 else 'inverse')
    with col3:
        ratio = dashboard_data['portfolio_ratio']
        limit = dashboard_data['hold_limit_pct']
        ratio_color = 'inverse' if ratio > limit else 'normal'
        st.metric('ポートフォリオ比率', f'{ratio*100:.1f}%', f'上限 {limit*100:.0f}%', delta_color=ratio_color)
        # 上限ゲージ
        st.progress(min(ratio / limit, 1.0))
    with col4:
        st.metric('平均取得単価', f'¥{dashboard_data["avg_cost"]:,.0f}' if dashboard_data['avg_cost'] else 'N/A')
        eff_ret = dashboard_data.get('effective_return')
        if eff_ret is not None:
            st.metric('奨励金込み実質リターン', f'{eff_ret*100:+.2f}%')

    # アラート表示
    alert = dashboard_data['concentration_alert']
    msg   = dashboard_data['concentration_message']
    if alert == 'warning':
        st.warning(f'⚠️ {msg}')
    elif alert == 'caution':
        st.info(f'ℹ️ {msg}')

    st.divider()

    # 積立情報
    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown('**積立情報**')
        st.write(f'- 月額積立: ¥{dashboard_data["monthly_amount"]:,}')
        st.write(f'- 奨励金率: {dashboard_data["incentive_rate"]*100:.0f}%')
        st.write(f'- 累計積立額: ¥{dashboard_data["total_invested"]:,.0f}')
        st.write(f'- 累計奨励金: ¥{dashboard_data["total_incentive"]:,.0f}')
        st.write(f'- 次回積立予定: {dashboard_data["next_purchase_date"]}')

    with col_r:
        st.markdown('**四半期売却推奨**')
        sell_rec = dashboard_data['sell_recommendation']
        if sell_rec > 0:
            st.warning(f'売却推奨額: ¥{sell_rec:,.0f}')
        else:
            st.success('現在売却不要（10%以内）')

    st.divider()

    # Claude判断ボタン
    st.markdown('**「売る？持つ？」Claude分析**')
    col_btn, col_tax = st.columns(2)
    with col_btn:
        if st.button('Claude に判断を依頼', type='primary', use_container_width=True):
            with st.spinner('データ収集中...'):
                analysis = espp_mgr.espp_hold_or_sell_analysis(portfolio_total)
            st.session_state['espp_analysis'] = analysis
            st.info('分析データを準備しました。下記プロンプトをClaudeに送付してください。')

    with col_tax:
        if st.button('損出し機会チェック', use_container_width=True):
            with st.spinner('確認中...'):
                tax_result = espp_mgr.check_tax_harvest_opportunity()
            if 'error' in tax_result:
                st.error(tax_result['error'])
            elif tax_result['has_unrealized_loss']:
                st.warning(tax_result['recommendation'])
            else:
                st.success(tax_result['recommendation'])

    if 'espp_analysis' in st.session_state:
        analysis = st.session_state['espp_analysis']
        with st.expander('Claude分析プロンプト', expanded=True):
            st.text_area('プロンプト（Claudeに送付）', analysis.get('analysis_prompt', ''), height=200)

    st.divider()

    # 積立記録フォーム
    with st.expander('月次積立を記録する'):
        with st.form('espp_purchase_form'):
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                shares = st.number_input('取得株数', min_value=0.01, step=0.01, format='%.2f')
            with col_b:
                price  = st.number_input('取得単価（円）', min_value=1, step=1)
            with col_c:
                inc_rate = st.number_input('奨励金率', min_value=0.0, max_value=0.20, value=0.05, step=0.01, format='%.2f')
            pdate = st.date_input('購入日', value=date.today())
            submitted = st.form_submit_button('記録する', type='primary')
            if submitted:
                result = espp_mgr.record_monthly_purchase(
                    shares=shares,
                    purchase_price=float(price),
                    incentive_rate=float(inc_rate),
                    purchase_date=pdate.isoformat(),
                )
                st.success(f'記録完了: {shares}株 @¥{price:,} 奨励金率{inc_rate*100:.0f}%')
                st.cache_data.clear()

    # 売却記録フォーム
    with st.expander('売却を記録する'):
        with st.form('espp_sell_form'):
            col_a, col_b = st.columns(2)
            with col_a:
                sell_shares = st.number_input('売却株数', min_value=0.01, step=0.01, format='%.2f')
            with col_b:
                sell_price  = st.number_input('売却単価（円）', min_value=1, step=1)
            sell_reason = st.selectbox('売却理由', ['quarterly', 'concentration', 'tax_harvest', 'その他'])
            sell_date   = st.date_input('売却日', value=date.today())
            submitted   = st.form_submit_button('売却記録する')
            if submitted:
                result = espp_mgr.record_sell(
                    shares=float(sell_shares),
                    sell_price=float(sell_price),
                    reason=sell_reason,
                    sell_date=sell_date.isoformat(),
                )
                if 'error' in result:
                    st.error(result['error'])
                else:
                    st.success(f'売却完了: {sell_shares}株 / 手取り¥{result["net_proceeds"]:,.0f} (税¥{result["tax"]:,.0f})')


# ============================================================
# タブ 5: クレカ積立
# ============================================================

def render_tab_credit_card():
    st.subheader('クレカ積立管理')

    if cc_mgr is None:
        st.error('credit_card_investment.py の読み込みに失敗しました')
        return

    dashboard = cc_mgr.get_dashboard_data()
    summary   = dashboard['summary']
    combined  = summary['combined']

    # 合計サマリー
    st.markdown('**2口座合計**')
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric('合計残高', f'¥{combined["total_value"]:,.0f}')
    with col2:
        st.metric('累計積立額', f'¥{combined["total_invested"]:,.0f}')
    with col3:
        st.metric('年間ポイント効果', f'¥{combined["annual_points_effect"]:,.0f}')
    with col4:
        st.metric('月額合計', f'¥{combined["total_monthly"]:,}')

    st.divider()

    # メイン・サブそれぞれのタブ
    tab_h, tab_w = st.tabs(['メイン', 'サブ'])

    def render_person_tab(person_label: str, person_key: str, person_data: dict):
        val      = person_data['valuation']
        rec      = person_data['sell_recommendation']
        pts      = person_data['point_benefit']

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.metric('現在残高', f'¥{val["current_value"]:,.0f}')
            pnl = val['unrealized_pnl']
            st.metric('含み損益', f'¥{pnl:+,.0f}', f'{val["unrealized_pnl_pct"]*100:+.2f}%',
                      delta_color='normal' if pnl >= 0 else 'inverse')
        with col_b:
            st.metric('月額積立', f'¥{person_data["monthly_amount"]:,}')
            st.metric('獲得ポイント累計', f'¥{val["total_points"]:,.0f}')
        with col_c:
            st.metric('年間ポイント効果', f'¥{pts["annual_points"]:,.0f}')
            st.metric('月間ポイント', f'¥{pts["monthly_points"]:,.0f}')

        st.write(f'対象ファンド: **{person_data["fund"]}**')

        # 売却推奨
        if rec['should_sell']:
            st.warning(
                f'⚠️ 売却推奨: {rec["reason"]}\n\n'
                f'売却額: ¥{rec["sell_amount"]:,.0f} / 手取り: ¥{rec["net_proceeds"]:,.0f} (税: ¥{rec["tax"]:,.0f})'
            )
        else:
            st.success(f'売却タイミングではありません | 次回推奨日: {rec["next_sell_date"]}')

        # 今月の積立状況
        this_month_data = dashboard['this_month'][person_key]
        if this_month_data:
            st.success(f'今月の積立完了: ¥{this_month_data["amount"]:,} @ NAV¥{this_month_data["nav"]:,.0f}')
        else:
            st.info(f'今月の積立（¥{person_data["monthly_amount"]:,}）は未記録です')

        # 積立記録フォーム
        with st.expander(f'{person_label}の月次積立を記録する'):
            with st.form(f'cc_purchase_{person_key}'):
                col_x, col_y, col_z = st.columns(3)
                with col_x:
                    amt  = st.number_input('積立金額（円）', value=person_data['monthly_amount'], step=1000)
                with col_y:
                    nav  = st.number_input('NAV（円/口）', min_value=0.01, step=0.01, format='%.4f')
                with col_z:
                    pdate = st.date_input('購入日', value=date.today(), key=f'pdate_{person_key}')
                submitted = st.form_submit_button('記録する', type='primary')
                if submitted:
                    result = cc_mgr.record_monthly_purchase(person_key, int(amt), float(nav), pdate.isoformat())
                    st.success(f'記録完了: ¥{amt:,} @ NAV¥{nav:.4f}')

        # 売却記録フォーム
        with st.expander(f'{person_label}の売却を記録する'):
            with st.form(f'cc_sell_{person_key}'):
                col_p, col_q = st.columns(2)
                with col_p:
                    sell_nav = st.number_input('売却時NAV（円/口）', min_value=0.01, step=0.01, format='%.4f', key=f'sell_nav_{person_key}')
                with col_q:
                    sell_reason = st.selectbox('売却理由', ['quarterly', 'living_expense', 'tax_harvest', 'その他'], key=f'sell_reason_{person_key}')
                sdate = st.date_input('売却日', value=date.today(), key=f'sdate_{person_key}')
                sell_all = st.checkbox('全口数を売却', value=True, key=f'sell_all_{person_key}')

                tax_preview = cc_mgr.calculate_sell_tax(person_key)
                st.caption(
                    f'売却額（全口数）: ¥{tax_preview.get("sell_amount", 0):,.0f} / '
                    f'税: ¥{tax_preview.get("tax", 0):,.0f} / '
                    f'手取り: ¥{tax_preview.get("net_proceeds", 0):,.0f}'
                )

                submitted = st.form_submit_button('売却記録する')
                if submitted:
                    data = cc_mgr.load_cc_data()
                    units = data[person_key]['current_units'] if sell_all else 0
                    if units > 0:
                        result = cc_mgr.record_sell(person_key, units, float(sell_nav), sell_reason, sdate.isoformat())
                        if 'error' in result:
                            st.error(result['error'])
                        else:
                            st.success(f'売却完了: 手取り¥{result["net_proceeds"]:,.0f}')

    with tab_h:
        render_person_tab('メイン', 'husband', summary['husband'])
    with tab_w:
        render_person_tab('サブ', 'wife', summary['wife'])

    # キャッシュフローサマリー
    st.divider()
    st.markdown('**売却資金フロー（用途はローカル設定）**')
    cash_flow = combined['sell_cash_flow']
    flow_data = pd.DataFrame([
        {'対象': 'メイン', '次回売却推奨日': cash_flow['husband_next_sell'], '予定手取り': f'¥{cash_flow["husband_net"]:,.0f}'},
        {'対象': 'サブ',  '次回売却推奨日': cash_flow['wife_next_sell'],    '予定手取り': f'¥{cash_flow["wife_net"]:,.0f}'},
        {'対象': '合計', '次回売却推奨日': '-', '予定手取り': f'¥{cash_flow["total_available"]:,.0f}'},
    ])
    st.dataframe(flow_data, use_container_width=True, hide_index=True)


# ============================================================
# タブ 6〜11: Phase 2以降（プレースホルダー）
# ============================================================

def render_tab_long_term():
    st.subheader('長期投資管理')

    tab_opt, tab_screen, tab_upgrade = st.tabs(['最適ウェイト', '長期候補スクリーニング', '短期→長期転換'])

    # ---- Tab A: 最適化ウェイト ----
    with tab_opt:
        if port_opt is None:
            st.error('portfolio_optimizer.py の読み込みに失敗しました')
        else:
            st.markdown('**ポートフォリオ最適化（PyPortfolioOpt）**')
            col_method, col_run = st.columns([3, 1])
            with col_method:
                method = st.selectbox('最適化手法', ['max_sharpe', 'equal_risk', 'min_cvar'], key='opt_method')
            with col_run:
                run_btn = st.button('最適化実行', type='primary', use_container_width=True)

            # キャッシュ済み結果を表示
            cached = port_opt.load_optimization()
            if run_btn:
                with st.spinner('最適化中...（30〜60秒）'):
                    try:
                        result = port_opt.run_optimization()
                        port_opt.save_optimization(result)
                        cached = result
                        st.success('最適化完了')
                    except Exception as e:
                        st.error(f'最適化エラー: {e}')

            if cached and 'error' not in cached:
                st.caption(f'最終実行: {cached.get("as_of", "不明")} / レジーム: {cached.get("regime", "不明")} / 推奨手法: {cached.get("recommended", "不明")}')

                res = cached['results'].get(method, {})
                if res.get('expected_return'):
                    c1, c2, c3 = st.columns(3)
                    c1.metric('期待リターン（年率）', f'{res["expected_return"]*100:.1f}%')
                    c2.metric('ボラティリティ', f'{res.get("volatility", 0)*100:.1f}%' if res.get('volatility') else 'N/A')
                    c3.metric('シャープレシオ', f'{res.get("sharpe", 0):.3f}' if res.get('sharpe') else 'N/A')

                weights = res.get('regime_weights', res.get('weights', {}))
                if weights:
                    rows = []
                    for ticker, w in sorted(weights.items(), key=lambda x: -x[1]):
                        rows.append({
                            '銘柄': '現金' if ticker == '_cash' else ticker,
                            '最適ウェイト': f'{w*100:.1f}%',
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                # 現在との比較
                st.markdown('**現在ポートフォリオとの差分**')
                with st.spinner('比較中...'):
                    actions = port_opt.compare_with_current(cached, method)
                if actions:
                    increase = [a for a in actions if a['action'] == 'increase']
                    decrease = [a for a in actions if a['action'] == 'decrease']
                    if increase:
                        st.markdown('増加推奨:')
                        for a in increase:
                            st.write(f'↑ **{a["ticker"]}**: {a["current_pct"]}% → {a["optimal_pct"]}% ({a["diff_pct"]:+.1f}%)')
                    if decrease:
                        st.markdown('削減推奨:')
                        for a in decrease:
                            st.write(f'↓ **{a["ticker"]}**: {a["current_pct"]}% → {a["optimal_pct"]}% ({a["diff_pct"]:+.1f}%)')
            elif cached and 'error' in cached:
                st.warning(cached['error'])
            else:
                st.info('「最適化実行」ボタンを押してください')

    # ---- Tab B: 長期候補スクリーニング ----
    with tab_screen:
        if lt_screener is None:
            st.error('long_term_screener.py の読み込みに失敗しました')
        else:
            st.markdown('**長期投資候補スクリーニング（テック集中解消優先）**')
            st.caption('基準: EPS成長≥15% / ROE≥15% / 売上成長≥10% / アナリストBuy≥70%')

            # キャッシュ済み結果
            cached_lt = lt_screener.load_results()

            col_q, col_f = st.columns(2)
            with col_q:
                if st.button('クイックスキャン（ヘルスケア・金融）', use_container_width=True):
                    quick_list = [t for t, v in lt_screener.WATCHLIST.items()
                                  if v['sector'] in ('Healthcare', 'Financial Services')]
                    with st.spinner(f'{len(quick_list)}銘柄をスキャン中...'):
                        cached_lt = lt_screener.run_screening(quick_list, top_n=8)
                        lt_screener.save_results(cached_lt)
            with col_f:
                if st.button('フルスキャン（全セクター・時間がかかります）', use_container_width=True):
                    with st.spinner(f'{len(lt_screener.WATCHLIST)}銘柄をスキャン中...'):
                        cached_lt = lt_screener.run_screening()
                        lt_screener.save_results(cached_lt)

            if cached_lt:
                st.caption(f'最終スキャン: {cached_lt.get("as_of", "不明")} / '
                           f'通過: {len(cached_lt["passed"])}件 / {cached_lt["total_screened"]}件中')

                passed = cached_lt.get('passed', [])
                if passed:
                    rows = []
                    for p in passed:
                        rows.append({
                            '銘柄':      p['ticker'],
                            '名称':      p.get('name', p['ticker']),
                            'セクター':  p.get('sector', '-'),
                            'スコア':    p['score'],
                            'ROE':       f'{(p.get("roe") or 0)*100:.0f}%',
                            'EPS成長':   f'{(p.get("eps_growth") or 0)*100:.0f}%',
                            'フォワードPER': f'{p.get("forward_pe") or "N/A"}',
                            '価格':      f'${p.get("price") or "N/A"}',
                            '優先':      '⭐' if p.get('priority_sector') else '',
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    st.caption('⭐ = テック集中解消優先セクター（ヘルスケア・金融・生活必需品・資本財）')
                else:
                    st.info('通過銘柄なし。スキャンを実行してください。')
            else:
                st.info('まず「クイックスキャン」を実行してください。')

    # ---- Tab C: 短期→長期転換候補 ----
    with tab_upgrade:
        st.markdown('**短期(short)ポジションのうち長期基準を満たす銘柄**')
        st.caption('長期投資基準を満たしていれば investment_type を medium/long に変更することを検討')
        if st.button('転換候補を検出', key='upgrade_btn'):
            with st.spinner('検出中...'):
                candidates = lt_screener.find_upgrade_candidates() if lt_screener else []
            if candidates:
                for c in candidates:
                    st.success(f'**{c["ticker"]}** (スコア{c["score"]:.0f}): {c["reason"]}')
            else:
                st.info('現在の短期ポジションに転換候補はありません。')

def render_tab_margin():
    st.subheader('信用・空売り管理')

    if margin_mgr is None or short_scr is None:
        st.error('margin_manager.py / short_screener.py の読み込みに失敗しました')
        return

    sub_margin, sub_screen = st.tabs(['建玉・証拠金', '空売りスクリーニング'])

    # ---- 建玉・証拠金 ----
    with sub_margin:
        with st.spinner('建玉データを取得中...'):
            try:
                summary = margin_mgr.get_summary()
            except Exception as e:
                st.error(f'取得エラー: {e}')
                summary = None

        if summary is None:
            return

        ratio  = summary['maintenance_ratio']
        status = summary['margin_status']
        open_p = summary['open_positions']

        # KPI
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric('委託保証金', f'¥{summary["collateral"]/10000:.1f}万')
        with col2:
            if ratio == float('inf'):
                st.metric('証拠金維持率', '---')
            else:
                color_map = {'safe': None, 'caution': None, 'warning': None, 'emergency': None}
                st.metric('証拠金維持率', f'{ratio:.1f}%')
        with col3:
            st.metric('含み損益', f'¥{summary["total_unrealized"]:+,.0f}')
        with col4:
            st.metric('確定損益', f'¥{summary["total_realized"]:+,.0f}')

        # 維持率ステータス
        if status == 'emergency':
            st.error(f'🚨 証拠金維持率 {ratio:.1f}% — 追証危険！即座に建玉縮小または担保追加が必要です。')
        elif status == 'warning':
            st.warning(f'⚠️ 証拠金維持率 {ratio:.1f}% — 警戒水準（{margin_mgr.MARGIN_WARNING_PCT}%）を下回っています。')
        elif status == 'caution':
            st.warning(f'🟡 証拠金維持率 {ratio:.1f}% — 注意水準。余裕を持った管理を推奨します。')
        elif open_p:
            st.success(f'✅ 証拠金維持率 {ratio:.1f}% — 安全水準')

        # 期日アラート
        for ea in summary['expiry_alerts']:
            st.warning(f"📅 {ea['ticker']} ({ea['side']}) — 期日まで残り **{ea['days_left']} 日** ({ea['expiry']})")

        st.divider()

        # オープン建玉一覧
        st.markdown('**オープン建玉**')
        if open_p:
            rows = []
            for pos in open_p:
                side_label = '信用買' if pos['side'] == 'long' else '空売り'
                rows.append({
                    'ID':     pos['id'],
                    '種別':   side_label,
                    '銘柄':   pos['ticker'],
                    '株数':   pos['shares'],
                    '建値':   pos['entry_price'],
                    '現値':   pos.get('current_price', '-'),
                    '含み(円)': f"¥{pos.get('unrealized_pnl_jpy', 0):+,.0f}",
                    '損益率': f"{pos.get('pnl_pct', 0):+.1f}%",
                    '口座種別': pos.get('position_type', ''),
                    '期日':   pos.get('expiry', '---'),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info('オープン建玉はありません。')

        # 担保設定フォーム
        st.divider()
        with st.expander('担保金額を更新'):
            c1, c2 = st.columns(2)
            with c1:
                cash_input = st.number_input('現金担保（円）', min_value=0, step=100000,
                                             value=int(summary['collateral']))
            with c2:
                sec_input  = st.number_input('有価証券担保（時価・円）', min_value=0, step=100000, value=0)
            if st.button('担保を保存'):
                margin_mgr.set_collateral(cash=cash_input, securities=sec_input)
                st.success('担保情報を保存しました。ページを更新してください。')

        # 建玉追加フォーム
        with st.expander('新規建玉を記録'):
            ac1, ac2 = st.columns(2)
            with ac1:
                new_ticker   = st.text_input('銘柄コード（例: NVDA, 6762.T）', key='margin_ticker')
                new_side     = st.selectbox('売買区分', ['long（信用買）', 'short（空売り）'], key='margin_side')
                new_shares   = st.number_input('株数', min_value=0.0, step=1.0, key='margin_shares')
            with ac2:
                new_entry    = st.number_input('建値', min_value=0.0, step=0.01, key='margin_entry')
                new_currency = st.selectbox('通貨', ['JPY', 'USD'], key='margin_currency')
                new_pos_type = st.selectbox('信用種別', ['一般信用', '制度信用'], key='margin_postype')
                new_memo     = st.text_input('メモ', key='margin_memo')
            if st.button('建玉を記録', key='margin_add_btn'):
                if new_ticker and new_shares > 0 and new_entry > 0:
                    side_val = 'long' if 'long' in new_side else 'short'
                    margin_mgr.add_position(
                        ticker=new_ticker, side=side_val, shares=new_shares,
                        entry_price=new_entry, currency=new_currency,
                        position_type=new_pos_type, memo=new_memo,
                    )
                    st.success(f'{new_ticker} の建玉を記録しました。ページを更新してください。')
                else:
                    st.error('銘柄コード・株数・建値を入力してください。')

    # ---- 空売りスクリーニング ----
    with sub_screen:
        # キャッシュ結果を表示
        last = short_scr.load_last_candidates()
        if last:
            st.markdown(f'**最終スクリーニング**: {last.get("as_of", "---")}')
            vix_val = last.get('vix', 0)
            regime  = last.get('regime', '---')

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric('VIX', f'{vix_val:.1f}')
            with col2:
                st.metric('レジーム', regime)
            with col3:
                blocked = last.get('vix_blocked', False)
                st.metric('空売りステータス', '🚫 全禁止' if blocked else '✅ 可能')

            if last.get('vix_blocked'):
                st.error(f"VIX={vix_val:.1f} ≥ {short_scr.VIX_BLOCK_THRESHOLD} — 空売り全禁止")
            else:
                cands = last.get('candidates', [])
                if cands:
                    st.markdown(f'**空売り候補 {len(cands)} 件**')
                    rows = []
                    for c in cands:
                        rows.append({
                            '銘柄':     c['ticker'],
                            '名称':     c.get('name', '')[:20],
                            '価格':     f"${c['price']:,.2f}" if c.get('currency') == 'USD' else f"¥{c['price']:,.0f}",
                            'RSI':      f"{c['rsi']:.1f}",
                            'MA50比':   f"{c['pct_from_ma50']:+.1f}%",
                            'ボラ':     f"{c.get('vol20', 0):.1f}%",
                            'セクター': c.get('sector', ''),
                            '強度':     c.get('strength', ''),
                            '理由':     c.get('reason', '')[:50],
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.info(f'候補なし — {last.get("message", "")}')
        else:
            st.info('スクリーニング結果がありません。スキャンを実行してください。')

        st.divider()

        col_r, col_t = st.columns([2, 1])
        with col_r:
            force_regime = st.selectbox(
                'レジーム（省略で自動判定）',
                ['自動', 'A_強気', 'B_中立', 'C_弱気'],
                key='short_regime',
            )
        with col_t:
            send_alert = st.checkbox('Telegram 通知', value=False, key='short_alert')

        if st.button('空売りスキャンを実行', key='short_scan_btn'):
            regime_val = None if force_regime == '自動' else force_regime
            with st.spinner('スキャン中（数分かかる場合があります）...'):
                try:
                    result = short_scr.screen_candidates(regime=regime_val)
                    if send_alert:
                        short_scr.send_short_alert(result)
                    st.success(f'スキャン完了 — {result.get("message")}')
                    st.rerun()
                except Exception as e:
                    st.error(f'スキャンエラー: {e}')

        # レジームルール説明
        with st.expander('空売りレジームルール'):
            st.markdown('''
| レジーム | ルール |
|----------|--------|
| **A_強気** | 原則禁止。例外: RSI ≥ 80 かつ MA50 から +20% 以上の急騰のみ |
| **B_中立** | 弱セクターの弱銘柄に限定（RSI ≥ 70 かつ MA50 比 +10% 以上）|
| **C_弱気** | メイン戦略として積極活用（RSI ≥ 65 または MA50 比 +10% 以上）|
| **VIX ≥ 50** | **全レジームで空売り禁止**（VIX 禁止閾値）|
            ''')

def render_tab_nisa():
    st.subheader('NISA管理')

    if tax_opt is None:
        st.error('tax_optimizer.py の読み込みに失敗しました')
        return

    with st.spinner('NISA・税務データを分析中...'):
        try:
            snapshot = portfolio_mgr.build_portfolio_snapshot() if portfolio_mgr else None
            report   = tax_opt.get_full_tax_report(snapshot)
        except Exception as e:
            st.error(f'分析エラー: {e}')
            return

    nisa   = report['nisa']
    losses = report['loss_harvest']
    ft     = report['foreign_tax']

    # ---- 緊急アクション ----
    if report['urgent_actions']:
        for a in report['urgent_actions']:
            st.warning(a['message'])

    st.divider()

    # ---- NISA枠 ----
    st.markdown('**NISA枠使用状況**')
    col1, col2 = st.columns(2)
    for i, (person, label) in enumerate([('husband', 'メイン'), ('wife', 'サブ')]):
        p = nisa.get(person, {})
        with (col1 if i == 0 else col2):
            st.markdown(f'**{label}（{p.get("broker", "未設定")}）**')
            mc1, mc2 = st.columns(2)
            with mc1:
                ts_pct = p['tsumitate_used'] / p['tsumitate_annual'] if p.get('tsumitate_annual') else 0
                st.metric('つみたて使用', f'¥{p["tsumitate_used"]/10000:.1f}万',
                          f'残¥{p["tsumitate_remaining"]/10000:.1f}万')
                st.progress(min(ts_pct, 1.0))
            with mc2:
                gw_pct = p['growth_used'] / p['growth_annual'] if p.get('growth_annual') else 0
                st.metric('成長投資枠使用', f'¥{p["growth_used"]/10000:.1f}万',
                          f'残¥{p["growth_remaining"]/10000:.1f}万')
                st.progress(min(gw_pct, 1.0))
            st.caption(f'生涯枠残余: ¥{p["lifetime_remaining"]/10000:.0f}万')

    st.divider()

    # ---- 損出し候補 ----
    st.markdown(f'**損出し候補（期限: {losses["deadline"]}・残{losses["days_to_deadline"]}日）**')
    if losses['candidates']:
        st.info(f'損出し可能合計: ¥{losses["total_loss_jpy"]/10000:.1f}万 → 節税効果: ¥{losses["total_tax_saving"]/10000:.1f}万')
        rows = []
        for c in losses['candidates']:
            rows.append({
                '銘柄':     c['name'],
                '口座':     c['account'],
                '含み損':   f'¥{c["unrealized_jpy"]/10000:.1f}万 ({c["unrealized_pct"]*100:.1f}%)',
                '節税効果': f'¥{c["tax_saving_jpy"]/10000:.1f}万',
                '優先度':   c['priority'],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption('日本にウォッシュセールルールはありません。損切り後に即日同一銘柄を再購入できます。')
    else:
        st.success('損出し候補なし（含み損¥5万以上の銘柄がありません）')

    st.divider()

    # ---- 外国税額控除 ----
    st.markdown('**外国税額控除シミュレーション**')
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.metric('推定年間米国配当', f'¥{ft["dividends_jpy"]/10000:.1f}万')
    with col_b:
        st.metric('取り戻せる税額', f'¥{ft["credit_jpy"]/10000:.1f}万')
    with col_c:
        st.metric('実効税率', f'{ft["effective_after_credit"]*100:.1f}%',
                  f'控除なし{ft["effective_rate_no_credit"]*100:.1f}%')
    st.caption(ft['recommendation'])

    # ---- 売却税金シミュレーター ----
    st.divider()
    st.markdown('**売却税金シミュレーター**')
    with st.form('sell_tax_sim'):
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            sim_ticker = st.text_input('銘柄', value='NVDA')
        with c2:
            sim_shares = st.number_input('株数', min_value=1.0, value=10.0)
        with c3:
            sim_entry = st.number_input('取得単価', min_value=0.01, value=116.0, format='%.2f')
        with c4:
            sim_current = st.number_input('現在値', min_value=0.01, value=120.0, format='%.2f')
        with c5:
            sim_account = st.selectbox('口座', ['tokutei', 'ippan', 'nisa'])
        sim_ccy = st.radio('通貨', ['USD', 'JPY'], horizontal=True)
        if st.form_submit_button('計算する', type='primary'):
            result = tax_opt.calculate_sell_tax(
                sim_ticker, sim_shares, sim_entry, sim_current, sim_account, sim_ccy
            )
            cols = st.columns(4)
            cols[0].metric('売却総額', f'¥{result["gross_proceeds_jpy"]/10000:.1f}万')
            cols[1].metric('利益', f'¥{result["gain_jpy"]/10000:.1f}万')
            cols[2].metric('税額', f'¥{result["tax_jpy"]/10000:.1f}万')
            cols[3].metric('手取り', f'¥{result["net_jpy"]/10000:.1f}万')

def render_tab_rebalance():
    st.subheader('リバランス提案')

    if rebalance_eng is None or portfolio_mgr is None:
        st.error('rebalance_engine.py または portfolio_manager.py の読み込みに失敗しました')
        return

    available_cash = st.number_input(
        '追加投資可能な現金（円）', min_value=0, max_value=100_000_000,
        value=0, step=100_000, format='%d', key='rebalance_cash'
    )

    with st.spinner('リバランス分析中...'):
        try:
            snapshot = portfolio_mgr.build_portfolio_snapshot()
            report   = rebalance_eng.calculate_rebalance_actions(snapshot, available_cash=available_cash)
        except Exception as e:
            st.error(f'分析エラー: {e}')
            return

    s = report['summary']
    status_icon = {'ok': '✅', 'warning': '⚠️', 'action_needed': '🔴'}.get(s['overall_status'], '')
    st.markdown(f'### {status_icon} 総合ステータス: {s["overall_status"]}')

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric('総資産', f'¥{s["total_jpy"]/10000:.0f}万')
    with col2:
        icon = '⚠️' if s['tech_concentration'] else '✅'
        st.metric('テック比率', f'{s["tech_ratio"]*100:.1f}%', delta=f'{icon}{"集中" if s["tech_concentration"] else "正常"}')
    with col3:
        q_icon = '⏰' if s['quarterly_check_due'] else '✅'
        st.metric('四半期チェック', f'{q_icon} {"実施推奨" if s["quarterly_check_due"] else "次回まで待機"}')

    st.divider()

    # ---- 通貨配分 ----
    col_cur, col_sec = st.columns(2)
    with col_cur:
        st.markdown('**通貨配分**')
        cur_result = report['currency_result']
        cur_rows = []
        for ccy, info in cur_result['currencies'].items():
            icon = '✅' if info['level'] == 'ok' else '⚠️'
            cur_rows.append({
                '通貨':  ccy,
                '現在': f'{info["ratio"]*100:.1f}%',
                '目標': f'{info["target_min"]*100:.0f}〜{info["target_max"]*100:.0f}%',
                '乖離': f'{info["deviation"]*100:+.1f}%',
                '状態': icon,
            })
        st.dataframe(pd.DataFrame(cur_rows), use_container_width=True, hide_index=True)

    with col_sec:
        st.markdown('**セクター配分（上位6）**')
        sec_result = report['sector_result']
        sec_rows = []
        sorted_secs = sorted(sec_result['sectors'].items(), key=lambda x: -x[1]['ratio'])[:6]
        for sector, info in sorted_secs:
            icon = {'ok': '✅', 'warning': '⚠️', 'action_needed': '🔴'}.get(info['level'], '')
            sec_rows.append({
                'セクター': sector,
                '現在': f'{info["ratio"]*100:.1f}%',
                '目標': f'{info["target"]*100:.0f}%',
                '最大': f'{info["max"]*100:.0f}%',
                '状態': icon,
            })
        st.dataframe(pd.DataFrame(sec_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ---- アクションプラン ----
    if report['action_plan']:
        st.markdown('**アクションプラン（優先順）**')
        for i, action in enumerate(report['action_plan'], 1):
            lv = action.get('level', 'info')
            msg = action['message']
            if lv == 'critical':
                st.error(f'{i}. {msg}')
            elif lv == 'warning':
                st.warning(f'{i}. {msg}')
            else:
                st.info(f'{i}. {msg}')
    else:
        st.success('アクション不要。すべての配分が目標範囲内です。')

    # ---- 新規資金の振り分け ----
    st.divider()
    st.markdown('**新規資金の振り分け提案**')
    for p in report['new_cash_plan']:
        st.write(f'→ **{p["action"]}**: {p["detail"]}')

    # ---- 損出し候補（簡易） ----
    if tax_opt:
        try:
            loss_result = rebalance_eng.find_loss_harvest_candidates(snapshot)
            if loss_result:
                st.divider()
                st.markdown('**損出し候補（含み損¥5万以上）**')
                rows = []
                for c in loss_result:
                    rows.append({
                        '銘柄':  c['name'],
                        '口座':  c['account'],
                        '含み損': f'¥{c["unrealized_jpy"]/10000:.1f}万',
                        '損益率': f'{c["unrealized_pct"]*100:.1f}%',
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                st.caption('詳細はNISA管理タブの損出しセクションを参照')
        except Exception:
            pass

    st.caption(f'分析日時: {report["as_of"]}')

def render_tab_decision():
    st.subheader('意思決定支援（Sonnet × Opus）')

    if decision_sup is None:
        st.error('decision_support.py の読み込みに失敗しました（anthropic パッケージを確認してください）')
        return

    # ---- ケース選択 ----
    case_opts = {
        'A: 短期トレードシグナル': 'A',
        'B: 長期銘柄の買い増し':   'B',
        'C: 持株会判断': 'C',
        'D: クレカ積立の売却タイミング': 'D',
        'E: リバランス実行判断':   'E',
    }
    selected_label = st.selectbox('相談ケースを選択', list(case_opts.keys()), key='decision_case')
    case_code = case_opts[selected_label]

    # ケース別入力
    ticker_input   = ''
    signal_input   = ''
    strategy_input = ''
    person_input   = 'husband'
    reason_input   = ''

    if case_code == 'A':
        col1, col2, col3 = st.columns(3)
        with col1:
            ticker_input = st.text_input('銘柄コード（例: NVDA）', key='dec_ticker_a')
        with col2:
            signal_input = st.selectbox('シグナル種別',
                ['モメンタム', '逆張り', 'ギャップダウン', 'イベントドリブン'], key='dec_signal')
        with col3:
            strategy_input = signal_input

    elif case_code == 'B':
        col1, col2 = st.columns(2)
        with col1:
            ticker_input = st.text_input('銘柄コード（例: AVGO）', key='dec_ticker_b')
        with col2:
            reason_input = st.text_input('買い増し理由（例: 決算好調・押し目）', key='dec_reason_b')

    elif case_code == 'D':
        person_input = st.radio('対象者', ['husband（メイン）', 'wife（サブ）'],
                                horizontal=True, key='dec_person')
        person_input = 'husband' if 'husband' in person_input else 'wife'

    question_input = st.text_area('追加の質問・懸念事項（任意）', height=80, key='dec_question')
    user_pref      = st.text_area('Opus への追加指示（任意）', height=60,
                                   placeholder='例: リスクを抑えた判断をお願いします',
                                   key='dec_pref')

    col_s, col_o = st.columns(2)
    run_sonnet = col_s.button('① Sonnet で分析', key='dec_sonnet_btn', use_container_width=True)
    run_opus   = col_o.button('② Opus で最終判断', key='dec_opus_btn', use_container_width=True,
                               disabled='dec_case_result' not in st.session_state)

    # Sonnet 分析
    if run_sonnet:
        with st.spinner('Sonnet が分析中...'):
            try:
                if case_code == 'A':
                    if not ticker_input:
                        st.error('銘柄コードを入力してください。')
                        st.stop()
                    result = decision_sup.run_case_a(ticker_input, signal_input, strategy_input, question_input)
                elif case_code == 'B':
                    if not ticker_input:
                        st.error('銘柄コードを入力してください。')
                        st.stop()
                    result = decision_sup.run_case_b(ticker_input, reason_input, question_input)
                elif case_code == 'C':
                    result = decision_sup.run_case_c(question_input)
                elif case_code == 'D':
                    result = decision_sup.run_case_d(person_input, question_input)
                else:
                    result = decision_sup.run_case_e(question_input)

                st.session_state['dec_case_result'] = result
                st.rerun()
            except Exception as e:
                st.error(f'Sonnet 分析エラー: {e}')

    # Sonnet 分析結果の表示
    if 'dec_case_result' in st.session_state:
        result = st.session_state['dec_case_result']

        st.divider()
        st.markdown(f'**ケース {result["case"]} — Sonnet 分析結果**')

        with st.expander('収集コンテキスト', expanded=False):
            st.text(result.get('context', '---'))

        st.markdown('**Sonnet 分析:**')
        st.markdown(result.get('sonnet_analysis', '分析結果なし'))

        st.divider()

    # Opus 最終判断
    if run_opus and 'dec_case_result' in st.session_state:
        result = st.session_state['dec_case_result']
        with st.spinner('Opus が最終判断中...'):
            try:
                judgment = decision_sup.get_opus_judgment(result, user_pref)
                st.session_state['dec_opus_judgment'] = judgment
                st.rerun()
            except Exception as e:
                st.error(f'Opus 判断エラー: {e}')

    if 'dec_opus_judgment' in st.session_state:
        st.markdown('**Opus 最終判断:**')
        st.markdown(st.session_state['dec_opus_judgment'])

        # アクション記録
        st.divider()
        action_taken = st.text_input('実際に取ったアクション（任意・ログ記録用）', key='dec_action')
        if st.button('ログに記録', key='dec_log_btn'):
            try:
                decision_sup.log_decision(
                    st.session_state['dec_case_result'],
                    st.session_state['dec_opus_judgment'],
                    action_taken,
                )
                st.success('判断ログを記録しました。')
                # セッションをリセット
                del st.session_state['dec_case_result']
                del st.session_state['dec_opus_judgment']
            except Exception as e:
                st.error(f'ログ記録エラー: {e}')

    # ---- 過去の判断ログ ----
    st.divider()
    log_file = BASE_DIR / 'decision_log.json'
    if log_file.exists():
        with st.expander('過去の判断ログ（最新10件）'):
            try:
                with open(log_file, encoding='utf-8') as f:
                    logs = json.load(f)
                for log in reversed(logs[-10:]):
                    st.markdown(
                        f"**{log.get('timestamp', '')}** — ケース{log.get('case', '?')} "
                        f"/ アクション: {log.get('action_taken', '未記録')}"
                    )
            except Exception:
                st.info('ログを読み込めません。')

    # ケース説明
    with st.expander('ケース説明'):
        st.markdown('''
| ケース | 内容 | 主な収集データ |
|--------|------|--------------|
| **A** | 短期トレードシグナル | 価格・RSI・MA50・ニュース・ガードレール状態 |
| **B** | 長期銘柄の買い増し | EPS成長・ROE・PER・アナリスト評価 |
| **C** | 持株会判断 | 株数・比率・含み益・売却戦略 |
| **D** | クレカ積立の売却タイミング | 積立額・含み益・NISA状況・生活費需要 |
| **E** | リバランス実行判断 | 通貨・セクター配分・逸脱度・市場状況 |
        ''')

def render_tab_performance():
    st.subheader('パフォーマンス（Phase 4）')
    st.info('Phase 4で実装予定: QuantStats HTMLレポート・Fama-Frenchファクター分析・法人化シミュレーション')

    trade_df = load_trade_history()
    if not trade_df.empty:
        st.markdown('**取引履歴プレビュー**')
        st.dataframe(trade_df.tail(20), use_container_width=True)


# ============================================================
# ホームページ（案A）
# ============================================================

def render_home_page():
    """AI-Native Morning Brief ホームページ"""
    import subprocess
    briefing = load_briefing()
    signals  = load_signals_log()
    regime   = load_regime_state()
    guard    = {}
    try:
        gp = BASE_DIR / 'guard_state.json'
        if gp.exists():
            guard = json.loads(gp.read_text())
    except Exception:
        pass

    portfolio_total  = _session_portfolio_total()
    daily_pnl_pct    = guard.get('daily_pnl_pct', 0.0)
    daily_pnl_jpy    = daily_pnl_pct * portfolio_total / 100
    monthly_pnl_pct  = guard.get('monthly_pnl_pct', 0.0)
    new_entry_ok     = guard.get('new_entry_allowed', True)
    trading_ok       = guard.get('trading_allowed', True)

    spy_above = bool(regime.get('spy_above', False))
    nk_above  = bool(regime.get('nk_above', False))
    if spy_above and nk_above:
        regime_label, regime_cls = 'A_強気', 'regime-A'
    elif not spy_above and not nk_above:
        regime_label, regime_cls = 'C_弱気', 'regime-C'
    else:
        regime_label, regime_cls = 'B_中立', 'regime-B'

    pnl_cls   = 'pos' if daily_pnl_jpy >= 0 else 'neg'
    pnl_sign  = '+' if daily_pnl_jpy >= 0 else ''
    mpnl_cls  = 'pos' if monthly_pnl_pct >= 0 else 'neg'
    mpnl_sign = '+' if monthly_pnl_pct >= 0 else ''
    now_str   = datetime.now().strftime('%Y/%m/%d  %H:%M')

    # ==================================================
    # ① ヒーローバー
    # ==================================================
    st.markdown(f'''
<div class="home-hero">
  <div class="home-hero-top">
    <div class="home-hero-brand">ALMANAC</div>
    <div style="display:flex; align-items:center; gap:12px; margin-left:auto;">
      <span class="regime-badge {regime_cls}" style="font-size:0.78rem; padding:3px 10px;">{regime_label}</span>
      <div class="home-hero-live"><span class="home-hero-dot"></span>LIVE</div>
      <div class="home-hero-date">{now_str}</div>
    </div>
  </div>
  <div style="display:flex; align-items:baseline; gap:20px; flex-wrap:wrap; margin-top:6px;">
    <div class="home-hero-assets">¥{portfolio_total/10000:.0f}<span style="font-size:1.1rem; color:#64748b; font-weight:400; margin-left:4px;">万円</span></div>
    <div class="home-hero-pnl {pnl_cls}">{pnl_sign}¥{daily_pnl_jpy:,.0f}（本日 {pnl_sign}{daily_pnl_pct:.2f}%）</div>
    <div style="font-size:0.85rem; color:var(--text-dim);">月間: <span style="font-weight:700; color:var(--{'green' if monthly_pnl_pct >= 0 else 'red'});">{mpnl_sign}{monthly_pnl_pct:.2f}%</span></div>
  </div>
</div>
''', unsafe_allow_html=True)

    if not trading_ok:
        st.error('🚨 **全取引停止中**：月間損失が -5% を超えました。全ポジションを見直してください。')
    elif not new_entry_ok:
        st.warning('⛔ **新規エントリー禁止**：本日の損失が -3% を超えました。')

    # ── Morning Brief アニメーションチャート行（常時表示） ──
    _hc = st.columns(4)
    _regime_score = {'A_強気': 2.0, 'B_中立': 1.0, 'C_弱気': 0.0}.get(regime_label, 1.0)
    _guard_val    = 1.0 if (trading_ok and new_entry_ok) else (0.5 if trading_ok else 0.0)
    _fig_home_pnl   = _make_indicator(daily_pnl_pct,   '本日 P&L',   '%')
    _fig_home_mpnl  = _make_indicator(monthly_pnl_pct, '月次リターン', '%')
    _fig_home_reg   = _make_gauge(_regime_score, 2.0, 'レジームスコア',
                                  green_end=1.5, amber_end=1.75)
    _fig_home_guard = _make_gauge(_guard_val, 1.0, 'ガードレール',
                                  green_end=0.9, amber_end=0.96)
    with _hc[0]: st.plotly_chart(_fig_home_pnl,   use_container_width=True, key='home_strip_pnl')
    with _hc[1]: st.plotly_chart(_fig_home_mpnl,  use_container_width=True, key='home_strip_mpnl')
    with _hc[2]: st.plotly_chart(_fig_home_reg,   use_container_width=True, key='home_strip_reg')
    with _hc[3]: st.plotly_chart(_fig_home_guard, use_container_width=True, key='home_strip_guard')

    _render_ai_explain(
        section_label='ホームサマリー',
        context={
            '総資産': f'¥{portfolio_total/10000:.0f}万円',
            '本日P&L': f'{pnl_sign}{daily_pnl_pct:.2f}%（{pnl_sign}¥{daily_pnl_jpy:,.0f}）',
            '月間損益': f'{mpnl_sign}{monthly_pnl_pct:.2f}%',
            'レジーム': regime_label,
            '取引ステータス': '停止' if not trading_ok else ('新規禁止' if not new_entry_ok else '正常'),
            'SPY_200MA': '上方' if spy_above else '下方',
            'NK_200MA': '上方' if nk_above else '下方',
            'ブリーフィングサマリー': briefing.get('summary', '') if briefing else '未生成',
        },
        key='home',
        figures=[_fig_home_pnl, _fig_home_mpnl, _fig_home_reg, _fig_home_guard],
    )

    # ==================================================
    # ② AI朝ブリーフィングカード（フルwidth・グラスモーフィズム）
    # ==================================================
    if briefing:
        gen_time = briefing.get('generated_at', '')
        try:
            gen_time = datetime.fromisoformat(gen_time).strftime('%m/%d %H:%M')
        except Exception:
            gen_time = ''
        summary        = briefing.get('summary', '')
        market_comment = briefing.get('market_comment', '')
        actions        = briefing.get('actions', [])
        risk_alert     = briefing.get('risk_alert', '')
        opportunity    = briefing.get('opportunity', '')

        badge_cls, badge_text = ('medium', '⚠ CAUTION') if risk_alert else ('high', '✓ NORMAL')

        actions_html = ''.join(
            f'<div class="home-act-item">'
            f'<div class="home-act-num">{i+1}</div>'
            f'<div class="home-act-text">{a}</div>'
            f'</div>'
            for i, a in enumerate(actions[:3])
        )
        risk_html  = f'<div class="home-risk-card" style="margin:10px 0 4px;">⚠️ <b>リスク警告：</b>{risk_alert}</div>' if risk_alert else ''
        oppo_html  = f'<div class="home-oppo-card" style="margin:10px 0 4px;">💡 <b>注目チャンス：</b>{opportunity}</div>' if opportunity else ''

        st.markdown(f'''
<div class="ai-decision-card">
  <div class="ai-card-header">
    <span class="ai-sparkle">✦</span>
    <span class="ai-label">AI Morning Brief</span>
    <span class="ai-badge {badge_cls}">{badge_text}</span>
    <span class="ai-timestamp">Haiku生成 · {gen_time}</span>
  </div>
  <div class="ai-card-headline">{summary}</div>
  <div class="ai-card-reason">🌅 {market_comment}</div>
  {risk_html}{oppo_html}
  <div style="font-size:0.68rem; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:var(--text-dim); margin:12px 0 8px;">今日やること</div>
  {actions_html}
</div>
''', unsafe_allow_html=True)
    else:
        st.markdown('''
<div class="ai-decision-card" style="text-align:center; padding:28px 20px;">
  <div style="font-size:1.8rem; margin-bottom:8px; color:var(--ai);">✦</div>
  <div style="font-size:0.9rem; color:var(--text-sub);">最新のAI分析はまだ生成されていません</div>
  <div style="font-size:0.78rem; color:var(--text-dim); margin-top:6px;">AI分析を実行してください</div>
</div>
''', unsafe_allow_html=True)
        if st.button('🔄 最新分析を再読込', key='home_gen_brief_empty'):
            st.cache_data.clear()
            st.rerun()

    # ==================================================
    # ③ 2カラム: 注目シグナルカード | システム状態
    # ==================================================
    col_signal, col_status = st.columns([3, 2])

    with col_signal:
        if signals:
            ticker, sig = next(iter(signals.items()))
            entry  = sig.get('entry_price', 0) or 0
            target = sig.get('target_price', 0) or 0
            stop   = sig.get('stop_loss', 0) or 0
            score  = sig.get('score', 0)
            sdate  = str(sig.get('signal_date', ''))[:10]
            reason = sig.get('reason', '')[:80]
            denom  = (entry - stop)
            rr     = abs((target - entry) / denom) if denom else 0
            upside = ((target - entry) / entry * 100) if entry else 0
            sig_badge_cls   = 'high' if score >= 4 else 'medium' if score >= 3 else 'low'
            sig_badge_label = '✓ HIGH' if score >= 4 else '⚠ MEDIUM' if score >= 3 else '✗ LOW'
            st.markdown(f'''
<div class="ai-decision-card">
  <div class="ai-card-header">
    <span class="ai-sparkle">✦</span>
    <span class="ai-label">Top AI Signal</span>
    <span class="ai-badge {sig_badge_cls}">{sig_badge_label}</span>
    <span class="ai-timestamp">{sdate}</span>
  </div>
  <div class="ai-card-headline" style="font-size:1.6rem; color:var(--green); letter-spacing:-0.01em;">{ticker}</div>
  <div class="ai-card-metrics">
    <div class="ai-metric">
      <div class="ai-metric-lbl">エントリー</div>
      <div class="ai-metric-val neutral">${entry:.2f}</div>
    </div>
    <div class="ai-metric">
      <div class="ai-metric-lbl">目標 Upside</div>
      <div class="ai-metric-val up">+{upside:.1f}%</div>
    </div>
    <div class="ai-metric">
      <div class="ai-metric-lbl">R/R 比率</div>
      <div class="ai-metric-val neutral">{rr:.1f}:1</div>
    </div>
  </div>
  <div class="ai-card-reason">{reason}…</div>
</div>
''', unsafe_allow_html=True)
        else:
            st.markdown('''
<div class="ai-decision-card" style="display:flex; align-items:center; justify-content:center; min-height:180px;">
  <div style="text-align:center; color:var(--text-dim);">
    <div style="font-size:2rem; margin-bottom:8px;">📭</div>
    <div style="font-size:0.85rem;">シグナルなし</div>
    <div style="font-size:0.72rem; margin-top:4px;">analyzer.py を実行してください</div>
  </div>
</div>
''', unsafe_allow_html=True)

    with col_status:
        if not trading_ok:
            g_icon, g_cls, g_text = '🔴', 'alert', '全取引停止中'
        elif not new_entry_ok:
            g_icon, g_cls, g_text = '🟡', 'warn', '新規禁止中'
        else:
            g_icon, g_cls, g_text = '🟢', 'ok', '取引 OK'

        st.markdown(f'''
<div class="home-panel">
  <div class="home-panel-title">システム状態</div>
  <div class="home-status-row">
    <span style="font-size:1rem;">{g_icon}</span>
    <span class="home-status-label">ガードレール</span>
    <span class="home-status-val status-{g_cls}">{g_text}</span>
  </div>
  <div class="home-status-row">
    <span style="font-size:1rem;">💹</span>
    <span class="home-status-label">SPY 200MA</span>
    <span class="home-status-val {'status-ok' if spy_above else 'status-alert'}">{'上方' if spy_above else '下方'}</span>
  </div>
  <div class="home-status-row">
    <span style="font-size:1rem;">🗾</span>
    <span class="home-status-label">NK 200MA</span>
    <span class="home-status-val {'status-ok' if nk_above else 'status-alert'}">{'上方' if nk_above else '下方'}</span>
  </div>
  <div class="home-status-row">
    <span style="font-size:1rem;">📊</span>
    <span class="home-status-label">月次損益</span>
    <span class="home-status-val {'status-ok' if monthly_pnl_pct >= 0 else 'status-alert'}">{mpnl_sign}{monthly_pnl_pct:.2f}%</span>
  </div>
</div>
''', unsafe_allow_html=True)

    st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
    col_b1, col_b2, _ = st.columns([1, 1, 3])
    with col_b1:
        if st.button('🔄 最新分析を再読込', use_container_width=True, key='home_gen_brief'):
            st.cache_data.clear()
            st.rerun()
    with col_b2:
        if st.button('🤖 AI分析を実行', use_container_width=True, key='home_run_analyzer'):
            try:
                subprocess.Popen(['venv/bin/python', 'analyzer.py'],
                                 cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                st.success('analyzer.py 起動しました')
            except Exception as e:
                st.error(str(e))


# ============================================================
# チャットパネル（案C）
# ============================================================

def render_chat_panel_section():
    """右カラムに Ollama チャットパネルを描画"""
    st.markdown('''
<div class="chat-panel-header">
  <span>💬</span><span>AI アシスタント</span>
</div>
''', unsafe_allow_html=True)

    ollama_ok = False
    if ollama_chat:
        try:
            ollama_ok = ollama_chat.is_ollama_available()
        except Exception:
            pass

    backend_label = 'Ollama (local)' if ollama_ok else 'Claude Haiku-4.5'
    st.markdown(f'<span class="chat-backend-badge">{backend_label}</span>', unsafe_allow_html=True)
    st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)

    if 'chat_messages' not in st.session_state:
        st.session_state['chat_messages'] = []

    chat_container = st.container(height=420)
    with chat_container:
        for msg in st.session_state['chat_messages'][-12:]:
            with st.chat_message(msg['role']):
                st.markdown(msg['content'])

    prompt = st.chat_input('ポートフォリオについて質問…', key='chat_panel_input')
    if prompt:
        st.session_state['chat_messages'].append({'role': 'user', 'content': prompt})
        with chat_container:
            with st.chat_message('user'):
                st.markdown(prompt)
            with st.chat_message('assistant'):
                placeholder = st.empty()
                full_text = ''
                try:
                    if ollama_chat:
                        gen, backend = ollama_chat.chat_stream(
                            st.session_state['chat_messages'], prefer_ollama=ollama_ok)
                        for chunk in gen:
                            full_text += chunk
                            placeholder.markdown(full_text + '▌')
                        placeholder.markdown(full_text)
                        st.caption(f'_{backend}_')
                    else:
                        full_text = '❌ ollama_chat.py が読み込めませんでした。'
                        placeholder.markdown(full_text)
                except Exception as e:
                    full_text = f'エラー: {e}'
                    placeholder.error(full_text)
                st.session_state['chat_messages'].append({'role': 'assistant', 'content': full_text})

    col_clr, col_esc = st.columns(2)
    with col_clr:
        if st.button('🗑️ クリア', use_container_width=True, key='cp_clear'):
            st.session_state['chat_messages'] = []
            st.rerun()
    with col_esc:
        if st.button('🔬 Sonnet へ', use_container_width=True, key='cp_escalate',
                     help='最後の質問を Sonnet→Opus で深掘り（管理タブ→意思決定支援）'):
            if st.session_state['chat_messages']:
                last_user = next(
                    (m['content'] for m in reversed(st.session_state['chat_messages'])
                     if m['role'] == 'user'), '')
                st.session_state['escalate_prompt'] = last_user
                st.info('⚙️ 管理タブ → 意思決定支援 で深掘り分析できます')


# ============================================================
# 5セクションナビゲーション
# ============================================================

def render_section_portfolio():
    tabs = st.tabs(['📊 総覧', '📈 長期投資', '⚖️ リバランス'])
    with tabs[0]: render_tab_portfolio()
    with tabs[1]: render_tab_long_term()
    with tabs[2]: render_tab_rebalance()


def render_section_trade():
    tabs = st.tabs(['⚡ AIシグナル / 短期トレード', '📉 信用・空売り'])
    with tabs[0]: render_tab_short_trade()
    with tabs[1]: render_tab_margin()


def render_section_risk():
    tabs = st.tabs(['🛡️ リスク管理', '🏦 NISA管理'])
    with tabs[0]: render_tab_risk()
    with tabs[1]: render_tab_nisa()


def render_section_admin():
    tabs = st.tabs(['🏭 持株会', '💳 クレカ積立', '🤖 意思決定支援', '📉 パフォーマンス'])
    with tabs[0]: render_tab_espp()
    with tabs[1]: render_tab_credit_card()
    with tabs[2]: render_tab_decision()
    with tabs[3]: render_tab_performance()


# ============================================================
# メイン
# ============================================================

def _render_top_nav():
    NAV = [
        ('home',      '🏠', 'Morning Brief'),
        ('portfolio', '📊', 'ポートフォリオ'),
        ('trade',     '⚡', 'トレード'),
        ('risk',      '🛡️', 'リスク・税務'),
        ('admin',     '⚙️', '管理'),
    ]
    current = st.session_state.get('nav_section', 'home')
    cols = st.columns(len(NAV))
    for col, (key, icon, label) in zip(cols, NAV):
        with col:
            active = current == key
            btn_style = (
                'background:#6366F1;color:#fff;border:none;'
                if active else
                'background:#161922;color:#C4C9D4;border:1px solid #1E2230;'
            )
            clicked = st.button(
                f'{icon} {label}',
                key=f'topnav_{key}',
                use_container_width=True,
                type='primary' if active else 'secondary',
            )
            if clicked and not active:
                st.session_state['nav_section'] = key
                st.rerun()
    st.markdown('<div style="height:4px;border-bottom:1px solid #1E2230;margin-bottom:16px;"></div>',
                unsafe_allow_html=True)


def main():
    inject_css()

    if 'nav_section' not in st.session_state:
        st.session_state['nav_section'] = 'home'
    if 'chat_open' not in st.session_state:
        st.session_state['chat_open'] = False

    render_sidebar()

    if st.session_state.get('chat_open', False):
        main_col, chat_col = st.columns([7, 3])
    else:
        main_col = st.container()
        chat_col = None

    with main_col:
        _render_top_nav()
        section = st.session_state.get('nav_section', 'home')
        if section == 'home':      render_home_page()
        elif section == 'portfolio': render_section_portfolio()
        elif section == 'trade':     render_section_trade()
        elif section == 'risk':      render_section_risk()
        elif section == 'admin':     render_section_admin()

    if chat_col:
        with chat_col:
            render_chat_panel_section()


if __name__ == '__main__':
    main()
