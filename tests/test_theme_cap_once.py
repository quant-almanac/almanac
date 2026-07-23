"""T9: theme cap idempotency (applied at most once per dict)"""
import portfolio_optimizer as po


def test_theme_cap_idempotent():
    weights = {'NVDA': 0.25, 'AVGO': 0.15, 'META': 0.10, 'IEV': 0.15, 'GLD': 0.10,
                '1489.T': 0.10, 'EWG': 0.10, '6762.T': 0.05}
    first = po._apply_theme_cap(dict(weights))
    # second call should be no-op
    second = po._apply_theme_cap(first)
    # compare numerical portions only
    for k in weights:
        assert abs(first.get(k, 0) - second.get(k, 0)) < 1e-9
    # sentinel present
    assert '_theme_capped' in first
