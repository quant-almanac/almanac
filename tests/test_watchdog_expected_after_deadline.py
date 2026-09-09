"""weekday スケジュールスクリプトの「完了確認期限」前は never_run を誤検知しない (S0-c)。

背景 (2026-09 レビュー): portfolio_agent を EXPECTED_INTERVALS へ新規登録した直後、
デプロイ直後〜平日06:35の初回実行までの間に watchdog が 20 回以上 never_run を
検知し、実際に Telegram 通知も発生した（本番 watchdog_log.txt で確認）。
earnings_proximity の新規登録でも同じ経路が再発する。

expected_after_jst は「その日の予定実行が完了しているべき時刻 (JST, HH:MM)」を
オプションで宣言する。この時刻より前の never_run は「まだ機会が無かっただけ」
として抑止し、時刻を過ぎてなお未実行なら通常どおり検知する。

ホスト TZ に依存せず判定すること（2026-09 レビュー・Codex 指摘2 と同根の問題:
_is_weekend / _is_monday_morning_grace は time.localtime() ベースで元々 TZ 依存だが、
この新機能はそれを踏襲しない）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import watchdog as wd

JST = ZoneInfo("Asia/Tokyo")


def _epoch_at_jst(y, m, d, hh, mm) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=JST).timestamp()


@pytest.fixture
def single_script(monkeypatch):
    """EXPECTED_INTERVALS を1エントリだけに絞り、他スクリプトの影響を排除する。"""
    cfg = {
        'earnings_proximity': {
            'max_stale_sec': 26 * 3600,
            'weekday_only': True,
            'warn_is_error': True,
            'expected_after_jst': '06:45',
        },
    }
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    return cfg


def test_before_deadline_never_run_is_not_flagged(single_script):
    # 2026-09-08 は火曜日、06:30 JST（06:45 より前）。
    now = _epoch_at_jst(2026, 9, 8, 6, 30)
    result = wd.evaluate_heartbeats({}, now=now)
    assert result['stale'] == [], "06:45 より前の never_run は誤検知させない"


def test_after_deadline_never_run_is_flagged(single_script):
    # 同じ火曜日、07:00 JST（06:45 より後）でまだ一度も実行記録が無い。
    now = _epoch_at_jst(2026, 9, 8, 7, 0)
    result = wd.evaluate_heartbeats({}, now=now)
    assert len(result['stale']) == 1
    assert result['stale'][0]['script'] == 'earnings_proximity'
    assert result['stale'][0]['reason'] == 'never_run'


def test_deadline_is_evaluated_in_jst_regardless_of_host_tz(single_script, monkeypatch):
    """同一 UTC 瞬間に対する before/after 判定が、ホスト TZ 環境変数で変わらない。"""
    # 06:30 JST は 21:30 UTC（前日）。TZ=UTC のホストでも 06:45 JST を基準に判定する。
    now = _epoch_at_jst(2026, 9, 8, 6, 30)
    monkeypatch.setenv("TZ", "UTC")
    result_utc_host = wd.evaluate_heartbeats({}, now=now)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    result_jst_host = wd.evaluate_heartbeats({}, now=now)
    assert result_utc_host['stale'] == result_jst_host['stale'] == []


def test_scripts_without_expected_after_jst_are_unaffected(monkeypatch):
    """既存スクリプトの挙動は一切変えない（フィールドが無ければ従来どおり即 never_run）。"""
    cfg = {
        'data_fetcher': {'max_stale_sec': 26 * 3600, 'weekday_only': True},
    }
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    now = _epoch_at_jst(2026, 9, 8, 0, 1)  # 深夜、どんな明示的締切より前でも
    result = wd.evaluate_heartbeats({}, now=now)
    assert len(result['stale']) == 1
    assert result['stale'][0]['script'] == 'data_fetcher'


def test_weekend_grace_still_applies_even_with_expected_after_jst(single_script, monkeypatch):
    # 2026-09-12 は土曜日。
    monkeypatch.setattr(wd, "_is_weekend", lambda: True)
    now = _epoch_at_jst(2026, 9, 12, 12, 0)
    result = wd.evaluate_heartbeats({}, now=now)
    assert result['stale'] == []


def test_deadline_only_applies_to_the_never_run_branch_not_to_stale_scripts_with_history(
    single_script,
):
    """既に実行履歴がある場合の 26h stale 判定は expected_after_jst の影響を受けない。"""
    old_ts = _epoch_at_jst(2026, 9, 6, 6, 15)  # 日曜扱いにならない直近の平日実行
    hb = {'earnings_proximity': {'last_run_ts': old_ts, 'status': 'ok'}}
    now = _epoch_at_jst(2026, 9, 8, 6, 30)  # 06:45 より前だが、履歴の age は 26h 超のはず
    age_hours = (now - old_ts) / 3600
    assert age_hours > 26, "テスト前提: 26h を超えていること"
    result = wd.evaluate_heartbeats(hb, now=now)
    assert len(result['stale']) == 1
    assert result['stale'][0]['reason'].startswith('older_than_')


def test_malformed_expected_after_jst_does_not_crash_and_falls_back_to_flagging(monkeypatch):
    cfg = {'earnings_proximity': {'max_stale_sec': 26 * 3600, 'weekday_only': True,
                                   'expected_after_jst': 'not-a-time'}}
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    now = _epoch_at_jst(2026, 9, 8, 0, 1)
    result = wd.evaluate_heartbeats({}, now=now)
    assert len(result['stale']) == 1


# --- S7-Codex#7: 実行履歴があるスクリプトでも「今日の締切超過」を検知する ---
#
# 上の test_deadline_only_applies_to_the_never_run_branch_not_to_stale_scripts_with_history
# が扱うのは age が既に max_stale_sec (26h) を超えているケースのみ。
# 実際に再現した欠陥はその手前: 前回成功が「前日」で、age がまだ 26h に
# 達していなくても、"今日の" 締切 (expected_after_jst) は既に過ぎている、
# という間隙が一切検知されなかった（2026-09 レビュー Codex 指摘 #7）。
# 例: 前回成功が前日06:15、今が当日08:59（age=24.7h、26h未満）。
# 締切06:45は過ぎているのに stale=[] のまま。


def test_history_present_but_stale_and_past_todays_deadline_is_flagged(single_script):
    """再現の核心: age=24.7h（26h未満）・前回成功は前日・当日06:45締切は超過済み。"""
    old_ts = _epoch_at_jst(2026, 9, 7, 6, 15)  # 月曜 06:15
    now = _epoch_at_jst(2026, 9, 8, 6, 59)     # 火曜 06:59（06:45締切より後）
    age_hours = (now - old_ts) / 3600
    assert age_hours < 26, "テスト前提: max_stale_sec にはまだ達していないこと"

    hb = {'earnings_proximity': {'last_run_ts': old_ts, 'status': 'ok'}}
    result = wd.evaluate_heartbeats(hb, now=now)

    assert len(result['stale']) == 1
    assert result['stale'][0]['script'] == 'earnings_proximity'
    assert result['stale'][0]['reason'] == 'missed_todays_deadline'
    assert result['ok'] == []


def test_history_present_ran_today_before_now_is_not_flagged_even_after_deadline(
    single_script,
):
    """当日中に既に実行済みなら、締切前・締切後を問わず missed 扱いにしない。"""
    today_ts = _epoch_at_jst(2026, 9, 8, 6, 20)  # 火曜 06:20（締切06:45より前に完了）
    now = _epoch_at_jst(2026, 9, 8, 7, 0)         # 締切より後に評価
    hb = {'earnings_proximity': {'last_run_ts': today_ts, 'status': 'ok'}}
    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []
    assert result['ok'] == ['earnings_proximity']


def test_history_present_stale_but_before_todays_deadline_is_not_flagged_yet(
    single_script,
):
    """前回成功が前日でも、今日の締切にまだ達していなければ猶予する
    （never_run 側の猶予ロジックと対称）。"""
    old_ts = _epoch_at_jst(2026, 9, 7, 6, 15)  # 月曜 06:15
    now = _epoch_at_jst(2026, 9, 8, 6, 30)     # 火曜 06:30（06:45締切より前）
    hb = {'earnings_proximity': {'last_run_ts': old_ts, 'status': 'ok'}}
    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []
    assert result['ok'] == ['earnings_proximity']


def test_history_present_malformed_deadline_does_not_add_spurious_flag(monkeypatch):
    cfg = {'earnings_proximity': {'max_stale_sec': 26 * 3600, 'weekday_only': True,
                                   'expected_after_jst': 'not-a-time'}}
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    old_ts = _epoch_at_jst(2026, 9, 7, 6, 15)
    now = _epoch_at_jst(2026, 9, 8, 6, 59)
    hb = {'earnings_proximity': {'last_run_ts': old_ts, 'status': 'ok'}}
    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []


def test_history_present_without_expected_after_jst_is_unaffected(monkeypatch):
    """expected_after_jst の無いスクリプトは新ロジックの対象外（従来どおり age のみ）。"""
    cfg = {'data_fetcher': {'max_stale_sec': 26 * 3600, 'weekday_only': True}}
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    old_ts = _epoch_at_jst(2026, 9, 7, 6, 15)
    now = _epoch_at_jst(2026, 9, 8, 6, 59)  # age < 26h
    hb = {'data_fetcher': {'last_run_ts': old_ts, 'status': 'ok'}}
    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []
    assert result['ok'] == ['data_fetcher']


# --- S7a-Codex#7 3ラウンド目: 月曜朝の猶予が「自分の締切」を上書きしない ---
#
# _is_monday_morning_grace()（月曜9:00まで、週末分の stale を一律で猶予）は
# entry is None 分岐の一番最初でチェックされ、expected_after_jst の
# 締切チェックより先に continue してしまっていた。earnings_proximity の
# 06:45 締切は月曜の猶予期間(〜9:00)にすっぽり収まるため、月曜だけ
# never_run 検知が実質無効化されていた（2026-09 レビュー Codex 3ラウンド目
# 指摘 #7a・実機再現）。


def test_monday_morning_grace_does_not_override_an_already_passed_deadline(
    single_script, monkeypatch,
):
    """再現の核心: 月曜07:00（月曜朝の猶予中=9:00より前）・締切06:45は
    既に超過・一度も実行記録が無い ―― 猶予より自スクリプトの締切を優先する。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    now = _epoch_at_jst(2026, 9, 14, 7, 0)  # 月曜 07:00（実際の暦日は無関係、grace は直接注入）

    result = wd.evaluate_heartbeats({}, now=now)

    assert len(result['stale']) == 1
    assert result['stale'][0]['script'] == 'earnings_proximity'
    assert result['stale'][0]['reason'] == 'never_run'


def test_monday_morning_grace_still_applies_before_the_deadline(
    single_script, monkeypatch,
):
    """月曜朝でも、まだ自分の締切前なら従来どおり猶予する（対称性）。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    now = _epoch_at_jst(2026, 9, 14, 6, 30)  # 06:45締切より前

    result = wd.evaluate_heartbeats({}, now=now)
    assert result['stale'] == []


def test_monday_morning_grace_still_applies_to_scripts_without_a_deadline(monkeypatch):
    """expected_after_jst の無いスクリプトへの月曜朝猶予は従来どおり維持する。"""
    cfg = {'data_fetcher': {'max_stale_sec': 26 * 3600, 'weekday_only': True}}
    monkeypatch.setattr(wd, "EXPECTED_INTERVALS", cfg)
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    now = _epoch_at_jst(2026, 9, 14, 7, 0)

    result = wd.evaluate_heartbeats({}, now=now)
    assert result['stale'] == []


def test_weekend_grace_is_unaffected_by_the_deadline_precedence_change(
    single_script, monkeypatch,
):
    """既存の週末猶予（_is_weekend）は引き続き最優先で無条件に効く。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: True)
    now = _epoch_at_jst(2026, 9, 12, 12, 0)  # 土曜、締切をとうに過ぎた時刻

    result = wd.evaluate_heartbeats({}, now=now)
    assert result['stale'] == []


# --- S7c-Codex#7 4ラウンド目: 履歴ありブランチも月曜朝の猶予に飲み込まれない ---
#
# ラウンド3で直したのは entry is None（実行履歴が一度も無い）分岐だけ
# だった。実行履歴がある分岐は、weekday_only スクリプトなら
# _is_weekend()/_is_monday_morning_grace() の判定が締切チェックより先に
# 無条件で continue するため、月曜09:00までは「金曜の成功履歴があるが
# 月曜まだ未実行」も「月曜中に履歴保存が失敗し status=warn になった」も
# 一律 ok に潰れていた（2026-09 レビュー Codex 4ラウンド目 指摘 #1・
# 実機再現）。


def test_history_present_monday_grace_does_not_override_an_already_passed_deadline(
    single_script, monkeypatch,
):
    """再現の核心: 前回成功はまだ26h未満(age < max_stale_sec)・月曜07:00は
    まだ未実行・締切06:45は既に超過 ―― 月曜朝の猶予中でも
    missed_todays_deadline を検知する（older_than_26h ではなくこちらが
    先に検知できることを確認する ―― age が先に閾値を超えるケースは
    既存の older_than_ テストで別途カバー済み）。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    sunday_ts = _epoch_at_jst(2026, 9, 13, 9, 5)  # 日曜、age<26hに収める
    now = _epoch_at_jst(2026, 9, 14, 7, 0)  # 月曜 07:00
    age_hours = (now - sunday_ts) / 3600
    assert age_hours < 26, "テスト前提: max_stale_sec にはまだ達していないこと"
    hb = {'earnings_proximity': {'last_run_ts': sunday_ts, 'status': 'ok'}}

    result = wd.evaluate_heartbeats(hb, now=now)

    assert len(result['stale']) == 1
    assert result['stale'][0]['script'] == 'earnings_proximity'
    assert result['stale'][0]['reason'] == 'missed_todays_deadline'
    assert result['ok'] == []


def test_history_present_monday_grace_still_applies_before_the_deadline(
    single_script, monkeypatch,
):
    """月曜朝でも、まだ自分の締切前なら従来どおり猶予する（対称性）。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    friday_ts = _epoch_at_jst(2026, 9, 11, 6, 15)
    now = _epoch_at_jst(2026, 9, 14, 6, 30)  # 06:45締切より前
    hb = {'earnings_proximity': {'last_run_ts': friday_ts, 'status': 'ok'}}

    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []
    assert result['ok'] == ['earnings_proximity']


def test_history_present_monday_grace_does_not_swallow_warn_is_error(
    single_script, monkeypatch,
):
    """再現の核心その2: 月曜中に実行はしたが run history 保存に失敗して
    status=warn になったケース（missed_todays_deadline は該当しない=
    today 実行済み）でも、warn_is_error 経由で errors に現れるべき。
    猶予ブロックの status 判定が status=='error' だけを見ていたため、
    warn を無条件 ok に落としていた。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: False)
    monkeypatch.setattr(wd, "_is_monday_morning_grace", lambda: True)
    monday_warn_ts = _epoch_at_jst(2026, 9, 14, 6, 40)  # 締切前に実行はした
    now = _epoch_at_jst(2026, 9, 14, 7, 0)
    hb = {'earnings_proximity': {
        'last_run_ts': monday_warn_ts, 'status': 'warn',
        'error': 'run_history_write_failed',
    }}

    result = wd.evaluate_heartbeats(hb, now=now)

    assert any(e['script'] == 'earnings_proximity' for e in result['errors']), (
        "warn_is_error のスクリプトの warn は、月曜朝の猶予中でも errors に現れるべき"
    )
    assert result['ok'] == []
    assert result['stale'] == []


def test_history_present_weekend_grace_still_swallows_ok_status(
    single_script, monkeypatch,
):
    """週末猶予（_is_weekend）自体は従来どおり ok/errors への振り分けのみ
    （stale化はしない）。既存動作を壊していないことの確認。"""
    monkeypatch.setattr(wd, "_is_weekend", lambda: True)
    friday_ts = _epoch_at_jst(2026, 9, 11, 6, 15)
    now = _epoch_at_jst(2026, 9, 12, 12, 0)  # 土曜
    hb = {'earnings_proximity': {'last_run_ts': friday_ts, 'status': 'ok'}}

    result = wd.evaluate_heartbeats(hb, now=now)
    assert result['stale'] == []
    assert result['ok'] == ['earnings_proximity']


def test_history_present_weekend_grace_still_surfaces_warn_is_error(
    single_script, monkeypatch,
):
    monkeypatch.setattr(wd, "_is_weekend", lambda: True)
    warn_ts = _epoch_at_jst(2026, 9, 12, 6, 40)
    now = _epoch_at_jst(2026, 9, 12, 12, 0)
    hb = {'earnings_proximity': {'last_run_ts': warn_ts, 'status': 'warn', 'error': 'x'}}

    result = wd.evaluate_heartbeats(hb, now=now)
    assert any(e['script'] == 'earnings_proximity' for e in result['errors']), (
        "週末猶予中でも warn_is_error は errors に現れるべき（既存動作の維持確認）"
    )
