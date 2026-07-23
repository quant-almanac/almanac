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
履歴 < MIN_TRADES なら固定 3% fallback（aggressive スタンスに合わせた中位）。
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

MIN_TRADES_FOR_KELLY = 5   # これ未満は fallback
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

def aggregate_ticker_stats(
    recs: Optional[list] = None,
    min_trades: int = MIN_TRADES_FOR_KELLY,
) -> dict:
    """
    ai_recommendation_log.json の verified エントリから銘柄別統計を作る。

    Returns:
        {ticker: {'win_rate', 'avg_win_pct', 'avg_loss_pct', 'n', 'sufficient'}}
    """
    if recs is None:
        try:
            recs = json.loads(REC_LOG.read_text(encoding='utf-8'))
        except Exception:
            recs = []

    by_ticker: dict = {}
    for r in recs:
        if not r.get('verified'):
            continue
        outcome = r.get('outcome_pct')
        if outcome is None:
            continue
        action_type = (r.get('type') or '').lower()
        # buy/add系のみリターンの向きが素直に扱える。sell 系は向きを反転。
        if action_type in ('sell', 'trim', 'stop_loss', 'take_profit'):
            outcome = -float(outcome)
        else:
            outcome = float(outcome)

        ticker = (r.get('ticker') or '').upper()
        if not ticker:
            continue
        slot = by_ticker.setdefault(ticker, {'wins': [], 'losses': []})
        if outcome > 0:
            slot['wins'].append(outcome / 100.0)    # % → 小数
        elif outcome < 0:
            slot['losses'].append(abs(outcome) / 100.0)

    result = {}
    for ticker, s in by_ticker.items():
        n = len(s['wins']) + len(s['losses'])
        if n == 0:
            continue
        win_rate = len(s['wins']) / n
        avg_win  = (sum(s['wins']) / len(s['wins'])) if s['wins']  else 0.0
        avg_loss = (sum(s['losses'])/len(s['losses'])) if s['losses'] else 0.0
        result[ticker] = {
            'win_rate':     round(win_rate, 4),
            'avg_win_pct':  round(avg_win, 4),
            'avg_loss_pct': round(avg_loss, 4),
            'n':            n,
            'sufficient':   n >= min_trades,
        }
    return result


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
        inputs = {
            'win_rate':     entry.get('win_rate', 0),
            'avg_win_pct':  entry.get('avg_win_pct', 0),
            'avg_loss_pct': entry.get('avg_loss_pct', 0),
            'n':            entry.get('n', 0),
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
