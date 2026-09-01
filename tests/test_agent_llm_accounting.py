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


_UNSET = object()


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    def __init__(self, name: str, input):
        self.name = name
        self.input = input


def _install_fake_sdk(monkeypatch, *, blocks, result, subtype="success", cost=0.0123,
                      structured=_UNSET):
    class AssistantMessage:
        def __init__(self):
            self.content = blocks

    class ResultMessage:
        pass

    ResultMessage.subtype = subtype
    ResultMessage.result = result
    ResultMessage.total_cost_usd = cost
    # 実 SDK は --json-schema を渡したとき structured_output に dict を入れる。
    # ホストはそちらを優先し、欠損なら fail-closed にする。
    if structured is not _UNSET:
        ResultMessage.structured_output = structured
    elif isinstance(result, str):
        try:
            ResultMessage.structured_output = json.loads(result)
        except json.JSONDecodeError:
            ResultMessage.structured_output = None
    else:
        ResultMessage.structured_output = None

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
    # default モードの action_scope は正式 priority_actions から作られる。
    # 空にすると候補ゼロになり、Agent へ渡すものが無くなる。
    from datetime import datetime, timezone
    _write("ai_portfolio_analysis.json", {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "synthesis": {"overall_stance": "neutral", "priority_actions": [
            {"ticker": "VT", "type": "buy", "execution_readiness": "review"},
        ]}})
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
    # 実ツール無し。StructuredOutputのschema修正だけ1回再試行できる。
    assert row["max_turns"] == 2
    assert row["structured_output_transport_seen"] is False
    assert row["forbidden_tool_use_seen"] is False


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
    assert rows[-1]["forbidden_tool_use_seen"] is True
    assert rows[-1]["structured_output_transport_seen"] is False
    # 違反時は保存しない。
    assert not (sandbox / "agent_briefing.json").exists()


def test_structured_output_transport_is_audited_separately(monkeypatch, sandbox):
    """構造化出力の搬送は実ツール使用ではないが、監査上は観測できる。

    2026-09-01 の本番失敗はこの搬送に対する schema error 後、修正ターンを
    max_turns=1 が止めたものだった。success 行にも搬送の有無を残すことで、
    禁止ツール使用と混同せず運用確認できる。
    """
    rows: list[dict] = []
    _install_fake_sdk(
        monkeypatch,
        blocks=[_ToolUseBlock("StructuredOutput", {"synthetic": True})],
        result=_valid_agent_output(sandbox),
    )
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: done" in chunk for chunk in chunks)
    assert rows[-1]["status"] == "success"
    assert rows[-1]["structured_output_transport_seen"] is True
    assert rows[-1]["forbidden_tool_use_seen"] is False
    assert rows[-1]["use_tool"] is False


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


def test_a_missing_structured_output_is_fail_closed(monkeypatch, sandbox):
    """structured_output が無ければ保存しない。

    result 文字列を当てにすると、スキーマが実際には効いていないとき
    (SDK へ渡す形を間違えている等) に自由形式のテキストを受け入れてしまう
    (Codex レビュー round 12: output_format の渡し方が違い --json-schema が
    付いていなかった)。
    """
    rows: list[dict] = []
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result="自由形式のテキストです", structured=None)
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: error" in chunk for chunk in chunks)
    assert rows[-1]["status"] == "output_rejected"
    assert not (sandbox / "agent_briefing.json").exists()


def test_a_known_cost_survives_being_logged_alongside_an_error(monkeypatch, sandbox):
    """判明済みのコストを、エラー記録が 0.0 で上書きしない。

    output_rejected は ResultMessage の後なので実コストを持つ。以前は
    error 引数がある行を無条件で cost_usd=0.0 に上書きしており、
    「不明」と「既知の0円」の区別も、既知のコスト自体も失っていた
    (レビューで実測)。
    """
    rows: list[dict] = []
    _install_fake_sdk(
        monkeypatch, blocks=[_TextBlock("analysis")], cost=0.0456,
        # projection に無い銘柄を提案してくる → output_rejected。
        result=json.dumps({
            "headline": "h", "overall_stance": "neutral", "risk_warnings": [],
            "actions": [{"rank": 1, "candidate_id": "candidate:FABRICATED",
                         "action_type": "buy", "actionability": "review",
                         "reason": "r"}],
        }),
    )
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    asyncio.run(_collect_agent_chunks("default"))

    row = rows[-1]
    assert row["status"] == "output_rejected"
    assert row["cost_usd"] == 0.0456, "判明済みのコストが 0.0 へ上書きされた"


def test_a_cost_unknown_before_any_result_is_not_reported_as_zero(monkeypatch, sandbox):
    """ResultMessage が届く前の失敗はコスト不明。0円と断定しない。"""
    rows: list[dict] = []
    _install_fake_sdk(
        monkeypatch,
        blocks=[_TextBlock("analysis"), _ToolUseBlock("Read", {"file": "x"})],
        result=_valid_agent_output(sandbox),
    )
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)

    asyncio.run(_collect_agent_chunks("default"))

    row = rows[-1]
    assert row["status"] == "protocol_violation"
    assert "cost_usd" not in row
    assert row.get("cost_status") == "unknown"


def test_a_persistence_failure_after_a_known_cost_still_logs_a_row(monkeypatch, sandbox):
    """検証を通った後の保存失敗も、既知のコストを持つ run として記録する。

    以前は save_verified_result() の例外を素通しにしており、課金確定後の
    保存失敗が「何も記録されない run」として消えていた
    (レビューで OSError 注入により再現)。
    """
    rows: list[dict] = []
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result=_valid_agent_output(sandbox), cost=0.0789)
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row),
                        raising=False)
    monkeypatch.setattr(
        agent, "save_verified_result",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: error" in chunk for chunk in chunks)
    assert not any("event: done" in chunk for chunk in chunks)
    assert rows, "保存失敗が会計ログに1行も残らなかった"
    row = rows[-1]
    assert row["status"] == "persistence_error"
    assert row["cost_usd"] == 0.0789, "確定済みのコストが保存失敗で失われた"


def test_a_true_dual_write_failure_still_surfaces_cost_via_sse(monkeypatch, sandbox):
    """真のディスク障害では、保存とログ書き込みが同時に失敗しうる。

    以前は analyst.llm_client._append_llm_call_log が自分の例外を握り潰して
    正常終了したように見せていたため、api/routes/agent.py 側の
    try/except は常に「成功」と判定していた。結果、既に確定した cost_usd が
    SSE にも監査ログにもどこにも残らなかった (レビューで実測)。

    ここでは監査ログの書き込み先を存在しないパスへ差し替えて、両方の
    書き込みが実際に失敗する状況を再現する。
    """
    import analyst.llm_client as llm_client

    rows: list[dict] = []
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result=_valid_agent_output(sandbox), cost=0.0789)
    monkeypatch.setattr(agent, "save_verified_result",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(llm_client, "_DEFAULT_LOG_PATH",
                        Path("/nonexistent_root_disk_full/x.jsonl"))
    # 会計ログの実体は書けないので rows は空のまま —— それでも SSE には
    # cost_usd/model/status が残ることを検証する。
    monkeypatch.setattr(agent, "_append_llm_call_log",
                        lambda row: llm_client._append_llm_call_log(row))

    chunks = asyncio.run(_collect_agent_chunks("default"))

    error_chunks = [c for c in chunks if "event: error" in c]
    assert error_chunks, "エラーイベントが1つも出なかった"
    assert "0.0789" in error_chunks[-1], (
        "監査ログが書けないとき、SSE にコストが残らなかった")
    assert "persistence_error" in error_chunks[-1]
    assert "claude-sonnet-5" in error_chunks[-1] or "sonnet" in error_chunks[-1]


def test_the_underlying_logger_reports_write_failure_truthfully(tmp_path, monkeypatch):
    """analyst.llm_client._append_llm_call_log が実際の書き込み成否を返す。

    以前は例外を握り潰すだけで戻り値も無く (-> None)、呼び出し側は
    「書けたかどうか」を一切判定できなかった。
    """
    import analyst.llm_client as llm_client

    monkeypatch.setattr(llm_client, "_DEFAULT_LOG_PATH",
                        tmp_path / "ok" / "log.jsonl")
    assert llm_client._append_llm_call_log({"role": "test"}) is True

    monkeypatch.setattr(llm_client, "_DEFAULT_LOG_PATH",
                        Path("/nonexistent_root_disk_full/x.jsonl"))
    assert llm_client._append_llm_call_log({"role": "test"}) is False


def test_a_non_success_result_message_sse_carries_the_status_field(monkeypatch, sandbox):
    """非success の ResultMessage も、他の失敗系 SSE と同じ "status" キーで
    会計行の状態を運ぶ。error フィールド (message.subtype と同値) だけでは
    キー名が他と揃わず、UI 側の統一的な扱いが崩れる (レビューで指摘)。
    """
    _install_fake_sdk(monkeypatch, blocks=[_TextBlock("analysis")],
                      result=_valid_agent_output(sandbox), subtype="error_max_turns",
                      cost=0.0234)
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: True, raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    done_chunks = [c for c in chunks if "event: done" in c]
    assert done_chunks
    assert '"status": "error_max_turns"' in done_chunks[-1]
    assert '"cost_usd": 0.0234' in done_chunks[-1]
