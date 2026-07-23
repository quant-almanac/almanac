import json

import backup_manager
import kabu_mini_eligibility as kme


def test_missing_kabu_mini_ledger_is_not_eligible(tmp_path):
    assert kme.is_kabu_mini_eligible("7203.T", ledger_path=tmp_path / "missing.json") is False


def test_kabu_mini_ledger_confirms_open_channel(tmp_path):
    ledger = tmp_path / "kabu_mini_eligible.json"
    ledger.write_text(json.dumps({
        "updated_at": "2026-07-01",
        "source": "manual_super_screener_export",
        "tickers": {
            "7203.T": {"eligible": True, "channels": ["open"]},
            "6861.T": {"eligible": True, "channels": ["open", "realtime"]},
            "9999.T": {"eligible": False},
        },
    }), encoding="utf-8")

    assert kme.is_kabu_mini_eligible("7203.T", channel="rakuten_kabu_mini_open", ledger_path=ledger) is True
    assert kme.is_kabu_mini_eligible("7203.T", channel="rakuten_kabu_mini_realtime", ledger_path=ledger) is False
    assert kme.is_kabu_mini_eligible("6861.T", channel="rakuten_kabu_mini_realtime", ledger_path=ledger) is True
    assert kme.is_kabu_mini_eligible("9999.T", channel="rakuten_kabu_mini_open", ledger_path=ledger) is False


def test_kabu_mini_ledger_is_backed_up():
    assert "data/kabu_mini_eligible.json" in backup_manager.TARGETS
    assert "data/kabu_mini_verification_needed.json" in backup_manager.TARGETS


def test_record_kabu_mini_verification_needed_merges_by_ticker_channel_action(tmp_path):
    path = tmp_path / "kabu_mini_verification_needed.json"
    kme.record_kabu_mini_verification_needed(
        [{
            "ticker": "7203.T",
            "requested_channel": "rakuten_kabu_mini_open",
            "action_type": "buy",
            "estimated_notional_jpy": 5_000,
        }],
        path=path,
        now="2026-07-01T09:00:00+09:00",
    )
    kme.record_kabu_mini_verification_needed(
        [{
            "ticker": "7203.T",
            "requested_channel": "rakuten_kabu_mini_open",
            "action_type": "buy",
            "estimated_notional_jpy": 90_000,
            "reason": "too_small",
        }],
        path=path,
        now="2026-07-01T10:00:00+09:00",
    )

    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["updated_at"] == "2026-07-01T10:00:00+09:00"
    assert len(saved["items"]) == 1
    assert saved["items"][0]["ticker"] == "7203.T"
    assert saved["items"][0]["estimated_notional_jpy"] == 90_000
    assert saved["items"][0]["last_seen_at"] == "2026-07-01T10:00:00+09:00"
