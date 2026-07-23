"""T15: cash drag 3% 7d warn / 15% critical / 提案額"""
import portfolio_manager as pm


def _use_default_tunables(monkeypatch):
    monkeypatch.setattr(pm, "_tp_pm", lambda _key, fallback: fallback)


def test_cash_drag_critical(monkeypatch):
    _use_default_tunables(monkeypatch)
    monkeypatch.setattr(pm, 'load_account', lambda: {'balance': 5_000_000, 'usd_balance': 0})
    snap = {'total_jpy': 30_000_000, 'positions': []}
    r = pm.detect_cash_drag(snap, persist=False)
    assert r['level'] == 'critical'
    assert r['cash_ratio'] > pm.CASH_CRITICAL_RATIO
    # JPY suggestion should be present
    assert any(s['currency'] == 'JPY' for s in r['suggestions'])


def test_cash_drag_ok(monkeypatch):
    _use_default_tunables(monkeypatch)
    monkeypatch.setattr(pm, 'load_account', lambda: {'balance': 100_000, 'usd_balance': 0})
    snap = {'total_jpy': 30_000_000, 'positions': []}
    r = pm.detect_cash_drag(snap, persist=False)
    assert r['level'] == 'ok'
    assert r['suggestions'] == []


def test_cash_drag_usd_routing(monkeypatch):
    _use_default_tunables(monkeypatch)
    monkeypatch.setattr(pm, 'load_account', lambda: {
        'balance': 0, 'usd_balance': 50_000, 'fx_rate_usdjpy': 150,
    })
    snap = {'total_jpy': 30_000_000, 'positions': []}
    r = pm.detect_cash_drag(snap, persist=False)
    assert r['level'] == 'critical'
    usd_sugg = [s for s in r['suggestions'] if s['currency'] == 'USD']
    assert usd_sugg
    assert 'SGOV' in usd_sugg[0]['candidates'] or 'BIL' in usd_sugg[0]['candidates']
