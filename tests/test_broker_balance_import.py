"""
tests/test_broker_balance_import.py — 楽天資産残高CSVの現金同期 + 4 モード
"""
import json

import pytest

import broker_balance_import as bbi
import event_ledger as el


def _sample_csv(tmp_path):
    p = tmp_path / "assetbalance(all)_20260517_131924.csv"
    p.write_text(
        '"■資産合計欄"\n'
        '"預り金合計","6,921,068"\n'
        '"預り金","264,695"\n'
        '"外貨預り金","6,656,373"\n'
        '"■ 保有商品詳細 (すべて）"\n'
        '"種別","銘柄コード・ティッカー","銘柄","口座","保有数量","［単位］","平均取得価額","［単位］","現在値","［単位］","現在値(更新日)","(参考為替)","前日比","［単位］","時価評価額[円]","時価評価額[外貨]","評価損益[円]","評価損益[％]"\n'
        '"外貨建MMF","","GS米ドルファンド","特定","401,252","口","16,026.08","円","158.77","円/USD","","","-","-","638,060","4,018.77 USD","-4,990","-0.77"\n'
        '"外貨預り金","","米ドル","-","41,924.63","USD","-","-","158.77","円/USD","","","-","-","6,656,373.00","-","-","-"\n'
        '"■参考為替レート"\n'
        '"米ドル","158.77","円/USD","(05/16  05:59)"\n',
        encoding="cp932",
    )
    return p


def _write_state(tmp_path):
    account = tmp_path / "account.json"
    holdings = tmp_path / "holdings.json"
    account.write_text(json.dumps({
        "balance": 9000,
        "usd_balance": 7684.0,
        "fx_rate_usdjpy": 150.5,
        "jpy_equivalent_usd": 1199088,
        "total_cash": 1208088,
    }), encoding="utf-8")
    holdings.write_text(json.dumps({
        "CASH_JPY": {"shares": 9000},
        "CASH_USD": {"shares": 49106.22},
        "CASH_JPY_SBI": {"shares": 195151},
        "GS_MMF_USD": {"shares": 4016.7, "current_nav": 1.0},
    }), encoding="utf-8")
    return account, holdings


def test_parse_rakuten_asset_balance_csv(tmp_path):
    parsed = bbi.parse_rakuten_asset_balance(_sample_csv(tmp_path))
    assert parsed["as_of"] == "2026-05-17"
    assert parsed["rakuten_jpy_cash"] == 264695
    assert parsed["rakuten_usd_cash"] == 41924.63
    assert parsed["rakuten_usd_cash_jpy"] == 6656373
    assert parsed["rakuten_cash_total_jpy"] == 6921068
    assert parsed["fx_rate_usdjpy"] == 158.77
    assert parsed["gs_mmf_usd_value"] == 4018.77


def test_build_reconciled_state_updates_cash_mirrors(tmp_path):
    account, holdings = _write_state(tmp_path)
    parsed = bbi.parse_rakuten_asset_balance(_sample_csv(tmp_path))
    next_account, next_holdings, diff = bbi.build_reconciled_state(
        rakuten=parsed,
        sbi_jpy=195151,
        sbi_note="SBIスクリーンショット同期 2026-05-17",
        account_path=account,
        holdings_path=holdings,
    )

    assert next_account["balance"] == 264695
    assert next_account["usd_balance"] == 41924.63
    assert next_account["fx_rate_usdjpy"] == 158.77
    assert next_account["jpy_equivalent_usd"] == 6656374
    assert next_account["total_cash"] == 6921069
    assert next_holdings["CASH_JPY"]["shares"] == 264695
    assert next_holdings["CASH_USD"]["shares"] == 41924.63
    assert next_holdings["CASH_JPY_SBI"]["shares"] == 195151
    assert next_holdings["GS_MMF_USD"]["shares"] == 4018.77
    assert diff["before"]["holdings"]["CASH_USD"] == 49106.22


# ────────────────────────────────────────────────────────────────
# 4-mode tests (Codex 2026-05-17)
# ────────────────────────────────────────────────────────────────

def _diff(before_jpy_r, before_usd_r, before_jpy_sbi, after_jpy_r, after_usd_r, after_jpy_sbi, fx=150.0):
    """_compute_cash_deltas に食わせる diff 構造を作る。"""
    return {
        "before": {
            "account":  {"balance": before_jpy_r, "usd_balance": before_usd_r, "fx_rate_usdjpy": fx},
            "holdings": {"CASH_JPY_SBI": before_jpy_sbi},
        },
        "after": {
            "account":  {"balance": after_jpy_r, "usd_balance": after_usd_r, "fx_rate_usdjpy": fx},
            "holdings": {"CASH_JPY_SBI": after_jpy_sbi},
        },
        "rakuten": {"as_of": "2026-05-17"},
        "sbi_jpy": after_jpy_sbi,
    }


def test_compute_cash_deltas_basic():
    d = _diff(100000, 0, 50000, 110000, 0, 40000, fx=150.0)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    # Rakuten +10k, SBI -10k → 合計 0
    assert deltas["delta_jpy_rakuten"] == 10000
    assert deltas["delta_jpy_sbi"] == -10000
    assert deltas["delta_jpy_total"] == 0
    assert deltas["delta_usd_total"] == 0
    assert deltas["net_delta_jpy_equivalent"] == 0


def test_mode_reset_creates_reconcile_event_twr_neutral():
    d = _diff(100000, 0, 50000, 110000, 0, 40000)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    events = bbi._build_ledger_events_for_mode(
        mode="reset", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "reconcile"
    # reconcile は cash_flow ではない → TWR 中立
    assert events[0]["event_type"] != "cash_flow"


def test_mode_internal_transfer_passes_when_net_zero():
    # SBI -100k, Rakuten +100k → net 0
    d = _diff(0, 0, 100000, 100000, 0, 0)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    events = bbi._build_ledger_events_for_mode(
        mode="internal_transfer", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "internal_transfer"
    # internal_transfer は cash_flow ではない → TWR 中立
    assert events[0]["event_type"] != "cash_flow"


def test_mode_internal_transfer_rejects_when_net_too_large():
    # Rakuten +100k, SBI 動かず → net +100k (内部移動ではない)
    d = _diff(0, 0, 50000, 100000, 0, 50000)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    with pytest.raises(ValueError, match="net delta が大きすぎます"):
        bbi._build_ledger_events_for_mode(
            mode="internal_transfer", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
        )


def test_mode_external_deposit_creates_cash_flow_in():
    # Rakuten +500k, SBI 不変 → 外部入金 +500k JPY
    d = _diff(100000, 0, 50000, 600000, 0, 50000)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    events = bbi._build_ledger_events_for_mode(
        mode="external_deposit", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "cash_flow"
    assert events[0]["direction"] == "in"
    assert events[0]["currency"] == "JPY"
    assert events[0]["quantity"] == 500000


def test_mode_external_deposit_creates_both_jpy_and_usd_events():
    # Rakuten +300k JPY + 1000 USD 同時入金
    d = _diff(100000, 5000, 50000, 400000, 6000, 50000, fx=150.0)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    events = bbi._build_ledger_events_for_mode(
        mode="external_deposit", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
    )
    assert len(events) == 2
    currencies = sorted(ev["currency"] for ev in events)
    assert currencies == ["JPY", "USD"]
    usd_event = next(e for e in events if e["currency"] == "USD")
    assert usd_event["fx_rate_usdjpy"] == 150.0
    assert usd_event["quantity"] == 1000


def test_mode_external_deposit_rejects_negative_delta():
    # mode=external_deposit なのに残高が減っている → error
    d = _diff(500000, 0, 50000, 100000, 0, 50000)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    with pytest.raises(ValueError, match="逆方向"):
        bbi._build_ledger_events_for_mode(
            mode="external_deposit", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
        )


def test_mode_external_withdraw_creates_cash_flow_out():
    # Rakuten -200k → 外部出金
    d = _diff(500000, 0, 50000, 300000, 0, 50000)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    events = bbi._build_ledger_events_for_mode(
        mode="external_withdraw", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
    )
    assert len(events) == 1
    assert events[0]["direction"] == "out"
    assert events[0]["quantity"] == 200000


def test_mode_unknown_raises():
    d = _diff(0, 0, 0, 0, 0, 0)
    deltas = bbi._compute_cash_deltas(d["before"], d["after"])
    with pytest.raises(ValueError, match="unknown mode"):
        bbi._build_ledger_events_for_mode(
            mode="bogus", diff=d, deltas=deltas, occurred_at="2026-05-17T16:00:00"
        )


def test_internal_transfer_is_twr_neutral_via_cash_flow_sum(tmp_path, monkeypatch):
    """internal_transfer event を ledger に記録しても cash_flow_sum_jpy が 0 のまま。"""
    db = tmp_path / "test_internal.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)

    # internal_transfer event を直接記録 (新規 schema で valid のはず)
    el.append_event(
        event_type="internal_transfer",
        occurred_at="2026-05-17T16:00:00",
        source="broker_import",
        note="SBI→楽天 195k",
        raw_payload={"net_delta_jpy_equivalent": 0},
        event_id="bbi_internal_test",
        db_path=db,
    )
    el.append_event(
        event_type="reconcile",
        occurred_at="2026-05-17T16:01:00",
        source="broker_import",
        note="楽天残高補正",
        raw_payload={"reason": "drift"},
        event_id="bbi_reconcile_test",
        db_path=db,
    )

    # cash_flow_sum_jpy は 0 (internal_transfer / reconcile は controlled out しない)
    total = el.cash_flow_sum_jpy(
        date_from="2026-05-01", date_to="2026-05-31", db_path=db,
    )
    assert total == 0.0

    # ただし audit としては記録されている
    events = el.query_events(types=["internal_transfer", "reconcile"], db_path=db)
    assert len(events) == 2


# Codex P1 #9 — prepare/commit journal で部分反映を検知・防止する

def test_journal_detects_incomplete_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(bbi, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    bbi._append_journal({"operation_id": "op1", "status": "prepared"})
    bbi._append_journal({"operation_id": "op1", "status": "committed"})
    bbi._assert_no_incomplete_journal()  # committed → 例外なし
    bbi._append_journal({"operation_id": "op2", "status": "prepared"})
    with pytest.raises(RuntimeError, match="完了していません"):
        bbi._assert_no_incomplete_journal()


def test_apply_writes_prepared_then_committed_journal(tmp_path, monkeypatch):
    account, holdings = _write_state(tmp_path)
    monkeypatch.setattr(bbi, "ACCOUNT_FILE", account)
    monkeypatch.setattr(bbi, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(bbi, "RECONCILE_LOG", tmp_path / "rec.jsonl")
    monkeypatch.setattr(bbi, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    db = tmp_path / "t.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)

    res = bbi.apply_reconcile(rakuten_csv=_sample_csv(tmp_path), sbi_jpy=195151,
                              apply=True, mode="reset")
    assert res["dry_run"] is False
    lines = [json.loads(l) for l in (tmp_path / "journal.jsonl").read_text().splitlines() if l.strip()]
    statuses = [r["status"] for r in lines]
    assert "prepared" in statuses and "committed" in statuses
    assert len({r["operation_id"] for r in lines}) == 1  # 同一 operation

    # 直前が committed なら次の apply は停止しない
    res2 = bbi.apply_reconcile(rakuten_csv=_sample_csv(tmp_path), sbi_jpy=195151,
                               apply=True, mode="reset")
    assert res2["dry_run"] is False


def test_resume_recovers_incomplete_op_without_dataloss(tmp_path, monkeypatch):
    """Codex P2 #9: prepared のまま落ちた op を記録 plan で resume し、欠落しかけた
    cash_flow を idempotent に反映する。"""
    account, holdings = _write_state(tmp_path)
    monkeypatch.setattr(bbi, "ACCOUNT_FILE", account)
    monkeypatch.setattr(bbi, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(bbi, "RECONCILE_LOG", tmp_path / "rec.jsonl")
    monkeypatch.setattr(bbi, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    db = tmp_path / "t.db"
    monkeypatch.setattr(el, "DB_PATH", db)
    el.init_schema(db)

    plan = {
        "operation_id": "opX", "mode": "external_deposit",
        "next_account": json.loads(account.read_text(encoding="utf-8")),
        "next_holdings": json.loads(holdings.read_text(encoding="utf-8")),
        "ledger_events": [{
            "event_type": "cash_flow", "occurred_at": "2026-05-17T16:00:00",
            "direction": "in", "quantity": 50000, "price": 1.0, "currency": "JPY",
            "source": "broker_import", "note": "resume test", "event_id": "opX:0",
        }],
    }
    bbi._append_journal({"operation_id": "opX", "status": "prepared",
                         "mode": "external_deposit", "plan": plan})

    assert el.cash_flow_sum_jpy(date_from="2026-05-01", date_to="2026-05-31", db_path=db) == 0.0
    bbi._resume_incomplete_journal()
    assert el.cash_flow_sum_jpy(date_from="2026-05-01", date_to="2026-05-31", db_path=db) == 50000.0
    assert bbi._read_last_journal_record()["status"] == "committed"

    # 二重 resume しても二重計上しない (決定論 event_id で dedup)
    bbi._resume_incomplete_journal()
    assert el.cash_flow_sum_jpy(date_from="2026-05-01", date_to="2026-05-31", db_path=db) == 50000.0


def test_operation_id_is_deterministic_from_input():
    """同一入力 → 同一 operation_id (UUID ではない)。"""
    a = bbi._operation_id(mode="reset", rakuten={"x": 1}, sbi_jpy=100.0)
    b = bbi._operation_id(mode="reset", rakuten={"x": 1}, sbi_jpy=100.0)
    c = bbi._operation_id(mode="reset", rakuten={"x": 2}, sbi_jpy=100.0)
    assert a == b
    assert a != c
