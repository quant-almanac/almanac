"""T14: loss harvest pair proposal + net benefit > switch cost"""
import json

import tax_optimizer as tx


def test_loss_harvest_positive_net_benefit(tmp_path, monkeypatch):
    # Codex re-re-review: loss_harvest_substitutes.json は gitignored で clean worktree に
    # 無いため、テストは一時 SUBSTITUTES_PATH を用意して自己完結させる (コミット単体で再現可能)。
    subs = tmp_path / "subs.json"
    subs.write_text(json.dumps({"CRWV": {"substitutes": ["SMH", "SOXX", "NVDA"]}}),
                    encoding="utf-8")
    monkeypatch.setattr(tx, "SUBSTITUTES_PATH", subs)

    snap = {'positions': [
        {'key': 'CRWV', 'ticker': 'CRWV', 'name': 'CoreWeave',
         'account': '特定', 'currency': 'USD',
         'shares': 50, 'entry_price': 120, 'current_price': 78,
         'unrealized_jpy': -300_000, 'unrealized_pct': -0.35,
         'value_jpy': 600_000, 'investment_type': 'swing'},
    ]}
    result = tx.suggest_loss_harvest_pairs(snap)
    assert len(result['pairs']) == 1
    p = result['pairs'][0]
    assert p['ticker'] == 'CRWV'
    assert p['substitutes'][0] in ('SMH', 'SOXX', 'NVDA')
    assert p['net_benefit_jpy'] > 0
    assert p['restart_eligible_date'] > p['sold_on']
    assert p['wash_sale_window_days'] == 30


def test_loss_harvest_excludes_nisa():
    snap = {'positions': [
        {'key': 'AVGO', 'ticker': 'AVGO',
         'account': 'NISA成長投資枠', 'currency': 'USD',
         'shares': 10, 'entry_price': 200, 'current_price': 150,
         'unrealized_jpy': -80_000, 'unrealized_pct': -0.25,
         'value_jpy': 150_000},
    ]}
    result = tx.suggest_loss_harvest_pairs(snap)
    assert result['pairs'] == []


def test_loss_harvest_skips_small_loss():
    snap = {'positions': [
        {'ticker': 'X', 'account': '特定', 'unrealized_jpy': -5000,
         'unrealized_pct': -0.05, 'value_jpy': 100_000},
    ]}
    result = tx.suggest_loss_harvest_pairs(snap)
    assert result['pairs'] == []


def test_substitutes_exclude_target_ticker():
    """Codex re-review P2: built-in fallback が対象ティッカー自身を代替候補に含めない。"""
    subs = tx._lookup_substitutes("VTI", is_japan=False, subs_map=tx._DEFAULT_SUBSTITUTES)
    assert "VTI" not in [s.upper() for s in subs]
    assert subs  # 自身を除いても候補が残る (VOO/SPY)
    jp = tx._lookup_substitutes("1306.T", is_japan=True, subs_map=tx._DEFAULT_SUBSTITUTES)
    assert "1306.T" not in [s.upper() for s in jp]


def test_substitutes_entry_lookup_is_case_insensitive():
    """Codex P3: curated entry キーは大小文字を区別しない ('vti' でも 'VTI' entry を引く)。"""
    m = {"VTI": {"substitutes": ["VOO", "SPY"]},
         "_fallback": {"us_equity_long": {"substitutes": ["AGG"]}}}
    assert tx._lookup_substitutes("vti", is_japan=False, subs_map=m) == ["VOO", "SPY"]
