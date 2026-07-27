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


# ---------------------------------------------------------------------------
# Stage 5C: analyze_loss_harvest への cost_basis_crosscheck 追加
# (候補選定・tax_saving_jpy 自体は変更しない — 追加情報の cross-check のみ)
# ---------------------------------------------------------------------------


def _snapshot_one_candidate():
    return {'positions': [
        {'key': 'AVGO_ippan', 'ticker': 'AVGO', 'name': 'Broadcom',
         'account': '特定', 'currency': 'USD',
         'unrealized_jpy': -100_000, 'unrealized_pct': -0.15,
         'investment_type': 'long'},
    ]}


def test_analyze_loss_harvest_candidate_selection_is_unchanged(monkeypatch):
    """本題: cost_basis_crosscheck の追加が既存の候補選定・数値を変えない
    (回帰確認)。"""
    monkeypatch.setattr(
        "tax_lot.compare_old_new_cost_basis",
        lambda ticker, **kwargs: {'ticker': ticker, 'rows': []},
    )
    result = tx.analyze_loss_harvest(_snapshot_one_candidate(), min_loss_jpy=50_000)
    assert len(result['candidates']) == 1
    c = result['candidates'][0]
    assert c['ticker'] == 'AVGO'
    assert c['unrealized_jpy'] == -100_000
    assert c['tax_saving_jpy'] == round(100_000 * tx.TAX_TOKUTEI, 0)
    assert result['total_loss_jpy'] == -100_000


def test_analyze_loss_harvest_attaches_crosscheck_when_available(monkeypatch):
    monkeypatch.setattr(
        "tax_lot.compare_old_new_cost_basis",
        lambda ticker, **kwargs: {
            'ticker': ticker,
            'rows': [{
                'ticker': ticker, 'account': '特定',
                'fifo_cost_basis_jpy': 900_000, 'total_average_cost_basis_jpy': 850_000,
                'diff_jpy': -50_000, 'total_average_data_quality_issues': [],
            }],
        },
    )
    result = tx.analyze_loss_harvest(_snapshot_one_candidate(), min_loss_jpy=50_000)
    cc = result['candidates'][0]['cost_basis_crosscheck']
    assert cc['available'] is True
    assert cc['fifo_cost_basis_jpy'] == 900_000
    assert cc['total_average_cost_basis_jpy'] == 850_000
    assert cc['diff_jpy'] == -50_000


def test_analyze_loss_harvest_crosscheck_unavailable_for_missing_ledger_data(monkeypatch):
    """本題: event_ledger に取引履歴の無い銘柄 (実在する ABNB/MA/MNXACT/RTX/
    SLIM_ORCAN のようなケース) は available=False で明示され、0円や
    holdings.json 由来の値で埋められない。"""
    monkeypatch.setattr(
        "tax_lot.compare_old_new_cost_basis",
        lambda ticker, **kwargs: {'ticker': ticker, 'rows': []},
    )
    result = tx.analyze_loss_harvest(_snapshot_one_candidate(), min_loss_jpy=50_000)
    cc = result['candidates'][0]['cost_basis_crosscheck']
    assert cc['available'] is False
    assert 'reason' in cc


def test_analyze_loss_harvest_crosscheck_failure_does_not_crash_the_function(monkeypatch):
    """本題: 突合計算の例外が analyze_loss_harvest 全体を落とさない
    (fail-closed だが fail-loud ではない)。"""
    def _raise(ticker, **kwargs):
        raise RuntimeError("event_ledger 接続失敗 (テスト用)")

    monkeypatch.setattr("tax_lot.compare_old_new_cost_basis", _raise)
    result = tx.analyze_loss_harvest(_snapshot_one_candidate(), min_loss_jpy=50_000)
    assert len(result['candidates']) == 1  # 候補自体は生成される
    cc = result['candidates'][0]['cost_basis_crosscheck']
    assert cc['available'] is False
    assert '突合計算失敗' in cc['reason']


def test_analyze_loss_harvest_crosscheck_matches_by_account():
    """複数口座がある場合、候補と同じ account の行だけを使う。"""
    with_multi_account_rows = {
        'ticker': 'AVGO',
        'rows': [
            {'ticker': 'AVGO', 'account': '一般', 'fifo_cost_basis_jpy': 1, 'total_average_cost_basis_jpy': 1, 'diff_jpy': 0, 'total_average_data_quality_issues': []},
            {'ticker': 'AVGO', 'account': '特定', 'fifo_cost_basis_jpy': 700_000, 'total_average_cost_basis_jpy': 720_000, 'diff_jpy': 20_000, 'total_average_data_quality_issues': []},
        ],
    }
    import unittest.mock as mock
    with mock.patch("tax_lot.compare_old_new_cost_basis", return_value=with_multi_account_rows):
        cc = tx._cost_basis_crosscheck_for_candidate(ticker='AVGO', account='特定')
    assert cc['available'] is True
    assert cc['fifo_cost_basis_jpy'] == 700_000
