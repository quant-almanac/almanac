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
    # Identity is stored in raw_payload, not as ledger columns.  tax_lot reads
    # it via ``_event_owner_broker``; without it a lot is keyed "(unknown)" and
    # the total-average basis stays non-authoritative.
    new_owner: Optional[str] = None
    new_broker: Optional[str] = None


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
    owner: Optional[str] = None
    broker: Optional[str] = None
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
    # --- XLF (v7 Slice 1C) ---------------------------------------------------
    # The 2026-07-16 sell was recorded against 特定, but the Rakuten trade
    # history shows it settled in NISA成長投資枠.  Only ``account`` is corrected:
    # price and quantity already match the broker row, and ``currency`` is left
    # unset so the recorded fx_rate_usdjpy is preserved rather than rewritten.
    TradeCorrection(
        event_id="exec_XLF_sell_20260716011043_e6e5f8ea",
        ticker="XLF",
        price_scale=1.0,
        new_account="NISA成長投資枠",
        new_owner="husband",
        new_broker="rakuten",
        reason=(
            "account correction: Rakuten trade history records the 2026-07-16 XLF "
            "sell in NISA成長投資枠, not 特定; misclassification overstates taxable "
            "realized gains by roughly ¥17,065"
        ),
    ),
    # Owner/broker + account correction: this row has no matching husband/Rakuten
    # position (husband's Rakuten SLIM_SP500 holding is 270,964 units per
    # broker_position_snapshot_rakuten.json, no ledger event exists for it), but
    # its quantity is an exact match for holdings.json's SLIM_SP500_WIFE entry
    # (191,819 units, owner=wife, broker=SBI証券（妻）, account=NISA成長投資枠).
    # Targets the ``:tradecorr:v1`` row: the original event_id is already
    # superseded by it (an earlier, unrelated currency-unit fix), so a
    # correction against the original id would be skipped as already_superseded.
    # No SELL event references this ticker yet (checked), so this is a latent
    # misattribution with no current effect on realized P&L -- fixed now to
    # prevent a future sell drawing from the wrong (unknown, unknown) bucket.
    TradeCorrection(
        event_id="backfill_744c26637500caeb:tradecorr:v1",
        ticker="SLIM_SP500",
        price_scale=1.0,
        new_account="NISA成長投資枠",
        new_owner="wife",
        new_broker="sbi",
        reason=(
            "owner/broker/account correction: quantity (191,819 units) exactly "
            "matches holdings.json SLIM_SP500_WIFE (owner=wife, SBI証券（妻）, "
            "NISA成長投資枠); ledger row was recorded as account=特定 with no "
            "owner/broker, which would misattribute the household's wife-owned "
            "NISA mutual fund position to husband's taxable account"
        ),
    ),
    # --- Q-B: owner/broker attribution for all remaining untagged trades ---
    # (v7 Slice 1B, ticker-complete pass). Every currently-untagged BUY/SELL
    # trade event is covered here -- partial (BUY-only) tagging was tried and
    # reverted: it broke FIFO consumption for the corresponding SELLs, which
    # key on (owner, broker, account, ticker) and fell into an
    # "(unknown, unknown, ...)" bucket with no matching lot.
    #
    # Matching method: for events within the 2026-04-28..2026-07-28 Rakuten
    # CSV window, matched by ticker + direction + quantity against all CSV
    # rows (JP/US) on either the raw unit price or the fee/tax-inclusive
    # settlement price -- existing ledger rows are not consistent about which
    # convention they use, and several have an occurred_at that does not match
    # the true CSV trade date (a recurring "opening lot" / placeholder-date
    # pattern, not unique to this ticker set).  Assignment is a strict greedy
    # 1:1 match (tolerance 0.06, tightest diff first, each CSV row consumed at
    # most once) -- an earlier looser-tolerance pass produced multiple ledger
    # events claiming the same CSV row for volatile tickers (e.g. three
    # different AVGO trades all within $4 of one another); this pass has zero
    # duplicate CSV-row claims.
    #
    # Events outside the CSV window (2026-02-17..2026-04-27) or without a
    # unique exact-price match are tagged on household-scope grounds only:
    # broker_position_snapshot_sbi.json attests
    # ("attested_no_other_financial_assets_in_confirmed_scope": true) that
    # wife's SBI holdings are limited to exactly 1489.T / SLIM_SP500 /
    # SLIM_ORCAN -- all three separately reconciled (see commits 2e0a743,
    # af12fc8). No other owner/broker appears anywhere in this ledger's trade
    # rows. This is weaker evidence than a matched CSV row and is stated as
    # such in each entry's reason string.
    TradeCorrection(
        event_id="exec_1306.T_buy_20260708225113_5824822e",
        ticker="1306.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-08 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_1489.T_buy_20260708224949_72517fb4",
        ticker="1489.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-08 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_opening_6762.T_20260301_100sh",
        ticker="6762.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_a8b1b5e343afba13",
        ticker="6762.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="exec_6762.T_sell_20260526200517",
        ticker="6762.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-26 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_6762.T_sell_20260529004619",
        ticker="6762.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-29 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-01 differs from CSV date=2026-05-29",
    ),
    TradeCorrection(
        event_id="manual_missing_7751.T_buy_20260428_100sh",
        ticker="7751.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, settle basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_7751.T_sell_20260609001534",
        ticker="7751.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-08 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-09 differs from CSV date=2026-06-08",
    ),
    TradeCorrection(
        event_id="backfill_6648248f5434a71e",
        ticker="9432.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, settle basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-28 differs from CSV date=2026-05-07",
    ),
    TradeCorrection(
        event_id="exec_9432.T_sell_20260708225022_9b098f8a",
        ticker="9432.T",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-08 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_d0f5296d7e53c920",
        ticker="AAPL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_32b0bca259d2b816",
        ticker="AAPL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_583f74eafe65fbde",
        ticker="ABBV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_0fd50de07abf1052",
        ticker="ABBV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_719a0ff2ad1aede6",
        ticker="ABBV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_ABBV_sell_20260702011746_f198ca47",
        ticker="ABBV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-02 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_79424418cba1fed4",
        ticker="ADBE",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_missing_ADBE_buy_20260422_1sh",
        ticker="ADBE",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_5fbbdd40aca00d4a",
        ticker="ADBE",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_ADBE_sell_20260619010100",
        ticker="ADBE",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_a7a6b82e6b1eb08c",
        ticker="ADI",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_250bcafe7e688e3b",
        ticker="ADI",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_AMAT_20260517_5sh",
        ticker="AMAT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, settle basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-17 differs from CSV date=2026-05-07",
    ),
    TradeCorrection(
        event_id="exec_AMAT_sell_20260619010014",
        ticker="AMAT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_AMAT_sell_20260624005345",
        ticker="AMAT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-23 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-26 differs from CSV date=2026-06-23",
    ),
    TradeCorrection(
        event_id="exec_AMAT_sell_20260626011011",
        ticker="AMAT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-26 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_3ce3c5e931ef049c",
        ticker="AMZN",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_85a2edc0cddd0650",
        ticker="AMZN",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_868f42a470f577ca",
        ticker="ANET",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_ANET_sell_20260619010221",
        ticker="ANET",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-21 differs from CSV date=2026-06-19",
    ),
    TradeCorrection(
        event_id="manual_opening_AVGO_toku_20260301_50sh",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_AVGO_ippan_20260301_27sh",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_fb5389def81ad442",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_fb5389def81ad442:tradecorr:v1",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_6fb07eefe0f2fcb7",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_45da544b670f2913",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_a89f6a99e8b9e93a",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_c3a961c89795785f",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_5591a2a342e308ae",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_missing_AVGO_sell_20260507_3sh",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_AVGO_sell_20260604005033",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-04 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_AVGO_sell_20260606002104",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-08 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-06 differs from CSV date=2026-06-08",
    ),
    TradeCorrection(
        event_id="exec_AVGO_sell_20260619010034",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_AVGO_sell_20260624004845",
        ticker="AVGO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-24 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-26 differs from CSV date=2026-06-24",
    ),
    TradeCorrection(
        event_id="backfill_be36749df0e1889b",
        ticker="COST",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_1280714189a35d5b",
        ticker="COST",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_09d2c6b25965fb68",
        ticker="COST",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-13 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_COST_sell_20260604005009",
        ticker="COST",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-04 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_COST_sell_20260606002217",
        ticker="COST",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_missing_CRM_buy_20260423_1sh",
        ticker="CRM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_6e7e0c8f45f15533",
        ticker="CRM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-13 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_9f3ac76a4adf3217",
        ticker="CRWV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_CRWV_20260307_10sh",
        ticker="CRWV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_EPOL_20260301_410sh",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_c742285dc077329e",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_5b1bda5015d95c3b",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_5b1bda5015d95c3b:tradecorr:v1",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_8ed1241f7a6f02b5",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_696e2b997c31138e",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_4685f85dddb89d5f",
        ticker="EPOL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_EWG_20260301_490sh",
        ticker="EWG",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d3f5269c6a0f4d3f",
        ticker="EWG",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_2cb58f863647f50c",
        ticker="EWG",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_2cb58f863647f50c:tradecorr:v1",
        ticker="EWG",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_4a4dbb8e2a2a6408",
        ticker="EWG",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_GLD_toku_20260301_67sh",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_3ddacf0558ba1335",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_70db339d3d884aa8",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_88c56e0b2edb21d0",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_ae7d7c3f5588c4d6",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_b244df44d3b994b1",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_acad6597f399ca92",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_dbbe9ee13ac3d2f5",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_99d320ab3b6ed359",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_missing_GLD_sell_20260507_5sh",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_GLD_sell_20260528013459",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_opening_GLD_nisa_20260528_5sh",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-14 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-28 differs from CSV date=2026-05-14",
    ),
    TradeCorrection(
        event_id="exec_GLD_sell_20260604004942",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-04 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_GLD_sell_20260606002135",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-08 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-06 differs from CSV date=2026-06-08",
    ),
    TradeCorrection(
        event_id="exec_GLD_sell_20260626010930",
        ticker="GLD",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-26 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_opening_IEV_20260301_340sh",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_aeae9469f5ddf2a5",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_aeae9469f5ddf2a5:tradecorr:v1",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_8ea19682a5746036",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_a8a731b24f227e30",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_35aa2de7492cd6f5",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d497f548e440f0a6",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_c6b8d392f46474b4",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_db4838c1139507a4",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_1f363868ca8d4311",
        ticker="IEV",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_4fa8f5b4b5312851",
        ticker="JNJ",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_a53bf9b9fbe38cb4",
        ticker="LIT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_6d46b3ba92412730",
        ticker="LIT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_3beb56829684bd2d",
        ticker="LLY",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-08 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-07 differs from CSV date=2026-05-08",
    ),
    TradeCorrection(
        event_id="exec_LLY_buy_20260528013329",
        ticker="LLY",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_LLY_buy_20260604004844",
        ticker="LLY",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-04 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_LLY_buy_20260619005949",
        ticker="LLY",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_execution_e8e4e93e617b7e9e1f9f8c26",
        ticker="LLY",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-23 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_c4f19bf3c77c54a0",
        ticker="LRCX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-11 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-09 differs from CSV date=2026-05-11",
    ),
    TradeCorrection(
        event_id="manual_opening_LRCX_20260509_1sh",
        ticker="LRCX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, settle basis, exact-match greedy assignment (diff=0.0474); ledger occurred_at=2026-05-09 differs from CSV date=2026-05-07",
    ),
    TradeCorrection(
        event_id="manual_opening_META_toku_20260301_4sh",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_META_ippan_20260301_2sh",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_8351d28fa8b92d72",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_8839775adf29460d",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_8839775adf29460d:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_de59fd27855f007d",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_950e0ec9aad2e1dc",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_9ff63d067d1d75e3",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_a836f7fd4d9cb6f6",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_bd1fd8475583ed62",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_0cf81a3676d17dee",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_missing_META_buy_20260507_2sh",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_META_buy_20260528013400",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_META_sell_20260626010952",
        ticker="META",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-26 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_730c46d589e338c1",
        ticker="MSFT",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-14 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_missing_NEM_buy_20260423_2sh",
        ticker="NEM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_missing_NEM_buy_20260507_30sh",
        ticker="NEM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_NEM_sell_20260609001703",
        ticker="NEM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-09 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-19 differs from CSV date=2026-06-09",
    ),
    TradeCorrection(
        event_id="backfill_0f32bd992f1c0bfb",
        ticker="NFLX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-13 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_NFLX_sell_20260702011716_91fcf750",
        ticker="NFLX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-02 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_7fc4217a144147cb",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_7fc4217a144147cb:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_fdd60c8676e4b771",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_fdd60c8676e4b771:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_NVDA_ippan_20260201_127sh",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_3d57e6bef64258c0",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d4937d19ae6f7833",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_3d57e6bef64258c0:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d4937d19ae6f7833:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_c808586f792c0754",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_c808586f792c0754:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d9f0fd541507c197",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_d9f0fd541507c197:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_733f088b3cc51fea",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_733f088b3cc51fea:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_missing_NVDA_sell_20260507_25sh",
        ticker="NVDA",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_2564aa62839e18ed",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_QCOM_20260423_2sh",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_9b522c94825d5aa8",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_f2b3edd2cdb40bea",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-14 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_QCOM_sell_20260519010710",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-19 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-05-26 differs from CSV date=2026-05-19",
    ),
    TradeCorrection(
        event_id="exec_QCOM_sell_20260619010129",
        ticker="QCOM",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-21 differs from CSV date=2026-06-19",
    ),
    TradeCorrection(
        event_id="backfill_646b7ed4cde81dbe",
        ticker="RCL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="manual_opening_RCL_20260313_12sh",
        ticker="RCL",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="exec_ROBO_buy_20260529004445",
        ticker="ROBO",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-29 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="backfill_477cba20e683ef53",
        ticker="SBUX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-13 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="manual_opening_SBUX_20260513_1sh",
        ticker="SBUX",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-05-07 row, settle basis, exact-match greedy assignment (diff=0.0001); ledger occurred_at=2026-05-13 differs from CSV date=2026-05-07",
    ),
    TradeCorrection(
        event_id="backfill_744c26637500caeb:tradecorr:v1",
        ticker="SLIM_SP500",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_862ebd12e8dbfa23",
        ticker="TXN",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="exec_TXN_sell_20260619010209",
        ticker="TXN",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-06-19 row, raw basis, exact-match greedy assignment (diff=0.0000); ledger occurred_at=2026-06-21 differs from CSV date=2026-06-19",
    ),
    TradeCorrection(
        event_id="backfill_ca427341d35a9f0d",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_91a3a186600da666",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_215e3bb95ff75662",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_e383bef5d9a69ad5",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_5569e9a8cd9ccecc",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_4f9e1b3f2c34795f",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: pre-CSV-window (2026-02-17..04-27), or no unique exact-price CSV match found (candidates existed but were not unique enough to assign safely); household-scope attribution only (SBI scope proven limited to 1489.T/SLIM_SP500/SLIM_ORCAN, none of which is this ticker)",
    ),
    TradeCorrection(
        event_id="backfill_394d2daa7aa46d79",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-04-28 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    TradeCorrection(
        event_id="exec_V_buy_20260702011548_f8fc6106",
        ticker="V",
        price_scale=1.0,
        new_owner="husband",
        new_broker="rakuten",
        reason="owner/broker attribution: CSV-verified against 2026-07-02 row, raw basis, exact-match greedy assignment (diff=0.0000)",
    ),
    # Rakuten broker_position_snapshot has no MSFT/特定 position; the CSV
    # export confirms this 2026-05-14 buy settled in NISA成長投資枠, not 特定.
    # Targets the ``:tradecorr:v1`` row (already owner/broker-tagged by the
    # Q-B pass above); the untagged original is already superseded by it.
    TradeCorrection(
        event_id="backfill_730c46d589e338c1:tradecorr:v1",
        ticker="MSFT",
        price_scale=1.0,
        new_account="NISA成長投資枠",
        reason=(
            "account correction: tradehistory(US)_20260728.csv line confirms "
            "2026/5/14 MSFT buy settled in NISA成長投資枠 ('2026/5/14','2026/5/18',"
            "'MSFT','MICROSOFT CORP.','NISA成長投資枠','現物','買付'), not 特定 as recorded"
        ),
    ),
    # META: CSV confirms the 2026-06-26 sell (currently 一般) actually settled
    # in 特定, and moving it there was tested on a clone -- but it turns 特定
    # from a 9-vs-9 match into an 8-vs-9 mismatch (a lateral shift, not a net
    # fix), meaning something else in 特定's reconstructed history is also
    # off (the four separate 1-share buys on 2026-04-17/18/21/22 are a
    # suspicious pattern not yet cross-checked against the CSV). Deliberately
    # NOT applying the account correction until that's resolved; see
    # docs/audit_2026_08/ for this open item.
    # GLD 特定 reconciliation gap (67 opening - 31 pre-window sells - 21
    # in-ledger window sells = 15, vs Rakuten's 20) traced to two CSV-verified
    # errors on the SAME underlying pair of transactions: the 2026-06-04 sell
    # was recorded as 特定 but settled in NISA成長投資枠, and the 2026-06-08(CSV)
    # / 2026-06-06(ledger occurred_at) buy of 3 shares is absent from the
    # ledger entirely. Fixing both moves 特定 from 15 to 20 (matches Rakuten)
    # and leaves NISA成長投資枠 at net 0 (matches: not in current holdings).
    TradeCorrection(
        event_id="exec_GLD_sell_20260604004942:tradecorr:v1",
        ticker="GLD",
        price_scale=1.0,
        new_account="NISA成長投資枠",
        reason=(
            "account correction: tradehistory(US)_20260728.csv confirms the "
            "2026/6/4 GLD sell (2株 @407.6795) settled in NISA成長投資枠, not 特定"
        ),
    ),
    # COST double-recorded sell. trade_history.csv contains two 1-share sells
    # at the same 962.47 price (2026-06-04 and 2026-06-09) with near-identical
    # realized P&L (¥-7,885 / ¥-7,892 -- the small delta is an FX artifact),
    # but the broker export shows exactly one COST sell in the whole window.
    # Voiding the later duplicate brings 特定 from 2 to 3, matching Rakuten.
    TradeCorrection(
        event_id="exec_COST_sell_20260606002217:tradecorr:v1",
        ticker="COST",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void duplicate sell: tradehistory(US)_20260728.csv records only one "
            "COST sell (2026/6/4, 1株 @962.47) in the covered window, but "
            "trade_history.csv logged it twice (2026-06-04 and 2026-06-09, same "
            "price, P&L differing only by an FX rounding artifact)"
        ),
    ),
    # --- META: full rebuild against the complete Rakuten history -------------
    # The 2026-08 audit could not reconcile META from the 3-month export: the
    # required opening lot solved to 0 shares, which contradicted a March sell.
    # The full export (2021-2026, supplied 2026-08-04) shows why -- the real
    # position originates from a 2024-07-16 一般 buy of 4 shares, and several
    # ledger rows carried the wrong account, a wrong price, or were duplicated
    # out of trade_history.csv.
    #
    # Broker truth:
    #   一般 : 2024-07-16 buy 4 @503.35, 2026-03-13 sell 1, 2026-04-10 sell 1
    #          -> 2 shares
    #   特定 : 04-17 buy 1, 04-21 buy 1, 04-22 buy 1, 04-27 sell 1,
    #          04-28 sell 1, 05-07 buy 2, 05-14 buy 4, 05-28 buy 2,
    #          06-08 buy 1, 06-26 sell 1  -> 9 shares
    # Both match broker_position_snapshot_rakuten.json (一般 2 / 特定 9).
    TradeCorrection(
        event_id="manual_opening_META_toku_20260301_4sh:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void inferred opening lot: the full Rakuten history shows no META "
            "position in 特定 before 2026-04-17; this 4-share lot was synthesized "
            "to balance books that were missing the 05-14 and 06-08 buys"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_META_ippan_20260301_2sh:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_quantity=4.0,
        new_price=503.35,
        reason=(
            "correct inferred opening lot to the real acquisition: Rakuten history "
            "records 2024-07-16 一般 買付 4株 @503.35 (約定2,013.40 USD / 受渡321,147円). "
            "occurred_at stays at the 2026-03-01 opening-lot marker used elsewhere "
            "in this file; only quantity and cost basis are corrected"
        ),
    ),
    TradeCorrection(
        event_id="backfill_8351d28fa8b92d72:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_account="一般",
        reason=(
            "account correction: Rakuten history records the 2026-03-13 META sell "
            "(1株 @640.915) in 一般, not 特定"
        ),
    ),
    # The 2026-04-10 sell was voided (quantity -> 0) when its recorded
    # "20株 @71.07" matched no known META price. The full Rakuten history
    # identifies the real trade, so it is restored here.
    #
    # Targets the row that production already carries (the original id and its
    # first correction are both superseded there). On a database rebuilt from
    # scratch the void spec above runs first in the same pass, so this link
    # does not exist yet and the restore is skipped as "missing" -- a second
    # invocation completes it. Callers that rebuild from empty should run
    # correct_known_trade_events() until planned == 0 rather than assuming a
    # single pass converges.
    TradeCorrection(
        event_id="backfill_8839775adf29460d:tradecorr:v1:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_quantity=1.0,
        new_price=631.56,
        new_account="一般",
        reason=(
            "restore trade voided for lack of evidence: this row was zeroed out "
            "when it read '20株 @71.07', which matched no known META price. The "
            "full Rakuten history identifies it as 2026-04-10 一般 売付 1株 @631.56 "
            "(約定631.56 USD / 受渡628.42 USD)"
        ),
    ),
    TradeCorrection(
        event_id="backfill_950e0ec9aad2e1dc:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void duplicate buy: trade_history.csv logged a 2026-04-18 META buy "
            "of 1株 @673.78, identical in price to the 04-17 buy. The Rakuten "
            "history has no 04-18 META trade -- same double-recording pattern as "
            "the COST sell voided above"
        ),
    ),
    TradeCorrection(
        event_id="backfill_a836f7fd4d9cb6f6:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_price=671.76,
        reason=(
            "price correction: Rakuten history records the 2026-04-22 META buy at "
            "671.76, not the 668.0 carried from trade_history.csv"
        ),
    ),
    TradeCorrection(
        event_id="exec_META_sell_20260626010952:tradecorr:v1",
        ticker="META",
        price_scale=1.0,
        new_account="特定",
        reason=(
            "account correction: Rakuten history records the 2026-06-26 META sell "
            "(1株 @551.095) in 特定, not 一般"
        ),
    ),
    # --- Cost-basis reconstruction against the full 2021-2026 Rakuten history
    # (2026-08-05). The "manual_opening_*" entries below were originally
    # estimated by reverse-solving weighted-average cost from
    # action_executions sell messages and broker_position_reconcile_log
    # snapshots (see each spec's own ``evidence`` in KNOWN_MISSING_TRADE_EVENTS)
    # because no full trade history was available at the time. The complete
    # export shows several of those estimates were off, and that three
    # tickers' "opening lot" entries were paired with entirely fabricated
    # round trips (buys/sells with no CSV counterpart, apparently inserted at
    # some point to force a running total to match a known checkpoint). All
    # new prices below are fee/tax-inclusive settlement price per share
    # (受渡金額 / 数量), matching the convention documented on the XLF entries.
    TradeCorrection(
        event_id="manual_opening_NVDA_ippan_20260201_127sh:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_quantity=140.0,
        new_price=116.624929,
        reason=(
            "correct inferred opening lot to the real acquisition: Rakuten full "
            "history shows NVDA一般 was 2 shares bought 2024-05-24 (pre-split), "
            "split 10-for-1 on 2024-06-10 to 20 shares, then +70 (2025-01-30) "
            "+50 (2025-03-10) = 140 shares by the 2026-03-01 opening marker, not 127"
        ),
    ),
    TradeCorrection(
        event_id="backfill_7fc4217a144147cb:tradecorr:v1:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void phantom trade: no 2026-02-19 NVDA buy exists anywhere in the "
            "full Rakuten history; this row and its siblings appear to have been "
            "inserted to compensate for the opening lot being under-counted by "
            "13 shares (127 vs the real 140) -- now that the opening lot itself "
            "is corrected, this compensating entry is redundant and wrong"
        ),
    ),
    TradeCorrection(
        event_id="backfill_fdd60c8676e4b771:tradecorr:v1:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void phantom trade: no 2026-02-28 NVDA buy exists anywhere in the "
            "full Rakuten history (same compensating pattern as "
            "backfill_7fc4217a144147cb above)"
        ),
    ),
    TradeCorrection(
        event_id="backfill_3d57e6bef64258c0:tradecorr:v1:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void phantom trade: no 2026-03-07 NVDA buy exists anywhere in the "
            "full Rakuten history; paired with the phantom same-day sell below "
            "(self-canceling, zero net effect on quantity, but both are fabricated)"
        ),
    ),
    TradeCorrection(
        event_id="backfill_d4937d19ae6f7833:tradecorr:v1:tradecorr:v1",
        ticker="NVDA",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void phantom trade: no 2026-03-07 NVDA sell exists anywhere in the "
            "full Rakuten history; paired with the phantom same-day buy above"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_QCOM_20260423_2sh:tradecorr:v1",
        ticker="QCOM",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void inferred opening lot: the full Rakuten history shows no QCOM "
            "position before 2026-04-23; the real 4-share total by 2026-04-28 is "
            "1 (04-23, already a separate ledger row) + 2 (04-27, missing -- added "
            "in KNOWN_MISSING_TRADE_EVENTS) + 1 (04-28, already a separate ledger "
            "row). This opening lot duplicated the 04-23 share under a blended, "
            "wrong price instead of the real 04-27 buy"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_EPOL_20260301_410sh:tradecorr:v1",
        ticker="EPOL",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void inferred opening lot: replaced by the real dated buys "
            "(2025-07-25/07-31/08-26) and the previously-unrecorded 2026-02-12 "
            "sell, all added in KNOWN_MISSING_TRADE_EVENTS. The estimate "
            "(33.1393) also could not have stood as a single 2026-03-01 lot "
            "without contradicting the 02-12 sell date -- a sell recorded before "
            "its only buy lot existed"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_IEV_20260301_340sh:tradecorr:v1",
        ticker="IEV",
        price_scale=1.0,
        new_quantity=220.0,
        new_price=63.7675,
        reason=(
            "correct inferred opening lot to the real acquisition: Rakuten full "
            "history shows IEV特定 was 110 (2025-07-25) + 110 (2025-07-31) = 220 "
            "shares by the 2026-03-01 opening marker, not 340 -- the extra 120 "
            "shares were compensating for a fabricated 140-share sell (voided "
            "below) netted against a real sell missing from the ledger (added in "
            "KNOWN_MISSING_TRADE_EVENTS)"
        ),
    ),
    TradeCorrection(
        event_id="backfill_aeae9469f5ddf2a5:tradecorr:v1:tradecorr:v1",
        ticker="IEV",
        price_scale=1.0,
        new_quantity=0.0,
        reason=(
            "void phantom trade: no 2026-03-27 IEV sell exists anywhere in the "
            "full Rakuten history. Its originally-recorded price (35.1759) was an "
            "exact match for EPOL's real 2026-03-27 sell price, not any real IEV "
            "price -- an earlier correction adjusted the leading digit (35->65) "
            "to look IEV-plausible, but the whole row was fabricated, not mis-keyed"
        ),
    ),
    TradeCorrection(
        event_id="backfill_2cb58f863647f50c:tradecorr:v1:tradecorr:v1",
        ticker="EWG",
        price_scale=1.0,
        new_price=40.1275,
        reason=(
            "price correction: the existing price_scale=0.01 fix (see "
            "backfill_2cb58f863647f50c above) assumed the stored 4815.3 was a "
            "per-share price with a misplaced decimal (/100 -> 48.153). The full "
            "Rakuten history shows 4815.3 was actually the total settlement "
            "amount for a 120-share sell (2026-04-02, merged from two same-day "
            "fills), so the correct per-share price is 4815.30 / 120 = 40.1275"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_EWG_20260301_490sh:tradecorr:v1",
        ticker="EWG",
        price_scale=1.0,
        new_price=42.441449,
        reason=(
            "price correction: Rakuten full history shows EWG特定's 3 real buys "
            "(160@2025-07-25, 170@07-31, 160@08-26) average to a fee-inclusive "
            "settlement price of 42.441449, not the estimated 42.222380"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_LRCX_20260509_1sh:tradecorr:v1",
        ticker="LRCX",
        price_scale=1.0,
        new_price=256.78,
        reason=(
            "price correction: the Q-B owner/broker pass already matched this row "
            "to the real 2026-05-07 LRCX buy (diff=0.0474, noted but not applied "
            "there since that pass only corrected owner/broker) -- now applying "
            "the exact settlement price (256.78) instead of the estimated "
            "256.827416"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_RCL_20260313_12sh:tradecorr:v1",
        ticker="RCL",
        price_scale=1.0,
        new_price=270.230833,
        reason=(
            "price correction: Rakuten full history shows the real RCL buy "
            "(2025-01-30, 12 shares) settled at a fee-inclusive price of "
            "270.230833, not the estimated 270.144076 (the estimate was "
            "back-solved from trade_history.csv's realized P&L, which itself has "
            "an error distinct from the broker record)"
        ),
    ),
    TradeCorrection(
        event_id="manual_opening_CRWV_20260307_10sh:tradecorr:v1",
        ticker="CRWV",
        price_scale=1.0,
        new_price=119.965,
        reason=(
            "price correction: Rakuten full history shows the real CRWV buy "
            "(2025-09-11, 10 shares) settled at a fee-inclusive price of 119.965, "
            "not the estimated 117.862232"
        ),
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
    # --- XLF (v7 Slice 1C) ---------------------------------------------------
    # Primary-source repair: every field below comes from the Rakuten trade
    # history export, not from inference, so these entries carry no
    # ``required_event_ids`` gate (unlike the inferred opening lots above).
    #
    # ``price`` is the settlement price per share (受渡金額 / 数量), i.e. it
    # includes 手数料 and 税金.  ``MissingTradeEvent`` has no fee field, and the
    # broker's reported 平均取得価額 is fee-inclusive, so this is the only
    # representation that reproduces 特定 52.9958 / NISA 51.4638.  The raw
    # 単価 and the fee are preserved in ``evidence`` for audit.
    MissingTradeEvent(
        event_id="manual_missing_XLF_buy_20260507_60sh",
        occurred_at="2026-05-07T00:00:00",
        ticker="XLF",
        direction="buy",
        quantity=60.0,
        price=52.3075,
        currency="USD",
        fx_rate_usdjpy=156.630,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "XLF buy present in Rakuten trade history but absent from event_ledger; "
            "without it build_lots('XLF') cannot reconstruct the 2026-07-16 sell"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv line15: 約定日2026/5/7 口座=特定 買付 60株",
            "単価52.0500 / 約定代金3,123.00 / 手数料14.05 / 税金1.40 / 受渡3,138.45 USD",
            "price=3138.45/60=52.3075 (fee-inclusive, matches broker average basis)",
            "為替レート=156.630",
        ),
        # Gate on the sibling XLF sell so this primary-source entry only
        # synthesizes into databases that actually contain the XLF position
        # this repair is for; an empty ``required_event_ids`` is unconditional
        # and would leak into every unrelated isolated test database.
        required_event_ids=("exec_XLF_sell_20260716011043_e6e5f8ea",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_XLF_buy_20260515_30sh",
        occurred_at="2026-05-15T00:00:00",
        ticker="XLF",
        direction="buy",
        quantity=30.0,
        price=51.3775,
        currency="USD",
        fx_rate_usdjpy=158.800,
        account="NISA成長投資枠",
        owner="husband",
        broker="rakuten",
        reason=(
            "XLF NISA growth-quota buy present in Rakuten trade history but absent "
            "from event_ledger; required to place the 2026-07-16 sell in NISA"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv line33: 約定日2026/5/15 口座=NISA成長投資枠 買付 30株",
            "単価51.3775 / 約定代金1,541.33 / 手数料- / 税金- / 受渡1,541.33 USD",
            "NISA has no commission, so settlement price equals 単価",
            "為替レート=158.800",
        ),
        required_event_ids=("exec_XLF_sell_20260716011043_e6e5f8ea",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_XLF_buy_20260520_30sh",
        occurred_at="2026-05-20T00:00:00",
        ticker="XLF",
        direction="buy",
        quantity=30.0,
        price=51.5500,
        currency="USD",
        fx_rate_usdjpy=159.240,
        account="NISA成長投資枠",
        owner="husband",
        broker="rakuten",
        reason=(
            "XLF NISA growth-quota buy present in Rakuten trade history but absent "
            "from event_ledger; second NISA lot consumed by the 2026-07-16 sell"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv line35: 約定日2026/5/20 口座=NISA成長投資枠 買付 30株",
            "単価51.5500 / 約定代金1,546.50 / 手数料- / 税金- / 受渡1,546.50 USD",
            "NISA has no commission, so settlement price equals 単価",
            "為替レート=159.240",
        ),
        required_event_ids=("exec_XLF_sell_20260716011043_e6e5f8ea",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_XLF_buy_20260702_20sh",
        occurred_at="2026-07-02T00:00:00",
        ticker="XLF",
        direction="buy",
        quantity=20.0,
        price=55.0610,
        currency="USD",
        fx_rate_usdjpy=162.820,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "XLF taxable buy present in Rakuten trade history but absent from "
            "event_ledger; second 特定 lot behind the reported 80-share holding"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv line65: 約定日2026/7/2 口座=特定 買付 20株",
            "単価54.7900 / 約定代金1,095.80 / 手数料4.93 / 税金0.49 / 受渡1,101.22 USD",
            "price=1101.22/20=55.0610 (fee-inclusive, matches broker average basis)",
            "為替レート=162.820",
        ),
        required_event_ids=("exec_XLF_sell_20260716011043_e6e5f8ea",),
    ),
    # --- ABNB / MA / RTX: opening lots absent from event_ledger entirely,
    # discovered via broker_position_snapshot_rakuten.json reconciliation
    # (ledger showed 0 shares where Rakuten reports 2 for each). No sibling
    # sell exists yet for any of the three -- unlike the XLF/AVGO gated
    # entries above, there is no event_id to gate on. This follows the
    # existing unggated pattern used elsewhere in this file for currently-held
    # opening lots with no corresponding sell (e.g. manual_opening_AVGO_*).
    MissingTradeEvent(
        event_id="manual_missing_ABNB_buy_20260514_2sh",
        occurred_at="2026-05-14T00:00:00",
        ticker="ABNB",
        direction="buy",
        quantity=2.0,
        price=135.355,
        currency="USD",
        fx_rate_usdjpy=158.180,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "ABNB buy present in Rakuten trade history but absent from "
            "event_ledger; discovered via broker_position_snapshot_rakuten.json "
            "reconciliation (ledger showed 0 vs broker's 2 shares)"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/5/14 口座=特定 買付 2株",
            "単価134.6900 / 約定代金269.38 / 手数料1.21 / 税金0.12 / 受渡270.71 USD",
            "price=270.71/2=135.355 (fee-inclusive, matches broker average basis)",
            "為替レート=158.180",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_MA_buy_20260507_2sh",
        occurred_at="2026-05-07T00:00:00",
        ticker="MA",
        direction="buy",
        quantity=2.0,
        price=502.19,
        currency="USD",
        fx_rate_usdjpy=156.630,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "MA buy present in Rakuten trade history but absent from "
            "event_ledger; discovered via broker_position_snapshot_rakuten.json "
            "reconciliation (ledger showed 0 vs broker's 2 shares)"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/5/7 口座=特定 買付 2株",
            "単価499.7250 / 約定代金999.45 / 手数料4.49 / 税金0.44 / 受渡1,004.38 USD",
            "price=1004.38/2=502.19 (fee-inclusive, matches broker average basis)",
            "為替レート=156.630",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_RTX_buy_20260428_2sh",
        occurred_at="2026-04-28T00:00:00",
        ticker="RTX",
        direction="buy",
        quantity=2.0,
        price=174.615,
        currency="USD",
        fx_rate_usdjpy=159.780,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "RTX buy present in Rakuten trade history but absent from "
            "event_ledger; discovered via broker_position_snapshot_rakuten.json "
            "reconciliation (ledger showed 0 vs broker's 2 shares)"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/4/28 口座=特定 買付 2株",
            "単価173.7600 / 約定代金347.52 / 手数料1.56 / 税金0.15 / 受渡349.23 USD",
            "price=349.23/2=174.615 (fee-inclusive, matches broker average basis)",
            "為替レート=159.780",
        ),
    ),
    # Second half of the GLD 特定 reconciliation fix (see the account
    # correction on exec_GLD_sell_20260604004942 above). Gated on the
    # 2026-06-06 sell whose ledger occurred_at is the same date-offset pattern
    # (CSV settlement 2026-06-08, ledger records it two days earlier) seen
    # repeatedly elsewhere in this file (LRCX, SBUX, AMAT, 9432.T).
    MissingTradeEvent(
        event_id="manual_missing_GLD_buy_20260608_3sh",
        occurred_at="2026-06-06T00:00:00",
        ticker="GLD",
        direction="buy",
        quantity=3.0,
        price=401.18,
        currency="USD",
        fx_rate_usdjpy=160.520,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "GLD buy present in Rakuten trade history (2026/6/8, 3 shares, 特定) "
            "but absent from event_ledger; without it 特定 reconstructs to 15 "
            "shares against Rakuten's reported 20"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/6/8 口座=特定 買付 3株",
            "単価399.2111 / 約定代金1,197.63 / 手数料5.38 / 税金0.53 / 受渡1,203.54 USD",
            "price=1203.54/3=401.18 (fee-inclusive, matches broker average basis)",
            "為替レート=160.520",
        ),
        required_event_ids=("exec_GLD_sell_20260606002135",),
    ),
    # --- Transactions confirmed by the broker export but absent from both
    # event_ledger and trade_history.csv. Each was found by reconciling
    # reconstructed quantities against broker_position_snapshot_rakuten.json
    # and each one, on its own, closes that ticker's gap exactly.
    #
    # Price convention: raw unit price (単価), matching every other row for
    # these tickers. (The XLF entries above use the fee-inclusive settlement
    # price because that was needed to reproduce the broker's reported
    # 平均取得価額; the ledger is not internally consistent on this point --
    # see the open items in docs/audit_2026_08/.)
    MissingTradeEvent(
        event_id="manual_missing_ADI_sell_20260507_1sh",
        occurred_at="2026-05-07T00:00:00",
        ticker="ADI",
        direction="sell",
        quantity=1.0,
        price=397.63,
        currency="USD",
        fx_rate_usdjpy=156.130,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "ADI sell present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 3 shares against "
            "Rakuten's reported 2"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/5/7 口座=特定 売付 1株",
            "単価397.6300 / 手数料1.79 / 税金0.17 / 受渡395.67 USD",
            "為替レート=156.130",
        ),
        required_event_ids=("backfill_a7a6b82e6b1eb08c",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_AVGO_buy_20260710_2sh",
        occurred_at="2026-07-10T00:00:00",
        ticker="AVGO",
        direction="buy",
        quantity=2.0,
        price=393.32,
        currency="USD",
        fx_rate_usdjpy=161.990,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "AVGO buy present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 3 shares against "
            "Rakuten's reported 5"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/7/10 口座=特定 買付 2株",
            "単価393.3200 / 手数料3.53 / 税金0.35 / 受渡790.52 USD",
            "為替レート=161.990",
        ),
        required_event_ids=("exec_AVGO_sell_20260624004845",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_JNJ_sell_20260702_1sh",
        occurred_at="2026-07-02T00:00:00",
        ticker="JNJ",
        direction="sell",
        quantity=1.0,
        price=253.90,
        currency="USD",
        fx_rate_usdjpy=162.320,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "JNJ sell present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 2 shares against "
            "Rakuten's reported 1"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/7/2 口座=特定 売付 1株",
            "単価253.9000 / 手数料1.15 / 税金0.11 / 受渡252.64 USD",
            "為替レート=162.320",
        ),
        required_event_ids=("backfill_4fa8f5b4b5312851",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_LIT_sell_20260708_5sh",
        occurred_at="2026-07-08T00:00:00",
        ticker="LIT",
        direction="sell",
        quantity=5.0,
        price=74.10,
        currency="USD",
        fx_rate_usdjpy=162.180,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "LIT sell present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 10 shares against "
            "Rakuten's reported 5"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/7/8 口座=特定 売付 5株",
            "単価74.1000 / 手数料1.67 / 税金0.16 / 受渡368.67 USD",
            "為替レート=162.180",
        ),
        required_event_ids=("backfill_a53bf9b9fbe38cb4",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_NEM_buy_20260708_6sh",
        occurred_at="2026-07-08T00:00:00",
        ticker="NEM",
        direction="buy",
        quantity=6.0,
        price=95.50,
        currency="USD",
        fx_rate_usdjpy=162.680,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "NEM buy present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 0 shares against "
            "Rakuten's reported 6"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/7/8 口座=特定 買付 6株",
            "単価95.5000 / 手数料2.57 / 税金0.25 / 受渡575.82 USD",
            "為替レート=162.680",
        ),
        required_event_ids=("exec_NEM_sell_20260609001703",),
    ),
    # LLY: two separate missing transactions. The ledger has four buys and one
    # sell; the broker export has five buys and two sells. Adding both the
    # 2026-06-08 buy and the 2026-07-08 sell takes 特定 from 3 to 2, matching
    # Rakuten. Gated on the one LLY sell that is already in the ledger.
    MissingTradeEvent(
        event_id="manual_missing_LLY_buy_20260608_1sh",
        occurred_at="2026-06-08T00:00:00",
        ticker="LLY",
        direction="buy",
        quantity=1.0,
        price=1141.00,
        currency="USD",
        fx_rate_usdjpy=160.520,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "LLY buy present in Rakuten trade history but absent from "
            "event_ledger; one of two missing LLY transactions"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/6/8 口座=特定 買付 1株",
            "単価1,141.0000 / 手数料5.13 / 税金0.51 / 受渡1,146.64 USD",
            "為替レート=160.520",
        ),
        required_event_ids=("exec_execution_e8e4e93e617b7e9e1f9f8c26",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_LLY_sell_20260708_2sh",
        occurred_at="2026-07-08T00:00:00",
        ticker="LLY",
        direction="sell",
        quantity=2.0,
        price=1236.57,
        currency="USD",
        fx_rate_usdjpy=162.180,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "LLY sell present in Rakuten trade history but absent from "
            "event_ledger; without it 特定 reconstructs to 3 shares against "
            "Rakuten's reported 2"
        ),
        evidence=(
            "tradehistory(US)_20260728.csv: 約定日2026/7/8 口座=特定 売付 2株",
            "単価1,236.5700 / 手数料11.18 / 税金1.11 / 受渡2,460.85 USD",
            "為替レート=162.180",
        ),
        required_event_ids=("exec_execution_e8e4e93e617b7e9e1f9f8c26",),
    ),
    # 1489.T NISA成長投資枠 opening lot. Acquired before the supplied trade
    # history begins (2026-04-28), so there is no transaction row to match --
    # but the cost basis is not inferred: holdings.json carries it straight
    # from the Rakuten assetbalance CSV (broker_cost_basis_source=
    # rakuten_assetbalance_csv, broker_total_cost_basis_jpy=364680 for 120
    # shares = exactly ¥3,039.0/share), and nisa_portfolio.json agrees.
    # The 特定 lot for the same ticker (100 shares, 2026-07-08) is a separate
    # in-window trade already present in the ledger.
    MissingTradeEvent(
        event_id="manual_opening_1489.T_nisa_20260301_120sh",
        occurred_at="2026-03-01T00:00:00",
        ticker="1489.T",
        direction="buy",
        quantity=120.0,
        price=3039.0,
        currency="JPY",
        account="NISA成長投資枠",
        owner="husband",
        broker="rakuten",
        reason=(
            "1489.T NISA成長投資枠 opening lot missing from event_ledger; broker "
            "reports 120 shares that the ledger reconstructs as 0"
        ),
        evidence=(
            "broker_position_snapshot_rakuten.json: 1489.T NISA成長投資枠 120株",
            "holdings.json['1489']: shares=120, entry_price=3039.0, entry_date=2026-03-01, "
            "broker_cost_basis_source=rakuten_assetbalance_csv, "
            "broker_total_cost_basis_jpy=364680",
            "nisa_portfolio.json husband.holdings['1489']: shares=120, avg_cost=3039.0, "
            "cost_basis=364680",
            "検算: 120 × 3039.0 = 364680 (broker報告値と一致)",
            "2026-08-04 追記: 全期間JP履歴で実約定日が判明 — "
            "tradehistory(JP)_20260804.csv 2026/2/3 NISA成長投資枠 買付 120株 @3,039.0 受渡364,680。"
            "occurred_at は opening-lot marker のまま (数量・原価は実約定と一致)",
        ),
        required_event_ids=("exec_1489.T_buy_20260708224949_72517fb4",),
    ),
    # META: the two buys the 3-month export was missing. Both are in the full
    # Rakuten history and are required for 特定 to reconstruct to 9 shares.
    MissingTradeEvent(
        event_id="manual_missing_META_buy_20260514_4sh",
        occurred_at="2026-05-14T00:00:00",
        ticker="META",
        direction="buy",
        quantity=4.0,
        price=598.83,
        currency="USD",
        fx_rate_usdjpy=158.180,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "META buy present in the full Rakuten history but absent from "
            "event_ledger and trade_history.csv; one of the two gaps that made "
            "META unreconcilable from the 3-month export"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2026/5/14 口座=特定 買付 4株",
            "単価598.8300 / 約定2,395.32 / 手数料10.77 / 税金1.07 / 受渡2,407.16 USD",
            "為替レート=158.180",
        ),
        required_event_ids=("exec_META_buy_20260528013400",),
    ),
    MissingTradeEvent(
        event_id="manual_missing_META_buy_20260608_1sh",
        occurred_at="2026-06-08T00:00:00",
        ticker="META",
        direction="buy",
        quantity=1.0,
        price=605.00,
        currency="USD",
        fx_rate_usdjpy=160.520,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "META buy present in the full Rakuten history but absent from "
            "event_ledger and trade_history.csv; second of the two gaps"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2026/6/8 口座=特定 買付 1株",
            "単価605.0000 / 約定605.00 / 手数料2.72 / 税金0.27 / 受渡607.99 USD",
            "為替レート=160.520",
        ),
        required_event_ids=("exec_META_buy_20260528013400",),
    ),
    # --- Trades confirmed against the full 2021-2026 Rakuten history
    # (2026-08-05), absent from event_ledger and from trade_history.csv.
    # Prices are fee/tax-inclusive settlement price per share, matching the
    # XLF primary-source entries above; no required_event_ids gate since each
    # is a standalone historical fact, not dependent on chain ordering.
    MissingTradeEvent(
        event_id="manual_missing_6762.T_sell_20260514_100sh",
        occurred_at="2026-05-14T00:00:00",
        ticker="6762.T",
        direction="sell",
        quantity=100.0,
        price=2976.25,
        currency="JPY",
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "6762.T sell present in Rakuten trade history but absent from "
            "event_ledger; without it, the 2026-04-22 buy lot is never realized "
            "and the 2026-05-20 buy (added below) has no matching prior sell"
        ),
        evidence=(
            "tradehistory(JP)_20260804.csv: 約定日2026/5/14 口座区分=特定 売付 100株",
            "単価2,979.0 / 手数料250 / 税金等25 / 受渡297,625円",
            "price=297625/100=2976.25 (fee-inclusive)",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_6762.T_buy_20260520_100sh",
        occurred_at="2026-05-20T00:00:00",
        ticker="6762.T",
        direction="buy",
        quantity=100.0,
        price=2888.25,
        currency="JPY",
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "6762.T buy present in Rakuten trade history but absent from "
            "event_ledger; re-opens the position between the 2026-05-14 sell "
            "(added above) and the 2026-05-26 sell already in the ledger"
        ),
        evidence=(
            "tradehistory(JP)_20260804.csv: 約定日2026/5/20 口座区分=特定 買付 100株",
            "単価2,885.5 / 手数料250 / 税金等25 / 受渡288,825円",
            "price=288825/100=2888.25 (fee-inclusive)",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_EPOL_buy_20250725_200sh",
        occurred_at="2025-07-25T00:00:00",
        ticker="EPOL",
        direction="buy",
        quantity=200.0,
        price=33.4867,
        currency="USD",
        fx_rate_usdjpy=147.500,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "EPOL buy present in Rakuten trade history but absent from "
            "event_ledger; first of three real buys underlying the position that "
            "manual_opening_EPOL_20260301_410sh (now voided) tried to estimate"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2025/7/25 口座=特定 買付 200株",
            "単価33.3767 / 手数料20.00 / 税金2.00 / 受渡6,697.34 USD",
            "price=6697.34/200=33.4867 (fee-inclusive)",
            "為替レート=147.500",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_EPOL_buy_20250731_210sh",
        occurred_at="2025-07-31T00:00:00",
        ticker="EPOL",
        direction="buy",
        quantity=210.0,
        price=32.402286,
        currency="USD",
        fx_rate_usdjpy=149.500,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason="EPOL buy present in Rakuten trade history but absent from event_ledger; second of three real buys",
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2025/7/31 口座=特定 買付 210株",
            "単価32.2975 / 手数料20.00 / 税金2.00 / 受渡6,804.48 USD",
            "price=6804.48/210=32.402286 (fee-inclusive)",
            "為替レート=149.500",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_EPOL_buy_20250826_200sh",
        occurred_at="2025-08-26T00:00:00",
        ticker="EPOL",
        direction="buy",
        quantity=200.0,
        price=33.0763,
        currency="USD",
        fx_rate_usdjpy=147.450,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "EPOL buy present in Rakuten trade history but absent from "
            "event_ledger; third of three real buys, merged from four same-day "
            "fills (126+32+32+10 shares) using settlement-weighted average price, "
            "matching the convention already used elsewhere in this file for "
            "same-day multi-fill trades"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2025/8/26 口座=特定 買付 126+32+32+10株, all 単価32.96-32.97",
            "settle sum=4168.14+1058.22+1058.22+330.68=6615.26 / 200 = 33.0763",
            "為替レート=147.450 (uniform across all four fills)",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_EPOL_sell_20260212_200sh",
        occurred_at="2026-02-12T00:00:00",
        ticker="EPOL",
        direction="sell",
        quantity=200.0,
        price=38.42,
        currency="USD",
        fx_rate_usdjpy=152.870,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "EPOL sell present in Rakuten trade history but absent from "
            "event_ledger; its realized gain was entirely unrecorded, and its "
            "quantity effect had only been folded silently into the (now voided) "
            "opening lot estimate"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2026/2/12 口座=特定 売付 200株",
            "単価38.5300 / 手数料20.00 / 税金2.00 / 受渡7,684.00 USD",
            "price=7684.00/200=38.42 (fee-inclusive)",
            "為替レート=152.870",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_IEV_sell_20260410_20sh",
        occurred_at="2026-04-10T00:00:00",
        ticker="IEV",
        direction="sell",
        quantity=20.0,
        price=70.7175,
        currency="USD",
        fx_rate_usdjpy=158.930,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "IEV sell present in Rakuten trade history but absent from "
            "event_ledger; the existing ledger row dated 2026-04-10 "
            "(backfill_d497f548e440f0a6, price 71.75) actually carries the price "
            "of the real 2026-04-13 sell, so this earlier same-quantity sell was "
            "never separately recorded"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2026/4/10 口座=特定 売付 20株 単価71.0700",
            "受渡1,414.35 USD; price=1414.35/20=70.7175 (fee-inclusive)",
            "為替レート=158.930",
        ),
    ),
    MissingTradeEvent(
        event_id="manual_missing_QCOM_buy_20260427_2sh",
        occurred_at="2026-04-27T00:00:00",
        ticker="QCOM",
        direction="buy",
        quantity=2.0,
        price=147.545,
        currency="USD",
        fx_rate_usdjpy=159.550,
        account="特定",
        owner="husband",
        broker="rakuten",
        reason=(
            "QCOM buy present in Rakuten trade history but absent from "
            "event_ledger; replaces the voided manual_opening_QCOM_20260423_2sh, "
            "which blended this buy's quantity into the wrong date and price"
        ),
        evidence=(
            "tradehistory(US)_20260804.csv: 約定日2026/4/27 口座=特定 買付 2株",
            "単価146.8200 / 手数料1.32 / 税金0.13 / 受渡295.09 USD",
            "price=295.09/2=147.545 (fee-inclusive)",
            "為替レート=159.550",
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
    if spec.new_owner:
        payload["owner"] = spec.new_owner
    if spec.new_broker:
        payload["broker"] = spec.new_broker
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
    if spec.owner:
        payload["owner"] = spec.owner
    if spec.broker:
        payload["broker"] = spec.broker
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

    # KNOWN_MISSING_TRADE_EVENTS runs first and each synthesized row is folded
    # into ``by_id`` immediately, so a KNOWN_CORRECTIONS spec targeting an
    # event_id that only comes into existence via synthesis (e.g. an
    # owner/broker attribution for a "manual_opening_*" opening lot) can still
    # find it in the same pass. Processing corrections first would silently
    # skip those as "missing" on a fresh database, since ``by_id`` was a
    # snapshot taken before the missing event existed -- correct on any
    # database that already had the row (production, at the time this was
    # written), but wrong on a from-scratch rebuild or restore.
    for spec in KNOWN_MISSING_TRADE_EVENTS:
        if spec.event_id in by_id:
            skipped.append({"event_id": spec.event_id, "reason": "already_present"})
            continue
        if spec.required_event_ids and not all(event_id in by_id for event_id in spec.required_event_ids):
            continue
        missing_row = _build_missing_event(spec)
        updates.append(missing_row)
        by_id[spec.event_id] = missing_row

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
