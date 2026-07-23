"""
バックテスト比較: Black-Litterman vs Max Sharpe vs Min CVaR
2年間のParquetデータで各手法のウェイトを月次リバランスで再現し、
Sharpe/CVaR/MaxDD/Calmarを比較する。

使い方:
    python backtest_comparison.py
    python backtest_comparison.py --methods max_sharpe,min_cvar,black_litterman
    python backtest_comparison.py --start 2024-01-01 --end 2026-04-07
"""

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))


def _load_all_returns(lookback_days: int = 756) -> pd.DataFrame:
    """全保有銘柄のParquetデータを読み込みリターン行列を返す"""
    from portfolio_optimizer import _load_holdings_tickers, load_returns
    tickers = _load_holdings_tickers()
    return load_returns(tickers, lookback_days=lookback_days)


def _calc_metrics(returns_series: pd.Series, rf_annual: float = 0.045) -> dict:
    """ポートフォリオリターン系列からパフォーマンス指標を計算"""
    r = returns_series.dropna()
    if len(r) < 20:
        return {'error': 'データ不足'}

    annual_return = float((1 + r).prod() ** (252 / len(r)) - 1)
    annual_vol = float(r.std() * np.sqrt(252))
    sharpe = (annual_return - rf_annual) / (annual_vol + 1e-9)

    # CVaR 95%
    threshold = np.percentile(r, 5)
    cvar = float(r[r <= threshold].mean())

    # Max Drawdown
    cum = (1 + r).cumprod()
    roll_max = cum.cummax()
    drawdowns = (cum - roll_max) / roll_max
    max_dd = float(drawdowns.min())

    # Calmar
    calmar = annual_return / (abs(max_dd) + 1e-9)

    return {
        'annual_return': round(annual_return, 4),
        'annual_vol':    round(annual_vol, 4),
        'sharpe':        round(sharpe, 4),
        'cvar_95':       round(cvar, 6),
        'max_dd':        round(max_dd, 4),
        'calmar':        round(calmar, 4),
        'n_days':        len(r),
    }


def run_comparison(
    start_date: str = None,
    end_date: str = None,
    methods: list = None,
    rebalance_freq: str = 'M',   # 'M'=月次, 'W'=週次
) -> dict:
    """
    各最適化手法のウェイトで再現ポートフォリオを構築しパフォーマンス比較。

    Args:
        start_date: 開始日 (YYYY-MM-DD)。Noneなら2年前
        end_date:   終了日 (YYYY-MM-DD)。Noneなら今日
        methods:    比較手法リスト
        rebalance_freq: リバランス頻度

    Returns:
        {method: {annual_return, sharpe, cvar_95, max_dd, calmar, weights_snapshots}}
    """
    if methods is None:
        methods = ['max_sharpe', 'min_cvar', 'equal_risk', 'black_litterman', 'sparse_mean_cvar']

    if end_date is None:
        end_date = datetime.now().strftime('%Y-%m-%d')
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')

    print(f"バックテスト比較: {start_date} → {end_date}")
    print(f"   手法: {methods}")

    # 全期間のリターンデータを読み込み
    all_returns = _load_all_returns(lookback_days=1260)  # 5年分読み込み
    if all_returns.empty:
        return {'error': 'リターンデータ取得失敗'}

    # 期間でフィルタ
    all_returns.index = pd.to_datetime(all_returns.index)
    mask = (all_returns.index >= start_date) & (all_returns.index <= end_date)
    period_returns = all_returns.loc[mask]

    if len(period_returns) < 60:
        return {'error': f'期間内のデータが不足 ({len(period_returns)}日)'}

    print(f"   データ: {len(period_returns)}日, {len(period_returns.columns)}銘柄")

    results = {}

    for method in methods:
        print(f"\n   [{method}] ウォークフォワード最適化中...")
        try:
            portfolio_returns = _run_walkforward(
                period_returns, method, rebalance_freq, lookback_days=252
            )
            metrics = _calc_metrics(portfolio_returns)
            metrics['method'] = method
            results[method] = metrics
            print(f"   [{method}] Sharpe={metrics.get('sharpe','?'):.3f}, "
                  f"MaxDD={metrics.get('max_dd','?'):.2%}, "
                  f"年率={metrics.get('annual_return','?'):.2%}")
        except Exception as e:
            results[method] = {'method': method, 'error': str(e)}
            print(f"   [{method}] エラー: {e}")

    output = {
        'comparison': results,
        'period':     {'start': start_date, 'end': end_date},
        'rebalance':  rebalance_freq,
        'generated':  datetime.now().strftime('%Y-%m-%d %H:%M'),
        'summary':    _build_summary(results),
    }

    # 保存
    out_path = BASE_DIR / 'reports' / 'upgrade_comparison.json'
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n結果保存: {out_path}")

    return output


def _run_walkforward(
    returns: pd.DataFrame,
    method: str,
    freq: str,
    lookback_days: int = 252,
) -> pd.Series:
    """ウォークフォワード最適化: 各リバランス時点でウェイトを再計算"""
    from portfolio_optimizer import (
        optimize_pypfopt, optimize_skfolio, bl_optimize, WEIGHT_CONSTRAINTS
    )

    # リバランス日を生成（月末が休日の場合も対応するため実際の取引日に丸める）
    rebalance_dates = returns.resample(freq).last().index
    # 取引日インデックスに存在しない日付（週末・祝日）を直前の取引日に置き換える
    trading_index = returns.index
    rebalance_dates = pd.DatetimeIndex([
        trading_index[trading_index.searchsorted(d, side='right') - 1]
        if d not in trading_index else d
        for d in rebalance_dates
    ]).unique()

    portfolio_rets = []
    current_weights = None

    for i, reb_date in enumerate(rebalance_dates[:-1]):
        next_reb = rebalance_dates[i + 1]

        # 学習期間のリターンでウェイト計算
        train_end = reb_date
        train_start_idx = max(0, returns.index.get_loc(train_end) - lookback_days)
        train_returns = returns.iloc[train_start_idx:returns.index.get_loc(train_end) + 1]

        if len(train_returns) < 30:
            continue

        try:
            if method == 'sparse_mean_cvar':
                from portfolio_optimizer import sparse_mean_cvar_optimize
                res = sparse_mean_cvar_optimize(train_returns)
            elif method in ('min_cvar', 'max_diversification'):
                res = optimize_skfolio(train_returns, method=method)
            elif method == 'black_litterman':
                res = bl_optimize(train_returns)
            else:
                res = optimize_pypfopt(train_returns, method=method)

            weights = res.get('weights', {})
            if not weights:
                continue
            current_weights = weights
        except Exception:
            pass

        if current_weights is None:
            continue

        # 保有期間のリターンを計算
        hold_mask = (returns.index > reb_date) & (returns.index <= next_reb)
        hold_returns = returns.loc[hold_mask]

        for _, day_row in hold_returns.iterrows():
            port_ret = sum(
                current_weights.get(t, 0) * day_row.get(t, 0)
                for t in current_weights
                if t != '_cash'
            )
            portfolio_rets.append(port_ret)

    return pd.Series(portfolio_rets)


def _build_summary(results: dict) -> dict:
    """最良手法を特定するサマリーを生成"""
    valid = {k: v for k, v in results.items() if 'error' not in v}
    if not valid:
        return {'best_sharpe': None, 'best_calmar': None}

    best_sharpe = max(valid, key=lambda k: valid[k].get('sharpe', -999))
    best_calmar = max(valid, key=lambda k: valid[k].get('calmar', -999))
    bl_vs_baseline = None

    if 'black_litterman' in valid and 'max_sharpe' in valid:
        bl_s = valid['black_litterman'].get('sharpe', 0)
        ms_s = valid['max_sharpe'].get('sharpe', 0)
        bl_vs_baseline = round(bl_s - ms_s, 4)

    return {
        'best_sharpe':      best_sharpe,
        'best_calmar':      best_calmar,
        'bl_vs_max_sharpe': bl_vs_baseline,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ALMANAC バックテスト比較')
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--methods', default='max_sharpe,min_cvar,equal_risk,black_litterman')
    parser.add_argument('--freq', default='M', choices=['M', 'W'])
    args = parser.parse_args()

    methods = [m.strip() for m in args.methods.split(',')]
    result = run_comparison(
        start_date=args.start,
        end_date=args.end,
        methods=methods,
        rebalance_freq=args.freq,
    )

    if 'error' not in result:
        print('\n=== パフォーマンス比較サマリー ===')
        for method, metrics in result.get('comparison', {}).items():
            if 'error' in metrics:
                print(f'{method}: エラー — {metrics["error"]}')
            else:
                print(f'{method}: Sharpe={metrics["sharpe"]:.3f} | '
                      f'年率={metrics["annual_return"]:.2%} | '
                      f'MaxDD={metrics["max_dd"]:.2%} | '
                      f'Calmar={metrics["calmar"]:.2f}')
        s = result.get('summary', {})
        print(f'\n最良Sharpe: {s.get("best_sharpe")} | '
              f'BL vs MaxSharpe: {s.get("bl_vs_max_sharpe")}')
