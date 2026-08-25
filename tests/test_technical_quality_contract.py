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
