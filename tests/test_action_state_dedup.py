"""Tests for action_state_tracker dedup logic (Codex review 2026-05-26).

Bug being fixed: register_pending was creating a new entry on every analyzer
run, causing META×4, LLY×4 etc. to accumulate over multiple days. Now any
existing pending entry with the same (ticker, normalized_action_type,
account_bucket) is updated in place instead.

Coverage:
  - _normalize_action_type: sell-bucket / buy-bucket / passthrough
  - _account_bucket: explicit account field / 妻NISA / 夫NISA / 一般 / 特定 / 信用 / 持株会 / default
  - _dedup_key: deterministic string
  - _find_existing_pending: pending only (placed/filled/cancelled excluded)
  - record_recommendations:
      · second call with same (ticker, type, account) → updates, no new entry
      · different account → separate entry
      · sell vs trim vs take_profit → all dedup to same "sell" bucket
      · placed entry not merged into (kept separate)
      · update_count increments / recommended_at refreshes
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import action_state_tracker as ast  # noqa: E402


def _write_state(path: Path, actions: dict) -> None:
    path.write_text(json.dumps({"actions": actions, "last_updated": ""},
                                ensure_ascii=False), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _normalize_action_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("sell",        "sell"),
    ("trim",        "sell"),
    ("take_profit", "sell"),
    ("reduce",      "sell"),
    ("stop_loss",   "sell"),
    ("exit",        "sell"),
    ("close",       "sell"),
    ("buy",         "buy"),
    ("add",         "buy"),
    ("dca",         "buy"),
    ("margin_buy",  "buy"),
    ("scale_in",    "buy"),
    ("hold",        "hold"),
    ("",            "other"),
    (None,          "other"),
    ("TRIM",        "sell"),    # case-insensitive
    (" BUY ",       "buy"),     # strip
])
def test_normalize_action_type(raw, expected) -> None:
    assert ast._normalize_action_type(raw) == expected


# ---------------------------------------------------------------------------
# _account_bucket
# ---------------------------------------------------------------------------


def test_account_bucket_explicit_account_field() -> None:
    assert ast._account_bucket({"account": "wife_nisa"}) == "wife_nisa"


def test_account_bucket_wife_nisa_from_detail() -> None:
    assert ast._account_bucket({"action_detail": "妻NISA成長投資枠でMETA 2株"}) == "wife_nisa"


def test_account_bucket_husband_nisa_from_detail() -> None:
    assert ast._account_bucket({"action_detail": "夫NISAつみたて枠"}) == "husband_nisa"


def test_account_bucket_nisa_growth_defaults_husband() -> None:
    """『NISA成長』だけで妻が付いていない → 夫NISA扱い。"""
    assert ast._account_bucket({"action_detail": "NISA成長投資枠でAVGO 1株"}) == "husband_nisa"


def test_account_bucket_general() -> None:
    assert ast._account_bucket({"action_detail": "AVGO一般口座から5株トリム"}) == "general"


def test_account_bucket_specific() -> None:
    assert ast._account_bucket({"action_detail": "TXN特定口座 1株売却"}) == "specific"


def test_account_bucket_margin() -> None:
    assert ast._account_bucket({"action_detail": "信用買い建て NVDA"}) == "margin"


def test_account_bucket_esop() -> None:
    assert ast._account_bucket({"action_detail": "持株会 9999.T 売却"}) == "esop"


def test_account_bucket_default_when_nothing_matches() -> None:
    assert ast._account_bucket({"action_detail": "AAPL 1株を購入"}) == "default"


def test_account_bucket_non_dict_returns_default() -> None:
    assert ast._account_bucket("not a dict") == "default"


def test_account_bucket_explicit_overrides_detail_text() -> None:
    """明示 account フィールドが detail のキーワードに優先する。"""
    act = {"account": "general", "action_detail": "妻NISA枠で買付"}
    assert ast._account_bucket(act) == "general"


def test_account_bucket_structured_routing_separates_owner_and_broker() -> None:
    wife = ast._account_bucket({
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
        "execution_investment_type": "long",
    })
    husband = ast._account_bucket({
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "NISA成長投資枠",
        "execution_investment_type": "long",
    })

    assert wife == "wife|sbi|nisa成長投資枠|long"
    assert husband == "husband|rakuten|nisa成長投資枠|long"
    assert wife != husband


# ---------------------------------------------------------------------------
# _dedup_key
# ---------------------------------------------------------------------------


def test_dedup_key_format() -> None:
    assert ast._dedup_key("META", "buy", "wife_nisa") == "META|buy|wife_nisa"


def test_dedup_key_normalizes_action_type() -> None:
    """trim と sell は同じバケット → 同じ key になる。"""
    k1 = ast._dedup_key("AVGO", "trim", "general")
    k2 = ast._dedup_key("AVGO", "sell", "general")
    k3 = ast._dedup_key("AVGO", "take_profit", "general")
    assert k1 == k2 == k3


def test_dedup_key_differs_on_account() -> None:
    k1 = ast._dedup_key("META", "buy", "wife_nisa")
    k2 = ast._dedup_key("META", "buy", "husband_nisa")
    assert k1 != k2


# ---------------------------------------------------------------------------
# _find_existing_pending
# ---------------------------------------------------------------------------


def test_find_existing_pending_returns_id_on_match() -> None:
    state = {"actions": {
        "abc123": {
            "id": "abc123", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA 2株", "status": "pending",
            "recommended_at": "2026-05-21T00:00:00",
        }
    }}
    key = ast._dedup_key("META", "buy", "wife_nisa")
    assert ast._find_existing_pending(state, key) == "abc123"


def test_find_existing_pending_skips_placed_entries() -> None:
    """placed のエントリは別注文として扱う → dedup の対象外。"""
    state = {"actions": {
        "abc123": {
            "id": "abc123", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA 2株", "status": "placed",
            "recommended_at": "2026-05-21T00:00:00",
        }
    }}
    key = ast._dedup_key("META", "buy", "wife_nisa")
    assert ast._find_existing_pending(state, key) is None


def test_find_existing_pending_picks_most_recent() -> None:
    """複数 pending あれば最新の recommended_at を選ぶ。"""
    state = {"actions": {
        "old": {
            "id": "old", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA", "status": "pending",
            "recommended_at": "2026-05-20T00:00:00",
        },
        "new": {
            "id": "new", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA", "status": "pending",
            "recommended_at": "2026-05-22T00:00:00",
        },
    }}
    key = ast._dedup_key("META", "buy", "wife_nisa")
    assert ast._find_existing_pending(state, key) == "new"


def test_find_existing_pending_returns_none_on_no_match() -> None:
    state = {"actions": {}}
    key = ast._dedup_key("META", "buy", "wife_nisa")
    assert ast._find_existing_pending(state, key) is None


# ---------------------------------------------------------------------------
# record_recommendations — the bug fix
# ---------------------------------------------------------------------------


def test_record_first_call_inserts_entry(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    n = ast.record_recommendations([{
        "ticker": "META", "type": "buy", "urgency": "medium",
        "action": "妻NISAでMETA 2株を指値$601で買付",
        "reason": "USD比率改善",
    }])
    assert n == 1
    state = _read_state(state_path)
    assert len(state["actions"]) == 1


def test_record_second_call_same_dedup_key_updates_not_inserts(tmp_path, monkeypatch) -> None:
    """REGRESSION: 同一 (ticker, type, account) を2回呼ぶ → 1件のみ存在。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([{
        "ticker": "META", "type": "buy", "urgency": "medium",
        "action": "妻NISAでMETA 2株を指値$601で買付", "reason": "round1",
    }])
    n2 = ast.record_recommendations([{
        "ticker": "META", "type": "buy", "urgency": "medium",
        "action": "妻NISAでMETA 2株を指値$608で買付", "reason": "round2 price updated",
    }])
    assert n2 == 0   # 新規 added は0、updated は1
    state = _read_state(state_path)
    assert len(state["actions"]) == 1
    entry = list(state["actions"].values())[0]
    assert entry["reason"] == "round2 price updated"   # 最新で上書き
    assert entry["update_count"] == 1
    assert "last_updated_at" in entry


def test_record_four_calls_no_accumulation(tmp_path, monkeypatch) -> None:
    """REGRESSION: 4日連続で同じ action を出しても entry は1件のまま。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    for i in range(4):
        ast.record_recommendations([{
            "ticker": "META", "type": "buy", "urgency": "medium",
            "action": f"妻NISAでMETA day{i}", "reason": f"day {i}",
        }])
    state = _read_state(state_path)
    pending = [a for a in state["actions"].values() if a.get("status") == "pending"]
    assert len(pending) == 1
    assert pending[0]["update_count"] == 3  # 1st insert + 3 updates


def test_record_different_account_creates_separate_entry(tmp_path, monkeypatch) -> None:
    """同じ ticker × buy でも 妻NISA と 夫NISA は別注文。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([
        {"ticker": "META", "type": "buy", "urgency": "medium",
         "action": "妻NISAでMETA 2株", "reason": "wife"},
        {"ticker": "META", "type": "buy", "urgency": "medium",
         "action": "夫NISAでMETA 1株", "reason": "husband"},
    ])
    state = _read_state(state_path)
    assert len(state["actions"]) == 2


def test_record_trim_and_sell_dedup_to_same_entry(tmp_path, monkeypatch) -> None:
    """trim と sell は同じバケット → 既存 pending を update。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([{
        "ticker": "AVGO", "type": "trim", "urgency": "medium",
        "action": "AVGO一般口座から5株トリム", "reason": "round1",
    }])
    ast.record_recommendations([{
        "ticker": "AVGO", "type": "take_profit", "urgency": "medium",
        "action": "AVGO一般口座から5株を利確", "reason": "round2",
    }])
    state = _read_state(state_path)
    pending = [a for a in state["actions"].values() if a.get("status") == "pending"]
    assert len(pending) == 1
    assert pending[0]["action_type"] == "take_profit"   # 最新で上書き


def test_record_recommendation_persists_inventory_evidence(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([{
        "ticker": "AVGO",
        "type": "trim",
        "urgency": "low",
        "action": "特定口座から3株トリム",
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
        "holding_shares_after": 2,
        "holding_quantity_exceeds_account": False,
    }])

    entry = next(iter(_read_state(state_path)["actions"].values()))
    assert entry["holding_shares_before"] == 5
    assert entry["requested_sell_quantity"] == 3
    assert entry["holding_shares_after"] == 2
    assert entry["holding_quantity_exceeds_account"] is False


def test_record_placed_entry_not_merged(tmp_path, monkeypatch) -> None:
    """placed 済みエントリは別注文として残り、新しい pending が追加される。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "placed1": {
            "id": "placed1", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA 2株", "status": "placed",
            "recommended_at": "2026-05-21T00:00:00",
            "placed_at": "2026-05-21T09:00:00", "filled_at": None,
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([{
        "ticker": "META", "type": "buy", "urgency": "medium",
        "action": "妻NISAでMETA 追加買付", "reason": "new round",
    }])
    state = _read_state(state_path)
    assert len(state["actions"]) == 2   # placed + new pending
    statuses = sorted(a["status"] for a in state["actions"].values())
    assert statuses == ["pending", "placed"]


def test_record_cancelled_entry_not_merged(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "cnl1": {
            "id": "cnl1", "ticker": "META", "action_type": "buy",
            "action_detail": "妻NISAでMETA", "status": "cancelled",
            "recommended_at": "2026-05-21T00:00:00",
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([{
        "ticker": "META", "type": "buy", "urgency": "medium",
        "action": "妻NISAでMETA再提案", "reason": "fresh",
    }])
    state = _read_state(state_path)
    assert len(state["actions"]) == 2


def test_record_dedup_summary_written_to_state(tmp_path, monkeypatch) -> None:
    """state['last_dedup_summary'] に added/updated 件数が記録される。"""
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    ast.record_recommendations([
        {"ticker": "META", "type": "buy",
         "action": "妻NISAでMETA", "reason": "x"},
        {"ticker": "LLY", "type": "buy",
         "action": "妻NISAでLLY", "reason": "y"},
    ])
    ast.record_recommendations([
        {"ticker": "META", "type": "buy",
         "action": "妻NISAでMETA updated", "reason": "z"},
    ])
    state = _read_state(state_path)
    s = state["last_dedup_summary"]
    assert s["added"] == 0
    assert s["updated"] == 1


def test_record_empty_ticker_skipped(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    n = ast.record_recommendations([
        {"ticker": "", "type": "buy", "action": "x", "reason": "y"},
        {"ticker": "META", "type": "", "action": "x", "reason": "y"},
    ])
    assert n == 0
    state = _read_state(state_path)
    assert len(state["actions"]) == 0


def test_expiry_minutes_persistently_expires_pending_action(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "old": {
            "id": "old", "ticker": "ROBO", "action_type": "sell",
            "status": "pending", "recommended_at": "2020-01-01T09:00:00",
            "expiry_minutes": 30,
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    assert ast.expire_old_actions() == 1
    row = _read_state(state_path)["actions"]["old"]
    assert row["status"] == "expired"
    assert row["expire_reason"] == "recommended_at_plus_expiry_minutes"
    assert row["expired_at"]


def test_expiry_does_not_run_before_next_market_open(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    opens_at = datetime.now().astimezone() + timedelta(hours=2)
    _write_state(state_path, {
        "waiting": {
            "id": "waiting", "ticker": "ROBO", "action_type": "sell",
            "status": "pending", "recommended_at": "2020-01-01T09:00:00",
            "expiry_starts_at": opens_at.isoformat(), "expiry_minutes": 30,
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    assert ast.expire_old_actions() == 0
    assert _read_state(state_path)["actions"]["waiting"]["status"] == "pending"


def test_market_reprice_candidate_is_not_pending_and_fresh_update_starts_new_ttl(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    assert ast.record_recommendations([{
        "ticker": "ROBO", "type": "sell", "action": "ROBOを売却", "reason": "weekend",
        "expiry_minutes": 720,
        "market_reprice_required": True,
        "expiry_deferred_until_reprice": True,
        "market_reprice_after": "2026-07-20T13:30:00+00:00",
    }]) == 1
    first = next(iter(_read_state(state_path)["actions"].values()))
    assert first["status"] == "reprice_required"
    assert ast.get_all_pending() == []

    assert ast.record_recommendations([{
        "ticker": "ROBO", "type": "sell", "action": "ROBOを売却", "reason": "fresh price",
        "expiry_minutes": 720,
    }]) == 0
    updated = next(iter(_read_state(state_path)["actions"].values()))
    assert updated["status"] == "pending"
    assert updated.get("expiry_deferred_until_reprice") is None


def test_explicit_structured_action_supersedes_legacy_pending_without_merge(
    tmp_path, monkeypatch,
) -> None:
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "legacy": {
            "id": "legacy", "ticker": "XLF", "action_type": "buy",
            "status": "pending", "recommended_at": "2026-07-16T06:00:00",
            "action_detail": "XLFを買付",
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    assert ast.record_recommendations([{
        "ticker": "XLF", "type": "add", "action": "妻NISAでXLFを買付",
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
        "supersedes_action_state_id": "legacy",
    }]) == 1

    rows = _read_state(state_path)["actions"]
    assert rows["legacy"]["status"] == "superseded"
    successor = next(row for key, row in rows.items() if key != "legacy")
    assert successor["supersedes"] == "legacy"
    assert rows["legacy"]["superseded_by"] == successor["id"]
