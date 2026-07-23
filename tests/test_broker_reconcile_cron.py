from pathlib import Path

import broker_reconcile as br
import broker_reconcile_cron as cron


def test_reconcile_broker_notifies_parse_skips_without_ledger_diff(monkeypatch, tmp_path):
    csv = tmp_path / "rakuten_bad.csv"
    csv.write_text("dummy", encoding="utf-8")

    parse_report = br.ParseReport(
        broker="rakuten",
        rows_total=2,
        parsed=1,
        skipped=1,
        skip_reasons=[{"row": 2, "reason": "quantity parse error"}],
    )
    report = br.ReconcileReport(matched_count=1)
    sent = []

    monkeypatch.setattr(
        br,
        "parse_csv_with_report",
        lambda path, broker: ([], parse_report),
    )
    monkeypatch.setattr(
        br,
        "compare_to_ledger",
        lambda trades, **kwargs: report,
    )
    monkeypatch.setattr(cron, "_send_telegram", lambda message: sent.append(message) or True)

    summary = cron.reconcile_broker(
        "rakuten",
        csv,
        date_from="2026-06-01",
        date_to="2026-06-07",
        notify=True,
    )

    # ALMANAC: telegram disabled — ai_analysis only
    assert summary["has_discrepancy"] is True
    assert summary["skipped"] == 1
    assert summary["telegram_notified"] is False
    assert sent == []


def test_reconcile_broker_does_not_notify_when_clean(monkeypatch, tmp_path):
    csv = tmp_path / "rakuten_clean.csv"
    csv.write_text("dummy", encoding="utf-8")

    parse_report = br.ParseReport(broker="rakuten", rows_total=1, parsed=1, skipped=0)
    report = br.ReconcileReport(matched_count=1)
    sent = []

    monkeypatch.setattr(br, "parse_csv_with_report", lambda path, broker: ([], parse_report))
    monkeypatch.setattr(br, "compare_to_ledger", lambda trades, **kwargs: report)
    monkeypatch.setattr(cron, "_send_telegram", lambda message: sent.append(message) or True)

    summary = cron.reconcile_broker(
        "rakuten",
        Path(csv),
        date_from="2026-06-01",
        date_to="2026-06-07",
        notify=True,
    )

    assert summary["has_discrepancy"] is False
    assert "telegram_notified" not in summary
    assert sent == []
