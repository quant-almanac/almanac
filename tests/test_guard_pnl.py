"""T2: behavioral_guard の P&L 源泉一本化（snapshot が daily_pnl_jpy の唯一の writer）"""
import json
import behavioral_guard as bg


def test_initial_state_has_new_keys():
    state = bg._default_state()
    assert 'daily_pnl_jpy' in state
    assert 'realized_pnl_jpy_today' in state
    assert 'last_eod_portfolio_value' in state
    assert state['daily_pnl_jpy'] == 0.0
    assert state['realized_pnl_jpy_today'] == 0.0


def test_update_pnl_writes_realized_not_daily(tmp_path, monkeypatch):
    """update_pnl は realized_pnl_jpy_today に累積し、daily_pnl_jpy は評価額ベースで上書き"""
    state_file = tmp_path / 'guard_state.json'
    monkeypatch.setattr(bg, 'STATE_FILE', state_file)

    # 初期化: 前日EOD = 10,000,000
    init = bg._default_state()
    init['last_eod_portfolio_value'] = 10_000_000
    init['portfolio_value']          = 10_000_000
    state_file.write_text(json.dumps(init))

    # sell で +50,000 確定、評価額は 10,040,000 に
    bg.update_pnl(pnl_jpy=50_000, portfolio_value=10_040_000)

    state = json.loads(state_file.read_text())
    # realized: +50,000 だけ累積
    assert state['realized_pnl_jpy_today'] == 50_000
    # daily_pnl_jpy は 評価額 - last_eod = +40,000（= 評価額ベース、一本化）
    assert state['daily_pnl_jpy'] == 40_000


def test_snapshot_is_sole_writer_of_daily_pnl(tmp_path, monkeypatch):
    """2 回 update_pnl しても daily_pnl_jpy は評価額ベースでのみ確定する"""
    state_file = tmp_path / 'guard_state.json'
    monkeypatch.setattr(bg, 'STATE_FILE', state_file)

    init = bg._default_state()
    init['last_eod_portfolio_value'] = 10_000_000
    init['portfolio_value']          = 10_000_000
    state_file.write_text(json.dumps(init))

    # 1 回目: +30,000 確定、評価額 10,020,000
    bg.update_pnl(30_000, 10_020_000)
    s1 = json.loads(state_file.read_text())
    # 2 回目: +20,000 確定、評価額 10,050,000
    bg.update_pnl(20_000, 10_050_000)
    s2 = json.loads(state_file.read_text())

    # realized は累積 (30k + 20k = 50k)
    assert s2['realized_pnl_jpy_today'] == 50_000
    # daily_pnl_jpy は eod 基準の評価額差分でのみ（累積ではない）
    assert s2['daily_pnl_jpy'] == 50_000   # 10,050,000 - 10,000,000 = 50,000
    # NOT 80_000 (= 30+20+30) のような二重計上が無い
