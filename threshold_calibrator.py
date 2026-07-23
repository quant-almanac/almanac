"""
threshold_calibrator.py — シグナル履歴からスクリーニング閾値を自動キャリブレーション

signal_history.json の実績データを元に、各戦略・レジームの閾値を評価し、
改善提案を calibration_report.json に出力する。

使い方:
  python threshold_calibrator.py            # 分析・レポート出力
  python threshold_calibrator.py --apply    # 提案をregime_params.pyに自動適用（実験的）

注意: データが少ない時期はノイズが大きい。最低30件以上のアウトカムが揃ってから適用推奨。
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
SIGNAL_HISTORY_FILE  = BASE_DIR / "signal_history.json"
REPORT_FILE          = BASE_DIR / "calibration_report.json"
THRESHOLDS_FILE      = BASE_DIR / "calibrated_thresholds.json"   # スクリーナーが読む簡素版
MIN_SAMPLES = 10  # 分析に必要な最低サンプル数

# キャリブレーション最大調整幅（±10%）— 暴走防止のハードキャップ
MAX_ADJUST_PCT = 0.10


def load_history() -> list:
    if not SIGNAL_HISTORY_FILE.exists():
        return []
    with open(SIGNAL_HISTORY_FILE) as f:
        return json.load(f)


def _sharpe(returns: list) -> float | None:
    if len(returns) < 3:
        return None
    import statistics
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    if std == 0:
        return None
    return round(mean / std * (52 ** 0.5), 2)


def analyze_threshold_sensitivity(history: list) -> dict:
    """
    各戦略の RSI / vol_ratio / mom5d ごとに、閾値を変えたときの
    勝率・平均リターンを計算する感度分析。

    Returns: {strategy: {param: {threshold_value: {win_rate, avg_return, count}}}}
    """
    # アウトカムありのBUYシグナルのみ使用
    buy_records = [
        r for r in history
        if r.get('signal') == 'BUY' and r.get('outcome_5d') is not None
    ]

    if not buy_records:
        return {}

    sensitivity: dict = {}

    strategies = list({r.get('strategy', '') for r in buy_records if r.get('strategy')})
    for strategy in strategies:
        strat_records = [r for r in buy_records if r.get('strategy') == strategy]
        if len(strat_records) < MIN_SAMPLES:
            continue

        sensitivity[strategy] = {}

        # RSI 感度 (逆張りは低い方が良い、モメンタムは高い方が良い)
        rsi_vals = sorted({round(r.get('rsi', 0), 0) for r in strat_records if r.get('rsi') is not None})
        if len(rsi_vals) >= 3:
            rsi_analysis = {}
            for threshold in [20, 25, 30, 35, 40, 50, 55, 60, 65, 70]:
                if strategy in ('逆張り', 'ギャップダウン', 'イベントドリブン後'):
                    subset = [r for r in strat_records if r.get('rsi', 100) <= threshold]
                else:
                    subset = [r for r in strat_records if r.get('rsi', 0) >= threshold]
                if len(subset) >= MIN_SAMPLES:
                    returns = [r['outcome_5d'] for r in subset]
                    wins = [x for x in returns if x > 0]
                    rsi_analysis[threshold] = {
                        'count': len(subset),
                        'win_rate': round(len(wins) / len(subset) * 100, 1),
                        'avg_return': round(sum(returns) / len(returns), 2),
                        'sharpe': _sharpe(returns),
                    }
            if rsi_analysis:
                sensitivity[strategy]['rsi'] = rsi_analysis

        # vol_ratio 感度
        vol_analysis = {}
        for threshold in [1.0, 1.2, 1.5, 2.0, 2.5, 3.0]:
            subset = [r for r in strat_records if r.get('volume_ratio', 0) >= threshold]
            if len(subset) >= MIN_SAMPLES:
                returns = [r['outcome_5d'] for r in subset]
                wins = [x for x in returns if x > 0]
                vol_analysis[threshold] = {
                    'count': len(subset),
                    'win_rate': round(len(wins) / len(subset) * 100, 1),
                    'avg_return': round(sum(returns) / len(returns), 2),
                    'sharpe': _sharpe(returns),
                }
        if vol_analysis:
            sensitivity[strategy]['vol_ratio'] = vol_analysis

    return sensitivity


def generate_recommendations(sensitivity: dict, current_params: dict | None = None) -> list:
    """
    感度分析結果から、改善が見込まれる閾値変更の提案リストを生成する。
    各提案には expected_improvement（勝率改善幅 pt）を付与する。
    """
    recommendations = []

    for strategy, params in sensitivity.items():
        for param_name, threshold_data in params.items():
            if not threshold_data:
                continue

            # Sharpe が最大になる閾値を探す
            best_threshold = None
            best_sharpe = None
            best_win_rate = None

            for threshold, metrics in sorted(threshold_data.items()):
                sharpe = metrics.get('sharpe')
                win_rate = metrics.get('win_rate', 0)
                if sharpe is not None and (best_sharpe is None or sharpe > best_sharpe):
                    best_sharpe = sharpe
                    best_threshold = threshold
                    best_win_rate = win_rate

            if best_threshold is None:
                continue

            # 現在の閾値との比較（regime_params.py から取得）
            current_val = _get_current_param(strategy, param_name)
            if current_val is not None and abs(best_threshold - current_val) < 1:
                continue  # 現在値と同じなら提案不要

            # 現在の閾値での性能
            current_metrics = threshold_data.get(current_val or best_threshold, {})
            current_win_rate = current_metrics.get('win_rate', 0)

            improvement = round((best_win_rate or 0) - current_win_rate, 1)
            if improvement <= 2:
                continue  # 2pt以下の改善は提案しない

            recommendations.append({
                'strategy': strategy,
                'param': param_name,
                'current_value': current_val,
                'recommended_value': best_threshold,
                'expected_win_rate': best_win_rate,
                'expected_sharpe': best_sharpe,
                'expected_improvement_pt': improvement,
                'sample_count': threshold_data[best_threshold]['count'],
                'note': f"{param_name}={best_threshold} で BUY 勝率が約{improvement}pt改善見込み",
            })

    # 改善幅順に並べる
    recommendations.sort(key=lambda x: x['expected_improvement_pt'], reverse=True)
    return recommendations


def _get_current_param(strategy: str, param_name: str) -> float | None:
    """regime_params.py の B_中立・US パラメータから現在値を取得する"""
    try:
        from regime_params import REGIME_PARAMS
        p = REGIME_PARAMS.get(strategy, {}).get('US', {}).get('B_中立', {})
        if not p:
            return None
        mapping = {
            'rsi': p.get('rsi', p.get('rsi_min')),
            'vol_ratio': p.get('vol'),
            'mom5d': p.get('mom5d'),
        }
        return mapping.get(param_name)
    except Exception:
        return None


def print_report(sensitivity: dict, recommendations: list) -> None:
    print("\n===== スクリーニング閾値 キャリブレーションレポート =====\n")

    if not sensitivity:
        print("分析に十分なデータがありません（最低10件のBUYアウトカムが必要）。")
        print("screener.py が毎日実行されていれば、数週間後に自動的にデータが蓄積されます。")
        return

    print("--- 感度分析サマリー ---")
    for strategy, params in sensitivity.items():
        print(f"\n戦略: {strategy}")
        for param, data in params.items():
            best = max(data.items(), key=lambda x: x[1].get('sharpe') or -999)
            print(f"  {param} 最適値: {best[0]} (Sharpe {best[1].get('sharpe')}, 勝率 {best[1].get('win_rate')}%, N={best[1].get('count')})")

    if not recommendations:
        print("\n現在の閾値は最適に近い状態です。改善提案なし。")
        return

    print("\n--- 改善提案 (勝率改善幅順) ---")
    for i, rec in enumerate(recommendations[:5], 1):
        print(f"{i}. [{rec['strategy']}] {rec['param']}: {rec['current_value']} → {rec['recommended_value']}")
        print(f"   期待勝率: {rec['expected_win_rate']}% (現在比 +{rec['expected_improvement_pt']}pt)")
        print(f"   Sharpe: {rec['expected_sharpe']}  サンプル: {rec['sample_count']}件")


def write_calibrated_thresholds(recommendations: list, freeze: bool = False) -> dict:
    """
    レコメンデーションを {strategy: {param: clipped_value}} 形式に整形し
    calibrated_thresholds.json に保存する（スクリーナーがランタイムで読む）。

    暴走防止: 現在値の ±MAX_ADJUST_PCT 範囲にハードキャップ。
    """
    out: dict = {"updated_at": datetime.now().isoformat(timespec="seconds"), "thresholds": {}}
    if freeze or not recommendations:
        # 凍結 or 提案ゼロ: thresholds は空（screener はデフォルト値を使う）
        try:
            from utils import atomic_write_json
            atomic_write_json(str(THRESHOLDS_FILE), out)
        except ImportError:
            with open(THRESHOLDS_FILE, "w") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
        return out

    for rec in recommendations:
        strategy = rec.get("strategy")
        param    = rec.get("param")
        target   = rec.get("recommended_value")
        current  = rec.get("current_value")
        if not strategy or not param or target is None:
            continue

        # ±10% ハードキャップ（current が None の場合は target をそのまま使う）
        if isinstance(current, (int, float)) and current > 0:
            min_v = current * (1 - MAX_ADJUST_PCT)
            max_v = current * (1 + MAX_ADJUST_PCT)
            clipped = max(min_v, min(max_v, float(target)))
        else:
            clipped = float(target)

        out["thresholds"].setdefault(strategy, {})[param] = round(clipped, 2)

    try:
        from utils import atomic_write_json
        atomic_write_json(str(THRESHOLDS_FILE), out)
    except ImportError:
        with open(THRESHOLDS_FILE, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
    return out


def load_calibrated_thresholds() -> dict:
    """スクリーナー側から呼ばれる軽量ローダー。失敗時は空 dict。"""
    if not THRESHOLDS_FILE.exists():
        return {}
    try:
        with open(THRESHOLDS_FILE) as f:
            data = json.load(f)
        return data.get("thresholds", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


if __name__ == '__main__':
    apply_changes = '--apply' in sys.argv

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 閾値キャリブレーション開始...")
    history = load_history()
    print(f"  総レコード数: {len(history)}")

    buy_with_outcome = [r for r in history if r.get('signal') == 'BUY' and r.get('outcome_5d') is not None]
    print(f"  BUYアウトカム確定: {len(buy_with_outcome)}件")

    # P2-13: outcome 未評価率を棚卸しして calibration_report に同梱
    try:
        from signal_tracker import audit_outcomes
        audit = audit_outcomes(history)
    except Exception as _e:
        audit = {'error': str(_e)}

    sensitivity = analyze_threshold_sensitivity(history)
    recommendations = generate_recommendations(sensitivity)
    print_report(sensitivity, recommendations)

    # P2-13: 欠損率が高い場合はキャリブレーション結果を信用しない（freeze 提案）
    freeze_required = bool(audit.get('null_5d_pct', 0) > 30)
    if freeze_required:
        print(f"\n⚠️  outcome 5d 欠損率 {audit['null_5d_pct']}% > 30%: "
              f"キャリブレーション結果の採用は保留してください")

    # レポート保存
    report = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total_buy_outcomes': len(buy_with_outcome),
        'audit': audit,                                  # P2-13
        'freeze_recommendations': freeze_required,       # P2-13
        'sensitivity': {
            strat: {
                param: {str(k): v for k, v in thresh.items()}
                for param, thresh in params.items()
            }
            for strat, params in sensitivity.items()
        },
        'recommendations': recommendations if not freeze_required else [],
        'status': 'frozen_low_audit' if freeze_required else ('insufficient_data' if not sensitivity else 'ok'),
        'min_samples_required': MIN_SAMPLES,
    }

    try:
        from utils import atomic_write_json
        atomic_write_json(str(REPORT_FILE), report)
    except ImportError:
        with open(REPORT_FILE, 'w') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n📄 レポート保存: {REPORT_FILE}")

    # スクリーナー連携用の軽量ファイルも更新
    write_calibrated_thresholds(recommendations, freeze=freeze_required)
    print(f"📐 キャリブ済み閾値: {THRESHOLDS_FILE}")

    # --apply オプション: 上位1件を regime_params.py に自動適用
    if apply_changes and recommendations:
        top = recommendations[0]
        print(f"\n⚠️  --apply モード: {top['strategy']} / {top['param']} = {top['recommended_value']} を適用します")
        print("   (regime_params.py を手動確認してから適用することを推奨します)")
        # 実際の適用はユーザーの確認後に実施するため、ここでは提案のみ表示
        print("   → 現在は提案のみ表示（自動書き換えは手動確認後に実行）")

    print("完了。")
