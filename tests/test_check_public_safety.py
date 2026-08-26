"""scripts/check_public_safety.py の identity baseline。

Codex レビュー round 19: 出現「回数」だけの baseline は、既存の識別子を
消して同数の**新しい**秘密情報を入れても通っていた (api/routes/cash.py の
許容9件を新規9件の wallet 名へ置換して failures=[])。ソース文字列の
grep ではなく、実関数 (`_scan_identities` / `_identity_baseline`) を直接
呼んで検証する — 文字列検査は自分自身への誤マッチを何度も踏んだ前科がある。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_public_safety.py"

if not SCRIPT.exists():
    pytest.skip("check_public_safety.py is public-mirror only", allow_module_level=True)

spec = importlib.util.spec_from_file_location("check_public_safety", SCRIPT)
cps = importlib.util.module_from_spec(spec)
sys.modules["check_public_safety"] = cps
spec.loader.exec_module(cps)


class TestValueBasedBaseline:
    def test_the_same_known_value_repeated_passes(self):
        baseline = {("f.py", "cash wallet route", "CASH_JPY_SBI")}
        failures = []
        cps._scan_identities("f.py", "CASH_JPY_SBI CASH_JPY_SBI CASH_JPY_SBI",
                             failures, baseline)
        assert failures == []

    def test_swapping_to_a_new_value_at_the_same_count_fails(self):
        """回数ベースの baseline が見逃していた事故そのもの。

        既知の wallet 名が消え、未知の新しい wallet 名が同数出現しても、
        baseline に載っていない値である以上は失敗しなければならない。
        """
        baseline = {("f.py", "cash wallet route", "CASH_JPY_SBI")}
        failures = []
        cps._scan_identities("f.py", "CASH_JPY_NEWSECRET CASH_JPY_NEWSECRET",
                             failures, baseline)
        assert failures, "未知の値への置換を検出できていない (回数ベースの穴)"
        assert "CASH_JPY_NEWSECRET" in failures[0]

    def test_an_unbaselined_new_value_in_an_already_baselined_file_fails(self):
        """既に baseline済みのファイルへの新規追記 (round 18 で修正した
        シナリオ) が、値ベースへの書き換え後も引き続き検出されること。"""
        baseline = {("f.py", "household role (en)", "husband")}
        failures = []
        cps._scan_identities(
            "f.py", "husband " * 5 + "wife", failures, baseline)
        assert any("wife" in f for f in failures)

    def test_reproduces_the_real_cash_py_scenario(self):
        """レビューで実測した再現手順そのもの。"""
        real_baseline = cps._identity_baseline()
        text = (ROOT / "api" / "routes" / "cash.py").read_text(encoding="utf-8")
        matches = cps.IDENTITY_PATTERNS["cash wallet route"].findall(text)
        assert matches, "fixture の前提が崩れている: cash.py に wallet route が無い"

        # 現状のファイルは baseline を通る。
        assert cps._scan_identities("api/routes/cash.py", text, [], real_baseline) is None
        clean_failures = []
        cps._scan_identities("api/routes/cash.py", text, clean_failures, real_baseline)
        assert clean_failures == []

        # 全出現を同数の新しい値へ置換すると、値ベースでは検出される。
        replaced = text
        for i, old_val in enumerate(sorted(set(matches))):
            replaced = replaced.replace(old_val, f"CASH_JPY_NEWSECRET{i}")
        swapped_failures = []
        cps._scan_identities("api/routes/cash.py", replaced, swapped_failures, real_baseline)
        assert swapped_failures, "同数の新しい値への置換が素通りした"


def test_the_baseline_data_file_is_excluded_from_its_own_scan():
    """baseline ファイル自身は識別子の一覧というデータファイル。

    生成した baseline.txt には実際の値が並ぶので、通常どおり identity
    検査にかけると自己参照で必ず失敗する (鶏卵問題)。除外されていること。
    """
    baseline_rel = str(cps.IDENTITY_BASELINE_PATH.relative_to(cps.ROOT))
    text = cps.IDENTITY_BASELINE_PATH.read_text(encoding="utf-8")
    # baseline ファイルには当然 baseline に載っている値しか無いはずだが、
    # 念のため「除外されているから通っている」ことを、除外を外した場合と
    # 比較して確認する。
    baseline = cps._identity_baseline()
    without_exclusion = []
    cps._scan_identities(baseline_rel, text, without_exclusion, baseline)
    # 除外なしで直接呼ぶと、baseline.txt 自身の内容が新規値として
    # 検出されてしまう (これが main() 側で除外している理由)。
    assert without_exclusion, (
        "baseline ファイルが誤って除外なしでも通るなら、"
        "そもそも実データを含んでいないということ — fixture が壊れている")
