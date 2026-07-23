"""tax_harvest_scanner: recommendation_id を action_state_tracker にリンクする回帰テスト。

損出し候補は human_execution_only のまま自動発注しないが、生成時に
action_state_tracker.record_recommendations() へ登録し、既存の
pending/filled/cancelled/expired ライフサイクルへ乗せる（新規台帳は作らない）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import action_state_tracker as ast  # noqa: E402
import tax_harvest_scanner as ths  # noqa: E402


def _lots_snapshot():
    return {
        "lots": {
            "9432.T": [
                {
                    "account": "特定",
                    "currency": "JPY",
                    "cost_per_share_jpy": 200.0,
                    "remaining_qty": 100,
                }
            ]
        }
    }


def _price_provider(ticker, currency):
    return 150.0, None  # 現在値150 < 取得単価200 → 含み損


def _recommend_func(ticker, quantity, **kwargs):
    return {"plan": [{"ticker": ticker, "quantity": quantity}]}


def test_candidate_gets_recommendation_id_and_registers_pending_action(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    report = ths.scan_tax_harvest(
        min_loss_jpy=1_000,
        lots_snapshot=_lots_snapshot(),
        price_provider=_price_provider,
        recommend_func=_recommend_func,
    )

    assert report["candidate_count"] == 1
    candidate = report["candidates"][0]
    rec_id = candidate["recommendation_id"]
    assert rec_id and isinstance(rec_id, str)

    state = ast._load()
    assert rec_id in state["actions"]
    entry = state["actions"][rec_id]
    assert entry["ticker"] == "9432.T"
    assert entry["action_type"] == ths.TAX_HARVEST_ACTION_TYPE
    assert entry["status"] == "pending"


def test_rescan_same_day_dedupes_to_same_recommendation_id(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    report1 = ths.scan_tax_harvest(
        min_loss_jpy=1_000,
        lots_snapshot=_lots_snapshot(),
        price_provider=_price_provider,
        recommend_func=_recommend_func,
    )
    report2 = ths.scan_tax_harvest(
        min_loss_jpy=1_000,
        lots_snapshot=_lots_snapshot(),
        price_provider=_price_provider,
        recommend_func=_recommend_func,
    )

    id1 = report1["candidates"][0]["recommendation_id"]
    id2 = report2["candidates"][0]["recommendation_id"]
    assert id1 == id2

    state = ast._load()
    assert len(state["actions"]) == 1, "同日再スキャンで重複エントリが増えないこと"


def test_skip_and_execute_lifecycle_via_existing_update_status(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    report = ths.scan_tax_harvest(
        min_loss_jpy=1_000,
        lots_snapshot=_lots_snapshot(),
        price_provider=_price_provider,
        recommend_func=_recommend_func,
    )
    rec_id = report["candidates"][0]["recommendation_id"]

    # skipped: 既存の cancelled + note で表現（別ステータスを新設しない）
    assert ast.update_status(rec_id, "cancelled", note="今回は見送り: 含み損が浅いため")
    state = ast._load()
    assert state["actions"][rec_id]["status"] == "cancelled"
    assert "見送り" in state["actions"][rec_id]["note"]

    # executed: filled + filled_at
    assert ast.update_status(rec_id, "filled", note="execution:manual_20260712")
    state = ast._load()
    assert state["actions"][rec_id]["status"] == "filled"
    assert state["actions"][rec_id]["filled_at"] is not None
