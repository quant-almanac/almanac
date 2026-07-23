"""
GINN: GARCH-Informed Neural Network (ICAIF 2024)
GJR-GARCHの予測ボラティリティをLSTMの物理制約として使用し、
Cornish-Fisher VaR/CVaRの入力ボラティリティ精度を改善する。

アーキテクチャ:
    入力: [returns_t, σ_GARCH_t, VIX_t, regime_state_t] × 60日シーケンス
    モデル: 2層LSTM (hidden=64) + Linear出力
    損失:   MSE(σ_pred, |ε_t|) + λ・MSE(σ_pred, σ_GARCH)
    λ=0.3  (GARCHへの正則化強度)

使い方:
    python ginn_model.py --train         # 全保有銘柄で学習
    python ginn_model.py --train --ticker NVDA  # 単一銘柄
"""

import argparse
import json
import sys
import warnings
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent
MODEL_PATH = BASE_DIR / 'models' / 'ginn_model.pt'
sys.path.insert(0, str(BASE_DIR))

# モデルパスディレクトリ作成
MODEL_PATH.parent.mkdir(exist_ok=True)

# ============================================================
# PyTorch モデル定義
# ============================================================

def _get_torch():
    try:
        import torch
        import torch.nn as nn
        return torch, nn
    except ImportError:
        return None, None


class GINNModel:
    """
    GARCHインフォームドLSTMボラティリティ予測モデル。
    PyTorchが利用不可の場合はNoneを返す。
    """

    def __init__(self, input_size: int = 4, hidden_size: int = 64, num_layers: int = 2):
        torch, nn = _get_torch()
        if torch is None:
            self._model = None
            return

        class _LSTM(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                    batch_first=True, dropout=0.1)
                self.fc = nn.Linear(hidden_size, 1)
                self.softplus = nn.Softplus()  # ボラは常に正

            def forward(self, x):
                out, _ = self.lstm(x)
                return self.softplus(self.fc(out[:, -1, :])).squeeze(-1)

        self._model = _LSTM()
        self._device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
        self._model = self._model.to(self._device)

    @property
    def model(self):
        return self._model

    def is_available(self) -> bool:
        return self._model is not None


def _build_sequences(
    returns: pd.Series,
    garch_sigma: pd.Series,
    vix_series: pd.Series | None,
    regime_series: pd.Series | None,
    seq_len: int = 60,
    fit_stats: dict | None = None,
) -> tuple:
    """
    LSTM入力シーケンスとターゲット（翌日絶対リターン）を構築。

    P2-8:
    - ターゲットを r.iloc[i+1] の絶対値に変更（docstring と一致）
    - ループ範囲を range(seq_len, len(feat) - 2) に調整（i+1 が有効）
    - 正規化統計（std, mean）は fit_stats に外部化 → train のみで fit して
      test に同じ統計を適用することで data leak を防ぐ
    - fit_stats=None の場合は入力 series で fit（後方互換、forecast 時用）

    入力特徴: [returns, σ_GARCH, VIX, regime_state]
    Returns:
        (X, y, stats) — stats は正規化統計（後段で test に再利用可）
    """
    r = returns.dropna()
    σ = garch_sigma.reindex(r.index).ffill().fillna(0.01)
    vix = vix_series.reindex(r.index).fillna(0.2) if vix_series is not None else pd.Series(0.2, index=r.index)
    reg = regime_series.reindex(r.index).fillna(1.0) if regime_series is not None else pd.Series(1.0, index=r.index)

    # 正規化統計: fit_stats があれば再利用、無ければ入力から fit
    if fit_stats is None:
        fit_stats = {
            'r_std':    float(r.std() + 1e-9),
            'sigma_mu': float(σ.mean() + 1e-9),
        }
    r_norm   = r / fit_stats['r_std']
    σ_norm   = σ / fit_stats['sigma_mu']
    vix_norm = vix / 30.0   # VIX 0-1スケール概算（定数スケール）
    reg_norm = reg / 3.0    # レジーム 0-1スケール（定数スケール）

    feat = pd.DataFrame({'r': r_norm, 'sigma': σ_norm, 'vix': vix_norm, 'regime': reg_norm})

    X, y = [], []
    # P2-8: i+1 が有効な範囲でループ（翌日のリアライズを target とする）
    for i in range(seq_len, len(feat) - 2):
        X.append(feat.iloc[i - seq_len:i].values)
        y.append(abs(float(r.iloc[i + 1])))   # 翌日の絶対リターン = 実現ボラ代理変数

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32), fit_stats


def train_ginn(
    tickers: list[str] | None = None,
    lookback_days: int = 1260,
    seq_len: int = 60,
    epochs: int = 50,
    lr: float = 1e-3,
    garch_lambda: float = 0.3,
) -> dict:
    """
    保有銘柄のParquetデータでGINNを学習し、models/ginn_model.pt に保存。

    Args:
        tickers: 学習対象ティッカー（None=全保有銘柄）
        lookback_days: 学習期間（日数）
        seq_len: LSTMシーケンス長
        epochs: 学習エポック数
        lr: 学習率
        garch_lambda: GARCH制約の正則化強度λ

    Returns:
        {'success': bool, 'loss': 最終損失, 'n_samples': サンプル数}
    """
    torch, nn = _get_torch()
    if torch is None:
        return {'success': False, 'error': 'torch未インストール'}

    from portfolio_optimizer import _load_holdings_tickers, load_returns
    from risk_engine import estimate_gjr_garch

    if tickers is None:
        tickers = _load_holdings_tickers()

    returns_df = load_returns(tickers, lookback_days=lookback_days)
    if returns_df.empty:
        return {'success': False, 'error': 'リターンデータ取得失敗'}

    ginn_obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
    if not ginn_obj.is_available():
        return {'success': False, 'error': 'PyTorchモデル初期化失敗'}

    model = ginn_obj.model
    device = ginn_obj._device
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    # P2-8: 時系列 train/test split（先頭 80% train / 末尾 20% test, shuffle=False 強制）
    # 銘柄ごとに chronological に分け、結合する
    all_X_tr, all_y_tr, all_σ_tr = [], [], []
    all_X_te, all_y_te, all_σ_te = [], [], []

    for ticker in tickers:
        if ticker not in returns_df.columns:
            continue
        r = returns_df[ticker].dropna()
        if len(r) < seq_len + 30:
            continue

        # P2-8: train/test を r の chronological 境界で分ける
        split_idx = int(len(r) * 0.8)
        r_train = r.iloc[:split_idx]
        r_test  = r.iloc[split_idx:]

        # GJR-GARCH σ は train のみで推定
        try:
            garch_res = estimate_gjr_garch(r_train, use_ginn=False)
            garch_sigma_val = garch_res.get('forecast_vol', r_train.std() * np.sqrt(252)) / np.sqrt(252)
            garch_sigma_train = pd.Series(garch_sigma_val, index=r_train.index)
            garch_sigma_test  = pd.Series(garch_sigma_val, index=r_test.index)
        except Exception:
            garch_sigma_val   = float(r_train.std())
            garch_sigma_train = pd.Series(garch_sigma_val, index=r_train.index)
            garch_sigma_test  = pd.Series(garch_sigma_val, index=r_test.index)

        # train で fit_stats を確定、test は同じ stats を再利用（data leak 防止）
        X_tr, y_tr, fit_stats = _build_sequences(
            r_train, garch_sigma_train, None, None, seq_len=seq_len, fit_stats=None,
        )
        X_te, y_te, _ = _build_sequences(
            r_test, garch_sigma_test, None, None, seq_len=seq_len, fit_stats=fit_stats,
        )
        if len(X_tr) == 0:
            continue

        all_X_tr.append(X_tr)
        all_y_tr.append(y_tr)
        all_σ_tr.append(np.full(len(y_tr), float(garch_sigma_val), dtype=np.float32))

        if len(X_te) > 0:
            all_X_te.append(X_te)
            all_y_te.append(y_te)
            all_σ_te.append(np.full(len(y_te), float(garch_sigma_val), dtype=np.float32))

    if not all_X_tr:
        return {'success': False, 'error': 'シーケンス構築失敗（データ不足）'}

    X_train = torch.FloatTensor(np.vstack(all_X_tr)).to(device)
    y_train = torch.FloatTensor(np.concatenate(all_y_tr)).to(device)
    σ_train = torch.FloatTensor(np.concatenate(all_σ_tr)).to(device)

    has_test = bool(all_X_te)
    if has_test:
        X_test = torch.FloatTensor(np.vstack(all_X_te)).to(device)
        y_test = torch.FloatTensor(np.concatenate(all_y_te)).to(device)

    n_train = len(X_train)
    n_test  = len(X_test) if has_test else 0
    print(f"  GINN学習開始: train={n_train} test={n_test} サンプル, {epochs}エポック, device={device}")

    model.train()
    final_loss = 0.0
    test_mse   = None

    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad()
        pred = model(X_train)

        # GINN損失: MSE(pred, |ε_t|) + λ・MSE(pred, σ_GARCH)
        mse_realized = torch.mean((pred - y_train) ** 2)
        mse_garch    = torch.mean((pred - σ_train) ** 2)
        loss         = mse_realized + garch_lambda * mse_garch

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()

        final_loss = float(loss.item())
        if (epoch + 1) % 10 == 0:
            print(f"    Epoch {epoch+1}/{epochs}: train_loss={final_loss:.6f}")

    # P2-8: train 終了後に test MSE を測定（過学習検知）
    if has_test:
        model.eval()
        with torch.no_grad():
            test_mse = float(torch.mean((model(X_test) - y_test) ** 2).item())
        print(f"  Test MSE: {test_mse:.6f}（train_loss との乖離 = 過学習シグナル）")

    # 保存
    torch.save(model.state_dict(), MODEL_PATH)
    meta = {
        'trained_at':   datetime.now().isoformat(),
        'n_samples':    n_train,
        'n_test':       n_test,
        'final_loss':   round(final_loss, 6),
        'test_mse':     round(test_mse, 6) if test_mse is not None else None,
        'tickers':      tickers[:10],
        'epochs':       epochs,
        'seq_len':      seq_len,
        'garch_lambda': garch_lambda,
        'split':        'chronological_80_20_shuffle_False',
    }
    with open(MODEL_PATH.parent / 'ginn_meta.json', 'w') as f:
        json.dump(meta, f, indent=2)

    print(f"  GINN学習完了: loss={final_loss:.6f} -> {MODEL_PATH}")
    return {'success': True, 'loss': final_loss, 'n_samples': n_train, 'test_mse': test_mse}


def forecast_ginn(
    returns: pd.Series,
    garch_sigma: float,
    seq_len: int = 60,
) -> float:
    """
    GINNで翌日の予測ボラティリティを返す（年率換算）。
    モデル未存在またはエラー時はgarch_sigmaをフォールバック。

    Args:
        returns: 直近リターン系列（60日以上）
        garch_sigma: GJR-GARCH予測σ（年率）。フォールバック値兼物理制約
        seq_len: シーケンス長

    Returns:
        float: 年率換算ボラティリティ予測値
    """
    if not MODEL_PATH.exists():
        return garch_sigma  # フォールバック

    torch, nn = _get_torch()
    if torch is None:
        return garch_sigma

    try:
        ginn_obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
        if not ginn_obj.is_available():
            return garch_sigma

        model = ginn_obj.model
        device = ginn_obj._device
        model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
        model.eval()

        r = returns.dropna().tail(seq_len + 10)
        if len(r) < seq_len:
            return garch_sigma

        # 日次σ（年率→日次変換）
        garch_sigma_daily = garch_sigma / np.sqrt(252)
        garch_s = pd.Series(garch_sigma_daily, index=r.index)

        X, _, _ = _build_sequences(r, garch_s, None, None, seq_len=seq_len)
        if len(X) == 0:
            return garch_sigma

        x_tensor = torch.FloatTensor(X[-1:]).to(device)

        with torch.no_grad():
            pred_daily = float(model(x_tensor).item())

        # 日次→年率換算
        pred_annual = pred_daily * np.sqrt(252)

        # 外れ値チェック: GARCHの0.3倍〜3倍の範囲に制限
        pred_annual = max(garch_sigma * 0.3, min(garch_sigma * 3.0, pred_annual))

        return round(pred_annual, 4)

    except Exception as e:
        print(f"  GINN予測失敗（フォールバック）: {e}")
        return garch_sigma


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='GINN: GARCH-Informed Neural Network')
    parser.add_argument('--train', action='store_true', help='モデルを学習する')
    parser.add_argument('--ticker', default=None, help='単一銘柄で学習（例: NVDA）')
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--lambda-garch', type=float, default=0.3, dest='garch_lambda')
    args = parser.parse_args()

    if args.train:
        tickers = [args.ticker] if args.ticker else None
        result = train_ginn(tickers=tickers, epochs=args.epochs, garch_lambda=args.garch_lambda)
        if result.get('success'):
            print(f'\n学習完了: samples={result["n_samples"]}, loss={result["loss"]:.6f}')
        else:
            print(f'\n学習失敗: {result.get("error")}')
            sys.exit(1)
    else:
        # フォールバックテスト
        print('GINNモデルのフォールバックテスト...')
        test_returns = pd.Series(np.random.randn(100) * 0.01)
        σ = forecast_ginn(test_returns, garch_sigma=0.25)
        print(f'予測σ(年率): {σ:.4f}  (モデル{"あり" if MODEL_PATH.exists() else "なし -> GARCH値"})')
