"""T17: AI follow-rate matching"""
from datetime import datetime, timedelta

import follow_rate_analyzer as fr


def test_follow_rate_basic():
    now = datetime.now()
    recs = [
        {'as_of': (now - timedelta(days=60)).isoformat(), 'ticker': 'NVDA',
         'type': 'buy', 'urgency': 'high', 'price_at_rec': 150},
        {'as_of': (now - timedelta(days=40)).isoformat(), 'ticker': 'CRWV',
         'type': 'sell', 'urgency': 'medium', 'price_at_rec': 100},
        {'as_of': (now - timedelta(days=30)).isoformat(), 'ticker': 'META',
         'type': 'add', 'urgency': 'low', 'price_at_rec': 500},
    ]
    execs = [
        {'id': 'e1', 'ticker': 'NVDA', 'direction': 'buy', 'price': 152, 'quantity': 5,
         'saved_at': (now - timedelta(days=59)).isoformat()},
        {'id': 'e2', 'ticker': 'CRWV', 'direction': 'sell', 'price': 95, 'quantity': 10,
         'saved_at': (now - timedelta(days=39)).isoformat()},
    ]
    r = fr.match_recommendations(recs, execs)
    # 3 recs, 2 matched
    assert r['total_recs'] == 3
    assert r['total_matched'] == 2
    assert abs(r['follow_rate'] - 2/3) < 1e-3   # rounded to 4dp in source


def test_rebalance_excluded():
    """rebalance 推奨はマッチ対象外（個別約定に直結しない）"""
    recs = [{'as_of': datetime.now().isoformat(), 'ticker': 'X',
             'type': 'rebalance', 'urgency': 'low'}]
    r = fr.match_recommendations(recs, [])
    assert r['total_recs'] == 0


def test_window_boundary():
    now = datetime.now()
    recs = [{'as_of': now.isoformat(), 'ticker': 'NVDA', 'type': 'buy'}]
    # outside window
    execs = [{'id': 'e1', 'ticker': 'NVDA', 'direction': 'buy',
              'price': 150, 'quantity': 1,
              'saved_at': (now + timedelta(days=10)).isoformat()}]
    r = fr.match_recommendations(recs, execs, window_days=3)
    assert r['total_matched'] == 0


def test_parse_dt_normalizes_tz():
    """tz-aware/naive を混在させても naive-UTC に正規化される。"""
    aware = fr._parse_dt("2026-05-26T07:50:31+09:00")
    naive = fr._parse_dt("2026-05-26T07:50:31.143718")
    assert aware is not None and aware.tzinfo is None
    assert naive is not None and naive.tzinfo is None
    # 引き算が TypeError にならない（回帰のコア）
    _ = abs((aware - naive).total_seconds())


def test_match_does_not_crash_on_mixed_tz():
    """rec=tz-aware / exec=naive の混在で match がクラッシュしないこと（L220 回帰）。"""
    now = datetime.now()
    recs = [{'as_of': (now - timedelta(days=2)).isoformat() + "+00:00",
             'ticker': 'NVDA', 'type': 'buy'}]
    execs = [{'id': 'e1', 'ticker': 'NVDA', 'direction': 'buy', 'price': 150,
              'quantity': 1, 'saved_at': (now - timedelta(days=1)).isoformat()}]
    r = fr.match_recommendations(recs, execs, window_days=5)
    assert r['total_recs'] == 1
    assert r['total_matched'] == 1


def test_reduce_recommendation_matches_sell_execution():
    now = datetime.now()
    recs = [{'as_of': now.isoformat(), 'ticker': 'GLD', 'type': 'reduce'}]
    execs = [{
        'id': 'e1',
        'ticker': 'GLD',
        'direction': 'sell',
        'price': 300,
        'quantity': 1,
        'saved_at': (now + timedelta(days=1)).isoformat(),
    }]

    r = fr.match_recommendations(recs, execs, window_days=3)

    assert r['total_recs'] == 1
    assert r['total_matched'] == 1
    assert r['by_direction']['sell'] == {'recs': 1, 'matched': 1}


def test_margin_buy_and_cover_are_follow_rate_actions():
    now = datetime.now()
    recs = [
        {'as_of': now.isoformat(), 'ticker': '7203.T', 'type': 'margin_buy'},
        {'as_of': now.isoformat(), 'ticker': '6758.T', 'type': 'cover'},
    ]
    execs = [
        {
            'id': 'e1',
            'ticker': '7203.T',
            'direction': 'margin_buy',
            'price': 3000,
            'quantity': 100,
            'saved_at': (now + timedelta(days=1)).isoformat(),
        },
        {
            'id': 'e2',
            'ticker': '6758.T',
            'direction': 'cover',
            'price': 4000,
            'quantity': 100,
            'saved_at': (now + timedelta(days=1)).isoformat(),
        },
    ]

    r = fr.match_recommendations(recs, execs, window_days=3)

    assert r['total_recs'] == 2
    assert r['total_matched'] == 2
    assert r['by_direction']['margin_buy'] == {'recs': 1, 'matched': 1}
    assert r['by_direction']['cover'] == {'recs': 1, 'matched': 1}


def test_status_snapshot_reports_follow_rate_without_shadow_file(tmp_path):
    now = datetime.now()
    shadow_path = tmp_path / "shadow_portfolio.json"
    recs = [{'as_of': now.isoformat(), 'ticker': 'NVDA', 'type': 'buy'}]
    execs = [{
        'id': 'e1',
        'ticker': 'NVDA',
        'direction': 'buy',
        'price': 500,
        'quantity': 1,
        'saved_at': now.isoformat(),
    }]

    snapshot = fr.build_status_snapshot(
        recs=recs,
        execs=execs,
        shadow_path=shadow_path,
    )

    assert not shadow_path.exists()
    assert snapshot['shadow_state_available'] is False
    assert snapshot['follow_rate']['total_recs'] == 1
    assert snapshot['follow_rate']['total_matched'] == 1
    assert snapshot['follow_rate']['follow_rate'] == 1.0
