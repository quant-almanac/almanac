"""Agent へ渡す sanitized projection と、その出力のホスト側検証。

背景 (Codex レビュー round 11):
CLI (portfolio_agent.py) と API (api/routes/agent.py) は、どちらも Agent に
作業ディレクトリの絶対パスと読むべきファイル名を渡し、Read (CLI は
Read/Write/Bash) を許可していた。つまり Agent は raw の technical_state.json
を読めて品質契約を迂回でき、holdings.json の note / owner / broker / account
まで見えていた。出力の検証もホスト側に無かった。

プロンプトのファイル名を変えるだけでは足りない —— ツールが残っていれば
raw ファイルへ戻れる。ここでは「入力が projection だけであること」
「漏れてはいけないものが漏れないこと」「Agent が projection の外へ出られない
こと」を固定する。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent_projection as ap


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _write(base: Path, name: str, payload) -> None:
    (base / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def base_dir(tmp_path):
    """本番と同じ形の最小データ。holdings は漏らしてはいけない
    フィールドを**わざと**全部持たせる。"""
    _write(tmp_path, "technical_state.json", {"tickers": {
        "VT": {"price": 160.0, "rsi": 57.0, "change_5d_pct": -0.9,
               "change_20d_pct": 4.2, "composite_signal": "mildly_bearish",
               "data_quality_status": "ok", "freshness_status": "fresh",
               "data_as_of": "2026-08-24"},
        "BAD": {"price": 1.0, "rsi": 10.0, "change_5d_pct": -20.0,
                "composite_signal": "bearish",
                "data_quality_status": "blocked", "freshness_status": "fresh",
                "data_as_of": "2026-08-01"},
    }})
    _write(tmp_path, "holdings.json", {
        "VT_row": {
            "ticker": "VT", "shares": 10.0, "currency": "USD",
            "asset_type": "fund", "investment_type": "long",
            # ↓ どれも projection へ出てはいけない
            "note": "楽天CSV保有同期 2026-08-24",
            "owner": "husband", "broker": "楽天証券", "account": "特定",
            "reconciliation_snapshot_hash": "deadbeef" * 8,
            "broker_total_cost_basis_jpy": 1234567,
        },
        "BAD_row": {"ticker": "BAD", "shares": 1.0, "currency": "USD"},
    })
    _write(tmp_path, "ai_portfolio_analysis.json", {
        "synthesis": {"overall_stance": "neutral",
                      "priority_actions": [{"ticker": "NEWONE"}]}})
    _write(tmp_path, "guard_state.json", {"daily_pnl_pct": -1.0, "monthly_pnl_pct": 2.0,
                                          "portfolio_value": 30_000_000,
                                          "short_positions": ["secret"]})
    _write(tmp_path, "macro_state.json", {"fed_rate": 4.0, "yield_10y": 4.2,
                                          "unemp_rate": 4.1, "internal_note": "INTERNALNOTEMARKER"})
    _write(tmp_path, "nisa_portfolio.json", {"husband": {"used": 1}, "wife": {"used": 2},
                                             "last_updated": "2026-08-24"})
    _write(tmp_path, "long_term_screen_results.json", {"passed": [{"ticker": "VT"}]})
    return tmp_path


class TestProjectionLeakage:
    @pytest.mark.parametrize("mode", ap.MODES)
    def test_no_internal_fields_reach_the_projection(self, base_dir, mode):
        projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        blob = ap.canonical_json(projection)
        # 値そのもの (owner 名・broker 名・note 本文・照合ハッシュ・原価)。
        for forbidden in ("楽天証券", "husband", "wife", "特定",
                          "保有同期", "deadbeef", "1234567", "secret", "INTERNALNOTEMARKER"):
            assert forbidden not in blob, f"{mode}: leaked value {forbidden!r}"
        # フィールド名も、値を伴う形では出てはいけない。
        for forbidden in ('"note"', '"owner"', '"broker"', '"account"',
                          '"reconciliation_snapshot_hash"',
                          '"broker_total_cost_basis_jpy"', '"internal_note"',
                          '"short_positions"'):
            assert forbidden not in blob, f"{mode}: leaked field {forbidden}"

    @pytest.mark.parametrize("mode", ap.MODES)
    def test_no_absolute_paths_or_filenames_reach_the_projection(self, base_dir, mode):
        projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        blob = ap.canonical_json(projection)
        assert str(base_dir) not in blob
        assert ".json" not in blob

    @pytest.mark.parametrize("mode", ap.MODES)
    def test_the_prompt_carries_no_path_and_no_filename(self, base_dir, mode):
        projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        prompt = ap.build_agent_prompt(projection)
        assert str(base_dir) not in prompt
        assert ".json" not in prompt
        # source_hashes のキーは内部ファイル名そのもの。hash 対象には残すが
        # Agent には見せない (agent_visible_projection)。
        assert "source_hashes" not in prompt
        for filename_key in ("technical_state", "ai_portfolio_analysis",
                             "nisa_portfolio", "long_term_screen_results",
                             "guard_state", "macro_state"):
            assert filename_key not in prompt, f"{mode}: internal filename {filename_key}"

    def test_an_unusable_row_carries_no_numbers(self, base_dir):
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        bad = next(c for c in projection["candidates"]
                   if c["canonical_instrument_id"] == "BAD")
        assert bad["technical"]["usable"] is False
        assert bad["technical"]["reason"] == "data_quality_blocked"
        for numeric in ("price", "rsi", "change_5d_pct", "change_20d_pct",
                        "composite_signal"):
            assert numeric not in bad["technical"]

    def test_the_validator_rejects_a_projection_that_leaks_values(self):
        """validate_projection 自体が守りになっていること。"""
        projection = {
            "schema_version": ap.SCHEMA_VERSION, "mode": "default",
            "analysis_id": "", "evaluation_as_of": NOW.isoformat(),
            "source_hashes": {}, "portfolio_context": {}, "market_context": {},
            "candidates": [{"candidate_id": "candidate:X",
                            "canonical_instrument_id": "X",
                            "technical": {"usable": False, "rsi": 10.0}}],
            "action_scope": [{"candidate_id": "candidate:X",
                              "canonical_instrument_id": "X",
                              "allowed_actions": ["watch"],
                              "max_actionability": "watch_only"}],
        }
        with pytest.raises(ap.ProjectionError, match="leaked"):
            ap.validate_projection(projection)


class TestModeIsolation:
    def test_risk_mode_does_not_carry_technical_or_analysis_data(self, base_dir):
        projection = ap.build_agent_projection("risk", base_dir=base_dir, now=NOW)
        assert all("technical" not in c for c in projection["candidates"])
        assert "ai_portfolio_analysis" not in projection["source_hashes"]

    def test_default_mode_does_not_carry_guardrail_or_nisa_data(self, base_dir):
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        assert "guardrails" not in projection["market_context"]
        assert "nisa" not in projection["portfolio_context"]
        assert "nisa_portfolio" not in projection["source_hashes"]

    def test_nisa_mode_exposes_only_the_owner_names_not_their_rows(self, base_dir):
        projection = ap.build_agent_projection("nisa", base_dir=base_dir, now=NOW)
        # owner ごとの内訳は出さない (件数だけ)。
        assert projection["portfolio_context"]["nisa"]["owner_count"] == 2
        assert "used" not in ap.canonical_json(projection)
        assert "husband" not in ap.canonical_json(projection)


class TestHashStability:
    def test_the_same_inputs_and_clock_give_the_same_hash(self, base_dir):
        a = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        b = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        assert ap.projection_sha256(a) == ap.projection_sha256(b)

    def test_evaluation_as_of_is_inside_the_hash(self, base_dir):
        """freshness の解釈を変えるので、別の as_of は別の projection。"""
        a = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        b = ap.build_agent_projection(
            "default", base_dir=base_dir, now=NOW.replace(hour=13))
        assert ap.projection_sha256(a) != ap.projection_sha256(b)

    def test_the_cli_and_api_paths_produce_the_same_payload(self, base_dir):
        """CLI と API が同じ builder を使っていること。別実装に分岐すると、
        片方だけ契約が緩むのがこの設計で一番怖い失敗。"""
        import api.routes.agent as api_agent
        import portfolio_agent as cli_agent

        assert api_agent.build_agent_projection is ap.build_agent_projection
        assert cli_agent.build_agent_projection is ap.build_agent_projection
        assert api_agent.build_agent_prompt is ap.build_agent_prompt
        assert cli_agent.build_agent_prompt is ap.build_agent_prompt
        assert api_agent.validate_agent_output is ap.validate_agent_output
        assert cli_agent.validate_agent_output is ap.validate_agent_output
        assert api_agent.OUTPUT_FILES == cli_agent.OUTPUT_FILES


class TestAgentOptionsHaveNoTools:
    def test_no_tools_are_granted(self):
        options = ap.build_agent_options()
        assert options.tools == []
        assert options.allowed_tools == []
        assert options.setting_sources == []
        assert options.max_turns == 1

    def test_file_tools_are_explicitly_denied(self):
        """allowed_tools が空であることの二重の担保。SDK の既定が将来
        変わってもファイル系ツールは効かない。"""
        options = ap.build_agent_options()
        for tool in ("Read", "Write", "Edit", "Bash", "Glob", "Grep"):
            assert tool in options.disallowed_tools

    def test_a_structured_output_schema_is_required(self):
        options = ap.build_agent_options()
        assert options.output_format is not None
        assert options.output_format["additionalProperties"] is False


class TestOutputValidation:
    @pytest.fixture
    def projection(self, base_dir):
        return ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def _output(self, projection, **over):
        scope = projection["action_scope"][0]
        action = {"rank": 1, "candidate_id": scope["candidate_id"],
                  "action_type": scope["allowed_actions"][0],
                  "actionability": "watch_only", "reason": "ok"}
        action.update(over.pop("action", {}))
        payload = {"headline": "h", "overall_stance": "neutral",
                   "risk_warnings": [], "actions": [action]}
        payload.update(over)
        return payload

    def test_the_host_resolves_the_ticker_from_the_projection(self, projection):
        """Agent は candidate_id しか返さない。ticker はホストが復元する
        —— これで別銘柄の捏造が構造的に不可能になる。"""
        verified = ap.validate_agent_output(self._output(projection), projection)
        scope = projection["action_scope"][0]
        assert verified["actions"][0]["ticker"] == scope["canonical_instrument_id"]

    def test_a_candidate_outside_the_scope_is_rejected(self, projection):
        bad = self._output(projection, action={"candidate_id": "candidate:FABRICATED"})
        with pytest.raises(ap.AgentOutputError, match="not in action_scope"):
            ap.validate_agent_output(bad, projection)

    def test_an_action_type_outside_allowed_actions_is_rejected(self, projection):
        bad = self._output(projection, action={"action_type": "short_sell"})
        with pytest.raises(ap.AgentOutputError, match="not allowed"):
            ap.validate_agent_output(bad, projection)

    def test_escalating_above_max_actionability_is_rejected(self, projection):
        watch_only = next(e for e in projection["action_scope"]
                          if e["max_actionability"] == "watch_only")
        bad = self._output(projection, action={
            "candidate_id": watch_only["candidate_id"],
            "action_type": watch_only["allowed_actions"][0],
            "actionability": "review"})
        with pytest.raises(ap.AgentOutputError, match="exceeds max"):
            ap.validate_agent_output(bad, projection)

    def test_the_ceiling_itself_is_allowed(self, projection):
        review_ok = next(e for e in projection["action_scope"]
                         if e["max_actionability"] == "review")
        ok = self._output(projection, action={
            "candidate_id": review_ok["candidate_id"],
            "action_type": review_ok["allowed_actions"][0],
            "actionability": "review"})
        assert ap.validate_agent_output(ok, projection)["actions"][0]["actionability"] == "review"

    def test_duplicate_ranks_are_rejected(self, projection):
        payload = self._output(projection)
        payload["actions"] = [payload["actions"][0], dict(payload["actions"][0])]
        with pytest.raises(ap.AgentOutputError, match="duplicate rank"):
            ap.validate_agent_output(payload, projection)

    def test_unknown_keys_are_rejected(self, projection):
        with pytest.raises(ap.AgentOutputError, match="unexpected output keys"):
            ap.validate_agent_output(self._output(projection, sneaky=1), projection)
        with pytest.raises(ap.AgentOutputError, match="unexpected action keys"):
            ap.validate_agent_output(
                self._output(projection, action={"ticker": "FAKE"}), projection)

    def test_an_unknown_enum_is_rejected(self, projection):
        with pytest.raises(ap.AgentOutputError, match="overall_stance"):
            ap.validate_agent_output(
                self._output(projection, overall_stance="yolo"), projection)
        with pytest.raises(ap.AgentOutputError, match="actionability"):
            ap.validate_agent_output(
                self._output(projection, action={"actionability": "ready"}), projection)

    def test_an_oversized_string_is_rejected(self, projection):
        huge = "x" * (ap.MAX_STRING_CHARS + 1)
        with pytest.raises(ap.AgentOutputError, match="headline"):
            ap.validate_agent_output(self._output(projection, headline=huge), projection)
        with pytest.raises(ap.AgentOutputError, match="reason"):
            ap.validate_agent_output(
                self._output(projection, action={"reason": huge}), projection)

    def test_a_source_change_during_the_run_is_rejected(self, projection, base_dir):
        """projection 生成後に元データが変わっていたら、Agent は古い世界像で
        判断している。保存すると現在の state と食い違う推奨が残る。"""
        _write(base_dir, "holdings.json", {"VT_row": {"ticker": "VT", "shares": 99.0}})
        with pytest.raises(ap.AgentOutputError, match="source changed"):
            ap.validate_agent_output(self._output(projection), projection,
                                     base_dir=base_dir)

    def test_non_json_output_is_rejected(self, projection):
        with pytest.raises(ap.AgentOutputError, match="not valid JSON"):
            ap.validate_agent_output(
                ap.parse_agent_result("これは JSON ではありません"), projection)
        with pytest.raises(ap.AgentOutputError, match="no structured output"):
            ap.parse_agent_result("")


def test_neither_agent_entrypoint_embeds_a_path_or_filename_in_a_prompt():
    """回帰: プロンプトに絶対パス・ファイル名を書き戻さないこと。

    旧実装は f-string で BASE_DIR とファイル名を埋め込んでいた。将来
    誰かが「読みやすさのため」に戻すと、契約の迂回口が再び開く。
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    for name in ("portfolio_agent.py", "api/routes/agent.py"):
        source = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # ⚠️ 素の grep はコメント本文にも当たる (このファイル自身が旧実装を
        # 説明する行を持つ)。AST を歩いて、実際の**コード**だけを見る。
        for node in ast.walk(tree):
            # ClaudeAgentOptions を直に組み立てていないこと
            # (build_agent_options 経由に統一されている)。
            if isinstance(node, ast.Call):
                func = node.func
                fname = getattr(func, "id", None) or getattr(func, "attr", None)
                assert fname != "ClaudeAgentOptions", (
                    f"{name}: options を直に組んでいる (build_agent_options を使うこと)")
            # プロンプト用 f-string に BASE_DIR を差し込んでいないこと。
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        inner = getattr(value.value, "id", None)
                        assert inner != "BASE_DIR", (
                            f"{name}: プロンプトへ絶対パスを埋め込んでいる")

        # 旧プロンプト表そのものが復活していないこと。
        assert "ANALYSIS_PROMPTS" not in source, name
