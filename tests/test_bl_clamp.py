"""T8: BL clamp per investment_type"""
import portfolio_optimizer as po


def test_clamp_values_differ_by_itype():
    assert po.get_max_abs_view('long') == 0.20
    assert po.get_max_abs_view('medium') == 0.15
    assert po.get_max_abs_view('swing') == 0.25


def test_unknown_itype_fallback():
    v = po.get_max_abs_view('unknown_type')
    # should return a safe default (15% medium fallback)
    assert 0.10 <= v <= 0.20
