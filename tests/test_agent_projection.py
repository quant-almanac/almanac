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
            # ⚠️ 実在の証券会社名・世帯区分・口座種別は書かない
            # (このリポジトリは公開ミラーへも流れる)。形だけ同じ合成値を使う。
            "note": "BROKERNOTEMARKER",
            "owner": "OWNERMARKER", "broker": "BROKERMARKER",
            "account": "ACCOUNTMARKER",
            "reconciliation_snapshot_hash": "deadbeef" * 8,
            "broker_total_cost_basis_jpy": 1234567,
        },
        "BAD_row": {"ticker": "BAD", "shares": 1.0, "currency": "USD"},
        # ↓ 本番 holdings.json はこれらを通常の保有行と同じ形で並べる。
        #   fixture に無いと漏洩テストが素通りする (round 12 の実際の見落とし)。
        # 判定は ticker 名ではなく行の型 (investment_type / asset_type) で
        # 行うので、合成名で十分に検査できる。実在のウォレット名は使わない。
        "CASHWALLET_A": {"ticker": "CASHWALLET_A", "shares": 512345.0,
                         "currency": "JPY", "investment_type": "cash"},
        "CASHWALLET_B": {"ticker": "CASHWALLET_B", "shares": 367891.0,
                         "currency": "JPY", "investment_type": "cash"},
        "MMFWALLET": {"ticker": "MMFWALLET", "shares": 1000.0,
                      "currency": "USD", "asset_type": "money_market_fund"},
        "FUNDNOMKT": {"ticker": "FUNDNOMKT", "shares": 100.0, "currency": "JPY"},
    })
    _write(tmp_path, "ai_portfolio_analysis.json", {
        # 本番は as_of を持つ。鮮度契約が効くので fixture にも要る。
        "as_of": NOW.isoformat(),
        "synthesis": {"overall_stance": "neutral", "priority_actions": [
            {"ticker": "VT", "type": "buy", "execution_readiness": "review"},
            {"ticker": "BAD", "type": "buy", "execution_readiness": "review"},
            {"ticker": "BLOCKEDONE", "type": "buy", "execution_readiness": "blocked"},
        ]}})
    _write(tmp_path, "guard_state.json", {"daily_pnl_pct": -1.0, "monthly_pnl_pct": 2.0,
                                          "portfolio_value": 30_000_000,
                                          "short_positions": ["secret"]})
    _write(tmp_path, "macro_state.json", {"fed_rate": 4.0, "yield_10y": 4.2,
                                          "unemp_rate": 4.1, "internal_note": "INTERNALNOTEMARKER"})
    _write(tmp_path, "nisa_portfolio.json", {"OWNER_A": {"used": 1}, "OWNER_B": {"used": 2},
                                             "last_updated": "2026-08-24"})
    _write(tmp_path, "long_term_screen_results.json", {"passed": [{"ticker": "VT"}]})
    return tmp_path


class TestProjectionLeakage:
    @pytest.mark.parametrize("mode", ap.ENABLED_MODES)
    def test_no_internal_fields_reach_the_projection(self, base_dir, mode):
        projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        blob = ap.canonical_json(projection)
        # 値そのもの (owner 名・broker 名・note 本文・照合ハッシュ・原価)。
        # 値そのもの (owner・broker・note 本文・照合ハッシュ・原価・現金残高)。
        # 投信 (FUNDNOMKT) は投資対象なので **出てよい**。
        for forbidden in ("BROKERMARKER", "OWNERMARKER", "ACCOUNTMARKER",
                          "BROKERNOTEMARKER", "deadbeef", "1234567", "secret",
                          "INTERNALNOTEMARKER",
                          "CASHWALLET_A", "CASHWALLET_B", "MMFWALLET",
                          "512345", "367891"):
            assert forbidden not in blob, f"{mode}: leaked value {forbidden!r}"
        # フィールド名も、値を伴う形では出てはいけない。
        for forbidden in ('"note"', '"owner"', '"broker"', '"account"',
                          '"reconciliation_snapshot_hash"',
                          '"broker_total_cost_basis_jpy"', '"internal_note"',
                          '"short_positions"'):
            assert forbidden not in blob, f"{mode}: leaked field {forbidden}"

    @pytest.mark.parametrize("mode", ap.ENABLED_MODES)
    def test_no_absolute_paths_or_filenames_reach_the_projection(self, base_dir, mode):
        projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        blob = ap.canonical_json(projection)
        assert str(base_dir) not in blob
        assert ".json" not in blob

    def test_cash_routes_are_never_projected(self, base_dir):
        """現金相当 (現金ウォレット・MMF) は候補にも明細にも出さない。

        判定は行の型 (investment_type / asset_type) で行い、ticker 名の
        パターンには頼らない —— 新しい現金経路が増えたとき素通りするため。
        """
        for mode in ap.ENABLED_MODES:
            projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
            blob = ap.canonical_json(projection)
            for cash_route in ("CASHWALLET_A", "CASHWALLET_B", "MMFWALLET"):
                assert cash_route not in blob, f"{mode}: {cash_route} が projection に出た"

    def test_funds_without_market_data_are_still_investable(self, base_dir):
        """risk/nisa は現在無効 (ENABLED_MODES)。判断材料が正しくなったら
        このテストを本来の内容へ戻すこと。ヘルパー単体の検証は
        test_every_jpy_valuation_source_is_summed が担う。"""
        # ⚠️ 無条件 skip にしない。risk/nisa を再有効化したとき自動で
        # 復帰しないと、検査されないまま動き出す (レビューで指摘)。
        if "risk" not in ap.ENABLED_MODES and "nisa" not in ap.ENABLED_MODES:
            pytest.skip("risk/nisa modes are disabled pending trustworthy inputs")
        raise AssertionError(
            "risk/nisa が再有効化された。このテストを本来の内容へ戻すこと")

    @pytest.mark.parametrize("mode", ap.ENABLED_MODES)
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
        """risk/nisa は現在無効 (ENABLED_MODES)。判断材料が正しくなったら
        このテストを本来の内容へ戻すこと。ヘルパー単体の検証は
        test_every_jpy_valuation_source_is_summed が担う。"""
        # ⚠️ 無条件 skip にしない。risk/nisa を再有効化したとき自動で
        # 復帰しないと、検査されないまま動き出す (レビューで指摘)。
        if "risk" not in ap.ENABLED_MODES and "nisa" not in ap.ENABLED_MODES:
            pytest.skip("risk/nisa modes are disabled pending trustworthy inputs")
        raise AssertionError(
            "risk/nisa が再有効化された。このテストを本来の内容へ戻すこと")

    def test_default_mode_does_not_carry_guardrail_or_nisa_data(self, base_dir):
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        assert "guardrails" not in projection["market_context"]
        assert "nisa" not in projection["portfolio_context"]
        assert "nisa_portfolio" not in projection["source_hashes"]

    def test_nisa_mode_exposes_only_the_owner_names_not_their_rows(self, base_dir):
        """risk/nisa は現在無効 (ENABLED_MODES)。判断材料が正しくなったら
        このテストを本来の内容へ戻すこと。ヘルパー単体の検証は
        test_every_jpy_valuation_source_is_summed が担う。"""
        # ⚠️ 無条件 skip にしない。risk/nisa を再有効化したとき自動で
        # 復帰しないと、検査されないまま動き出す (レビューで指摘)。
        if "risk" not in ap.ENABLED_MODES and "nisa" not in ap.ENABLED_MODES:
            pytest.skip("risk/nisa modes are disabled pending trustworthy inputs")
        raise AssertionError(
            "risk/nisa が再有効化された。このテストを本来の内容へ戻すこと")

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
    """安全契約 (ツール禁止・構造化出力の強制) を検証する。

    ⚠️ 以前はここが `claude_agent_sdk` のインストールに依存しており、
    CI の軽量依存セット (SDK 無し) では importorskip で丸ごと skip されて
    いた。「CI 向けの Linux wheel が無いから入れられない」というのは
    誤り —— claude-agent-sdk 0.1.50 は PyPI に manylinux_2_17_x86_64/
    aarch64 wheel を公開しており普通にインストールできる (レビューで
    指摘・PyPI のファイル一覧で確認)。原因は単に requirements.txt に
    含まれていなかったことで、追加済み。

    Round 11-13 で塞いだはずのこの安全契約が CI 上は一度も検証されて
    いなかった、という指摘に対する修正: SDK を requirements.txt へ追加した
    上で、さらに `build_agent_options_kwargs()` (SDK 非依存の純粋な dict
    構築) を直接検査する。SDK オブジェクトの内部プロパティ名や型に
    依存しない、より直接的な契約検査になる。
    """

    def test_no_tools_are_granted(self):
        kwargs = ap.build_agent_options_kwargs()
        assert kwargs["tools"] == []
        assert kwargs["allowed_tools"] == []
        assert kwargs["setting_sources"] == []
        assert kwargs["max_turns"] == 1

    def test_file_tools_are_explicitly_denied(self):
        """allowed_tools が空であることの二重の担保。SDK の既定が将来
        変わってもファイル系ツールは効かない。"""
        kwargs = ap.build_agent_options_kwargs()
        for tool in ("Read", "Write", "Edit", "Bash", "Glob", "Grep"):
            assert tool in kwargs["disallowed_tools"]

    def test_a_structured_output_schema_is_required(self):
        kwargs = ap.build_agent_options_kwargs()
        assert kwargs["output_format"] is not None
        # SDK は {"type": "json_schema", "schema": ...} でしか --json-schema を
        # 渡さない。素のスキーマだと黙って無視される (round 12)。
        assert kwargs["output_format"]["type"] == "json_schema"
        assert kwargs["output_format"]["schema"]["additionalProperties"] is False

    def test_thinking_is_adaptive(self):
        """claude-sonnet-5 は thinking.type=enabled (レガシー) を拒否し、
        adaptive を要求する。明示しないと SDK 同梱 CLI がレガシー値を
        デフォルト注入し、API が 400 で拒否する (レビューで指摘・
        隔離ライブで実測・再現)。"""
        kwargs = ap.build_agent_options_kwargs()
        assert kwargs["thinking"] == {"type": "adaptive"}

    def test_effort_is_medium(self):
        kwargs = ap.build_agent_options_kwargs()
        assert kwargs["effort"] == "medium"


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


class TestRound13Blockers:
    """round 13 で再現された6件の P1 と2件の P2 を固定する。"""

    def test_valuations_are_normalised_to_jpy(self, base_dir):
        """risk/nisa は現在無効 (ENABLED_MODES)。判断材料が正しくなったら
        このテストを本来の内容へ戻すこと。ヘルパー単体の検証は
        test_every_jpy_valuation_source_is_summed が担う。"""
        # ⚠️ 無条件 skip にしない。risk/nisa を再有効化したとき自動で
        # 復帰しないと、検査されないまま動き出す (レビューで指摘)。
        if "risk" not in ap.ENABLED_MODES and "nisa" not in ap.ENABLED_MODES:
            pytest.skip("risk/nisa modes are disabled pending trustworthy inputs")
        raise AssertionError(
            "risk/nisa が再有効化された。このテストを本来の内容へ戻すこと")

    def test_the_currency_mix_is_labelled_as_listing_currency(self, base_dir):
        """risk/nisa は現在無効 (ENABLED_MODES)。判断材料が正しくなったら
        このテストを本来の内容へ戻すこと。ヘルパー単体の検証は
        test_every_jpy_valuation_source_is_summed が担う。"""
        # ⚠️ 無条件 skip にしない。risk/nisa を再有効化したとき自動で
        # 復帰しないと、検査されないまま動き出す (レビューで指摘)。
        if "risk" not in ap.ENABLED_MODES and "nisa" not in ap.ENABLED_MODES:
            pytest.skip("risk/nisa modes are disabled pending trustworthy inputs")
        raise AssertionError(
            "risk/nisa が再有効化された。このテストを本来の内容へ戻すこと")

    def test_a_blocked_candidate_is_not_relaxed_by_an_unusable_row(self):
        """blocked + technical unusable は blocked のまま。

        以前は technical 由来の watch_only で **上書き** しており、
        blocked が緩和されていた。
        """
        unusable = {"usable": False, "reason": "technical_row_missing"}
        scope = ap._scope_for_official(
            "X", {"action_type": "buy", "readiness": "blocked",
                  "recommendation_id": "", "index": 0}, unusable)
        assert scope["max_actionability"] == "blocked"
        assert scope["allowed_actions"] == ["watch"]

    def test_a_corrupt_required_input_refuses_to_build(self, base_dir):
        """破損した必須入力で空の分析を作らない。"""
        (base_dir / "ai_portfolio_analysis.json").write_text("{ not json",
                                                            encoding="utf-8")
        with pytest.raises(ap.RequiredInputError, match="not valid JSON"):
            ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def test_a_stale_analysis_refuses_to_build(self, base_dir):
        """古い正式分析で default を走らせない。"""
        from datetime import timedelta

        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": (NOW - timedelta(hours=48)).isoformat(),
            "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy", "execution_readiness": "review"}]}})
        with pytest.raises(ap.RequiredInputError, match="stale"):
            ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def test_an_empty_scope_refuses_to_build(self, base_dir):
        """候補ゼロの projection を Agent へ渡さない。

        空の分析が保存されると last-known-good が消える。
        """
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": NOW.isoformat(), "synthesis": {"priority_actions": []}})
        with pytest.raises(ap.RequiredInputError, match="no candidates in scope"):
            ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def test_the_agent_cannot_be_more_aggressive_than_the_official_stance(self, base_dir):
        """構造化 action を縛っても stance が自由なら意味が薄れる。"""
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        scope = projection["action_scope"][0]
        payload = {
            "headline": "h", "overall_stance": "aggressive", "risk_warnings": [],
            "actions": [{"rank": 1, "candidate_id": scope["candidate_id"],
                         "action_type": scope["allowed_actions"][0],
                         "actionability": "watch_only", "reason": "r"}],
        }
        with pytest.raises(ap.AgentOutputError, match="more aggressive"):
            ap.validate_agent_output(payload, projection)

        payload["overall_stance"] = "defensive"   # 守り側は通る
        assert ap.validate_agent_output(payload, projection)

    def test_the_saved_result_marks_free_text_as_non_actionable(self, base_dir):
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        scope = projection["action_scope"][0]
        verified = ap.validate_agent_output({
            "headline": "h", "overall_stance": "neutral",
            "risk_warnings": ["NVDA を追加購入すべき"],
            "actions": [{"rank": 1, "candidate_id": scope["candidate_id"],
                         "action_type": scope["allowed_actions"][0],
                         "actionability": "watch_only", "reason": "r"}],
        }, projection)
        assert verified["commentary_is_non_actionable"] is True

    def test_two_official_actions_on_one_ticker_are_kept_apart(self, base_dir):
        """同一 ticker の2判断を1件へ上書きしない。

        実データでは recommendation_id が全件 null なので、入力配列の
        安定 index を併用する。
        """
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": NOW.isoformat(), "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy", "execution_readiness": "review"},
                {"ticker": "VT", "type": "trim", "execution_readiness": "review"},
            ]}})
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        assert len(projection["action_scope"]) == 2
        assert len({e["candidate_id"] for e in projection["action_scope"]}) == 2
        directions = {e["allowed_actions"][0] for e in projection["action_scope"]}
        assert directions == {"buy", "trim"}

    def test_freshness_uses_the_canonical_ticker_not_the_row(self, base_dir):
        """technical 行は ticker フィールドを持たない (実測 0/72)。

        行から取ると空文字になり、JP 銘柄が NYSE カレンダーで判定される。
        """
        import inspect

        src = inspect.getsource(ap._technical_projection)
        assert "ticker=ticker" in src or "ticker: str" in src
        # 明示的に渡した ticker が使われること (JP 銘柄で確認)。
        # 合成の JP ティッカー。実在銘柄を書かない (公開ミラーへ流れるため)。
        jp_row = {"price": 100.0, "data_quality_status": "ok",
                  "freshness_status": "fresh", "data_as_of": "2026-08-24"}
        out = ap._technical_projection(jp_row, now=NOW, ticker="9999.T")
        assert "usable" in out

    def test_the_run_uses_an_explicit_model_and_budget(self):
        kwargs = ap.build_agent_options_kwargs()
        assert kwargs["model"], "課金経路で model を既定任せにしない"
        assert kwargs["max_budget_usd"] is not None

    def test_a_stale_write_is_refused(self, tmp_path):
        """古い run が新しい保存物を上書きしない (CAS)。"""
        path = tmp_path / "out.json"
        newer = {"evaluation_as_of": "2026-08-25T12:00:00+00:00", "headline": "new"}
        assert ap.save_verified_result(path, newer, as_of="x") is True

        older = {"evaluation_as_of": "2026-08-25T11:00:00+00:00", "headline": "old"}
        assert ap.save_verified_result(path, older, as_of="y") is False
        assert json.loads(path.read_text(encoding="utf-8"))["headline"] == "new"


def test_neither_entrypoint_displays_unverified_free_text():
    """TextBlock を検証前に表示しない。

    scope 外の銘柄に触れる自由文が人の目に入ると、構造化 action を縛った
    意味が薄れる (Codex レビュー round 13)。
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("portfolio_agent.py", "api/routes/agent.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "yield _sse(\"text\"" not in source, name
        assert "print(block.text" not in source, name


def test_both_entrypoints_take_the_same_run_lock():
    """CLI と API が同じロック名を取る (二重課金・巻き戻しの防止)。"""
    import api.routes.agent as api_agent
    import portfolio_agent as cli_agent

    assert api_agent.AGENT_RUN_LOCK_NAME == cli_agent.AGENT_RUN_LOCK_NAME
    assert api_agent.save_verified_result is ap.save_verified_result
    assert cli_agent.save_verified_result is ap.save_verified_result


class TestRound14Blockers:
    """round 14 で再現された P1 群を固定する。"""

    def test_a_jst_naive_timestamp_is_not_read_as_utc(self, base_dir):
        """本番の as_of は JST の naive 時刻。UTC 解釈だと9時間ぶん未来へ
        ずれ、25時間前の分析が24時間制限を素通りする
        (Codex レビュー round 14 で 25h36m 前が受理された)。
        """
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        # 評価時刻 (UTC) から見て JST で 25 時間前 = 制限超過。
        jst_now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
        stale_jst = (jst_now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M")
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": stale_jst,
            "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy", "execution_readiness": "review"}]}})
        with pytest.raises(ap.RequiredInputError, match="stale"):
            ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

        # 同じ形式で 1 時間前なら通る。
        fresh_jst = (jst_now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": fresh_jst,
            "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy", "execution_readiness": "review"}]}})
        assert ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def test_a_future_timestamp_is_refused(self, base_dir):
        """未来の as_of を素通しすると「どれだけ古くても通る」になる。"""
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        jst_now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
        future = (jst_now + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": future,
            "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy", "execution_readiness": "review"}]}})
        with pytest.raises(ap.RequiredInputError, match="future"):
            ap.build_agent_projection("default", base_dir=base_dir, now=NOW)

    def test_a_missing_readiness_is_blocked_not_review(self, base_dir):
        """欠損・未知値の readiness を review へ昇格させない。"""
        _write(base_dir, "ai_portfolio_analysis.json", {
            "as_of": NOW.isoformat(),
            "synthesis": {"priority_actions": [
                {"ticker": "VT", "type": "buy"},                       # 欠損
                {"ticker": "BAD", "type": "buy", "execution_readiness": "weird"},
            ]}})
        projection = ap.build_agent_projection("default", base_dir=base_dir, now=NOW)
        for entry in projection["action_scope"]:
            assert entry["max_actionability"] == "blocked", entry
            assert entry["allowed_actions"] == ["watch"]

    def test_risk_and_nisa_modes_are_disabled(self, base_dir):
        """判断材料が正しくないモードは projection すら作らせない。

        作れてしまうと、いずれ誰かが呼ぶ。
        """
        for mode in ("risk", "nisa"):
            with pytest.raises(ap.ModeDisabledError):
                ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
        assert ap.ENABLED_MODES == ("default",)

    def test_the_model_comes_from_the_router_not_a_literal(self):
        """モデル ID を直書きしない。router の sonnet は claude-sonnet-5 で、
        以前 claude-sonnet-4-5 を固定しており旧モデルを使っていた。
        """
        from model_router import MODEL_REGISTRY

        assert ap.resolve_agent_model() == MODEL_REGISTRY["sonnet"]
        # ⚠️ 素の grep はコメント本文にも当たる (このモジュール自身が旧 ID を
        # 説明する行を持つ)。AST を歩いて **文字列リテラル** だけを見る。
        import ast as _ast

        tree = _ast.parse(Path(ap.__file__).read_text(encoding="utf-8"))
        literals = {n.value for n in _ast.walk(tree)
                    if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
        hardcoded = {v for v in literals if v.startswith("claude-")}
        assert not hardcoded, f"モデル ID が直書きされている: {hardcoded}"


def test_every_jpy_valuation_source_is_summed(tmp_path):
    """同一銘柄が複数口座に分かれ、口座ごとに JPY 額のフィールドが違う。

    片方だけ見ると別口座ぶんが丸ごと分母から落ちる (Codex レビュー
    レビューで百万円規模の欠落を実測)。
    """
    _write(tmp_path, "holdings.json", {
        "A": {"ticker": "X", "shares": 1.0, "currency": "JPY",
              "broker_position_value_jpy": 100.0,
              "broker_cost_basis_as_of": "2026-07-28"},
        "B": {"ticker": "X", "shares": 1.0, "currency": "JPY",
              "current_value_jpy": 400.0},
        "C": {"ticker": "Y", "shares": 1.0, "currency": "JPY"},   # 評価額なし
    })
    _write(tmp_path, "technical_state.json", {"tickers": {}})

    rows = ap._exposure_rows(["X", "Y"], json.loads(
        (tmp_path / "holdings.json").read_text()), {}, fx_usdjpy=None)
    by_id = {r["canonical_instrument_id"]: r for r in rows}
    assert by_id["X"]["market_value_jpy"] == 500.0
    assert by_id["X"]["amount_complete"] is True
    assert by_id["X"]["as_of_complete"] is False   # B 行に as_of が無い
    assert by_id["X"]["valuation_as_of"] == "2026-07-28"
    # 評価額を持たない銘柄は金額を作らない。
    assert by_id["Y"]["valuation_available"] is False


def test_the_api_does_not_block_the_event_loop_on_the_lock():
    """process_lock の待機は同期 time.sleep。API は即時 LockBusy にする。"""
    import inspect

    import api.routes.agent as api_agent

    src = inspect.getsource(api_agent._run_agent)
    assert "timeout=0" in src, "API がロック待ちで event loop を止めている"


class TestStructuredOutputIsNotAToolUseViolation:
    """output_format={"type": "json_schema", ...} を要求すると、SDK 同梱の
    CLI はこの機構自体を STRUCTURED_OUTPUT_TOOL_NAME という名前の
    ToolUseBlock で配信する (tools=[] で実ツールを一切与えなくても現れる)。
    これを「禁止したツールの使用」と区別しないと、構造化出力を要求する限り
    **成功する run が存在し得ない** (毎回 AgentProtocolViolation として
    誤検知される)。隔離ライブの検証で実際に発生することを実測して発見した
    —— SDK 0.1.50/0.2.145・claude-haiku-4-5-20251001/claude-sonnet-5 の
    いずれの組み合わせでも再現した。
    """

    def test_the_structured_output_tool_name_is_allowed(self):
        block = type("FakeBlock", (), {"name": ap.STRUCTURED_OUTPUT_TOOL_NAME})()
        ap.assert_no_forbidden_tool_use(block)  # raise しないこと自体が確認

    def test_a_genuinely_forbidden_tool_still_raises(self):
        block = type("FakeBlock", (), {"name": "Bash"})()
        with pytest.raises(ap.AgentProtocolViolation, match="Bash"):
            ap.assert_no_forbidden_tool_use(block)

    def test_both_entrypoints_call_the_shared_guard(self):
        """CLI と API が別々に isinstance(ToolUseBlock) の後続処理を持つと、
        どちらか片方だけ STRUCTURED_OUTPUT_TOOL_NAME の除外を実装し忘れて
        再びこのクラスの最初の欠陥に戻り得る。両方が同じ関数を呼ぶことを
        ソース検査で固定する。"""
        import inspect

        import api.routes.agent as api_agent
        import portfolio_agent as cli_agent

        api_src = inspect.getsource(api_agent._run_agent_locked)
        cli_src = inspect.getsource(cli_agent._run_locked)
        assert "assert_no_forbidden_tool_use(block)" in api_src
        assert "assert_no_forbidden_tool_use(block)" in cli_src
        # 生の isinstance(...) 直後に raise AgentProtocolViolation を書く
        # 旧パターンへの回帰がないこと (呼び出しを経由せず直接 raise すると
        # STRUCTURED_OUTPUT_TOOL_NAME の除外が効かない)。
        assert "raise AgentProtocolViolation(\n" not in api_src
        assert "raise AgentProtocolViolation(\n" not in cli_src

    def test_both_entrypoints_build_options_via_the_shared_builder(self):
        """CLI/API が別々に ClaudeAgentOptions(...) を組み立てると、
        安全契約 (ツール禁止・thinking・budget 等) が片方だけ更新されて
        食い違いうる。両方が build_agent_options() だけを呼ぶことを
        ソース検査で固定する。"""
        import inspect

        import api.routes.agent as api_agent
        import portfolio_agent as cli_agent

        api_src = inspect.getsource(api_agent._run_agent_locked)
        cli_src = inspect.getsource(cli_agent._run_locked)
        assert "build_agent_options()" in api_src
        assert "build_agent_options()" in cli_src
        assert "ClaudeAgentOptions(" not in api_src
        assert "ClaudeAgentOptions(" not in cli_src


def test_both_entrypoints_record_the_resolved_model():
    """「どのモデルにいくら使ったか」を後から検証できること。"""
    import inspect

    import api.routes.agent as api_agent
    import portfolio_agent as cli_agent

    import ast as _ast

    api_tree = _ast.parse(inspect.getsource(api_agent._log_agent_result))
    api_literals = {n.value for n in _ast.walk(api_tree)
                    if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
    assert "claude-agent-sdk" not in api_literals, "総称のままでは費用監査できない"
    assert "resolve_agent_model" in inspect.getsource(api_agent._log_agent_result)

    cli_src = inspect.getsource(cli_agent._run_locked)
    assert "total_cost_usd" in cli_src, "CLI がコストを記録していない"


class TestRound15Blockers:
    """round 15 で指摘された残件を固定する。"""

    def test_no_owner_identifying_strings_in_this_module_or_its_tests(self):
        """公開ミラーへ流れるファイルに、実在の証券会社名・世帯区分・
        現金ウォレット名を書かない。

        以前これらをコメントと fixture に書いて公開版へ push しており、
        公開版 README の「保有者を特定できる情報を含めない」契約に反していた
        (レビューで発覚)。fixture は合成名で同じ形を作れば足りる。
        """
        import re as _re

        # ⚠️ パターン自身がこのファイルにマッチしないよう、リテラルを
        # 分割して組み立てる (素直に書くと自己一致で常に落ちる)。
        patterns = {
            "broker name": _re.compile("楽" + "天"),
            "household role": _re.compile(
                r"\b(?:" + "hus" + "band|" + "wi" + "fe)\b", _re.IGNORECASE),
            "cash wallet": _re.compile(r"\b" + "CASH" + r"_(?:JPY|USD)_[A-Z_]+\b"),
            "mmf wallet": _re.compile(r"\b" + "GS_MMF" + r"_[A-Z]+\b"),
        }
        for target in (Path(ap.__file__), Path(__file__)):
            text = target.read_text(encoding="utf-8")
            for name, pattern in patterns.items():
                assert not pattern.search(text), f"{target.name}: {name} が残っている"

    def test_the_result_endpoint_refuses_disabled_and_unknown_modes(self):
        """閲覧も塞ぐ。実行だけ止めても、以前保存された信頼できない結果を
        読めてしまう。未知モードを default へ倒すのも危険。
        """
        import asyncio

        import api.routes.agent as api_agent

        for mode in ("risk", "nisa", "totally-unknown"):
            response = asyncio.run(api_agent.get_agent_result(mode))
            assert getattr(response, "status_code", None) == 409, mode

    def test_the_enabled_modes_endpoint_is_the_ui_authority(self):
        import asyncio

        import api.routes.agent as api_agent

        payload = asyncio.run(api_agent.get_enabled_modes())
        assert payload["enabled_modes"] == list(ap.ENABLED_MODES)

    def test_valuation_completeness_is_split_by_dimension(self, tmp_path):
        """金額の完全性・時点の完全性・source を分けて持つ。

        一括フラグだと、評価基準日が欠けている行を検知できず、
        混在した source も一律に見える。
        """
        _write(tmp_path, "holdings.json", {
            "A": {"ticker": "X", "shares": 1.0, "currency": "JPY",
                  "broker_position_value_jpy": 100.0,
                  "broker_cost_basis_as_of": "2026-07-28"},
            "B": {"ticker": "X", "shares": 1.0, "currency": "JPY",
                  "current_value_jpy": 400.0},        # as_of なし・別 source
        })
        rows = ap._exposure_rows(["X"], json.loads(
            (tmp_path / "holdings.json").read_text()), {}, fx_usdjpy=None)
        entry = rows[0]
        assert entry["market_value_jpy"] == 500.0
        assert entry["amount_complete"] is True      # 金額は全行そろっている
        assert entry["as_of_complete"] is False      # 時点は欠けている
        assert entry["valuation_source"] == "mixed"  # source が混在

    def test_the_model_resolves_through_role_routing(self):
        """MODEL_REGISTRY を直接引くと eco/premium と role override を
        迂回する。"""
        from model_router import ROLE_ROUTING, get_model

        assert "agent_sdk_run" in ROLE_ROUTING
        assert ap.resolve_agent_model() == get_model("agent_sdk_run")


class TestRound17Blockers:
    """round 17 で指摘された残件を固定する。"""

    def test_no_real_ticker_survives_in_this_test_file(self):
        """公開ミラーへ流れるこのファイルに、実在銘柄のティッカーを
        書かない。以前 実在の JP ETF (自分の追加分) が残っていた。"""
        source = Path(__file__).read_text(encoding="utf-8")
        # リテラルを断片から組み立てる —— そのまま書くとこの assert 自身が
        # 自分にマッチして常に失敗する (このファイルで複数回踏んだ罠)。
        real_ticker = "14" + "89.T"
        assert real_ticker not in source



def test_the_identity_baseline_is_occurrence_count_not_file_exemption():
    """baseline はファイル単位の免除であってはならない。

    以前は baseline に載ったファイルを丸ごと検査から除外しており、
    既にパターンを含むファイルへの **新規追記** を検出できなかった
    (レビューで実測: baseline 済みファイルへ "husband" を追記しても素通り)。
    """
    root = Path(__file__).resolve().parent.parent
    checker_src = (root / "scripts" / "check_public_safety.py").read_text(
        encoding="utf-8") if (root / "scripts" / "check_public_safety.py").exists() else None
    if checker_src is None:
        pytest.skip("check_public_safety.py is public-mirror only")
    assert "if label in baseline:" not in checker_src, (
        "ファイル単位の丸ごと免除に戻っている")
    assert "findall" in checker_src, "出現回数で比較していない"


def test_build_agent_options_wraps_the_kwargs_unchanged():
    """`ClaudeAgentOptions(**kwargs)` が `build_agent_options_kwargs()` の
    値をそのまま反映しているか、という配線を検証する。

    安全契約の中身そのものは TestAgentOptionsHaveNoTools が SDK 非依存で
    直接検証する。

    ⚠️ ここは importorskip にしない。claude-agent-sdk は requirements.txt
    に載った正式な実行時依存なので、入っていない環境は「skip してよい
    環境」ではなく「壊れた環境」。importorskip のままだと、将来
    requirements.txt から落ちる等の依存設定ミスを緑で通してしまう
    (レビューで指摘)。素の import にして、欠損したら失敗させる。
    """
    import claude_agent_sdk  # noqa: F401  — 欠損を skip ではなく失敗にする

    kwargs = ap.build_agent_options_kwargs()
    options = ap.build_agent_options()
    assert options.tools == kwargs["tools"]
    assert options.allowed_tools == kwargs["allowed_tools"]
    assert options.disallowed_tools == kwargs["disallowed_tools"]
    assert options.setting_sources == kwargs["setting_sources"]
    assert options.output_format == kwargs["output_format"]
    assert options.max_turns == kwargs["max_turns"]
    assert options.model == kwargs["model"]
    assert options.max_budget_usd == kwargs["max_budget_usd"]
    assert options.thinking == kwargs["thinking"]
    assert options.effort == kwargs["effort"]


def test_claude_agent_sdk_version_is_pinned_exactly():
    """requirements.txt と実際にインストールされている SDK のバージョンが
    一致すること。SDK は cli_path 未指定時、システム PATH の claude より
    同梱 CLI を優先するため (_find_bundled_cli() が shutil.which() より先)、
    「どの SDK バージョンが入っているか」が「どの CLI バイナリが実際に
    起動するか」をそのまま決める。ここがずれると、CI で検証した契約と
    本番が使う CLI の実際の挙動が一致しなくなる (レビューで指摘:
    0.1.50 が同梱していた CLI 2.1.81 は claude-sonnet-5 のリクエストを
    400 で拒否していた)。
    """
    import re

    import claude_agent_sdk

    req_text = (Path(ap.__file__).parent / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^claude-agent-sdk==([0-9.]+)\s*$", req_text, re.MULTILINE)
    assert m is not None, "requirements.txt に claude-agent-sdk==X.Y.Z の完全固定が無い"
    pinned = m.group(1)
    assert claude_agent_sdk.__version__ == pinned, (
        f"インストール済み SDK ({claude_agent_sdk.__version__}) が "
        f"requirements.txt の固定値 ({pinned}) と食い違っている")


def test_bundled_cli_reports_the_expected_version():
    """SDK が実際に起動する同梱 CLI (システム PATH の claude ではない) の
    バージョンを固定する。SDK バージョンを上げるたびに同梱 CLI も変わる
    ため、意図した CLI が実際にインストールされたことをここで確認する。
    """
    import subprocess

    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    import claude_agent_sdk as _sdk

    transport = SubprocessCLITransport(
        prompt="unused", options=_sdk.ClaudeAgentOptions()
    )
    bundled = transport._find_bundled_cli()
    assert bundled is not None, "同梱 CLI が見つからない（システム CLI へ fallback しうる）"
    out = subprocess.run([bundled, "--version"], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    assert out.stdout.strip().startswith("2.1.247"), (
        f"同梱 CLI のバージョンが想定と異なる: {out.stdout.strip()!r}")
