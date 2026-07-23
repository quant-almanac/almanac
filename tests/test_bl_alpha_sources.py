"""
tests/test_bl_alpha_sources.py — P2-27 独立 alpha source
"""
import pytest

import bl_alpha_sources as bas


# ────────────────────────────────────────────────────────
# Analyst consensus mapping
# ────────────────────────────────────────────────────────

def test_reco_to_view_strong_buy_positive():
    """reco_mean=1.0 → +0.18 (Strong Buy)"""
    assert bas._reco_to_view(1.0) == pytest.approx(0.18)


def test_reco_to_view_neutral():
    """reco_mean=3.0 → 0.0 (Neutral)"""
    assert bas._reco_to_view(3.0) == pytest.approx(0.0)


def test_reco_to_view_strong_sell_negative():
    """reco_mean=5.0 → -0.18 (Strong Sell)"""
    assert bas._reco_to_view(5.0) == pytest.approx(-0.18)


def test_reco_to_view_none_safe():
    assert bas._reco_to_view(None) == 0.0


def test_analyst_consensus_with_fixed_fetcher():
    def fake_fetcher(t):
        return {
            "AAPL": {"reco_mean": 1.5, "analyst_count": 30},
            "BAD":  {"reco_mean": None, "analyst_count": 0},
        }.get(t, {"reco_mean": None})

    out = bas.analyst_consensus_alpha(["AAPL", "BAD", "MISSING"], fetcher=fake_fetcher)
    assert "AAPL" in out
    assert out["AAPL"]["view"] == pytest.approx(0.135)  # -0.09 * (1.5 - 3.0) = 0.135
    assert out["AAPL"]["n_analysts"] == 30
    # reco=None なら除外
    assert "BAD" not in out
    assert "MISSING" not in out


# ────────────────────────────────────────────────────────
# Momentum
# ────────────────────────────────────────────────────────

def test_twelve_minus_one_basic():
    """price 252 営業日: start=100, end (-22)=120 → +20%"""
    prices = [100.0] * 252
    prices[-22] = 120.0  # 末端から 22 番目
    r = bas._twelve_minus_one_return(prices)
    assert r == pytest.approx(0.20)


def test_twelve_minus_one_insufficient_data():
    """252 営業日未満 → None"""
    assert bas._twelve_minus_one_return([100.0] * 100) is None
    assert bas._twelve_minus_one_return([]) is None


def test_momentum_alpha_applies_decay_and_clamp():
    def loader(t):
        if t == "STRONG":
            prices = [100.0] * 252
            prices[-22] = 200.0  # +100% raw → decay 0.5 → +50% → clamp 25%
            return prices
        if t == "WEAK":
            prices = [100.0] * 252
            prices[-22] = 105.0  # +5% raw → decay 0.5 → +2.5%
            return prices
        if t == "SHORT":
            return [100.0] * 100  # 不足
        return []

    out = bas.momentum_alpha(["STRONG", "WEAK", "SHORT"], price_loader=loader)
    assert "STRONG" in out and out["STRONG"]["view"] == pytest.approx(0.25)
    assert "WEAK" in out and out["WEAK"]["view"] == pytest.approx(0.025)
    assert "SHORT" not in out


# ────────────────────────────────────────────────────────
# Factor beta
# ────────────────────────────────────────────────────────

def test_factor_beta_alpha_with_dict():
    fa = {"betas": {"MOM": 0.5, "QMJ": 0.3, "VAL": 0.2}}
    out = bas.factor_beta_alpha(factor_attribution=fa)
    assert "PORTFOLIO_TILT" in out
    # 0.5*0.05 + 0.3*0.03 + 0.2*0.02 = 0.025 + 0.009 + 0.004 = 0.038
    assert out["PORTFOLIO_TILT"]["view"] == pytest.approx(0.038, abs=1e-4)
    assert len(out["PORTFOLIO_TILT"]["decomposition"]) == 3


def test_factor_beta_alpha_empty(tmp_path):
    assert bas.factor_beta_alpha(factor_attribution={}) == {}
    assert bas.factor_beta_alpha(factor_attribution={"error": "x"}) == {}
    # 存在しないファイルを明示的に指定
    assert bas.factor_beta_alpha(factor_attribution=None, fa_path=tmp_path / "no.json") == {}


def test_factor_beta_alpha_unknown_factors_skipped():
    """FACTOR_PREMIUMS にない factor は無視。"""
    fa = {"betas": {"UNKNOWN_FACTOR": 10.0, "MOM": 0.5}}
    out = bas.factor_beta_alpha(factor_attribution=fa)
    # MOM のみ寄与
    assert out["PORTFOLIO_TILT"]["view"] == pytest.approx(0.025, abs=1e-4)
    assert len(out["PORTFOLIO_TILT"]["decomposition"]) == 1


# ────────────────────────────────────────────────────────
# compute_independent_views
# ────────────────────────────────────────────────────────

def test_compute_independent_views_uses_only_requested_sources():
    """analyst_consensus のみ requested なら momentum/factor は使わない。"""
    def afetch(t):
        return {"reco_mean": 1.5, "analyst_count": 5}

    out = bas.compute_independent_views(
        tickers=["AAPL"],
        sources=["analyst_consensus"],
        fetcher_analyst=afetch,
    )
    assert "AAPL" in out
    assert out["AAPL"]["n_signals"] == 1
    assert out["AAPL"]["sources"][0]["source"] == "analyst_consensus"


def test_compute_independent_views_aggregates_multiple_sources():
    """analyst + momentum 2 source の集約。"""
    def afetch(t):
        return {"reco_mean": 2.0, "analyst_count": 10}  # view = +0.09

    def mload(t):
        prices = [100.0] * 252
        prices[-22] = 120.0  # +20% → decay 0.5 → +10%
        return prices

    out = bas.compute_independent_views(
        tickers=["AAPL"],
        sources=["analyst_consensus", "momentum"],
        fetcher_analyst=afetch,
        loader_momentum=mload,
    )
    a = out["AAPL"]
    assert a["n_signals"] == 2
    assert a["bull_view"] == pytest.approx(0.10, abs=0.01)
    assert a["bear_view"] == pytest.approx(0.09, abs=0.01)
    assert a["mean_view"] == pytest.approx(0.095, abs=0.01)
    # 独立 source なので avg_confidence=1.0 (deweight しない印)
    assert a["avg_confidence"] == 1.0


def test_compute_independent_views_single_source_uses_default_variance():
    """1 source のみなら variance=0.02 が使われる (BL の Ω に流す用)。"""
    def afetch(t):
        return {"reco_mean": 2.0, "analyst_count": 1}

    out = bas.compute_independent_views(
        tickers=["AAPL"],
        sources=["analyst_consensus"],
        fetcher_analyst=afetch,
    )
    assert out["AAPL"]["variance"] == 0.02


def test_compute_independent_views_empty_when_no_data():
    """全 source が空を返したら結果も空。"""
    def afetch(t):
        return {"reco_mean": None}

    out = bas.compute_independent_views(
        tickers=["GHOST"],
        sources=["analyst_consensus"],
        fetcher_analyst=afetch,
    )
    assert out == {}
