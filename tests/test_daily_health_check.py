import json

import daily_health_check as dhc
import scenario_strategy


def test_load_total_cash_prefers_account_json(tmp_path, monkeypatch):
    monkeypatch.setattr(dhc, "BASE", tmp_path)
    (tmp_path / "account.json").write_text(json.dumps({
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 150,
        "total_cash": 250_000,
    }), encoding="utf-8")

    assert dhc._load_total_cash_jpy() == 250_000


def test_load_total_cash_recomputes_when_account_total_cash_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(dhc, "BASE", tmp_path)
    (tmp_path / "account.json").write_text(json.dumps({
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 151.25,
        "total_cash": 249_000,
    }), encoding="utf-8")

    assert dhc._load_total_cash_jpy() == 251_250


def test_margin_buy_with_cash_allowed_in_bull_attack_mode(monkeypatch):
    monkeypatch.setattr(scenario_strategy, "get_strategy", lambda: {
        "scenario": "BULL",
        "leverage_allowed": True,
        "cash_ratio_target": 0,
    })

    assert dhc._margin_buy_allowed_with_cash() is True


def test_margin_buy_with_cash_not_allowed_outside_attack_mode(monkeypatch):
    monkeypatch.setattr(scenario_strategy, "get_strategy", lambda: {
        "scenario": "NEUTRAL",
        "leverage_allowed": False,
        "cash_ratio_target": 15,
    })

    assert dhc._margin_buy_allowed_with_cash() is False


def test_load_primary_analysis_prefers_ai_portfolio_synthesis(tmp_path, monkeypatch):
    monkeypatch.setattr(dhc, "BASE", tmp_path)
    (tmp_path / "agent_briefing.json").write_text(json.dumps({
        "priority_actions": [],
        "headline": "legacy",
    }), encoding="utf-8")
    (tmp_path / "ai_portfolio_analysis.json").write_text(json.dumps({
        "synthesis": {
            "priority_actions": [{"ticker": "NVDA", "type": "buy"}],
            "summary": "current",
        }
    }), encoding="utf-8")

    payload, path, age_h, error = dhc._load_primary_analysis()

    assert path.name == "ai_portfolio_analysis.json"
    assert payload["summary"] == "current"
    assert payload["priority_actions"][0]["ticker"] == "NVDA"
    assert age_h is not None
    assert error is None


def test_health_check_reports_issues_but_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dhc, "BASE", tmp_path)
    monkeypatch.setattr(dhc, "_send_telegram", lambda msg: None)
    monkeypatch.setattr(dhc, "_margin_buy_allowed_with_cash", lambda: False)

    (tmp_path / "ai_portfolio_analysis.json").write_text(json.dumps({
        "synthesis": {
            "priority_actions": [],
            "headline": "",
        }
    }), encoding="utf-8")

    rc = dhc.main()

    captured = capsys.readouterr().out
    assert rc == 0
    assert "priority_actions=[]" in captured


def test_recent_log_error_count_ignores_errors_before_latest_run(tmp_path, monkeypatch):
    log = tmp_path / "screener_log.txt"
    log.write_text(
        "\n".join([
            "OperationalError('unable to open database file')",
            "スクリーニング開始... (レジーム指定: 自動)",
            "[short] --us-only: 124 銘柄に絞込",
            "候補なし",
        ]),
        encoding="utf-8",
    )

    assert dhc._recent_log_error_count(log) == 0


def test_recent_log_error_count_counts_errors_after_latest_run(tmp_path):
    log = tmp_path / "screener_log.txt"
    log.write_text(
        "\n".join([
            "スクリーニング開始... (レジーム指定: 自動)",
            "[short] --us-only: 124 銘柄に絞込",
            "OperationalError('unable to open database file')",
        ]),
        encoding="utf-8",
    )

    assert dhc._recent_log_error_count(log) == 1
