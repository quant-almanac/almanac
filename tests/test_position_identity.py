"""Stage 0B: PositionIdentity 単位の鮮度権威。

背景: 2026-07-27 のインシデントで、AVGO/XLF の証券会社同期は holdings.json
の該当エントリで note="楽天CSV保有同期 2026-07-14" のまま止まっていたが、
無関係な LLY の約定が 2026-07-23 に portfolio_applied=True となったことで
execution_readiness.portfolio_snapshot_health() のファイル全体判定が
「全ポジション新鮮」を返し、AVGO/XLF の trim が execution_readiness="ready"
と誤判定される一因になった。旧チェックは risk_increasing (買い系) でしか
評価されず、売り系には鮮度チェックが一切無かった。

2026-07-30 のユーザー判断で、証券会社確認済みの数量・取得原価は時間経過
だけでは失効させない契約へ変更した。snapshot 後に同一 PositionIdentity の
約定が発生した場合だけ invalidated にし、再照合を要求する。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from execution_safety import canonical_broker

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import position_identity as pi  # noqa: E402
from execution_readiness import classify_execution_readiness  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


# ---------------------------------------------------------------------------
# canonical_instrument_id / parse_note_sync_date
# ---------------------------------------------------------------------------


def test_canonical_instrument_id_normalizes_case():
    assert pi.canonical_instrument_id("avgo") == "AVGO"
    assert pi.canonical_instrument_id(" 1489.t ") == "1489.T"


def test_parse_note_sync_date_extracts_date():
    assert pi.parse_note_sync_date("楽天CSV保有同期 2026-07-14") == datetime(2026, 7, 14)


@pytest.mark.parametrize("note", [None, "", "同期日不明", "2026/07/14 誤フォーマット"])
def test_parse_note_sync_date_returns_none_when_unparseable(note):
    assert pi.parse_note_sync_date(note) is None


# ---------------------------------------------------------------------------
# position_identity_for_holding / position_identity_for_action — 鍵の一致
# ---------------------------------------------------------------------------


def test_holding_and_action_identities_match_for_the_same_real_position():
    """holdings.json 側 (日本語表記) と action 側 (canonical英語) が
    同じ実ポジションを指す場合、同じ PositionIdentity になること。"""
    holding_entry = {
        "ticker": "AVGO",
        "account": "一般",
        "broker": "楽天証券",
        "owner": "husband",
    }
    action = {"ticker": "AVGO", "execution_owner": "husband",
              "execution_broker": "rakuten", "execution_account": "一般"}

    from_holding = pi.position_identity_for_holding(holding_entry)
    from_action = pi.position_identity_for_action(action)

    assert from_holding is not None
    assert from_action is not None
    assert from_holding == from_action


def test_wife_owner_inferred_from_account_label():
    entry = {"ticker": "1489", "account": "妻NISA成長投資枠", "broker": "楽天証券"}
    identity = pi.position_identity_for_holding(entry)
    assert identity is not None
    assert identity.owner == "wife"


def test_missing_fields_return_none_not_a_guess():
    assert pi.position_identity_for_holding({"ticker": "AVGO"}) is None
    assert pi.position_identity_for_action({"ticker": "AVGO"}) is None


def test_owner_inference_requires_positive_evidence():
    assert pi.infer_owner_from_holding("妻特定") == "wife"
    assert pi.infer_owner_from_holding("特定", key="AAA_WIFE") == "wife"
    assert pi.infer_owner_from_holding("夫特定") == "husband"
    assert pi.infer_owner_from_holding("特定", key="AAA_HUSBAND") == "husband"
    assert pi.infer_owner_from_holding("特定") is None


def test_holding_with_unknown_owner_fails_closed():
    assert pi.position_identity_for_holding({
        "ticker": "AVGO",
        "account": "特定",
        "broker": "楽天証券",
    }) is None


# ---------------------------------------------------------------------------
# position_freshness — 本件の直接再現
# ---------------------------------------------------------------------------


def test_old_position_snapshot_remains_valid_without_later_events():
    """経過時間は監査情報として残すが、それだけでは失効させない。"""
    position = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-14"}

    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "fresh"
    assert result["synced_at"] == "2026-07-14T00:00:00+09:00"
    assert result["age_hours"] == pytest.approx(330.0, abs=1.0)  # 13.75日


def test_unrelated_ticker_update_does_not_refresh_this_position():
    """本件の核心: 無関係な銘柄 (LLY) の更新はこのポジションの鮮度に影響しない。

    旧 portfolio_snapshot_health() はファイル全体で1つの鮮度しか持たないため
    これができなかった。position_freshness() は holdings_entry を明示的に
    受け取るので、他銘柄の状態を一切参照しない。
    """
    now = datetime(2026, 7, 27, 18, 0)
    avgo = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    avgo_entry = {"note": "楽天CSV保有同期 2026-07-14"}

    result = pi.position_freshness(avgo, base_dir=Path("."), now=now, holdings_entry=avgo_entry)
    assert result["status"] == "fresh"
    assert result["synced_at"] == "2026-07-14T00:00:00+09:00"


def test_fresh_position_within_24h():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NVDA")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-27"}
    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "fresh"


def test_position_does_not_degrade_between_24_and_72_hours():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NVDA")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-25"}  # 2日前 = 48h
    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "fresh"
    assert result["age_hours"] == 66.0


def test_source_as_of_remains_the_audit_timestamp_without_time_expiry(tmp_path):
    position = pi.PositionIdentity("wife", "sbi", "nisa_growth", "1489.T")
    now = datetime(2026, 7, 29, 18, 0, tzinfo=JST)
    entry = {
        "source_as_of": "2026-07-20T09:00:00+09:00",
        "broker_reconciled_at": "2026-07-29T17:00:00+09:00",
    }
    result = pi.position_freshness(position, base_dir=tmp_path, now=now, holdings_entry=entry)
    assert result["status"] == "fresh"
    assert result["source"] == "holding.source_as_of"


def test_unparseable_note_is_unknown_not_silently_fresh():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NVDA")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "手入力・日付なし"}
    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "unknown"


def test_no_matching_holding_is_unknown():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NOTHELD")
    now = datetime(2026, 7, 27, 18, 0)
    result = pi.position_freshness(position, base_dir=Path("/nonexistent"), now=now)
    assert result["status"] == "unknown"
    assert result["source"] == "unknown"


def test_internal_portfolio_applied_invalidates_broker_snapshot(tmp_path):
    position = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "ticker": "AVGO",
            "status": "executed",
            "portfolio_applied": True,
            "saved_at": "2026-07-27T17:00:00+09:00",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "一般",
        }],
    }), encoding="utf-8")

    result = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 18, 0, tzinfo=JST),
        holdings_entry={"note": "楽天CSV保有同期 2026-07-14"},
    )

    assert result["status"] == "invalidated"
    assert result["source"] == "holdings_note_legacy"
    assert result["invalidating_event"]["execution_id"] is None


def test_complete_broker_fill_advances_position_authority_without_new_snapshot(tmp_path):
    position = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    base = {
        "ticker": "AVGO",
        "status": "broker_confirmed_filled",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "一般",
        "external_execution_id": "broker-123",
        "broker_source": "broker_csv",
        "broker_reported_at": "2026-07-27T16:55:00+09:00",
        "saved_at": "2026-07-27T17:00:00+09:00",
        "filled_quantity": 5,
        "filled_price": 410.0,
        "reconciled_at": "2026-07-27T17:00:00+09:00",
        "broker_confirmed_filled": True,
    }
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{**base}],
    }), encoding="utf-8")
    incomplete = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 18, 0, tzinfo=JST),
        holdings_entry={"note": "楽天CSV保有同期 2026-07-14"},
    )
    assert incomplete["status"] == "invalidated"

    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            **base,
            "reconciliation_snapshot_hash": "sha256:abc",
            "portfolio_applied": True,
        }],
    }), encoding="utf-8")
    confirmed = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 18, 0, tzinfo=JST),
        holdings_entry={"note": "楽天CSV保有同期 2026-07-14"},
    )
    assert confirmed["status"] == "fresh"
    assert confirmed["source"] == "broker_confirmed_fill"
    assert confirmed["synced_at"] == "2026-07-27T17:00:00+09:00"

    reconciled = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 19, 0, tzinfo=JST),
        holdings_entry={"source_as_of": "2026-07-27T18:00:00+09:00"},
    )
    assert reconciled["status"] == "fresh"


def test_route_overlay_is_used_for_position_freshness(tmp_path):
    from execution_reconciliation import record_route_correction

    position = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    raw = {
        "id": "broker-fill",
        "ticker": "AVGO",
        "status": "broker_confirmed_filled",
        "account": "特定",
        "external_execution_id": "broker-123",
        "broker_source": "broker_csv",
        "broker_reported_at": "2026-07-27T16:55:00+09:00",
        "filled_quantity": 5,
        "filled_price": 410.0,
        "reconciled_at": "2026-07-27T17:00:00+09:00",
        "reconciliation_snapshot_hash": "sha256:abc",
        "broker_confirmed_filled": True,
        "portfolio_applied": True,
    }
    (tmp_path / "action_executions.json").write_text(
        json.dumps({"executions": [raw]}),
        encoding="utf-8",
    )
    record_route_correction(
        execution_record=raw,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "一般",
        },
        evidence={"row_hash": "fixture"},
        reason="broker evidence",
        approved_by="test",
        state_path=tmp_path / "execution_reconciliation_state.json",
    )

    result = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 18, 0, tzinfo=JST),
        holdings_entry={"note": "楽天CSV保有同期 2026-07-14"},
    )

    assert result["status"] == "fresh"
    assert result["source"] == "broker_confirmed_fill"


# ---------------------------------------------------------------------------
# execution_readiness への配線: review止まり、blockedにはしない
# ---------------------------------------------------------------------------


def _write_holdings(tmp_path: Path, entries: dict) -> None:
    (tmp_path / "holdings.json").write_text(json.dumps(entries), encoding="utf-8")


def test_old_sell_snapshot_is_ready_without_later_events(tmp_path):
    now = datetime(2026, 7, 27, 18, 0, tzinfo=JST)
    _write_holdings(tmp_path, {
        "AVGO_ippan": {"ticker": "AVGO", "account": "一般", "broker": "楽天証券", "owner": "husband",
                       "note": "楽天CSV保有同期 2026-07-14", "shares": 27.0},
    })
    (tmp_path / "account.json").write_text("{}", encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")

    action = {
        "ticker": "AVGO", "type": "trim",
        "execution_owner": "husband", "execution_broker": "rakuten", "execution_account": "一般",
        "holding_shares_before": 27.0, "quantity": 5,
        "order_type": "limit", "limit_price": 400,
    }
    result = classify_execution_readiness(action, base_dir=tmp_path, now=now)

    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert not any(code.startswith("position_broker_") for code in codes)
    assert result["execution_readiness"] == "ready"


def test_buy_action_does_not_expire_position_by_age(tmp_path):
    now = datetime(2026, 7, 27, 18, 0, tzinfo=JST)
    _write_holdings(tmp_path, {
        "AVGO_ippan": {"ticker": "AVGO", "account": "一般", "broker": "楽天証券", "owner": "husband",
                       "note": "楽天CSV保有同期 2026-07-14"},
    })
    (tmp_path / "account.json").write_text(json.dumps({"balance": 1_000_000}), encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({
        "status": "active",
        "budgets": {"normal_pool_available_jpy": 500_000, "opportunity_pool_available_jpy": 0},
        "contribution_summary": {"available_jpy": 500_000},
    }), encoding="utf-8")

    action = {
        "ticker": "AVGO", "type": "add",
        "execution_owner": "husband", "execution_broker": "rakuten", "execution_account": "一般",
        "order_type": "limit", "limit_price": 400, "quantity": 1,
        "execution_plan_gate_mode": "off",
    }
    result = classify_execution_readiness(action, base_dir=tmp_path, now=now)
    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert not any(code.startswith("position_broker_") for code in codes)


def test_fresh_holding_produces_no_freshness_reason(tmp_path):
    """誤検知の防止: 今日同期されたポジションには position_broker_sync_* が出ない。"""
    now = datetime(2026, 7, 27, 18, 0, tzinfo=JST)
    _write_holdings(tmp_path, {
        "AVGO_ippan": {"ticker": "AVGO", "account": "一般", "broker": "楽天証券", "owner": "husband",
                       "note": "楽天CSV保有同期 2026-07-27", "shares": 27.0},
    })
    (tmp_path / "account.json").write_text("{}", encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")

    action = {
        "ticker": "AVGO", "type": "trim",
        "execution_owner": "husband", "execution_broker": "rakuten", "execution_account": "一般",
        "holding_shares_before": 27.0, "quantity": 5,
        "order_type": "limit", "limit_price": 400,
    }
    result = classify_execution_readiness(action, base_dir=tmp_path, now=now)
    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert not any(c.startswith("position_broker_sync_") for c in codes)


def test_missing_execution_routing_skips_check_without_crashing(tmp_path):
    """execution_owner/broker/account の無い legacy action は、識別子を
    解決できないため鮮度チェック自体をスキップする (fail-open で例外にしない。
    holding_scope 等の既存チェックが別途これらの action を扱う)。"""
    now = datetime(2026, 7, 27, 18, 0, tzinfo=JST)
    _write_holdings(tmp_path, {})
    (tmp_path / "account.json").write_text("{}", encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")

    action = {"ticker": "AVGO", "type": "trim"}
    result = classify_execution_readiness(action, base_dir=tmp_path, now=now)  # 例外にならない
    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert not any(c.startswith("position_broker_sync_") for c in codes)


@pytest.mark.parametrize("note,expected_code", [
    ("楽天CSV保有同期 2026-07-14", None),
    ("楽天CSV保有同期 2026-07-25", None),
    ("手入力・日付なし", "position_broker_sync_unknown"),
])
def test_only_unknown_timestamp_requires_review_without_an_event(tmp_path, note, expected_code):
    now = datetime(2026, 7, 27, 18, 0, tzinfo=JST)
    _write_holdings(tmp_path, {
        "AVGO_ippan": {"ticker": "AVGO", "account": "一般", "broker": "楽天証券", "owner": "husband",
                       "note": note, "shares": 27.0},
    })
    (tmp_path / "account.json").write_text("{}", encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({"status": "active"}), encoding="utf-8")

    action = {
        "ticker": "AVGO", "type": "trim",
        "execution_owner": "husband", "execution_broker": "rakuten", "execution_account": "一般",
        "holding_shares_before": 27.0, "quantity": 5,
        "order_type": "limit", "limit_price": 400,
    }
    result = classify_execution_readiness(action, base_dir=tmp_path, now=now)

    codes = {row["code"] for row in result["execution_block_reasons"]}
    if expected_code is None:
        assert not any(code.startswith("position_broker_") for code in codes)
    else:
        assert expected_code in codes
    assert result["execution_readiness"] != "blocked"


def test_canonical_broker_supports_all_position_identity_sources():
    assert canonical_broker("楽天証券") == "rakuten"
    assert canonical_broker("SBI証券") == "sbi"
    assert canonical_broker("マネックス証券") == "monex"
    assert canonical_broker("employee_plan") == "employee_plan"
