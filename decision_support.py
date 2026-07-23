"""
ALMANAC v4.0 - 意思決定支援エンジン
Sonnet（claude-sonnet-4-6）が状況を分析し、
Opus（claude-opus-4-6）が最終判断を下す。

対応ケース:
  A. 短期トレードシグナル
  B. 長期銘柄の買い増し
  C. 持株会: 売る？持つ？
  D. クレカ積立の売却タイミング
  E. リバランス実行判断
"""

import json
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import anthropic

BASE_DIR = Path(__file__).parent

# model_router 経由で動的解決。decision_support は Opus → Sonnet にコスト最適化。
# ALMANAC_BUDGET_MODE=premium なら Opus に自動昇格。
try:
    from model_router import get_model as _get_model_router
    SONNET_MODEL = _get_model_router('decision_support')   # 以前は sonnet 固定
    OPUS_MODEL   = _get_model_router('decision_support')   # 以前は opus 固定 → Sonnet に降格
except ImportError:
    # フォールバック
    SONNET_MODEL = 'claude-sonnet-4-6'
    OPUS_MODEL   = 'claude-sonnet-4-6'  # Opus → Sonnet にコスト最適化

SYSTEM_PROMPT = """あなたはALMANAC v4.0の専任投資アドバイザーです。
ユーザーはユーザー（長期投資家）です。

投資スタイル:
- コアポジション: 分散された長期保有資産
- サテライト: 短期モメンタム（1-2週間）
- 持株会: 10%上限、集中リスク管理
- 定期積立: 金額・売却目的はローカルの非公開設定に従う

判断の原則:
- ガードレール優先（日次-3%→新規禁止、月間-5%→全停止）
- 長期投資家として短期ノイズに惑わされない
- 税務最適化（損出し・NISA活用・外国税額控除）を常に考慮
- 日本語で、簡潔かつ実用的に回答する"""


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_anthropic_usage(
    *,
    role: str,
    model: str,
    case: str,
    max_tokens: int,
    started: float,
    prompt_chars: int,
    response=None,
    status: str = "ok",
    error: Exception | None = None,
    **extra,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": model,
        "case": case,
        "use_tool": False,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status,
        **extra,
    }
    if response is not None:
        row.update({
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        })
    _append_llm_call_log(row)


# ============================================================
# データ収集（各ケース共通）
# ============================================================

def _collect_ticker_data(ticker: str) -> dict:
    """yfinanceからティッカーの基本データを収集する。"""
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        info = t.info
        hist = t.history(period='3mo')

        price = info.get('regularMarketPrice') or info.get('currentPrice', 0)
        ma50  = float(hist['Close'].rolling(50).mean().iloc[-1]) if len(hist) >= 50 else None
        rsi   = _calc_rsi(hist['Close']) if not hist.empty else None

        return {
            'ticker':       ticker,
            'price':        price,
            'ma50':         round(ma50, 2) if ma50 else None,
            'rsi':          round(rsi, 1) if rsi else None,
            'pe_ratio':     info.get('forwardPE') or info.get('trailingPE'),
            'sector':       info.get('sector', '不明'),
            'name':         info.get('longName', ticker),
            'analyst_reco': info.get('recommendationKey', '不明'),
            '52w_high':     info.get('fiftyTwoWeekHigh'),
            '52w_low':      info.get('fiftyTwoWeekLow'),
            'volume':       info.get('regularMarketVolume'),
        }
    except Exception as e:
        return {'ticker': ticker, 'error': str(e)}


def _calc_rsi(close, period: int = 14) -> Optional[float]:
    """RSI(14)を計算する。"""
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _load_guard_state() -> dict:
    try:
        import behavioral_guard
        return behavioral_guard.load_state()
    except Exception:
        return {}


def _load_holding_info(ticker: str) -> Optional[dict]:
    """holdings.jsonから保有情報を取得する。"""
    path = BASE_DIR / 'holdings.json'
    if not path.exists():
        return None
    with open(path, encoding='utf-8') as f:
        holdings = json.load(f)
    for key, info in holdings.items():
        if info.get('ticker', key) == ticker or key == ticker:
            return {**info, 'key': key}
    return None


# ============================================================
# ケース別コンテキスト構築
# ============================================================

def _build_case_a(ticker: str, signal: str, strategy: str) -> str:
    """A: 短期トレードシグナル"""
    mkt   = _collect_ticker_data(ticker)
    guard = _load_guard_state()
    hold  = _load_holding_info(ticker)

    ctx = f"""【ケースA: 短期トレードシグナル判断】

■ 銘柄: {ticker} ({mkt.get('name', ticker)})
■ シグナル: {signal}
■ 戦略: {strategy}

■ 現在の市場状況:
  価格: ${mkt.get('price', 'N/A')}
  RSI(14): {mkt.get('rsi', 'N/A')}
  MA50: ${mkt.get('ma50', 'N/A')}
  52週高値: ${mkt.get('52w_high', 'N/A')} / 安値: ${mkt.get('52w_low', 'N/A')}
  セクター: {mkt.get('sector', 'N/A')}
  アナリスト評価: {mkt.get('analyst_reco', 'N/A')}

■ ガードレール状態:
  取引可能: {guard.get('trading_allowed', True)}
  新規エントリー可: {guard.get('new_entry_allowed', True)}
  本日P&L: {guard.get('daily_pnl_pct', 0)*100:.2f}%
  月間P&L: {guard.get('monthly_pnl_pct', 0)*100:.2f}%
  アクティブトレード: {guard.get('active_trades', 0)}/5"""

    if hold:
        entry = hold.get('entry_price', 0)
        current = mkt.get('price', 0)
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        ctx += f"""

■ 既存ポジション:
  取得単価: ${entry}
  現在損益: {pnl_pct:+.1f}%
  投資区分: {hold.get('investment_type', '不明')}
  口座: {hold.get('account', '不明')}"""

    return ctx


def _build_case_b(ticker: str, reason: str) -> str:
    """B: 長期銘柄の買い増し"""
    mkt  = _collect_ticker_data(ticker)
    hold = _load_holding_info(ticker)

    try:
        import risk_engine, data_fetcher
        returns = data_fetcher.get_returns(ticker, 252)
        var_result = risk_engine.calculate_var_cornish_fisher(returns, 0.95, 1_000_000) if returns is not None else {}
    except Exception:
        var_result = {}

    ctx = f"""【ケースB: 長期銘柄の買い増し判断】

■ 銘柄: {ticker} ({mkt.get('name', ticker)})
■ 買い増し理由: {reason}

■ 現在の市場状況:
  価格: ${mkt.get('price', 'N/A')}
  RSI(14): {mkt.get('rsi', 'N/A')}
  MA50: ${mkt.get('ma50', 'N/A')}（{'上' if (mkt.get('price', 0) or 0) > (mkt.get('ma50', 0) or 0) else '下'}）
  フォワードPER: {mkt.get('pe_ratio', 'N/A')}
  セクター: {mkt.get('sector', 'N/A')}
  アナリスト: {mkt.get('analyst_reco', 'N/A')}"""

    if var_result:
        ctx += f"""

■ リスク指標:
  VaR(95%): ¥{var_result.get('var_jpy', 0):,.0f}（100万円あたり）"""

    if hold:
        entry   = hold.get('entry_price', 0)
        current = mkt.get('price', 0) or 0
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        ctx += f"""

■ 現在のポジション:
  取得単価: ${entry}
  現在損益: {pnl_pct:+.1f}%
  保有株数: {hold.get('shares', 0)}株
  口座: {hold.get('account', '不明')}（{hold.get('investment_type', '不明')}）"""

    return ctx


def _build_case_c() -> str:
    """C: 持株会 売る？持つ？"""
    try:
        import espp_plan_manager as km
        data = km.load_espp_data()
        conc = km.analyze_espp_concentration()
        plan = km.get_quarterly_sell_plan()
        analysis = km.espp_hold_or_sell_analysis()
    except Exception as e:
        return f'持株会データ取得エラー: {e}'

    mkt = _collect_ticker_data(str(data.get("ticker") or km.ESPP_PLAN_CONFIG["ticker"]))

    ctx = f"""【ケースC: 持株会 売る？持つ？】

■ 保有状況:
  株数: {data.get('current_shares', 0):.3f}株
  平均取得単価: ¥{data.get('avg_cost', 0):,.0f}
  現在価格: ¥{mkt.get('price', 0):,.0f}
  含み益: ¥{(mkt.get('price', 0) - data.get('avg_cost', 0)) * data.get('current_shares', 0):,.0f}
  奨励金込みリターン: {analysis.get('effective_return_pct', 0):.1f}%

■ 集中リスク:
  現在比率: {conc.get('concentration_pct', 0)*100:.1f}%（上限10%）
  超過額: ¥{conc.get('excess_value', 0):,.0f}

■ 四半期売却計画:
  推奨売却株数: {plan.get('recommended_sell_shares', 0):.0f}株
  推奨売却額: ¥{plan.get('recommended_sell_value', 0):,.0f}
  次回推奨時期: {plan.get('next_sell_quarter', '不明')}

■ RSI: {mkt.get('rsi', 'N/A')} / MA50: ¥{mkt.get('ma50', 'N/A')}"""

    return ctx


def _build_case_d(person: str = 'husband') -> str:
    """D: クレカ積立の売却タイミング"""
    try:
        import credit_card_investment as cc
        summary = cc.get_combined_summary()
        p_data  = summary[person]
        tax     = cc.calculate_sell_tax(person)
    except Exception as e:
        return f'クレカ積立データ取得エラー: {e}'

    label = 'メイン' if person == 'husband' else 'サブ'
    val   = p_data['valuation']
    rec   = p_data['sell_recommendation']

    ctx = f"""【ケースD: クレカ積立売却タイミング判断（{label}）】

■ 積立状況:
  ファンド: {p_data.get('fund', '不明')}
  現在残高: ¥{val['current_value']:,.0f}
  累計積立: ¥{val.get('total_invested', val['cost_basis']):,.0f}
  含み損益: ¥{val['unrealized_pnl']:+,.0f}（{val['unrealized_pnl_pct']*100:+.2f}%）

■ 売却試算:
  売却額: ¥{tax.get('sell_amount', 0):,.0f}
  税額: ¥{tax.get('tax', 0):,.0f}（{tax.get('tax_rate', 0)*100:.2f}%）
  手取り: ¥{tax.get('net_proceeds', 0):,.0f}

■ 現在の推奨:
  売却推奨: {'はい' if rec['should_sell'] else 'いいえ'}
  理由: {rec.get('reason', 'なし')}
  次回推奨日: {rec.get('next_sell_date', '不明')}"""

    return ctx


def _build_case_e() -> str:
    """E: リバランス実行判断"""
    try:
        import portfolio_manager as pm
        import rebalance_engine as re
        snapshot = pm.build_portfolio_snapshot()
        # 2026-07: 通貨目標は AI 動的方針 (basis=long_tier・未期限切れ) を解決して注入。
        # 無効/期限切れは static CURRENCY_TARGETS に fail-closed。
        currency_targets = re.CURRENCY_TARGETS
        try:
            import currency_policy
            currency_targets, _ = currency_policy.resolve_effective_targets(static=re.CURRENCY_TARGETS)
        except Exception:
            currency_targets = re.CURRENCY_TARGETS
        report   = re.calculate_rebalance_actions(snapshot, currency_targets=currency_targets)
    except Exception as e:
        return f'リバランスデータ取得エラー: {e}'

    s    = report['summary']
    acts = report['action_plan']

    ctx = f"""【ケースE: リバランス実行判断】

■ 現在の配分状態:
  総資産: ¥{s['total_jpy']/10000:.0f}万
  通貨ステータス: {report['currency_result']['status']}
  セクターステータス: {report['sector_result']['status']}
  テック比率: {s['tech_ratio']*100:.1f}%（目標30%）

■ 通貨配分:"""
    for ccy, info in report['currency_result']['currencies'].items():
        ctx += f"""
  {ccy}: {info['ratio']*100:.1f}%（目標{info['target_min']*100:.0f}〜{info['target_max']*100:.0f}%）"""

    if acts:
        ctx += '\n\n■ 推奨アクション:'
        for a in acts[:3]:
            ctx += f"\n  - {a['message']}"

    buy = report['buy_candidates']
    if buy['currencies']:
        ctx += f"\n\n■ 優先購入通貨: {buy['currencies'][0].get('currency', '')}建て資産"
    if buy['sectors']:
        ctx += f"\n■ 優先補充セクター: {buy['sectors'][0].get('sector', '')}"

    return ctx


# ============================================================
# Sonnet 分析
# ============================================================

def analyze_with_sonnet(case: str, context: str, question: str = '') -> str:
    """
    Sonnetがケースを分析し、構造化された分析レポートを返す。

    Args:
        case:     'A'〜'E'
        context:  ケース別コンテキスト文字列
        question: ユーザーの追加質問

    Returns:
        Sonnetの分析テキスト
    """
    from almanac.llm_safety import assert_book_aware_allowed, BookAwareDisabled, log_book_aware_call
    try:
        assert_book_aware_allowed(provider="anthropic")
    except BookAwareDisabled as e:
        log_book_aware_call(role="decision_support_sonnet", model=SONNET_MODEL,
                             fields=["portfolio_context", "case_context"], status="blocked")
        return f"（この機能は現在プライバシーモードにより無効化されています: {e}）"

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

    case_labels = {
        'A': '短期トレードシグナル',
        'B': '長期銘柄の買い増し',
        'C': '持株会の売買判断',
        'D': 'クレカ積立の売却タイミング',
        'E': 'リバランス実行判断',
    }

    prompt = f"""{context}

{'追加質問: ' + question if question else ''}

上記のデータを分析し、以下の構造で回答してください：

## 状況サマリー
（現在の状況を2-3行で要約）

## リスク評価
（主なリスクと懸念点を箇条書き）

## 定量分析
（数値ベースの重要ポイント）

## 推奨アクション候補
（選択肢A・B・Cを提示。各メリット/デメリット）

## Opusへの相談推奨事項
（最終判断にあたって確認すべき点）"""

    try:
        started = time.monotonic()
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_anthropic_usage(
            role="decision_support_sonnet_analysis",
            model=SONNET_MODEL,
            case=case,
            max_tokens=1500,
            started=started,
            prompt_chars=len(prompt),
            response=response,
            question_present=bool(question),
        )
    except Exception as e:
        _log_anthropic_usage(
            role="decision_support_sonnet_analysis",
            model=SONNET_MODEL,
            case=case,
            max_tokens=1500,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(prompt),
            status="error",
            question_present=bool(question),
            error=e,
        )
        raise

    return response.content[0].text


# ============================================================
# Opus 最終判断
# ============================================================

def final_judgment_with_opus(
    case:            str,
    context:         str,
    sonnet_analysis: str,
    user_preference: str = '',
) -> str:
    """
    Opusがすべての情報を統合して最終判断を下す。

    Args:
        case:             'A'〜'E'
        context:          元のコンテキスト
        sonnet_analysis:  Sonnetの分析結果
        user_preference:  ユーザーの追加意見・条件

    Returns:
        Opusの最終判断テキスト
    """
    from almanac.llm_safety import assert_book_aware_allowed, BookAwareDisabled, log_book_aware_call
    try:
        assert_book_aware_allowed(provider="anthropic")
    except BookAwareDisabled as e:
        log_book_aware_call(role="decision_support_opus", model=OPUS_MODEL,
                             fields=["portfolio_context", "case_context", "sonnet_analysis"], status="blocked")
        return f"（この機能は現在プライバシーモードにより無効化されています: {e}）"

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

    prompt = f"""以下の情報をすべて踏まえて、最終的な投資判断を下してください。

【元データ】
{context}

【Sonnetの分析】
{sonnet_analysis}

{'【ユーザーの補足】\n' + user_preference if user_preference else ''}

最終判断として以下を明確に答えてください：

## 最終判断
**[実行する / 見送る / 条件付き実行]**

（1-2文で核心的な理由を述べる）

## 具体的アクション
（実行するなら: 何を、いつ、いくらで）
（見送るなら: 次の確認タイミング）

## リスク管理
（損切りライン / ポジションサイズ / モニタリング指標）

## 長期戦略との整合性
（この判断がポートフォリオ全体の長期目標に合致しているか）"""

    try:
        started = time.monotonic()
        response = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': prompt}],
        )
        _log_anthropic_usage(
            role="decision_support_final_judgment",
            model=OPUS_MODEL,
            case=case,
            max_tokens=1000,
            started=started,
            prompt_chars=len(prompt),
            response=response,
            user_preference_present=bool(user_preference),
        )
    except Exception as e:
        _log_anthropic_usage(
            role="decision_support_final_judgment",
            model=OPUS_MODEL,
            case=case,
            max_tokens=1000,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(prompt),
            status="error",
            user_preference_present=bool(user_preference),
            error=e,
        )
        raise

    return response.content[0].text


# ============================================================
# メインAPI（Streamlitから呼び出す）
# ============================================================

def run_case_a(ticker: str, signal: str, strategy: str, question: str = '') -> dict:
    context  = _build_case_a(ticker, signal, strategy)
    analysis = analyze_with_sonnet('A', context, question)
    return {'case': 'A', 'context': context, 'sonnet_analysis': analysis}


def run_case_b(ticker: str, reason: str, question: str = '') -> dict:
    context  = _build_case_b(ticker, reason)
    analysis = analyze_with_sonnet('B', context, question)
    return {'case': 'B', 'context': context, 'sonnet_analysis': analysis}


def run_case_c(question: str = '') -> dict:
    """Deterministic employee-plan exit support; never sends the book to an LLM."""
    import employee_plan_exit as exit_planner
    import espp_plan_manager as km
    import portfolio_manager

    data = km.load_espp_data()
    snapshot = portfolio_manager.build_portfolio_snapshot()
    current_price = km.get_espp_price() or float(data.get("avg_cost") or 0)
    try:
        from tunable_params import get as _tp_get
        raw_limit = _tp_get("employee_plan_hold_limit_pct")
        limit_pct = float(raw_limit) / 100.0 if raw_limit is not None else 0.08
    except Exception:
        limit_pct = 0.08
    proposal = exit_planner.build_exit_proposal(
        portfolio_total_jpy=float(snapshot.get("total_jpy") or 0),
        current_price_jpy=current_price,
        current_shares=float(data.get("current_shares") or 0),
        purchase_history=data.get("purchase_history") or [],
        limit_pct=limit_pct,
        window_config=exit_planner.load_insider_window(),
    )
    context = (
        "Employee share-plan concentration review. "
        "Monthly contributions continue; any exit is human execution only."
    )
    analysis = json.dumps(proposal, ensure_ascii=False, indent=2)
    return {
        'case': 'C',
        'context': context,
        'sonnet_analysis': analysis,
        'deterministic': True,
        'proposal': proposal,
    }


def run_case_d(person: str = 'husband', question: str = '') -> dict:
    context  = _build_case_d(person)
    analysis = analyze_with_sonnet('D', context, question)
    return {'case': 'D', 'context': context, 'sonnet_analysis': analysis}


def run_case_e(question: str = '') -> dict:
    context  = _build_case_e()
    analysis = analyze_with_sonnet('E', context, question)
    return {'case': 'E', 'context': context, 'sonnet_analysis': analysis}


def get_opus_judgment(case_result: dict, user_preference: str = '') -> str:
    """Sonnet分析済みの結果をOpusに送って最終判断を取得する。"""
    if case_result.get("case") == "C":
        return str(case_result.get("sonnet_analysis") or "")
    return final_judgment_with_opus(
        case             = case_result['case'],
        context          = case_result['context'],
        sonnet_analysis  = case_result['sonnet_analysis'],
        user_preference  = user_preference,
    )


def log_decision(case_result: dict, opus_judgment: str, action_taken: str):
    """意思決定の記録を保存する（月次検証用）。"""
    log_path = BASE_DIR / 'decision_log.json'
    logs     = []
    if log_path.exists():
        with open(log_path, encoding='utf-8') as f:
            logs = json.load(f)

    logs.append({
        'timestamp':      datetime.now().isoformat(),
        'case':           case_result['case'],
        'sonnet_summary': case_result['sonnet_analysis'][:500],
        'opus_judgment':  opus_judgment[:500],
        'action_taken':   action_taken,
    })

    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(logs[-100:], f, ensure_ascii=False, indent=2)  # 直近100件のみ保持


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    if not args:
        print('使い方:')
        print('  python decision_support.py A <ticker> "<シグナル>" "<戦略>"')
        print('  python decision_support.py B <ticker> "<買い増し理由>"')
        print('  python decision_support.py C')
        print('  python decision_support.py D [husband|wife]')
        print('  python decision_support.py E')
        sys.exit(0)

    case = args[0].upper()

    print(f'[Sonnet] {case}ケースを分析中...')
    if case == 'A' and len(args) >= 4:
        result = run_case_a(args[1], args[2], args[3])
    elif case == 'B' and len(args) >= 3:
        result = run_case_b(args[1], args[2])
    elif case == 'C':
        result = run_case_c()
    elif case == 'D':
        result = run_case_d(args[1] if len(args) > 1 else 'husband')
    elif case == 'E':
        result = run_case_e()
    else:
        print('引数が不正です')
        sys.exit(1)

    print('\n=== Sonnet分析 ===')
    print(result['sonnet_analysis'])

    ans = input('\n[Opus]で最終判断を取得しますか？ (y/N): ').strip().lower()
    if ans == 'y':
        pref = input('追加の条件・意見があれば入力（Enter でスキップ）: ').strip()
        print('\n[Opus] 最終判断中...')
        judgment = get_opus_judgment(result, pref)
        print('\n=== Opus最終判断 ===')
        print(judgment)

        action = input('\n実際に取ったアクションを入力（記録用）: ').strip()
        if action:
            log_decision(result, judgment, action)
            print('意思決定を記録しました（decision_log.json）')
