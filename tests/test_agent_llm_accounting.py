"""Agent SDK 経路のコスト会計と、ツール使用の扱い。

2026-08-25 に契約が変わった (Codex レビュー round 11):
以前は Agent に Read を許可しており、ToolUseBlock は SSE の "tool" イベント
として素通しされていた。今はツールを一切与えないので、ToolUseBlock が
1回でも返ったらプロトコル違反として失敗させる。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from api.routes import agent


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    def __init__(self, name: str, input):
        self.name = name
        self.input = input


def _install_fake_sdk(monkeypatch, *, blocks, result, subtype="success", cost=0.0123):
    class AssistantMessage:
        def __init__(self):
            self.content = blocks

    class ResultMessage:
        pass

    ResultMessage.subtype = subtype
    ResultMessage.result = result
    ResultMessage.total_cost_usd = cost

    async def fake_query(prompt, options):
        yield AssistantMessage()
        yield ResultMessage()

    fake_sdk = types.SimpleNamespace(
        query=fake_query,
        ClaudeAgentOptions=lambda **kw: types.SimpleNamespace(**kw),
        ResultMessage=ResultMessage,
        AssistantMessage=AssistantMessage,
    )
    fake_types = types.SimpleNamespace(TextBlock=_TextBlock, ToolUseBlock=_ToolUseBlock)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)


async def _collect_agent_chunks(mode: str) -> list[str]:
    return [chunk async for chunk in agent._run_agent(mode)]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """本番ファイルへ書かないよう BASE_DIR を差し替える。"""
    def _write(name, payload):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    _write("technical_state.json", {"tickers": {
        "VT": {"price": 160.0, "rsi": 57.0, "data_quality_status": "ok",
               "freshness_status": "fresh", "data_as_of": "2026-08-24"}}})
    _write("holdings.json", {"VT_row": {"ticker": "VT", "shares": 10.0}})
    _write("ai_portfolio_analysis.json", {"synthesis": {}})
    monkeypatch.setattr(agent, "BASE_DIR", tmp_path)
    return tmp_path


def _valid_agent_output(base_dir: Path) -> str:
    """検証を通る最小の構造化出力。candidate_id は projection から取る。"""
    import agent_projection as ap

    projection = ap.build_agent_projection("default", base_dir=base_dir)
    scope = projection["action_scope"][0]
    return json.dumps({
        "headline": "h",
        "overall_stance": "neutral",
        "risk_warnings": [],
        "actions": [{
            "rank": 1,
            "candidate_id": scope["candidate_id"],
            "action_type": scope["allowed_actions"][0],
            "actionability": "watch_only",
            "reason": "ok",
        }],
    })


def test_agent_sdk_result_logs_cost_accounting(monkeypatch, sandbox):
    rows: list[dict] = []
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result=_valid_agent_output(sandbox))
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: done" in chunk for chunk in chunks)
    assert rows, "Agent SDK runs should log ResultMessage cost for spend accounting"
    row = rows[-1]
    assert row["role"] == "agent_sdk_run"
    assert row["mode"] == "default"
    assert row["status"] == "success"
    assert row["cost_usd"] == 0.0123
    # ツール無し・1ターンの契約になった。
    assert row["max_turns"] == 1


def test_a_tool_use_block_is_a_protocol_violation(monkeypatch, sandbox):
    """ツールを与えていないので、使おうとした時点で失敗させる。

    以前はこれを SSE の "tool" イベントとして素通ししていた。素通しすると、
    SDK 側の既定が変わってツールが復活したときに黙って raw ファイルへ
    戻れてしまう。
    """
    rows: list[dict] = []
    _install_fake_sdk(
        monkeypatch,
        blocks=[_TextBlock("analysis"), _ToolUseBlock("Read", {"file": "x"})],
        result=_valid_agent_output(sandbox),
    )
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: error" in chunk for chunk in chunks)
    assert not any("event: done" in chunk for chunk in chunks)
    assert rows[-1]["status"] == "protocol_violation"
    # 違反時は保存しない。
    assert not (sandbox / "agent_briefing.json").exists()


def test_a_rejected_output_is_not_saved(monkeypatch, sandbox):
    """検証に落ちた出力は保存せず、last-known-good を残す。"""
    rows: list[dict] = []
    previous = {"headline": "last known good"}
    (sandbox / "agent_briefing.json").write_text(json.dumps(previous), encoding="utf-8")

    _install_fake_sdk(
        monkeypatch, blocks=[_TextBlock("analysis")],
        # projection に無い銘柄を提案してくる。
        result=json.dumps({
            "headline": "h", "overall_stance": "neutral", "risk_warnings": [],
            "actions": [{"rank": 1, "candidate_id": "candidate:FABRICATED",
                         "action_type": "buy", "actionability": "review",
                         "reason": "r"}],
        }),
    )
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: error" in chunk for chunk in chunks)
    assert rows[-1]["status"] == "output_rejected"
    saved = json.loads((sandbox / "agent_briefing.json").read_text(encoding="utf-8"))
    assert saved == previous, "検証に落ちた出力で last-known-good が上書きされた"


def test_a_verified_output_is_saved_with_the_projection_hash(monkeypatch, sandbox):
    """保存物は「どの projection を見て出した結論か」を持つこと。"""
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result=_valid_agent_output(sandbox))
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: None, raising=False)

    asyncio.run(_collect_agent_chunks("default"))

    saved = json.loads((sandbox / "agent_briefing.json").read_text(encoding="utf-8"))
    assert len(saved["projection_sha256"]) == 64
    assert saved["actions"][0]["ticker"] == "VT"
    assert saved["as_of"]
