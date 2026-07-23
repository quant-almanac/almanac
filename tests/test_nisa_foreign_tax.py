"""T13: NISA 内 US 高配当で leak 検知"""
import tax_optimizer as tx


def test_us_high_yield_in_nisa_flagged():
    holdings = {
        'VYM_NISA': {
            'ticker': 'VYM', 'name': 'Vanguard HD',
            'account': 'NISA成長投資枠', 'currency': 'USD',
            'shares': 100, 'current_price': 110,
        },
    }
    result = tx.detect_nisa_foreign_tax_leak(holdings=holdings)
    assert result['leaks']
    assert result['total_leak_jpy'] > 0
    # Leak entries should reference VYM by ticker
    assert any(l.get('ticker') == 'VYM' for l in result['leaks'])
    # Recommendation should suggest moving to specific account
    assert '特定' in result['recommendation']


def test_low_yield_not_flagged():
    holdings = {
        'QQQ_NISA': {
            'ticker': 'QQQ', 'account': 'NISA成長投資枠', 'currency': 'USD',
            'dividend_yield': 0.006,   # 0.6% < 1.5% threshold
            'shares': 10, 'current_price': 400,
        },
    }
    result = tx.detect_nisa_foreign_tax_leak(holdings=holdings)
    assert result['leaks'] == []


def test_specific_account_not_flagged():
    holdings = {
        'VYM': {'ticker': 'VYM', 'account': '特定', 'currency': 'USD',
                 'shares': 50, 'current_price': 110},
    }
    result = tx.detect_nisa_foreign_tax_leak(holdings=holdings)
    assert result['leaks'] == []
