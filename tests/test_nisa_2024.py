"""T11: 新 NISA 2024 — 翌年簿価復活、特定口座と通算不可"""
import tax_optimizer as tx
from datetime import date


def test_sell_nisa_gain_records_restore(tmp_path, monkeypatch):
    store = tmp_path / 'sales.json'
    monkeypatch.setattr(tx, 'NISA_SALE_HISTORY_PATH', store)

    # Sell NISA with gain — no tax, next-year quota restored by cost basis
    result = tx.record_nisa_sale(
        person='husband',
        key='NVDA_NISA',
        cost_basis_jpy=1_000_000,
        proceeds_jpy=1_500_000,
        sale_date=date(2026, 4, 1).isoformat(),
        quota_type='growth',
    )
    assert result['restored_next_year'] == 1_000_000
    assert result['gain_loss_jpy'] == 500_000
    assert result['restore_year'] == 2027
    assert result['loss_offsetable'] is False


def test_nisa_sell_tax_warnings():
    # NISA gain → tax-free + 翌年簿価復活メッセージ
    r = tx.calculate_sell_tax(
        ticker='NVDA',
        shares=10,
        entry_price=100,
        current_price=150,
        account_type='nisa',
        currency='USD',
        fx_rate=150,
    )
    assert r['tax_jpy'] == 0
    assert r['nisa_quota_restored_next_year'] > 0
    assert any('翌年' in w or '非課税' in w for w in r['warnings'])


def test_nisa_loss_is_permanent():
    r = tx.calculate_sell_tax(
        ticker='LOSS',
        shares=10,
        entry_price=150,
        current_price=100,
        account_type='nisa',
        currency='USD',
        fx_rate=150,
    )
    # NISA 損失は通算不可 → 警告必須
    assert r['tax_jpy'] == 0
    assert any('通算' in w or '永久' in w or '損失' in w for w in r['warnings'])


def test_quota_restoration_prior_year_only(tmp_path, monkeypatch):
    store = tmp_path / 'sales.json'
    monkeypatch.setattr(tx, 'NISA_SALE_HISTORY_PATH', store)

    # 2025 sold
    tx.record_nisa_sale('husband', 'A', 500_000, 700_000, '2025-06-01', 'growth')
    # 2026 sold
    tx.record_nisa_sale('husband', 'B', 300_000, 400_000, '2026-02-01', 'growth')

    # as_of 2026: restoration counts only 2025 sales = 500k
    r = tx.compute_nisa_quota_restoration('husband', as_of_year=2026)
    assert r == 500_000
