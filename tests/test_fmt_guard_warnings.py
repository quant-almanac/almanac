"""_fmt_guard_warnings must not tell the AI's own prompt context that a
real loss threshold was breached when the actual cause is an unconfirmed
EOD basis (new_entry_allowed=None). Misreporting this as "日次損失閾値-4%超過"
makes the analysis reason from a loss that never happened (2026-09 review, F1).
"""
import analyst


def test_known_bad_daily_threshold_still_reports_as_a_breach():
    guard = {"trading_allowed": True, "new_entry_allowed": False, "alerts": []}
    text = analyst._fmt_guard_warnings(guard)
    assert "日次損失閾値-4%超過" in text
    assert "確認基準が不足" not in text


def test_unknown_basis_reports_confirmation_needed_not_a_loss_breach():
    guard = {"trading_allowed": True, "new_entry_allowed": None, "alerts": []}
    text = analyst._fmt_guard_warnings(guard)
    assert "確認基準が不足" in text
    assert "損失閾値超過ではない" in text
    assert "日次損失閾値-4%超過" not in text


def test_active_trade_cap_reason_is_unaffected():
    guard = {
        "trading_allowed": True, "new_entry_allowed": False, "active_trades": 40,
        "alerts": [{"message": "アクティブトレード数上限"}],
    }
    text = analyst._fmt_guard_warnings(guard)
    assert "保有ポジション数40件が上限到達" in text


def test_all_trading_stopped_is_unaffected_by_the_none_case():
    guard = {"trading_allowed": False, "new_entry_allowed": None, "alerts": []}
    text = analyst._fmt_guard_warnings(guard)
    assert "全トレード停止" in text
    assert "確認基準が不足" not in text


def test_ok_state_produces_no_warning_text():
    guard = {"trading_allowed": True, "new_entry_allowed": True, "alerts": []}
    assert analyst._fmt_guard_warnings(guard) == ""


def test_empty_guard_produces_no_warning_text():
    assert analyst._fmt_guard_warnings({}) == ""
