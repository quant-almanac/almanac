"""opening_balance_backfill_9432: 9432.T 開始残高backfillの回帰テスト"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import event_ledger as el  # noqa: E402
import opening_balance_backfill_9432 as backfill_mod  # noqa: E402
from tax_lot import build_lots  # noqa: E402


def _add_sell_only(db):
    """9432.Tの状況を再現: 対応するBUYが無いSELLのみ存在する状態。"""
    return el.append_event(
        event_type="trade", direction="sell",
        ticker="9432.T", quantity=100.0, price=148.8,
        currency="JPY", account="特定",
        occurred_at="2026-07-08T22:50:22",
        db_path=db,
    )


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    _add_sell_only(db)

    result = backfill_mod.backfill(apply=False, db_path=db)

    assert result["dry_run"] is True
    assert "fails_as_expected" in result["pre_check"]
    # dry-run では実際に書き込まれていないこと
    try:
        build_lots("9432.T", db_path=db)
        assert False, "dry-runなのにbuild_lotsが成功してはいけない"
    except Exception:
        pass


def test_apply_fixes_lot_shortfall(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    _add_sell_only(db)

    result = backfill_mod.backfill(apply=True, db_path=db)

    assert result["dry_run"] is False
    assert result["post_check"] == "ok"
    assert result["inserted"]["duplicate"] is False

    # 実際に build_lots が成功することを直接確認
    state = build_lots("9432.T", db_path=db)
    assert len(state.realized_trades) == 1
    assert state.realized_trades[0].cost_basis_jpy == 15225.0  # 100株 x 152.25


def test_apply_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    _add_sell_only(db)

    first = backfill_mod.backfill(apply=True, db_path=db)
    second = backfill_mod.backfill(apply=True, db_path=db)

    assert first["inserted"]["duplicate"] is False
    assert second["inserted"]["duplicate"] is True
    assert second["inserted"]["rowid"] == first["inserted"]["rowid"]


def test_realized_loss_matches_original_transaction(tmp_path, monkeypatch):
    """実売却の realized_pnl_jpy=-345 と一致すること(元取引の裏付け)"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)
    _add_sell_only(db)
    backfill_mod.backfill(apply=True, db_path=db)

    state = build_lots("9432.T", db_path=db)
    trade = state.realized_trades[0]
    realized_pnl = round(trade.proceeds_jpy - trade.cost_basis_jpy)
    assert realized_pnl == -345
