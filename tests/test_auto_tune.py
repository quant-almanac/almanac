"""Safety-contract tests for the guarded Auto Tune runtime."""
from __future__ import annotations

import json
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest

import auto_tune as at
import tunable_params as tp
import utils


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    definitions = {
        "max_short_positions": {
            "value": 1, "default": 1, "min": 1, "max": 5, "step": 1,
            "integer": True, "type": "number", "auto_apply": True,
        },
        "currency_usd_target_pct": {
            "value": 65, "default": 65, "min": 50, "max": 80, "step": 1,
            "integer": True, "type": "number", "auto_apply": True,
        },
        "currency_jpy_target_pct": {
            "value": 35, "default": 35, "min": 20, "max": 50, "step": 1,
            "integer": True, "type": "number", "auto_apply": True,
        },
        "news_articles_per_ticker": {
            "value": 5, "default": 5, "min": 1, "max": 10, "step": 1,
            "integer": True, "type": "number", "auto_apply": True,
        },
    }
    policy = {
        "version": 2,
        "auto_apply_allowlist": list(definitions),
        "auto_apply_denylist": [],
        "risk_class": {
            "max_short_positions": "high",
            "currency_usd_target_pct": "medium",
            "currency_jpy_target_pct": "medium",
            "news_articles_per_ticker": "low",
        },
        "atomic_groups": [["currency_usd_target_pct", "currency_jpy_target_pct"]],
        "max_absolute_step": {
            "max_short_positions": 1,
            "currency_usd_target_pct": 3,
            "currency_jpy_target_pct": 3,
            "news_articles_per_ticker": 1,
        },
        "max_groups_per_risk_class": {"high": 1, "medium": 1, "low": 1},
        "cooldown_trading_days": 1,
        "freshness_hours": {"guard": 3, "vix": 4, "regime": 8, "macro": 12},
        "schedule": {"times": ["06:30"]},
    }
    params_file = tmp_path / "tunable_params.json"
    param_state = tmp_path / "tunable_params_state.json"
    history = tmp_path / "tunable_params_history.jsonl"
    mode_file = tmp_path / "tuning_auto_state.json"
    policy_file = tmp_path / "tuning_auto_policy.json"
    run_log = tmp_path / "tuning_auto_runs.jsonl"
    _write(params_file, definitions)
    _write(policy_file, policy)
    _write(mode_file, {"version": 2, "mode": "apply"})

    monkeypatch.setattr(tp, "PARAMS_FILE", params_file)
    monkeypatch.setattr(tp, "STATE_FILE", param_state)
    monkeypatch.setattr(tp, "HISTORY_FILE", history)
    monkeypatch.setattr(tp, "LOCK_FILE", tmp_path / "locks" / "params.lock")
    monkeypatch.setattr(at, "POLICY_FILE", policy_file)
    monkeypatch.setattr(at, "AUTO_MODE_FILE", mode_file)
    monkeypatch.setattr(at, "LEGACY_MODE_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(at, "RUN_LOG_FILE", run_log)
    monkeypatch.setattr(at, "HISTORY_FILE", history)
    monkeypatch.setattr(at, "LOG_FILE", tmp_path / "logs" / "auto_tune.log")
    monkeypatch.setattr(at, "PARAMS_FILE", params_file)
    monkeypatch.setattr(at, "_collect_input_health", lambda policy: {"ok": True, "sources": {}, "blockers": []})
    monkeypatch.setattr(utils, "process_lock", lambda name: nullcontext())
    monkeypatch.setattr(utils, "heartbeat", lambda *args, **kwargs: None)

    context = {
        "regime": "A_強気", "vix": 18.0, "daily_pnl_pct": 0.1, "monthly_pnl_pct": 2.0,
        "cash_ratio_pct": 30.0, "top_sector": "Cash", "top_sector_pct": 35.0,
        "recent_too_small_count": 2,
    }

    def install_advisor(recommendations):
        module = types.ModuleType("tuning_advisor")
        module.load_market_context = lambda: dict(context)
        module.generate_recommendations = lambda *, keys, market_context: {
            "recommendations": recommendations,
            "market_context": market_context,
        }
        monkeypatch.setitem(sys.modules, "tuning_advisor", module)

    def recommendations(**overrides):
        current = {key: row["value"] for key, row in tp.list_all().items()}
        proposed = {**current, **overrides}
        return [
            {"key": key, "current": current[key], "recommended": proposed[key], "rationale": f"test {key}"}
            for key in definitions
        ]

    return types.SimpleNamespace(
        tmp=tmp_path, definitions=definitions, policy=policy, mode=mode_file,
        run_log=run_log, context=context, install_advisor=install_advisor,
        recommendations=recommendations,
    )


def test_get_current_vix_supports_nested_and_flat(monkeypatch, tmp_path):
    _write(tmp_path / "vix_state.json", {"vix": {"level": 25.5}})
    monkeypatch.setattr(at, "BASE_DIR", tmp_path)
    assert at._get_current_vix() == pytest.approx(25.5)
    _write(tmp_path / "vix_state.json", {"level": 20.0})
    assert at._get_current_vix() == pytest.approx(20.0)


def test_force_cannot_apply_while_mode_off(runtime):
    _write(runtime.mode, {"version": 2, "mode": "off"})
    runtime.install_advisor(runtime.recommendations(max_short_positions=2))
    result = at.run(force=True)
    assert result["status"] == "disabled"
    assert tp.get("max_short_positions") == 1


def test_force_dry_run_is_allowed_off_and_does_not_consume_context(runtime):
    _write(runtime.mode, {"version": 2, "mode": "off"})
    runtime.install_advisor(runtime.recommendations(max_short_positions=2))
    result = at.run(dry_run=True, force=True)
    assert result["status"] == "dry_run"
    assert result["would_apply_count"] == 1
    assert tp.get("max_short_positions") == 1
    state = json.loads(runtime.mode.read_text())
    assert "last_evaluated_context_hash" not in state


def test_stale_input_blocks_before_advisor(runtime, monkeypatch):
    monkeypatch.setattr(at, "_collect_input_health", lambda policy: {
        "ok": False, "sources": {"vix": {"fresh": False}}, "blockers": ["vix"]
    })
    result = at.run()
    assert result["status"] == "blocked_stale_inputs"
    assert result["blockers"] == ["vix"]


def test_apply_changes_one_group_per_risk_class(runtime):
    runtime.install_advisor(runtime.recommendations(
        max_short_positions=2,
        currency_usd_target_pct=67,
        currency_jpy_target_pct=33,
        news_articles_per_ticker=6,
    ))
    result = at.run()
    assert result["status"] == "applied"
    assert result["applied_count"] == 4
    assert tp.get("max_short_positions") == 2
    assert tp.get("currency_usd_target_pct") == 67
    assert tp.get("currency_jpy_target_pct") == 33
    assert tp.get("news_articles_per_ticker") == 6
    assert runtime.run_log.exists()


def test_absolute_step_violation_is_rejected(runtime):
    runtime.install_advisor(runtime.recommendations(max_short_positions=4))
    result = at.run()
    assert result["status"] == "no_change"
    assert result["rejected_count"] == 1
    assert tp.get("max_short_positions") == 1


def test_same_context_skips_after_real_evaluation(runtime):
    runtime.install_advisor(runtime.recommendations())
    first = at.run()
    second = at.run()
    assert first["status"] == "no_change"
    assert second["status"] == "skipped_same_context"


def test_missing_allowlisted_recommendation_fails_closed(runtime):
    recommendations = runtime.recommendations()[:-1]
    runtime.install_advisor(recommendations)
    result = at.run()
    assert result["status"] == "failed"
    assert "missing allowlisted recommendations" in result["error"]


def test_pair_invariant_requires_atomic_currency_update(runtime):
    with pytest.raises(tp.TuningValidationError, match="合計は100"):
        tp.apply_batch({"currency_usd_target_pct": 66})
    tp.apply_batch({"currency_usd_target_pct": 66, "currency_jpy_target_pct": 34})
    assert tp.get("currency_usd_target_pct") == 66
    assert tp.get("currency_jpy_target_pct") == 34


def test_compare_and_set_rejects_concurrent_change(runtime):
    with pytest.raises(tp.TuningConflictError):
        tp.apply_batch({"max_short_positions": 2}, expected_values={"max_short_positions": 3})


def test_rollback_restores_applied_values(runtime):
    runtime.install_advisor(runtime.recommendations(max_short_positions=2))
    applied = at.run()
    rollback = at.rollback_run(applied["run_id"], actor="test")
    assert rollback["status"] == "rolled_back"
    assert tp.get("max_short_positions") == 1


def test_rollback_refuses_when_value_changed_after_run(runtime):
    runtime.install_advisor(runtime.recommendations(max_short_positions=2))
    applied = at.run()
    tp.set_value("max_short_positions", 3, source="user")
    with pytest.raises(tp.TuningConflictError):
        at.rollback_run(applied["run_id"], actor="test")


def test_runtime_state_keeps_definition_file_unchanged(runtime):
    before = runtime.definitions
    tp.set_value("news_articles_per_ticker", 6, source="user")
    assert json.loads((runtime.tmp / "tunable_params.json").read_text()) == before
    assert (runtime.tmp / "tunable_params_state.json").exists()


def test_audit_accepts_consistent_runtime(runtime):
    runtime.install_advisor(runtime.recommendations(max_short_positions=2))
    at.run()
    audit = at.audit_state_consistency()
    assert audit["status"] == "ok"
    assert audit["allowlist_count"] == 4


def test_audit_honors_explicit_legacy_reconciliation(runtime):
    tp.set_value("max_short_positions", 2, source="ai_auto", rationale="old",)
    tp.set_value("max_short_positions", 1, source="user", rationale="canonical")
    rows = [json.loads(line) for line in (runtime.tmp / "tunable_params_history.jsonl").read_text().splitlines()]
    ai_row = next(row for row in rows if row["source"] == "ai_auto")
    # Recreate the legacy shape where the latest recorded ai_auto value differs
    # from the reviewed canonical value and has been explicitly reconciled.
    (runtime.tmp / "tunable_params_history.jsonl").write_text(json.dumps(ai_row) + "\n", encoding="utf-8")
    _write(runtime.mode, {
        "version": 2,
        "mode": "off",
        "audit_reconciliation": {
            "canonical_source": "tunable_params.json",
            "reconciled_history_mismatches": [{
                "key": "max_short_positions",
                "canonical_value": 1,
                "latest_history_value": 2,
                "history_timestamp": ai_row["timestamp"],
            }],
        },
    })
    audit = at.audit_state_consistency()
    assert audit["status"] == "ok"
    assert audit["reconciled_count"] == 1


def test_set_mode_requires_known_mode(runtime):
    with pytest.raises(ValueError):
        at.set_mode("unsafe")
    state = at.set_mode("shadow", actor="test")
    assert state["mode"] == "shadow"
    assert state["disabled_reason"] is None
