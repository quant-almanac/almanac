"""
A-6: Half-Kelly ポジションサイジング
-------------------------------------
ai_recommendation_log.json の verified outcomes から銘柄別
(win_rate, avg_win_pct, avg_loss_pct) を集計し、half-Kelly で
ポジション比率を提案する。

  kelly_fraction = 0.5 * (p*b - q) / b
    p = win_rate, q = 1-p, b = avg_win / avg_loss

  投資タイプ別上限:
    long   5%
    medium 3%
    swing  2%

負の Kelly（EV ≤ 0）は entry reject。
履歴 < MIN_TRADES なら entry を許可せず、観察用 0.5% だけを表示する。

Stage 6A (2026-07): この統計は「AI推奨シグナルの的中率」であって
「実際に執行した売買の勝率」ではない — 呼び出し側・表示側は
recommendation_kelly_stats() の名前が示す通り、これを実売買の
パフォーマンス指標として提示してはならない。現状の入力統計には
以下の是正を実施済み:
  - buy/add/dca のみを母集団にする (sell/trim/stop_loss/take_profit を
    符号反転して混ぜていた旧実装は方向の違う判断を同一母集団として
    扱っていた — 新規エントリーのサイジングに使う統計として不適切)
  - (analysis_id, ticker, direction) で重複除去 (同一分析からの
    重複ログ行が母集団を水増ししない)
  - 統計キーを (ticker, direction, horizon) に拡張。検証器が保存する
    5/20/60営業日を swing/medium/long に対応させ、必要ホライズンが
    未観測の推薦は母集団へ入れない
  - analysis_id と signal_evaluable が明示された新契約の行だけを使う。
    旧ログは安全に重複除去・入力品質判定できないため監査表示専用とする

解消済み (2026-07-28):
  - _log_recommendations() は凍結済み decision_price（無ければ
    limit_price）だけを使い、両方無ければ外部価格を再取得せず
    signal_evaluable=false とする。price_at_rec_source は
    'decision_price' | 'limit_price' | None。
  - signal_evaluable / execution_eligible を別々に記録する。現金不足等で
    execution_eligible=false でも signal_evaluable=true なら予測母集団へ残す。
    stale/unknown入力・provenance不明・価格不明は signal_evaluable=false。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
REC_LOG  = BASE_DIR / 'ai_recommendation_log.json'

# ── 上限 ──────────────────────────────────────────────
CAPS_BY_ITYPE = {
    'long':   0.05,
    'medium': 0.03,
    'swing':  0.02,
}

MIN_TRADES_FOR_KELLY = 20  # 低標本の勝率を絶対上限に使わない
# P1-20: 旧 fallback 3% / entry_allowed=True は「履歴不足でも 3% で入る」default-allow で、
# 行動量最大化バイアスの原因の 1 つだった。fail-safe で entry_allowed=False を default にし、
# Policy Engine / 人間判断で例外的に許可する流れに変更。観察用の最小サイズだけ別途定義。
FALLBACK_SIZE_PCT          = 0.005  # 0.5% — 例外的に許可する場合の観察用サイズ
FALLBACK_ENTRY_ALLOWED     = False  # 履歴不足時は default-deny
KELLY_SCALE          = 0.5     # half-Kelly


# ============================================================
# Core math
# ============================================================

def kelly_fraction(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    scale: float = KELLY_SCALE,
) -> float:
    """
    half-Kelly 分数。Return 0 if EV <= 0 or invalid inputs.

    Args:
        win_rate:     勝率 (0〜1)
        avg_win_pct:  平均利益（小数、絶対値）
        avg_loss_pct: 平均損失（小数、絶対値）
        scale:        Kelly の何分の一か（default 0.5 = half-Kelly）

    Returns:
        提案配分比率（小数、0以上）。EV<=0 は 0。
    """
    if not (0 < win_rate < 1):
        return 0.0
    if avg_win_pct <= 0 or avg_loss_pct <= 0:
        return 0.0

    p = win_rate
    q = 1 - p
    b = avg_win_pct / avg_loss_pct
    raw = (p * b - q) / b
    if raw <= 0:
        return 0.0
    return float(scale * raw)


# ============================================================
# 履歴集計（verified outcomes から）
# ============================================================

# Stage 6A: 新規エントリーのサイジングに使ってよい action type。
# take_profit は正の期待リターンを持つが「保有ポジションを閉じる/縮小する」
# 決定であり、将来の新規 buy が成功するかどうかの情報を持たない。
BUY_SIDE_TYPES = frozenset({'buy', 'add', 'dca'})

HORIZON_BY_ITYPE = {
    'swing': '5d',
    'medium': '20d',
    'long': '60d',
}


def aggregate_ticker_stats(
    recs: Optional[list] = None,
    min_trades: int = MIN_TRADES_FOR_KELLY,
) -> dict:
    """
    ai_recommendation_log.json の verified エントリから銘柄別統計を作る。

    Stage 6A: 母集団を buy/add/dca のみに限定する (sell/trim/stop_loss/
    take_profit を符号反転して混ぜていた旧実装は、方向の異なる判断を
    同一母集団として扱っており、新規エントリーのサイジング根拠として
    不適切だった)。(analysis_id, ticker, direction) で重複除去し、
    同一分析からの重複ログ行が母集団を水増ししないようにする。

    Returns:
        {ticker: {'win_rate', 'avg_win_pct', 'avg_loss_pct', 'n', 'sufficient',
                   'direction', 'horizon'}}

    Note: この統計は「AI推奨シグナルの的中率」であり実売買の勝率ではない
    (モジュール docstring 参照)。
    """
    if recs is None:
        try:
            recs = json.loads(REC_LOG.read_text(encoding='utf-8'))
        except Exception:
            recs = []

    by_group: dict = {}
    seen_dedup_keys: set = set()
    for r in recs:
        if not r.get('verified'):
            continue
        # Stage 6A migration boundary: legacy rows have neither a stable
        # analysis identity nor an explicit input-quality decision.  Counting
        # them would silently fail open and can inflate n with repeated runs.
        if r.get('signal_evaluable') is not True:
            continue
        outcome = r.get('outcome_pct')
        if outcome is None:
            continue
        action_type = (r.get('type') or '').lower()
        if action_type not in BUY_SIDE_TYPES:
            continue

        ticker = (r.get('ticker') or '').upper()
        if not ticker:
            continue
        itype = str(r.get('tier') or r.get('investment_type') or '').lower()
        horizon = HORIZON_BY_ITYPE.get(itype)
        if horizon is None:
            continue
        horizon_row = (r.get('horizons') or {}).get(horizon)
        if isinstance(horizon_row, dict):
            outcome = horizon_row.get('outcome_pct')
        elif horizon == '5d':
            outcome = r.get('outcome_pct')
        else:
            outcome = None
        if outcome is None:
            continue

        # (analysis_id, ticker, direction) で重複除去。analysis_id の無い
        # legacy 行は監査用に保持するが sizing 母集団には入れない。
        analysis_id = r.get('analysis_id')
        if not analysis_id:
            continue
        dedup_key = (analysis_id, ticker, 'buy', horizon)
        if dedup_key in seen_dedup_keys:
            continue
        seen_dedup_keys.add(dedup_key)

        outcome = float(outcome)
        slot = by_group.setdefault((ticker, horizon), {'wins': [], 'losses': []})
        if outcome > 0:
            slot['wins'].append(outcome / 100.0)    # % → 小数
        elif outcome < 0:
            slot['losses'].append(abs(outcome) / 100.0)

    result = {}
    for (ticker, horizon), s in by_group.items():
        n = len(s['wins']) + len(s['losses'])
        if n == 0:
            continue
        win_rate = len(s['wins']) / n
        avg_win  = (sum(s['wins']) / len(s['wins'])) if s['wins']  else 0.0
        avg_loss = (sum(s['losses'])/len(s['losses'])) if s['losses'] else 0.0
        metrics = {
            'win_rate':     round(win_rate, 4),
            'avg_win_pct':  round(avg_win, 4),
            'avg_loss_pct': round(avg_loss, 4),
            'n':            n,
            'sufficient':   n >= min_trades,
            'direction':    'buy',
            'horizon':      horizon,
        }
        ticker_slot = result.setdefault(ticker, {'by_horizon': {}})
        ticker_slot['by_horizon'][horizon] = metrics
    for ticker_slot in result.values():
        # Backward-compatible top-level metrics for display callers.  The
        # sizing decision below always selects the investment-type horizon.
        preferred = next(
            (
                ticker_slot['by_horizon'][h]
                for h in ('20d', '5d', '60d')
                if h in ticker_slot['by_horizon']
            ),
            None,
        )
        if preferred:
            ticker_slot.update(preferred)
    return result


# Stage 6A: この統計は実売買の勝率ではなく AI 推奨シグナルの的中率である
# ことを名前で明示するエイリアス。内部実装は aggregate_ticker_stats() と
# 同一 (後方互換のため元の名前も残す)。
recommendation_kelly_stats = aggregate_ticker_stats


# ============================================================
# サイズ提案
# ============================================================

def suggest_size_pct(
    ticker: str,
    investment_type: str,
    stats: Optional[dict] = None,
    overrides: Optional[dict] = None,
) -> dict:
    """
    1 銘柄ぶんの配分比率を提案。

    Args:
        ticker:           銘柄
        investment_type:  'long' | 'medium' | 'swing'
        stats:            aggregate_ticker_stats() の返り値（None なら自動計算）
        overrides:        テスト用 {'win_rate':0.6, 'avg_win_pct':0.05, 'avg_loss_pct':0.03}

    Returns:
        {
          'ticker':          ...,
          'investment_type': ...,
          'entry_allowed':   bool,
          'size_pct':        0〜cap,
          'method':          'kelly' | 'fallback' | 'rejected',
          'kelly_raw':       half-Kelly 生値,
          'cap':             cap,
          'inputs':          {win_rate, avg_win_pct, avg_loss_pct, n},
          'reason':          説明,
        }
    """
    itype = (investment_type or 'medium').lower()
    cap   = CAPS_BY_ITYPE.get(itype, 0.03)
    ticker_upper = (ticker or '').upper()

    if overrides:
        inputs = overrides
        n = overrides.get('n', MIN_TRADES_FOR_KELLY)
        sufficient = overrides.get('sufficient', n >= MIN_TRADES_FOR_KELLY)
    else:
        if stats is None:
            stats = aggregate_ticker_stats()
        entry = stats.get(ticker_upper, {})
        horizon = HORIZON_BY_ITYPE.get(itype, '20d')
        if isinstance(entry.get('by_horizon'), dict):
            entry = entry['by_horizon'].get(horizon, {})
        inputs = {
            'win_rate':     entry.get('win_rate', 0),
            'avg_win_pct':  entry.get('avg_win_pct', 0),
            'avg_loss_pct': entry.get('avg_loss_pct', 0),
            'n':            entry.get('n', 0),
            'horizon':      entry.get('horizon', horizon),
        }
        sufficient = entry.get('sufficient', False)

    # 履歴不足 → fail-safe fallback（P1-20: default-deny + 観察用 size のみ提示）
    if not sufficient:
        size = min(FALLBACK_SIZE_PCT, cap)
        return {
            'ticker':          ticker_upper,
            'investment_type': itype,
            'entry_allowed':   FALLBACK_ENTRY_ALLOWED,
            'size_pct':        round(size, 4),
            'method':          'fallback',
            'kelly_raw':       None,
            'cap':             cap,
            'inputs':          inputs,
            'reason':          (
                f'履歴 {inputs["n"]} 件 < {MIN_TRADES_FOR_KELLY}: 期待値不確定のため entry_allowed=False。'
                f' 例外許可する場合の観察用 size は {FALLBACK_SIZE_PCT*100:.1f}% (cap {cap*100:.0f}%)。'
            ),
        }

    kelly = kelly_fraction(
        inputs['win_rate'], inputs['avg_win_pct'], inputs['avg_loss_pct'],
    )

    if kelly <= 0:
        return {
            'ticker':          ticker_upper,
            'investment_type': itype,
            'entry_allowed':   False,
            'size_pct':        0.0,
            'method':          'rejected',
            'kelly_raw':       round(kelly, 4),
            'cap':             cap,
            'inputs':          inputs,
            'reason':          f'Kelly ≤ 0（EV 負）: win_rate={inputs["win_rate"]*100:.0f}% / '
                                f'avg_win={inputs["avg_win_pct"]*100:.1f}% / '
                                f'avg_loss={inputs["avg_loss_pct"]*100:.1f}% → エントリー禁止',
        }

    clipped = min(kelly, cap)
    return {
        'ticker':          ticker_upper,
        'investment_type': itype,
        'entry_allowed':   True,
        'size_pct':        round(clipped, 4),
        'method':          'kelly',
        'kelly_raw':       round(kelly, 4),
        'cap':             cap,
        'inputs':          inputs,
        'reason':          f'half-Kelly {kelly*100:.1f}% → cap {cap*100:.0f}% で {clipped*100:.1f}% に制限',
    }


def suggest_sizes_batch(
    tickers_with_itype: list[tuple[str, str]],
) -> list[dict]:
    """複数銘柄を一括処理"""
    stats = aggregate_ticker_stats()
    return [suggest_size_pct(t, itype, stats=stats) for t, itype in tickers_with_itype]


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'

    if cmd == 'stats':
        s = aggregate_ticker_stats()
        if not s:
            print('verified 履歴なし（verifier を先に実行してください）')
        else:
            print(f'銘柄別統計 {len(s)} 件:')
            for t, v in sorted(s.items(), key=lambda x: -x[1]['n'])[:20]:
                mark = '✓' if v['sufficient'] else ' '
                print(f'  {mark} {t}: n={v["n"]} wr={v["win_rate"]*100:.0f}% '
                      f'avg_win={v["avg_win_pct"]*100:.2f}% avg_loss={v["avg_loss_pct"]*100:.2f}%')

    elif cmd == 'size' and len(sys.argv) >= 4:
        ticker = sys.argv[2]
        itype  = sys.argv[3]
        result = suggest_size_pct(ticker, itype)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == 'test':
        # 合成ケース
        cases = [
            ('NVDA', 'long',   {'win_rate': 0.6, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.03, 'n': 10}),
            ('CRWV', 'swing',  {'win_rate': 0.4, 'avg_win_pct': 0.08, 'avg_loss_pct': 0.04, 'n': 8}),
            ('META', 'medium', {'win_rate': 0.55, 'avg_win_pct': 0.04, 'avg_loss_pct': 0.05, 'n': 12}),
            ('TEST1', 'long',  {'win_rate': 0.3, 'avg_win_pct': 0.02, 'avg_loss_pct': 0.05, 'n': 10}),  # negative Kelly
            ('TEST2', 'swing', {'win_rate': 0.5, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.05, 'n': 2}),   # insufficient
        ]
        for t, itype, o in cases:
            r = suggest_size_pct(t, itype, overrides=o)
            print(f'  {t}({itype}): size={r["size_pct"]*100:.1f}% method={r["method"]} — {r["reason"]}')

    else:
        print('Usage: kelly_sizing.py [stats | size <ticker> <itype> | test]')
