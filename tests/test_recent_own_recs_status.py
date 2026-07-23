import json

import analyst


def _recommendation(*, day: str, action: str) -> dict:
    return {
        "as_of": f"{day}T06:22:33",
        "ticker": "AVGO",
        "type": "trim",
        "urgency": "low",
        "action": action,
    }


def test_recent_recommendation_is_expired_not_executed(monkeypatch, tmp_path):
    rec = _recommendation(
        day="2026-07-22",
        action="AVGO含み益ロットから8株を段階売却",
    )
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [rec])
    (tmp_path / "action_executions.json").write_text(
        json.dumps({"executions": []}),
        encoding="utf-8",
    )
    (tmp_path / "action_state.json").write_text(json.dumps({"actions": {
        "avgo-expired": {
            "ticker": "AVGO",
            "action_type": "trim",
            "action_detail": rec["action"],
            "recommended_at": "2026-07-22T06:22:35",
            "status": "expired",
        },
    }}), encoding="utf-8")

    prompt = analyst._format_recent_own_recs_for_prompt()

    assert "2026-07-22:trim(low) [expired・未約定]" in prompt
    assert "推奨を「実行済み」「売却済み」と表現してはならない" in prompt


def test_recent_recommendation_uses_execution_log_as_fill_truth(monkeypatch, tmp_path):
    rec = _recommendation(
        day="2026-06-24",
        action="AVGO 3株売却（特定口座・指値）",
    )
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [rec])
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "ticker": "AVGO",
            "direction": "sell",
            "action": rec["action"],
            "status": "executed",
            "saved_at": "2026-06-24T00:48:45",
        }],
    }), encoding="utf-8")
    (tmp_path / "action_state.json").write_text(
        json.dumps({"actions": {}}),
        encoding="utf-8",
    )

    prompt = analyst._format_recent_own_recs_for_prompt()

    assert "2026-06-24:trim(low) [約定済 06-24]" in prompt


def test_recent_recommendation_execution_log_failure_is_unknown(monkeypatch, tmp_path):
    rec = _recommendation(
        day="2026-07-22",
        action="AVGOを8株トリム",
    )
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [rec])
    (tmp_path / "action_executions.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "action_state.json").write_text(json.dumps({"actions": {
        "avgo-filled": {
            "ticker": "AVGO",
            "action_type": "trim",
            "action_detail": rec["action"],
            "recommended_at": "2026-07-22T06:22:35",
            "status": "filled",
        },
    }}), encoding="utf-8")

    prompt = analyst._format_recent_own_recs_for_prompt()

    assert "[状態不明・約定扱い禁止]" in prompt
    avgo_line = next(line for line in prompt.splitlines() if line.strip().startswith("AVGO:"))
    assert "[約定済 " not in avgo_line
