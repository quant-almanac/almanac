"""T7: GINN _build_sequences — X に未来インデックス混入が無い"""
import numpy as np
import pandas as pd
import ginn_model as gm


def _synthetic_series(n=200, seed=0):
    np.random.seed(seed)
    r = pd.Series(np.random.normal(0, 0.01, n), index=pd.date_range('2024-01-01', periods=n))
    sigma = pd.Series(np.abs(r) + 0.005, index=r.index)
    return r, sigma


def test_X_does_not_include_future():
    """X[k] は [i - seq_len, i) のウィンドウ、y[k] は i+1 の絶対リターン。
    つまり X は i-1 時点までのみを含み、未来の情報は含まれない。"""
    r, sigma = _synthetic_series(n=200)
    seq_len = 60
    X, y, stats = gm._build_sequences(r, sigma, None, None, seq_len=seq_len)
    # ループ範囲: range(seq_len, len(feat) - 2) → 期待件数 = 200 - seq_len - 2 = 138
    assert len(X) == 200 - seq_len - 2
    assert len(y) == len(X)
    # 各 X[k] は 60 行 × 4 列
    assert X[0].shape == (seq_len, 4)
    # X[0] に含まれる最後の行は feat.iloc[seq_len - 1]（= i-1 の情報）
    # y[0] は r.iloc[seq_len + 1] の絶対値（= 翌日）
    #
    # ⚠️ _build_sequences は y を float32 で返す (np.array(y, dtype=np.float32))
    # が、ここでの期待値は Python の float (float64 相当) で計算していた。
    # float64→float32 の丸めは値ごとに ~1e-7 相対の誤差を持ちうるため、
    # 1e-12 という絶対許容誤差はそもそも数学的に成立しえない基準だった
    # (レビューが導入した CI 分離で初めて別の numpy/BLAS ビルド上で実行され、
    # 実測 diff=6.99e-11 で発覚。ローカルではたまたま丸め方向が一致し
    # diff が表示上 0.0 になっていた)。期待値も float32 精度へ揃えてから
    # 比較する。
    expected_y0 = np.float32(abs(float(r.iloc[seq_len + 1])))
    assert abs(y[0] - expected_y0) < 1e-9


def test_fit_stats_applied_externally():
    """fit_stats を渡すと train/test で同じ正規化統計を使用できる（leak 防止）"""
    r, sigma = _synthetic_series(n=200)
    # train 側
    Xtr, ytr, stats_train = gm._build_sequences(r.iloc[:150], sigma.iloc[:150], None, None, seq_len=60)
    # test 側に train 統計を注入
    Xte, yte, stats_test = gm._build_sequences(r.iloc[150:], sigma.iloc[150:], None, None,
                                                seq_len=60, fit_stats=stats_train)
    # 同じ統計が使われている
    assert stats_test == stats_train


def test_no_nan_in_output():
    r, sigma = _synthetic_series(n=200)
    X, y, _ = gm._build_sequences(r, sigma, None, None, seq_len=60)
    arr = np.array(X)
    assert not np.isnan(arr).any()
    assert not np.isnan(np.array(y)).any()
