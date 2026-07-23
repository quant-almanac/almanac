from __future__ import annotations

from datetime import date
import json

from employee_plan_exit import build_exit_proposal
from nisa_migration_planner import build_migration_plan, build_plan_from_files


def test_nisa_migration_uses_low_gain_lot_and_never_executes() -> None:
    plan = build_migration_plan(
        nisa_data={
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        },
        holdings={
            "AAA": {
                "ticker": "AAA",
                "account": "特定",
                "currency": "JPY",
                "shares": 20,
                "current_price": 100_000,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        },
        lots_by_ticker={
            "AAA": [
                {
                    "lot_id": "high-gain",
                    "purchase_date": "2020-01-01",
                    "remaining_qty": 10,
                    "cost_per_share_jpy": 20_000,
                    "account": "特定",
                },
                {
                    "lot_id": "low-gain",
                    "purchase_date": "2024-01-01",
                    "remaining_qty": 10,
                    "cost_per_share_jpy": 95_000,
                    "account": "特定",
                },
            ]
        },
        start_year=2027,
        years=1,
    )

    assert plan["human_execution_only"] is True
    assert plan["display_only"] is True
    assert plan["moves"][0]["lot_id"] == "low-gain"
    assert all(move["human_execution_only"] for move in plan["moves"])


def test_nisa_migration_uses_portfolio_fx_for_usd_holdings() -> None:
    plan = build_migration_plan(
        nisa_data={
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        },
        holdings={
            "AAA": {
                "ticker": "AAA",
                "account": "特定",
                "currency": "USD",
                "shares": 10,
                "current_price": 200.0,
                "entry_price": 100.0,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        },
        lots_by_ticker={},
        start_year=2027,
        years=1,
        fx_rate_usdjpy=150.0,
    )

    assert plan["moves"][0]["market_value_jpy"] == 300_000
    assert plan["moves"][0]["estimated_realized_gain_jpy"] == 150_000
    assert plan["moves"][0]["estimated_tax_jpy"] == 30_472


def test_nisa_migration_missing_usd_fx_fails_closed() -> None:
    try:
        build_migration_plan(
            nisa_data={
                "husband": {"growth_limit_annual": 2_400_000},
                "wife": {"growth_limit_annual": 2_400_000},
            },
            holdings={
                "AAA": {
                    "ticker": "AAA",
                    "account": "特定",
                    "currency": "USD",
                    "shares": 10,
                    "current_price": 200.0,
                    "entry_price": 100.0,
                    "expected_return_pct": 0.15,
                    "dividend_yield": 0.0,
                    "investment_type": "long",
                }
            },
            lots_by_ticker={},
            start_year=2027,
            years=1,
        )
    except ValueError as exc:
        assert "USD holding AAA requires fx_rate_usdjpy" in str(exc)
    else:
        raise AssertionError("missing USD FX should fail closed")


def test_nisa_migration_file_loader_uses_account_fx_read_only(tmp_path, monkeypatch) -> None:
    (tmp_path / "nisa_portfolio.json").write_text(
        json.dumps({
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        }),
        encoding="utf-8",
    )
    (tmp_path / "holdings.json").write_text(
        json.dumps({
            "AAA": {
                "ticker": "AAA",
                "account": "特定",
                "currency": "USD",
                "shares": 10,
                "current_price": 200.0,
                "entry_price": 100.0,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        }),
        encoding="utf-8",
    )
    account = {"fx_rate_usdjpy": 150.0}
    account_path = tmp_path / "account.json"
    account_path.write_text(json.dumps(account), encoding="utf-8")

    import tax_lot

    monkeypatch.setattr(tax_lot, "portfolio_lot_snapshot", lambda: {"lots": {}})
    plan = build_plan_from_files(root=tmp_path, years=1)

    assert plan["moves"][0]["market_value_jpy"] == 300_000
    assert json.loads(account_path.read_text(encoding="utf-8")) == account


def test_nisa_migration_file_loader_uses_local_parquet_current_price(tmp_path, monkeypatch) -> None:
    (tmp_path / "nisa_portfolio.json").write_text(
        json.dumps({
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        }),
        encoding="utf-8",
    )
    (tmp_path / "holdings.json").write_text(
        json.dumps({
            "AAA": {
                "ticker": "AAA",
                "account": "特定",
                "currency": "JPY",
                "shares": 10,
                "entry_price": 100.0,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "account.json").write_text(json.dumps({"fx_rate_usdjpy": 150.0}), encoding="utf-8")
    ohlcv = tmp_path / "data" / "ohlcv"
    ohlcv.mkdir(parents=True)

    import pandas as pd

    pd.DataFrame(
        {"Close": [180.0, 200.0]},
        index=pd.to_datetime(["2026-06-01", "2026-06-02"]),
    ).to_parquet(ohlcv / "AAA.parquet")

    import tax_lot

    monkeypatch.setattr(tax_lot, "portfolio_lot_snapshot", lambda: {"lots": {}})
    plan = build_plan_from_files(root=tmp_path, years=1)

    assert plan["moves"][0]["market_value_jpy"] == 2_000
    assert plan["moves"][0]["estimated_realized_gain_jpy"] == 1_000
    assert plan["moves"][0]["estimated_tax_jpy"] == 203


def test_nisa_migration_file_loader_surfaces_tax_lot_failure(tmp_path, monkeypatch) -> None:
    (tmp_path / "nisa_portfolio.json").write_text(
        json.dumps({
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        }),
        encoding="utf-8",
    )
    (tmp_path / "holdings.json").write_text(
        json.dumps({
            "AAA": {
                "ticker": "AAA",
                "account": "特定",
                "currency": "JPY",
                "shares": 10,
                "current_price": 200.0,
                "entry_price": 100.0,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "account.json").write_text(json.dumps({"fx_rate_usdjpy": 150.0}), encoding="utf-8")

    import tax_lot

    def fail_snapshot():
        raise RuntimeError("ledger mismatch")

    monkeypatch.setattr(tax_lot, "portfolio_lot_snapshot", fail_snapshot)
    plan = build_plan_from_files(root=tmp_path, years=1)

    assert plan["actionable"] is False
    assert plan["tax_lot_source"] == "holding_fallback_due_to_error"
    assert plan["data_quality_issues"] == ["tax_lot_snapshot_error: ledger mismatch"]


def test_nisa_migration_jpy_fund_units_use_nav_per_10000() -> None:
    plan = build_migration_plan(
        nisa_data={
            "husband": {"growth_limit_annual": 2_400_000},
            "wife": {"growth_limit_annual": 2_400_000},
        },
        holdings={
            "FUND": {
                "ticker": "FUND",
                "account": "特定",
                "currency": "JPY",
                "unit": "口",
                "shares": 10_000,
                "current_nav": 12_000.0,
                "entry_price": 10_000.0,
                "expected_return_pct": 0.15,
                "dividend_yield": 0.0,
                "investment_type": "long",
            }
        },
        lots_by_ticker={},
        start_year=2027,
        years=1,
    )

    assert plan["moves"][0]["quantity"] == 10_000
    assert plan["moves"][0]["market_value_jpy"] == 12_000
    assert plan["moves"][0]["estimated_realized_gain_jpy"] == 2_000
    assert plan["moves"][0]["estimated_tax_jpy"] == 406


def test_employee_plan_is_fail_safe_without_configured_window() -> None:
    result = build_exit_proposal(
        portfolio_total_jpy=10_000_000,
        current_price_jpy=2_000,
        current_shares=1_000,
        purchase_history=[],
        as_of=date(2026, 6, 12),
        window_config={"allowed_windows": []},
    )

    assert result["status"] == "blocked"
    assert result["proposal"] is None
    assert result["human_execution_only"] is True


def test_employee_plan_proposes_oldest_lots_only_inside_window() -> None:
    result = build_exit_proposal(
        portfolio_total_jpy=10_000_000,
        current_price_jpy=2_000,
        current_shares=1_000,
        purchase_history=[
            {"date": "2025-01-01", "shares": 500, "price": 1_800},
            {"date": "2024-01-01", "shares": 500, "price": 1_600},
        ],
        as_of=date(2026, 6, 12),
        limit_pct=0.08,
        window_config={
            "allowed_windows": [
                {"start": "2026-06-01", "end": "2026-06-30", "note": "confirmed"}
            ]
        },
    )

    assert result["status"] == "proposal"
    assert result["proposal"]["sell_shares"] == 600
    assert result["proposal"]["lots"][0]["purchase_date"] == "2024-01-01"
    assert result["proposal"]["lots"][0]["quantity"] == 500
    assert result["proposal"]["lots"][1]["quantity"] == 100
    assert result["contribution_continues"] is True
