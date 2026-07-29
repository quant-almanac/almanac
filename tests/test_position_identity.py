"""Stage 0B: PositionIdentity 単位の鮮度権威。

背景: 2026-07-27 のインシデントで、AVGO/XLF の証券会社同期は holdings.json
の該当エントリで note="楽天CSV保有同期 2026-07-14" のまま止まっていたが、
無関係な LLY の約定が 2026-07-23 に portfolio_applied=True となったことで
execution_readiness.portfolio_snapshot_health() のファイル全体判定が
「全ポジション新鮮」を返し、AVGO/XLF の trim が execution_readiness="ready"
と誤判定される一因になった。旧チェックは risk_increasing (買い系) でしか
評価されず、売り系には鮮度チェックが一切無かった。

ユーザー判断: 実データで測定すると 39 ポジション中 35 件が72時間超の
staleとなり (証券会社同期が実際に13日止まっている)、既存の72h=blocked
基準をそのまま適用すると発注機能が事実上停止する。そのため stale/degraded/
unknown はいずれも review 止まりとし、blocked にはしない。
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


def test_stale_position_matches_the_actual_incident_math():
    """2026-07-27 インシデントの実数値を再現する。"""
    position = pi.PositionIdentity("husband", "rakuten", "general", "AVGO")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-14"}

    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "stale"
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
    avgo_entry = {"note": "楽天CSV保有同期 2026-07-14"}  # 古いまま

    result = pi.position_freshness(avgo, base_dir=Path("."), now=now, holdings_entry=avgo_entry)
    assert result["status"] == "stale"  # LLY が7/23に更新されていても関係ない


def test_fresh_position_within_24h():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NVDA")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-27"}
    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "fresh"


def test_degraded_between_24_and_72_hours():
    position = pi.PositionIdentity("husband", "rakuten", "general", "NVDA")
    now = datetime(2026, 7, 27, 18, 0)
    entry = {"note": "楽天CSV保有同期 2026-07-25"}  # 2日前 = 48h
    result = pi.position_freshness(position, base_dir=Path("."), now=now, holdings_entry=entry)
    assert result["status"] == "degraded"


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


def test_internal_portfolio_applied_does_not_advance_position_freshness(tmp_path):
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

    assert result["status"] == "stale"
    assert result["source"] == "holdings_note_legacy"


def test_only_fully_evidenced_broker_fill_advances_position_freshness(tmp_path):
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
        "filled_quantity": 5,
        "filled_price": 410.0,
        "reconciled_at": "2026-07-27T17:00:00+09:00",
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
    assert incomplete["status"] == "stale"

    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            **base,
            "reconciliation_snapshot_hash": "sha256:abc",
        }],
    }), encoding="utf-8")
    confirmed = pi.position_freshness(
        position,
        base_dir=tmp_path,
        now=datetime(2026, 7, 27, 18, 0, tzinfo=JST),
        holdings_entry={"note": "楽天CSV保有同期 2026-07-14"},
    )
    assert confirmed["status"] == "fresh"
    assert confirmed["source"] == "broker_confirmed_fill:broker-123"


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
    assert result["source"] == "broker_confirmed_fill:broker-123"


# ---------------------------------------------------------------------------
# execution_readiness への配線: review止まり、blockedにはしない
# ---------------------------------------------------------------------------


def _write_holdings(tmp_path: Path, entries: dict) -> None:
    (tmp_path / "holdings.json").write_text(json.dumps(entries), encoding="utf-8")


def test_stale_sell_action_is_review_not_blocked(tmp_path):
    """本件の直接再現: 売り系 (trim) には旧コードで鮮度チェックが皆無だった。"""
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
    assert "position_broker_sync_stale" in codes
    stale_row = next(r for r in result["execution_block_reasons"] if r["code"] == "position_broker_sync_stale")
    assert stale_row.get("status") == "stale"
    # 本題: stale であっても全体の execution_readiness は blocked まで
    # 上がらない (review 止まり)。この fixture には他に blocked を招く
    # 要因が無いため、これは position_broker_sync_stale 自体の severity を
    # 直接反映する。
    assert result["execution_readiness"] == "review"


def test_buy_action_also_gets_position_level_freshness_check(tmp_path):
    """買い系は従来からファイル全体チェックがあったが、ポジション単位の
    チェックも additive に効くこと。"""
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
    assert "position_broker_sync_stale" in codes


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
    ("楽天CSV保有同期 2026-07-14", "position_broker_sync_stale"),     # 330h
    ("楽天CSV保有同期 2026-07-25", "position_broker_sync_degraded"),  # 48h
    ("手入力・日付なし", "position_broker_sync_unknown"),
])
def test_severity_never_escalates_to_blocked_from_this_check_alone(tmp_path, note, expected_code):
    """ユーザー判断: ポートフォリオ全体の同期停止を blocked にすると発注機能が
    事実上停止するため、stale/degraded/unknown はいずれも review 止まり。

    fixture は position_broker_sync_* 以外の理由で blocked にならないよう
    健全な状態 (execution_plan active, holding_shares_before 十分) にした
    うえで、この特定コードが混ざっても execution_readiness が blocked に
    到達しないことを実際の関門呼び出しで確認する (source文字列の一致に
    頼らない)。"""
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
    assert expected_code in codes
    assert result["execution_readiness"] != "blocked"


def test_canonical_broker_supports_all_position_identity_sources():
    assert canonical_broker("楽天証券") == "rakuten"
    assert canonical_broker("SBI証券") == "sbi"
    assert canonical_broker("マネックス証券") == "monex"
    assert canonical_broker("employee_plan") == "employee_plan"
