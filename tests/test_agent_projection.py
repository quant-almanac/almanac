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
        # ↓ 本番 holdings.json はこれらを通常の保有行と同じ形で並べる。
        #   fixture に無いと漏洩テストが素通りする (round 12 の実際の見落とし)。
        "CASH_JPY_SBI_WIFE": {"ticker": "CASH_JPY_SBI_WIFE", "shares": 512345.0,
                              "currency": "JPY", "investment_type": "cash"},
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 367891.0,
                         "currency": "JPY", "investment_type": "cash"},
        "GS_MMF_USD": {"ticker": "GS_MMF_USD", "shares": 1000.0,
                       "currency": "USD", "asset_type": "money_market_fund"},
        "SLIM_SP500": {"ticker": "SLIM_SP500", "shares": 100.0, "currency": "JPY"},
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
                          "保有同期", "deadbeef", "1234567", "secret",
                          "INTERNALNOTEMARKER",
                          # 現金経路・世帯構成・MMF・投信の疑似ティッカー
                          # 現金経路・世帯構成・現金残高。投信 (SLIM_SP500) は
                          # 投資対象なので**出てよい**。
                          "CASH_JPY_SBI_WIFE", "CASH_JPY_SBI", "GS_MMF_USD",
                          "SBI", "512345", "367891"):
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

    def test_cash_routes_are_never_projected(self, base_dir):
        """現金相当 (現金ウォレット・MMF) は候補にも明細にも出さない。

        判定は行の型 (investment_type / asset_type) で行い、ticker 名の
        パターンには頼らない —— 新しい現金経路が増えたとき素通りするため。
        """
        for mode in ap.MODES:
            projection = ap.build_agent_projection(mode, base_dir=base_dir, now=NOW)
            blob = ap.canonical_json(projection)
            for cash_route in ("CASH_JPY_SBI_WIFE", "CASH_JPY_SBI", "GS_MMF_USD"):
                assert cash_route not in blob, f"{mode}: {cash_route} が projection に出た"

    def test_funds_without_market_data_are_still_investable(self, base_dir):
        """投信は「市場データが無い」だけで投資対象。

        以前 is_pseudo_market_ticker (= yfinance へ送れない) を投資対象の
        判定に流用しており、実在するコア投信が risk 集計から丸ごと消えて
        比率が狂っていた (Codex レビュー round 13)。
        """
        projection = ap.build_agent_projection("risk", base_dir=base_dir, now=NOW)
        exposures = projection["portfolio_context"].get("exposures", [])
        ids = {e["canonical_instrument_id"] for e in exposures}
        assert "SLIM_SP500" in ids, "投信が集中リスクの分母から消えている"
        assert not (ids & {"CASH_JPY_SBI_WIFE", "GS_MMF_USD"})

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
        # SDK は {"type": "json_schema", "schema": ...} でしか --json-schema を
        # 渡さない。素のスキーマだと黙って無視される (round 12)。
        assert options.output_format["type"] == "json_schema"
        assert options.output_format["schema"]["additionalProperties"] is False


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
        """通貨を混ぜて合計しない。

        以前は USD の shares×USD価格 と JPY の shares×円価格 をそのまま
        足しており、比率が無意味になっていた (1489.T が 94.75%)。
        """
        _write(base_dir, "account.json", {"fx_rate_usdjpy": 150.0})
        _write(base_dir, "holdings.json", {
            "JP": {"ticker": "JPSTOCK", "shares": 100.0, "currency": "JPY"},
            "US": {"ticker": "USSTOCK", "shares": 100.0, "currency": "USD"},
        })
        _write(base_dir, "technical_state.json", {"tickers": {
            t: {"price": 100.0, "data_quality_status": "ok",
                "freshness_status": "fresh", "data_as_of": "2026-08-24"}
            for t in ("JPSTOCK", "USSTOCK")}})

        projection = ap.build_agent_projection("risk", base_dir=base_dir, now=NOW)
        by_id = {e["canonical_instrument_id"]: e
                 for e in projection["portfolio_context"]["exposures"]}
        # 同じ 100株×100 でも、USD 側は FX を掛けた JPY 額になる。
        assert by_id["JPSTOCK"]["market_value_jpy"] == 10_000
        assert by_id["USSTOCK"]["market_value_jpy"] == 1_500_000
        assert by_id["USSTOCK"]["weight_pct"] > by_id["JPSTOCK"]["weight_pct"]

    def test_the_currency_mix_is_labelled_as_listing_currency(self, base_dir):
        """look-through していないので、名前でそれと分かるようにする。"""
        projection = ap.build_agent_projection("risk", base_dir=base_dir, now=NOW)
        pc = projection["portfolio_context"]
        assert "listing_currency_mix_pct" in pc
        assert "currency_mix_pct" not in pc

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
        jp_row = {"price": 100.0, "data_quality_status": "ok",
                  "freshness_status": "fresh", "data_as_of": "2026-08-24"}
        out = ap._technical_projection(jp_row, now=NOW, ticker="1489.T")
        assert "usable" in out

    def test_the_run_uses_an_explicit_model_and_budget(self):
        options = ap.build_agent_options()
        assert options.model, "課金経路で model を既定任せにしない"
        assert options.max_budget_usd is not None

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
