"""
ledger_trade_corrections.py - append-only corrections for known bad trade events.

This is intentionally not a broad heuristic repair tool. Each correction is
keyed by event_id and only applies when the stored row still matches the
expected ticker. The original row remains in ledger_events, while the appended
correction carries raw_payload.supersedes so normal readers see the corrected
event through event_ledger.query_events().
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from almanac.runtime_config import resolve_db_path

BASE_DIR = Path(__file__).parent
DB_PATH = resolve_db_path(BASE_DIR)


@dataclass(frozen=True)
class TradeCorrection:
    event_id: str
    ticker: str
    price_scale: float
    reason: str
    new_price: Optional[float] = None
    new_quantity: Optional[float] = None
    new_account: Optional[str] = None
    currency: Optional[str] = None
    fx_rate_usdjpy: Optional[float] = None


@dataclass(frozen=True)
class MissingTradeEvent:
    event_id: str
    occurred_at: str
    ticker: str
    direction: str
    quantity: float
    price: float
    currency: str
    account: str
    reason: str
    fx_rate_usdjpy: Optional[float] = None
    evidence: tuple[str, ...] = ()
    required_event_ids: tuple[str, ...] = ()


KNOWN_CORRECTIONS: tuple[TradeCorrection, ...] = (
    TradeCorrection(
        event_id="backfill_744c26637500caeb",
        ticker="SLIM_SP500",
        price_scale=0.0001,
        currency="JPY",
        fx_rate_usdjpy=None,
        reason="domestic fund NAV was stored as USD share price; normalize NAV per 10000 units to JPY per unit",
    ),
    TradeCorrection(
        event_id="backfill_5b1bda5015d95c3b",
        ticker="EPOL",
        price_scale=0.01,
        reason="broker CSV price decimal shifted by 100x",
    ),
    TradeCorrection(
        event_id="backfill_2cb58f863647f50c",
        ticker="EWG",
        price_scale=0.01,
        reason="broker CSV price decimal shifted by 100x",
    ),
    TradeCorrection(
        event_id="backfill_fb5389def81ad442",
        ticker="AVGO",
        price_scale=(1.0 / 15.0),
        reason=(
            "AVGO 2026-04-02 broker gross proceeds 4693.28 USD were stored as per-share price; "
            "quantity was 15 shares, so per-share price is 4693.28 / 15"
        ),
    ),
    TradeCorrection(
        event_id="backfill_aeae9469f5ddf2a5",
        ticker="IEV",
        price_scale=1.0,
        new_price=65.1759,
        reason=(
            "IEV 2026-03-27 sell price was stored as 35.1759, matching the adjacent EPOL sell price; "
            "local IEV OHLCV was in the mid-65 USD range, so restore the likely leading digit"
        ),
    ),
    TradeCorrection(
        event_id="backfill_8839775adf29460d",
        ticker="META",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "META 2026-04-10 sell 20 @ 71.07 conflicts with META market price, later action state, "
            "and broker-synced holdings; void this corrupted trade row via append-only supersession"
        ),
    ),
    *(
        TradeCorrection(
            event_id=event_id,
            ticker="NVDA",
            price_scale=1.0,
            new_account="一般",
            reason=(
                "NVDA backfill row defaulted to 特定, but action_executions, broker sync, "
                "and current holdings identify the NVDA position as 一般 account"
            ),
        )
        for event_id in (
            "backfill_7fc4217a144147cb",
            "backfill_fdd60c8676e4b771",
            "backfill_3d57e6bef64258c0",
            "backfill_d4937d19ae6f7833",
            "backfill_c808586f792c0754",
            "backfill_d9f0fd541507c197",
            "backfill_733f088b3cc51fea",
        )
    ),
)


KNOWN_MISSING_TRADE_EVENTS: tuple[MissingTradeEvent, ...] = (
    MissingTradeEvent(
        event_id="manual_opening_6762.T_20260301_100sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="6762.T",
        direction="buy",
        quantity=100.0,
        price=2203.75,
        currency="JPY",
        account="特定",
        reason=(
            "opening lot inferred from action_executions 6762.T_buy_20260422230805 "
            "portfolio_message '100.0 -> 200.0 shares, average cost 2447.175'; "
            "new buy was 100 shares at 2690.6"
        ),
        evidence=(
            "action_executions.json id=6762.T_buy_20260422230805",
            "portfolio_message=6762.T: 100.0 -> 200.0 shares, average cost 2447.175",
            "inferred_price=(2447.175 * 200 - 2690.6 * 100) / 100",
        ),
        required_event_ids=(
            "backfill_a8b1b5e343afba13",
            "exec_6762.T_sell_20260526200517",
            "exec_6762.T_sell_20260529004619",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_7751.T_buy_20260428_100sh",
        occurred_at="2026-04-28T00:44:18",
        ticker="7751.T",
        direction="buy",
        quantity=100.0,
        price=4045.75,
        currency="JPY",
        account="特定",
        reason=(
            "missing buy inferred from action_executions 7751.T_buy_20260428004418 "
            "and broker_position_reconcile_log; sell realized_pnl_jpy=33425 implies "
            "cost basis 4045.75 per share"
        ),
        evidence=(
            "action_executions.json id=7751.T_buy_20260428004418",
            "broker_position_reconcile_log shows 100 shares held before sell",
            "sell proceeds 438000 - realized_pnl_jpy 33425 = cost basis 404575",
        ),
        required_event_ids=(
            "exec_7751.T_sell_20260609001534",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_ADBE_buy_20260422_1sh",
        occurred_at="2026-04-22T00:00:00",
        ticker="ADBE",
        direction="buy",
        quantity=1.0,
        price=246.19,
        currency="USD",
        fx_rate_usdjpy=159.3730010986328,
        account="特定",
        reason=(
            "missing one-share opening lot inferred from broker_position_reconcile_log "
            "showing ADBE 4 shares at average cost 246.855 USD; existing ledger buys "
            "cover 2 shares at 249.76 and 1 share at 241.71"
        ),
        evidence=(
            "broker_position_reconcile_log 2026-05-17 and 2026-05-28: ADBE 4 shares, entry_price 246.855 USD",
            "trade_history.csv has buys for 2 shares at 249.76 and 1 share at 241.71, then sell 4 shares",
            "inferred_price=(246.855 * 4 - 249.76 * 2 - 241.71 * 1) / 1",
            "fx_rate_usdjpy matches historical FX used by 2026-04-22 ADBE backfill event",
        ),
        required_event_ids=(
            "backfill_79424418cba1fed4",
            "backfill_5fbbdd40aca00d4a",
            "exec_ADBE_sell_20260619010100",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_AMAT_20260517_5sh",
        occurred_at="2026-05-17T00:00:00",
        ticker="AMAT",
        direction="buy",
        quantity=5.0,
        price=393.918,
        currency="USD",
        fx_rate_usdjpy=156.6295523433811,
        account="特定",
        reason=(
            "opening lot inferred from broker_position_reconcile_log showing AMAT "
            "5 shares at average cost 393.918 USD; later executions sell 4 shares "
            "and leave 1 share"
        ),
        evidence=(
            "broker_position_reconcile_log 2026-05-17 and 2026-05-28: AMAT 5 shares, entry_price 393.918 USD",
            "broker cost basis inferred as value_jpy - unrealized_jpy = 346610 - 38114 = 308496",
            "fx_rate_usdjpy=308496 / (393.918 * 5)",
            "action_executions sell messages: AMAT 5.0 -> 3.0 -> 2.0 -> 1.0 shares",
        ),
        required_event_ids=(
            "exec_AMAT_sell_20260619010014",
            "exec_AMAT_sell_20260624005345",
            "exec_AMAT_sell_20260626011011",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_AVGO_toku_20260301_50sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="AVGO",
        direction="buy",
        quantity=50.0,
        price=140.04,
        currency="USD",
        fx_rate_usdjpy=156.662382176521,
        account="特定",
        reason=(
            "opening lot inferred from action_executions showing AVGO特定 50 -> 35 shares "
            "on 2026-04-02 and broker_position_reconcile_log showing 15 shares remaining "
            "at average cost 140.04 USD"
        ),
        evidence=(
            "action_executions.json id=AVGO_sell_20260402012942 shows AVGO: 50.0 -> 35.0 shares",
            "broker_position_reconcile_log 2026-05-17 and 2026-05-28: AVGO特定 15 shares, entry_price 140.04 USD",
            "broker cost basis inferred as value_jpy - unrealized_jpy = 1012611 - 683526 = 329085",
            "fx_rate_usdjpy=329085 / (140.04 * 15)",
        ),
        required_event_ids=(
            "backfill_fb5389def81ad442",
            "exec_AVGO_sell_20260624004845",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_AVGO_sell_20260507_3sh",
        occurred_at="2026-05-07T12:58:40",
        ticker="AVGO",
        direction="sell",
        quantity=3.0,
        price=421.5,
        currency="USD",
        fx_rate_usdjpy=156.50799560546875,
        account="特定",
        reason=(
            "externally reconciled AVGO sell was reflected in holdings but missing from event_ledger; "
            "needed to reconcile AVGO特定 from 18 shares after 2026-04-25 to 15 shares in broker sync"
        ),
        evidence=(
            "action_executions.json id=AVGO_sell_20260505125840, trade_date=2026-05-07, quantity=3, price=421.5",
            "reports/unapplied_execution_review_2026-05-21 marks AVGO_sell_20260505125840 externally reconciled",
            "broker_position_reconcile_log 2026-05-17 shows AVGO特定 15 shares after the sell",
            "fx_rate_usdjpy matches historical FX used by other 2026-05-07 ledger events",
        ),
        required_event_ids=(
            "backfill_5591a2a342e308ae",
            "exec_AVGO_sell_20260604005033",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_AVGO_ippan_20260301_27sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="AVGO",
        direction="buy",
        quantity=27.0,
        price=203.8148,
        currency="USD",
        fx_rate_usdjpy=155.9396806061916,
        account="一般",
        reason=(
            "opening lot inferred from broker_position_reconcile_log and holdings backups "
            "showing AVGO一般 27 shares at average cost 203.8148 USD"
        ),
        evidence=(
            "broker_position_reconcile_log 2026-05-17 and 2026-05-28: AVGO一般 27 shares, entry_price 203.8148 USD",
            "holdings backups from 2026-06-01 through 2026-06-28 keep AVGO_ippan at 27 shares",
            "broker cost basis inferred as value_jpy - unrealized_jpy = 1822700 - 964564 = 858136",
            "fx_rate_usdjpy=858136 / (203.8148 * 27)",
        ),
        required_event_ids=(
            "backfill_fb5389def81ad442",
            "exec_AVGO_sell_20260624004845",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_CRM_buy_20260423_1sh",
        occurred_at="2026-04-23T23:09:57",
        ticker="CRM",
        direction="buy",
        quantity=1.0,
        price=190.4,
        currency="USD",
        fx_rate_usdjpy=159.48800659179688,
        account="特定",
        reason=(
            "externally reconciled CRM buy was reflected in holdings but missing from event_ledger; "
            "needed to match the 2026-05-13 full sell"
        ),
        evidence=(
            "action_executions.json id=CRM_buy_20260422230957, trade_date=2026-04-23, quantity=1, price=190.4",
            "reports/unapplied_execution_review_2026-05-21 marks CRM_buy_20260422230957 externally reconciled",
            "action_executions.json id=CRM_sell_20260513000812 later sells the full CRM position",
            "fx_rate_usdjpy matches historical FX used by other 2026-04-23 ledger events",
        ),
        required_event_ids=(
            "backfill_6e7e0c8f45f15533",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_CRWV_20260307_10sh",
        occurred_at="2026-03-07T00:00:00",
        ticker="CRWV",
        direction="buy",
        quantity=10.0,
        price=117.86223166502458,
        currency="USD",
        fx_rate_usdjpy=157.53399658203125,
        account="特定",
        reason=(
            "opening lot inferred from CRWV full sell realized loss; no matching buy event exists "
            "in event_ledger or action_executions"
        ),
        evidence=(
            "action_executions.json id=CRWV_sell_20260307002707 shows CRWV full sell of 10 shares",
            "trade_history.csv row shows sell 10 at 75.99 with realized_jpy=-65963",
            "cost_basis_jpy=(10 * 75.99 * 157.53399658203125) - (-65963)",
            "price_usd=cost_basis_jpy / (10 * 157.53399658203125)",
        ),
        required_event_ids=(
            "backfill_9f3ac76a4adf3217",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_EPOL_20260301_410sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="EPOL",
        direction="buy",
        quantity=410.0,
        price=33.1393486323846,
        currency="USD",
        fx_rate_usdjpy=159.70399475097656,
        account="特定",
        reason=(
            "opening lot inferred from EPOL sell sequence showing 410 -> 270 -> 170 -> 100 -> 20 -> 0 shares; "
            "cost basis uses weighted realized PnL from non-corrupted sell rows"
        ),
        evidence=(
            "action_executions.json EPOL sell messages show start quantity 410 and final full sell",
            "trade_history.csv rows except 2026-04-02 imply weighted cost_per_share_jpy=5292.486360037133",
            "2026-04-02 realized PnL is excluded because the same row has a known 100x price-scale correction",
            "price_usd=5292.486360037133 / 159.70399475097656",
        ),
        required_event_ids=(
            "backfill_c742285dc077329e",
            "backfill_5b1bda5015d95c3b",
            "backfill_8ed1241f7a6f02b5",
            "backfill_696e2b997c31138e",
            "backfill_4685f85dddb89d5f",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_EWG_20260301_490sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="EWG",
        direction="buy",
        quantity=490.0,
        price=42.222379521177295,
        currency="USD",
        fx_rate_usdjpy=159.70399475097656,
        account="特定",
        reason=(
            "opening lot inferred from EWG sell sequence showing 490 -> 360 -> 240 -> 0 shares; "
            "cost basis uses weighted realized PnL from non-corrupted sell rows"
        ),
        evidence=(
            "action_executions.json EWG sell messages show start quantity 490 and final full sell",
            "trade_history.csv rows except 2026-04-02 imply weighted cost_per_share_jpy=6743.0826774238385",
            "2026-04-02 realized PnL is excluded because the same row has a known 100x price-scale correction",
            "price_usd=6743.0826774238385 / 159.70399475097656",
        ),
        required_event_ids=(
            "backfill_d3f5269c6a0f4d3f",
            "backfill_2cb58f863647f50c",
            "backfill_4a4dbb8e2a2a6408",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_GLD_toku_20260301_67sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="GLD",
        direction="buy",
        quantity=67.0,
        price=309.5461,
        currency="USD",
        fx_rate_usdjpy=160.57466422054407,
        account="特定",
        reason=(
            "opening lot inferred from GLD特定 sell sequence showing 67 -> 60 -> ... -> 15 shares; "
            "cost basis uses current broker average cost and weighted realized PnL"
        ),
        evidence=(
            "action_executions.json GLD特定 sell messages show start quantity 67 and current holdings show 15 shares",
            "holdings backups from 2026-06-01 through 2026-06-28 keep GLD特定 entry_price 309.5461 USD",
            "trade_history and API sell rows imply weighted cost_per_share_jpy=49705.26106827896",
            "fx_rate_usdjpy=49705.26106827896 / 309.5461",
        ),
        required_event_ids=(
            "backfill_3ddacf0558ba1335",
            "backfill_99d320ab3b6ed359",
            "exec_GLD_sell_20260626010930",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_GLD_sell_20260507_5sh",
        occurred_at="2026-05-07T12:58:48",
        ticker="GLD",
        direction="sell",
        quantity=5.0,
        price=420.13,
        currency="USD",
        fx_rate_usdjpy=156.50799560546875,
        account="特定",
        reason=(
            "externally reconciled GLD特定 sell was reflected in holdings but missing from event_ledger; "
            "needed to reconcile GLD特定 from 31 shares after 2026-04-28 to 26 shares in broker sync"
        ),
        evidence=(
            "action_executions.json id=GLD_sell_20260505125848, trade_date=2026-05-07, quantity=5, price=420.13",
            "reports/unapplied_execution_review_2026-05-21 marks GLD_sell_20260505125848 externally reconciled",
            "holdings backup 2026-06-01 shows GLD特定 26 shares after the sell",
            "fx_rate_usdjpy matches historical FX used by other 2026-05-07 ledger events",
        ),
        required_event_ids=(
            "backfill_99d320ab3b6ed359",
            "exec_GLD_sell_20260604004942",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_GLD_nisa_20260528_5sh",
        occurred_at="2026-05-28T00:00:00",
        ticker="GLD",
        direction="buy",
        quantity=5.0,
        price=430.62,
        currency="USD",
        fx_rate_usdjpy=159.4909962540177,
        account="NISA成長投資枠",
        reason=(
            "opening NISA lot inferred from GLD_NISA broker sync and 2026-05-28 NISA sell of 3 shares"
        ),
        evidence=(
            "action_executions.json id=GLD_sell_20260528013459 shows GLD_NISA 5.0 -> 2.0 shares",
            "holdings.json keeps GLD_NISA 2 shares at entry_price 430.62 USD",
            "NISA sell note realized_pnl_jpy=-10693.87 implies cost basis 206040.0384207153 for 3 shares",
            "fx_rate_usdjpy=206040.0384207153 / (430.62 * 3)",
        ),
        required_event_ids=(
            "exec_GLD_sell_20260528013459",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_IEV_20260301_340sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="IEV",
        direction="buy",
        quantity=340.0,
        price=63.88260990976915,
        currency="USD",
        fx_rate_usdjpy=159.70399475097656,
        account="特定",
        reason=(
            "opening lot inferred from IEV sell/buy sequence; 340 shares less 280 sold plus 20 later buys "
            "reconciles to current 80 shares"
        ),
        evidence=(
            "action_executions and holdings backups show IEV current quantity 80 shares",
            "event_ledger has IEV sells totaling 280 shares and later buys totaling 20 shares",
            "trade_history.csv reliable sell rows after 2026-04-03 imply weighted cost_per_share_jpy=10202.307997708456",
            "price_usd=10202.307997708456 / 159.70399475097656",
            "3/27 sell price is separately corrected because original 35.1759 matches EPOL, not IEV market price",
        ),
        required_event_ids=(
            "backfill_aeae9469f5ddf2a5",
            "backfill_d497f548e440f0a6",
            "backfill_c6b8d392f46474b4",
            "backfill_db4838c1139507a4",
            "backfill_1f363868ca8d4311",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_LRCX_20260509_1sh",
        occurred_at="2026-05-09T00:00:00",
        ticker="LRCX",
        direction="buy",
        quantity=1.0,
        price=256.82741605350094,
        currency="USD",
        fx_rate_usdjpy=156.82899475097656,
        account="特定",
        reason=(
            "opening lot inferred from LRCX full sell realized gain; no matching buy event exists "
            "in event_ledger or action_executions"
        ),
        evidence=(
            "action_executions.json id=LRCX_sell_20260509000150 shows LRCX full sell of 1 share",
            "trade_history.csv row shows sell 1 at 295.51 with realized_jpy=6067",
            "cost_basis_jpy=(1 * 295.51 * 156.82899475097656) - 6066.55075469971",
            "price_usd=cost_basis_jpy / 156.82899475097656",
        ),
        required_event_ids=(
            "backfill_c4f19bf3c77c54a0",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_META_toku_20260301_4sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="META",
        direction="buy",
        quantity=4.0,
        price=513.6522033226104,
        currency="USD",
        fx_rate_usdjpy=159.20599365234375,
        account="特定",
        reason=(
            "opening lot inferred from META 2026-03-13 sell message showing 4 -> 3 shares; "
            "cost basis inferred from realized gain"
        ),
        evidence=(
            "action_executions.json id=META_sell_20260313004839 shows META: 4.0 -> 3.0 shares",
            "trade_history.csv row shows sell 1 at 640.915 with realized_jpy=20261",
            "cost_basis_jpy=(1 * 640.915 * 159.20599365234375) - 20261",
            "price_usd=cost_basis_jpy / 159.20599365234375",
        ),
        required_event_ids=(
            "backfill_8351d28fa8b92d72",
            "backfill_8839775adf29460d",
            "exec_META_buy_20260528013400",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_META_buy_20260507_2sh",
        occurred_at="2026-05-07T12:58:00",
        ticker="META",
        direction="buy",
        quantity=2.0,
        price=613.25,
        currency="USD",
        fx_rate_usdjpy=156.50799560546875,
        account="特定",
        reason=(
            "externally reconciled META buy was reflected in holdings but missing from event_ledger"
        ),
        evidence=(
            "action_executions.json id=META_buy_20260505125800, trade_date=2026-05-07, quantity=2, price=613.25",
            "reports/unapplied_execution_review_2026-05-21 marks META_buy_20260505125800 externally reconciled",
            "report notes current META was 特定 7 shares + 一般 2 shares before later 5/28 buy",
            "fx_rate_usdjpy matches historical FX used by other 2026-05-07 ledger events",
        ),
        required_event_ids=(
            "backfill_0cf81a3676d17dee",
            "exec_META_buy_20260528013400",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_META_ippan_20260301_2sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="META",
        direction="buy",
        quantity=2.0,
        price=505.84,
        currency="USD",
        fx_rate_usdjpy=161.69000244140625,
        account="一般",
        reason=(
            "opening lot inferred from META一般 holdings backups showing 2 shares before the 2026-06-26 sell"
        ),
        evidence=(
            "holdings backups 2026-06-01 through 2026-06-26 show META_ippan 2 shares at entry_price 505.84 USD",
            "action_executions.json id=META_sell_20260626010952 sells 1 share from META一般 2.0 -> 1.0",
            "sell note realized_pnl_jpy=7317 and sell FX 161.69000244140625 are consistent with entry_price 505.84",
        ),
        required_event_ids=(
            "exec_META_sell_20260626010952",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_NEM_buy_20260423_2sh",
        occurred_at="2026-04-23T23:09:42",
        ticker="NEM",
        direction="buy",
        quantity=2.0,
        price=111.68,
        currency="USD",
        fx_rate_usdjpy=159.48800659179688,
        account="特定",
        reason="externally reconciled NEM buy was reflected in holdings but missing from event_ledger",
        evidence=(
            "action_executions.json id=NEM_buy_20260422230942, trade_date=2026-04-23, quantity=2, price=111.68",
            "reports/unapplied_execution_review_2026-05-21 marks NEM_buy_20260422230942 externally reconciled",
            "fx_rate_usdjpy matches historical FX used by other 2026-04-23 ledger events",
        ),
        required_event_ids=(
            "exec_NEM_sell_20260609001703",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_NEM_buy_20260507_30sh",
        occurred_at="2026-05-07T12:57:48",
        ticker="NEM",
        direction="buy",
        quantity=30.0,
        price=110.59,
        currency="USD",
        fx_rate_usdjpy=156.50799560546875,
        account="特定",
        reason="externally reconciled NEM buy was reflected in holdings but missing from event_ledger",
        evidence=(
            "action_executions.json id=NEM_buy_20260505125748, trade_date=2026-05-07, quantity=30, price=110.59",
            "reports/unapplied_execution_review_2026-05-21 marks NEM_buy_20260505125748 externally reconciled",
            "holdings backups show NEM 32 shares before the 2026-06-19 full sell",
            "fx_rate_usdjpy matches historical FX used by other 2026-05-07 ledger events",
        ),
        required_event_ids=(
            "exec_NEM_sell_20260609001703",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_NVDA_ippan_20260201_127sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="NVDA",
        direction="buy",
        quantity=127.0,
        price=110.16986614173227,
        currency="USD",
        fx_rate_usdjpy=155.85899353027344,
        account="一般",
        reason=(
            "opening lot inferred from NVDA action state showing 140 shares before the 2026-03-07 buy; "
            "existing ledger buys already cover 13 of those shares, so the missing opening lot is 127 shares"
        ),
        evidence=(
            "action_executions.json id=NVDA_buy_20260307002454 shows NVDA: 140.0 -> 145.0 shares",
            "ledger already has 2026-02-19 buy 3 shares and 2026-02-28 buy 10 shares",
            "missing_quantity=140 - 3 - 10",
            "average after 2026-03-07 buy implies opening_price=(118.8274 * 145 - 188 * 3 - 177.19 * 10 - 180.5 * 5) / 127",
            "holdings backups show current NVDA 75 shares at entry_price 116.6248 in 一般 account",
            "fx_rate_usdjpy uses nearest prior historical FX present in ledger (2026-02-27)",
        ),
        required_event_ids=(
            "backfill_3d57e6bef64258c0",
            "backfill_d4937d19ae6f7833",
            "backfill_d9f0fd541507c197",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_NVDA_sell_20260507_25sh",
        occurred_at="2026-05-07T12:58:57",
        ticker="NVDA",
        direction="sell",
        quantity=25.0,
        price=199.02,
        currency="USD",
        fx_rate_usdjpy=156.50799560546875,
        account="一般",
        reason=(
            "externally reconciled NVDA sell was reflected in holdings but missing from event_ledger; "
            "needed to reconcile 100 shares after 2026-04-28 to current 75 shares"
        ),
        evidence=(
            "action_executions.json id=NVDA_sell_20260505125857, trade_date=2026-05-07, quantity=25, price=199.02",
            "reports/unapplied_execution_review_2026-05-21 marks NVDA_sell_20260505125857 externally reconciled",
            "holdings backups show current NVDA 75 shares in 一般 account",
            "fx_rate_usdjpy matches historical FX used by other 2026-05-07 ledger events",
        ),
        required_event_ids=(
            "backfill_733f088b3cc51fea",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_QCOM_20260423_2sh",
        occurred_at="2026-04-23T00:00:00",
        ticker="QCOM",
        direction="buy",
        quantity=2.0,
        price=145.33360486250112,
        currency="USD",
        fx_rate_usdjpy=157.8509979248047,
        account="特定",
        reason=(
            "opening lot inferred from QCOM action state showing 4 shares before the 2026-05-14 sell; "
            "existing ledger buys cover 2 shares"
        ),
        evidence=(
            "action_executions.json id=QCOM_sell_20260514005607 shows QCOM: 4.0 -> 2.0 shares",
            "ledger has only two QCOM buys before that sell",
            "trade_history.csv row shows sell 2 at 214.02 with realized_jpy=21684.432032623296",
            "price_usd=((2 * 214.02 * 157.8509979248047) - 21684.432032623296) / (2 * 157.8509979248047)",
        ),
        required_event_ids=(
            "backfill_2564aa62839e18ed",
            "backfill_9b522c94825d5aa8",
            "backfill_f2b3edd2cdb40bea",
            "exec_QCOM_sell_20260619010129",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_RCL_20260313_12sh",
        occurred_at="2026-03-13T00:00:00",
        ticker="RCL",
        direction="buy",
        quantity=12.0,
        price=270.14407592853075,
        currency="USD",
        fx_rate_usdjpy=159.20599365234375,
        account="特定",
        reason=(
            "opening lot inferred from RCL full sell realized loss; no matching buy event exists "
            "in event_ledger or action_executions"
        ),
        evidence=(
            "action_executions.json id=RCL_sell_20260313004657 shows RCL full sell of 12 shares",
            "trade_history.csv row shows sell 12 at 268.735 with realized_jpy=-2692",
            "cost_basis_jpy=(12 * 268.735 * 159.20599365234375) - (-2692)",
            "price_usd=cost_basis_jpy / (12 * 159.20599365234375)",
        ),
        required_event_ids=(
            "backfill_646b7ed4cde81dbe",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_opening_SBUX_20260513_1sh",
        occurred_at="2026-05-13T00:00:00",
        ticker="SBUX",
        direction="buy",
        quantity=1.0,
        price=107.15991837127328,
        currency="USD",
        fx_rate_usdjpy=157.67100524902344,
        account="特定",
        reason=(
            "opening lot inferred from SBUX full sell realized loss; no matching buy event exists "
            "in event_ledger or action_executions"
        ),
        evidence=(
            "action_executions.json id=SBUX_sell_20260513000839 shows SBUX full sell of 1 share",
            "trade_history.csv row shows sell 1 at 106.17 with realized_jpy=-156.08142471313397",
            "cost_basis_jpy=(1 * 106.17 * 157.67100524902344) - (-156.08142471313397)",
            "price_usd=cost_basis_jpy / 157.67100524902344",
        ),
        required_event_ids=(
            "backfill_477cba20e683ef53",
        ),
    ),
)


def _parse_payload(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"raw_payload_parse_error": True, "raw_payload_original": str(raw)[:500]}
    return parsed if isinstance(parsed, dict) else {"raw_payload_original": parsed}


def _raw_rows(db_path: Path) -> list[dict]:
    from event_ledger import init_schema

    init_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ledger_events ORDER BY id").fetchall()]
    finally:
        conn.close()


def _row_by_event_id(rows: list[dict]) -> dict[str, dict]:
    return {str(r.get("event_id")): r for r in rows}


def _build_update(row: dict, spec: TradeCorrection) -> dict:
    old_price = float(row.get("price") or 0.0)
    new_price = round(spec.new_price if spec.new_price is not None else old_price * spec.price_scale, 8)
    new_quantity = spec.new_quantity if spec.new_quantity is not None else row.get("quantity")
    new_account = spec.new_account if spec.new_account is not None else row.get("account")
    new_currency = spec.currency or row.get("currency")
    new_fx = spec.fx_rate_usdjpy if spec.currency is not None else row.get("fx_rate_usdjpy")
    payload = _parse_payload(row.get("raw_payload"))
    history = payload.get("trade_correction_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "corrected_at": datetime.now().isoformat(timespec="seconds"),
        "reason": spec.reason,
        "previous_price": row.get("price"),
        "previous_quantity": row.get("quantity"),
        "previous_account": row.get("account"),
        "previous_currency": row.get("currency"),
        "previous_fx_rate_usdjpy": row.get("fx_rate_usdjpy"),
        "previous_amount_jpy": row.get("amount_jpy"),
        "price_scale": spec.price_scale,
        "explicit_new_price": spec.new_price,
        "explicit_new_quantity": spec.new_quantity,
        "explicit_new_account": spec.new_account,
    })
    payload.update({
        "supersedes": row["event_id"],
        "trade_correction_history": history,
        "original_amount_jpy": row.get("amount_jpy"),
        "original_price": row.get("price"),
        "original_quantity": row.get("quantity"),
        "original_account": row.get("account"),
        "original_currency": row.get("currency"),
        "original_fx_rate_usdjpy": row.get("fx_rate_usdjpy"),
        "correction_reason": spec.reason,
        "price_scale": spec.price_scale,
        "explicit_new_price": spec.new_price,
        "explicit_new_quantity": spec.new_quantity,
        "explicit_new_account": spec.new_account,
    })
    return {
        "event_id": row["event_id"],
        "correction_event_id": f"{row['event_id']}:tradecorr:v1",
        "occurred_at": row.get("occurred_at"),
        "event_type": row.get("event_type"),
        "ticker": row.get("ticker"),
        "direction": row.get("direction"),
        "quantity": new_quantity,
        "old_quantity": row.get("quantity"),
        "old_price": row.get("price"),
        "new_price": new_price,
        "old_currency": row.get("currency"),
        "new_currency": new_currency,
        "old_fx_rate_usdjpy": row.get("fx_rate_usdjpy"),
        "new_fx_rate_usdjpy": new_fx,
        "old_amount_jpy": row.get("amount_jpy"),
        "old_account": row.get("account"),
        "account": new_account,
        "note": f"{row.get('note') or 'trade event'} (trade correction: {spec.reason})",
        "raw_payload": payload,
        "reason": spec.reason,
    }


def _build_missing_event(spec: MissingTradeEvent) -> dict:
    payload = {
        "manual_correction": True,
        "correction_reason": spec.reason,
        "evidence": list(spec.evidence),
    }
    return {
        "event_id": spec.event_id,
        "occurred_at": spec.occurred_at,
        "event_type": "trade",
        "ticker": spec.ticker,
        "direction": spec.direction,
        "quantity": spec.quantity,
        "price": spec.price,
        "currency": spec.currency,
        "fx_rate_usdjpy": spec.fx_rate_usdjpy,
        "account": spec.account,
        "source": "trade_correction",
        "note": f"manual trade correction: {spec.reason}",
        "raw_payload": payload,
        "reason": spec.reason,
    }


def correct_known_trade_events(*, apply: bool = False, db_path: Path = DB_PATH) -> dict:
    rows = _raw_rows(db_path)
    by_id = _row_by_event_id(rows)

    from event_ledger import _superseded_ids

    superseded = _superseded_ids(rows)
    updates: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for spec in KNOWN_CORRECTIONS:
        row = by_id.get(spec.event_id)
        if row is None:
            skipped.append({"event_id": spec.event_id, "reason": "missing"})
            continue
        if spec.event_id in superseded:
            skipped.append({"event_id": spec.event_id, "reason": "already_superseded"})
            continue
        if row.get("ticker") != spec.ticker:
            errors.append({
                "event_id": spec.event_id,
                "error": f"ticker mismatch: expected {spec.ticker}, got {row.get('ticker')}",
            })
            continue
        updates.append(_build_update(row, spec))

    for spec in KNOWN_MISSING_TRADE_EVENTS:
        if spec.event_id in by_id:
            skipped.append({"event_id": spec.event_id, "reason": "already_present"})
            continue
        if spec.required_event_ids and not all(event_id in by_id for event_id in spec.required_event_ids):
            continue
        updates.append(_build_missing_event(spec))

    if apply and updates:
        from event_ledger import append_event

        for item in updates:
            price = item["new_price"] if "new_price" in item else item["price"]
            currency = item["new_currency"] if "new_currency" in item else item["currency"]
            fx_rate = (
                item["new_fx_rate_usdjpy"]
                if "new_fx_rate_usdjpy" in item
                else item.get("fx_rate_usdjpy")
            )
            event_id = item["correction_event_id"] if "correction_event_id" in item else item["event_id"]
            r = append_event(
                event_type=item["event_type"],
                occurred_at=item["occurred_at"],
                ticker=item["ticker"],
                direction=item["direction"],
                quantity=item["quantity"],
                price=price,
                currency=currency,
                fx_rate_usdjpy=fx_rate,
                account=item["account"],
                source=item.get("source", "trade_correction"),
                note=item["note"],
                raw_payload=item["raw_payload"],
                event_id=event_id,
                db_path=db_path,
            )
            item["new_amount_jpy"] = r.get("amount_jpy")
            item["duplicate"] = r.get("duplicate")

    return {
        "dry_run": not apply,
        "planned": len(updates),
        "corrected": len(updates) if apply else 0,
        "skipped": skipped,
        "errors": errors,
        "sample": updates[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Append-only corrections for known bad trade ledger rows")
    parser.add_argument("--apply", action="store_true", help="append correction events")
    parser.add_argument("--db", default=str(DB_PATH), help="ledger sqlite path")
    args = parser.parse_args()
    result = correct_known_trade_events(apply=args.apply, db_path=Path(args.db))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
