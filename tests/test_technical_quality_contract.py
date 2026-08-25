"""テクニカル行の品質契約 (technical_quality.py) の単体テスト。

以前この述語は「明示的に blocked」「明示的に rebuild_unresolved」だけを
拒否する fail-open で、品質・鮮度フィールドの無い行がそのまま
シナリオ条件を成立させ AI へも再注入されていた
(Codex レビュー round 9 で RSI=10 の裸の行が usable=True になるのを再現)。
"""
import technical_quality as tq


def _row(**over):
    base = {"rsi": 10.0, "price": 100.0, "data_as_of": "2026-08-24",
            "data_quality_status": "ok", "freshness_status": "fresh"}
    base.update(over)
    return base


class TestFailClosed:
    def test_a_row_without_quality_fields_is_not_usable(self):
        """本番の行は全72件が両フィールドを持つ。欠けている行は
        「未知」であって「良好」ではない。"""
        verdict, reason = tq.classify_technical_row({"rsi": 10.0})
        assert verdict == tq.UNUSABLE
        assert reason == "data_quality_unknown"
        assert tq.technical_row_is_usable({"rsi": 10.0}) is False

    def test_an_unknown_quality_value_is_not_usable(self):
        assert tq.technical_row_is_usable(_row(data_quality_status="weird")) is False

    def test_a_missing_freshness_value_is_not_usable(self):
        row = _row()
        del row["freshness_status"]
        verdict, reason = tq.classify_technical_row(row)
        assert verdict == tq.UNUSABLE
        assert reason == "technical_freshness_unknown"

    def test_a_stale_row_is_not_usable(self):
        verdict, reason = tq.classify_technical_row(_row(freshness_status="stale"))
        assert verdict == tq.UNUSABLE
        assert reason == "technical_data_stale"

    def test_an_empty_or_non_dict_row_is_not_usable(self):
        for bad in ({}, None, [], "row"):
            assert tq.technical_row_is_usable(bad) is False

    def test_blocked_and_unresolved_are_still_rejected(self):
        assert tq.technical_row_is_usable(_row(data_quality_status="blocked")) is False
        assert tq.technical_row_is_usable(_row(rebuild_unresolved=True)) is False

    def test_unresolved_wins_over_an_otherwise_good_row(self):
        """印は品質・鮮度が良好でも優先して弾く (凍結値だから)。"""
        _, reason = tq.classify_technical_row(_row(rebuild_unresolved=True))
        assert reason == "rebuild_unresolved"


class TestUsableAndDegraded:
    def test_ok_and_fresh_is_usable(self):
        assert tq.classify_technical_row(_row()) == (tq.USABLE, None)

    def test_degraded_is_numeric_ok_but_flagged(self):
        verdict, reason = tq.classify_technical_row(_row(freshness_status="degraded"))
        assert verdict == tq.DEGRADED
        assert reason == "technical_data_degraded"
        # 既定 (allow_degraded=True) では数値を出してよい。
        assert tq.technical_row_is_usable(_row(freshness_status="degraded")) is True
        # 数値を一切許さない経路は False を渡す。
        assert tq.technical_row_is_usable(
            _row(freshness_status="degraded"), allow_degraded=False) is False


class TestPromptFormatting:
    def test_an_unusable_row_shows_the_reason_and_date_not_none_values(self):
        """理由と基準日が消えて "price=None RSI=None" になってはならない
        (Codex レビュー round 9)。"""
        text = tq.format_for_prompt(_row(rebuild_unresolved=True))
        assert "判定不能" in text
        assert "rebuild_unresolved" in text
        assert "2026-08-24" in text
        assert "None" not in text
        assert "RSI=10" not in text

    def test_a_usable_row_still_shows_numbers(self):
        text = tq.format_for_prompt(_row())
        assert "RSI=10" in text
        assert "判定不能" not in text

    def test_a_degraded_row_shows_numbers_with_the_delay_noted(self):
        text = tq.format_for_prompt(_row(freshness_status="degraded"))
        assert "RSI=10" in text
        assert "1セッション遅延" in text
        assert "2026-08-24" in text


def test_the_module_stays_free_of_heavy_imports():
    """どこからでも安全に import できること。以前この述語は
    scenario_engine にあったが、あちらは import 時に alert 経由で
    yfinance を読みグローバル timeout を書き換える
    (Codex レビュー round 9 で実測)。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import technical_quality; "
         "print(int(any(m in sys.modules for m in ('yfinance', 'pandas', 'alert'))))"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "0", "technical_quality が重い依存を引いている"


class TestConsumersUseTheContract:
    """契約は「読む側すべてが通る」ことで初めて意味を持つ。

    round 9 までは technical_quality.py 自体は正しかったのに、本体AIプロンプトと
    execution_readiness が旧判定のまま残っていた (Codex レビュー round 10)。
    consumer ごとに、契約が拒否する行が実際に拒否されることを固定する。
    """

    def test_the_core_ai_prompt_omits_numbers_for_every_unusable_row(self):
        """Long/Medium/Swing 全プロンプトが通る唯一のテクニカル要約。"""
        import analyst

        numbers = {"rsi": 10.0, "rsi_signal": "oversold", "macd_histogram": -1.0,
                   "composite_score": -80.0}
        for bad in [
            {**numbers},                                                    # 品質欠損
            {**numbers, "data_quality_status": "weird"},                    # 未知値
            {**numbers, "data_quality_status": "ok", "freshness_status": "stale"},
            {**numbers, "data_quality_status": "ok"},                       # 鮮度欠損
            {**numbers, "data_quality_status": "ok", "freshness_status": "fresh",
             "rebuild_unresolved": True},
        ]:
            text = analyst._fmt_technical_state(["MDB"], {"MDB": bad})
            assert "RSI=10" not in text, f"数値が漏れた: {bad} -> {text}"
            assert "oversold" not in text, f"シグナルが漏れた: {bad} -> {text}"

    def test_the_core_ai_prompt_flags_a_degraded_row_but_keeps_numbers(self):
        import analyst

        text = analyst._fmt_technical_state(["MDB"], {"MDB": {
            "rsi": 10.0, "rsi_signal": "oversold",
            "data_quality_status": "ok", "freshness_status": "degraded",
            "data_as_of": "2026-08-20",
        }})
        assert "RSI=10" in text
        assert "1セッション遅延" in text
        assert "2026-08-20" in text

    def test_scenarios_do_not_fire_on_a_degraded_row(self):
        """シナリオ発火は決定論的買付を生む。しかもシグナル銘柄と購入銘柄は
        別のことがある (credit_crisis: SPY/XLF 観測 -> GLD 購入) ので、
        購入銘柄側の readiness では degraded を検知できない。発火根拠に
        しないのが唯一の fail-closed な扱い (Codex レビュー round 10)。
        """
        import scenario_engine as se

        scenario = {"detect": {"technical": {"MDB_rsi": {
            "condition": "below", "threshold": 20,
            "ticker": "MDB", "indicator": "rsi"}}}}
        degraded = {"rsi": 10.0, "data_quality_status": "ok",
                    "freshness_status": "degraded", "data_as_of": "2026-08-20"}

        assert se._eval_technical(scenario, {"tickers": {"MDB": degraded}})[0]["matched"] is False
        # fresh なら従来どおり成立する = 対照群。
        fresh = {**degraded, "freshness_status": "fresh"}
        assert se._eval_technical(scenario, {"tickers": {"MDB": fresh}})[0]["matched"] is True

    def test_every_scenario_read_site_rejects_degraded(self):
        """4経路すべてが allow_degraded=False を通ること。"""
        import scenario_engine as se

        degraded = {"data_quality_status": "ok", "freshness_status": "degraded"}

        # エイリアス経由
        assert se._resolve_ticker_change(
            "defense_etf_ita", {"condition": "drop_pct_5d"},
            {"tickers": {"ITA": {**degraded, "change_5d_pct": -20.0}}}) is None
        # EWJ 相対
        ewj = se._eval_technical(
            {"detect": {"technical": {"ewj_outperforms_spy_20d": {"condition": "true"}}}},
            {"tickers": {"EWJ": {**degraded, "change_20d_pct": 20.0},
                         "SPY": {**degraded, "change_20d_pct": 0.0}}})
        assert ewj[0]["matched"] is False
        # 日経/TOPIX
        nikkei = se._eval_technical(
            {"detect": {"technical": {"nikkei_or_topix_above_ma50": {"condition": "true"}}}},
            {"tickers": {"1306.T": {**degraded, "ma50_diff": 10.0}}})
        assert nikkei[0]["matched"] is False


class TestAxesAreIndependent:
    """品質軸と鮮度軸を別々に問える必要がある。

    execution_readiness は引き継ぎ行 (rebuild_unresolved) で保存済みの鮮度
    だけを無視し、品質軸は必ず評価する。合成判定しか無いと軸を分けられず、
    品質 block が一緒に消える (Codex レビュー round 11 で再現)。
    """

    def test_the_quality_axis_ignores_freshness_and_the_marker(self):
        assert tq.classify_quality_axis(
            {"data_quality_status": "ok", "freshness_status": "stale"}) == (tq.USABLE, None)
        assert tq.classify_quality_axis(
            {"data_quality_status": "ok", "rebuild_unresolved": True}) == (tq.USABLE, None)
        assert tq.classify_quality_axis(
            {"data_quality_status": "blocked", "rebuild_unresolved": True}) == (
                tq.UNUSABLE, "data_quality_blocked")
        assert tq.classify_quality_axis({"freshness_status": "fresh"}) == (
            tq.UNUSABLE, "data_quality_unknown")

    def test_the_freshness_axis_ignores_quality_and_the_marker(self):
        assert tq.classify_freshness_axis(
            {"freshness_status": "fresh", "data_quality_status": "blocked"}) == (tq.USABLE, None)
        assert tq.classify_freshness_axis(
            {"freshness_status": "degraded"}) == (tq.DEGRADED, "technical_data_degraded")
        assert tq.classify_freshness_axis(
            {"freshness_status": "stale"}) == (tq.UNUSABLE, "technical_data_stale")
        assert tq.classify_freshness_axis({}) == (tq.UNUSABLE, "technical_row_missing")

    def test_the_combined_verdict_reports_quality_before_the_marker(self):
        """引き継ぎ行でも、品質軸の方が重い問題ならそちらを理由に出す
        (理由コードの取り違えを防ぐ)。"""
        verdict, reason = tq.classify_technical_row({
            "data_quality_status": "blocked", "freshness_status": "fresh",
            "rebuild_unresolved": True})
        assert (verdict, reason) == (tq.UNUSABLE, "data_quality_blocked")

        verdict, reason = tq.classify_technical_row({
            "data_quality_status": "ok", "freshness_status": "fresh",
            "rebuild_unresolved": True})
        assert (verdict, reason) == (tq.UNUSABLE, "rebuild_unresolved")
