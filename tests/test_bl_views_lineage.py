"""Stage 0C: evidence anti-circularity for Black-Litterman tier views.

背景: bl_alpha_sources.py の docstring が既に名指ししている "confidence
laundering" 問題 (Codex Round 2 指摘、P2-27 で BL_USE_INDEPENDENT_ALPHA を
追加して部分対応済み) の残存部分。デフォルト経路 (bl_mode="0",
independent_count=0) では、_extract_bl_views() が long/medium/short の
3ティア分析から集めた view の分散 (Ω) をそのまま「Black-Litterman 定量モデル
期待リターン」として Opus プロンプトへ提示していた。これは:

  (a) 同一ティアの priority_actions に同じ ticker が複数回出現した場合、
      その重複をそのまま n_signals に数えて Ω を過小評価しうる (同一
      lineage の水増し)
  (b) 3ティアは同じ shared_ctx を読むため、3ティア一致は本来「独立した
      3つの情報源が裏付けた」ことを意味しないにもかかわらず、"Black-
      Litterman" 「定量モデル」という語彙がそう読める権威づけをしていた

本テストは (a) の重複排除と、independent_count=0 のときの文言変更 (b) を
検証する。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyst  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_bl_views(tmp_path, monkeypatch):
    """bl_views.json の読み書きを実リポジトリの外へ逃がす。

    BASE_DIR は analyst/__init__.py のモジュールグローバルで、
    _extract_bl_views / _load_bl_views_for_opus はどちらも呼び出しの都度
    BASE_DIR を再評価するため、これで両方が tmp_path に向く。
    """
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.delenv("BL_USE_INDEPENDENT_ALPHA", raising=False)
    return tmp_path


def _action(ticker, atype="add", urgency="medium", confidence_pct=70):
    return {"ticker": ticker, "type": atype, "urgency": urgency, "confidence_pct": confidence_pct}


# ---------------------------------------------------------------------------
# (a) 同一ティア内の重複は1 lineage にまとめる
# ---------------------------------------------------------------------------


def test_duplicate_ticker_within_one_tier_counts_as_one_lineage():
    """long_a に同じ ticker が2回出現しても、long lineage は1件のみ。"""
    long_a = {"priority_actions": [_action("AVGO"), _action("AVGO")]}
    medium_a = {"priority_actions": []}
    short_a = {"priority_actions": []}

    views = analyst._extract_bl_views(long_a, medium_a, short_a)

    assert views["AVGO"]["n_signals"] == 1
    assert views["AVGO"]["evidence_lineage_ids"] == ["long"]


def test_three_distinct_tiers_count_as_three_lineages():
    long_a = {"priority_actions": [_action("NVDA")]}
    medium_a = {"priority_actions": [_action("NVDA")]}
    short_a = {"priority_actions": [_action("NVDA")]}

    views = analyst._extract_bl_views(long_a, medium_a, short_a)

    assert views["NVDA"]["n_signals"] == 3
    assert views["NVDA"]["evidence_lineage_ids"] == ["long", "medium", "short"]


def test_lineage_order_is_stable_long_medium_short():
    """medium だけ、long だけ、のように部分的にしか無い場合も順序が安定する。"""
    long_a = {"priority_actions": []}
    medium_a = {"priority_actions": [_action("META")]}
    short_a = {"priority_actions": [_action("META")]}

    views = analyst._extract_bl_views(long_a, medium_a, short_a)
    assert views["META"]["evidence_lineage_ids"] == ["medium", "short"]


def test_duplicate_does_not_shrink_omega_via_spurious_n_signals():
    """本題: 同一ティアの重複が n_signals を水増しして Ω (不確実性) を
    過小評価させないこと。1 lineage のみの場合と比べて Ω が変わらない。"""
    single = analyst._extract_bl_views(
        {"priority_actions": [_action("XLF")]}, {"priority_actions": []}, {"priority_actions": []},
    )
    duplicated = analyst._extract_bl_views(
        {"priority_actions": [_action("XLF"), _action("XLF"), _action("XLF")]},
        {"priority_actions": []}, {"priority_actions": []},
    )
    assert single["XLF"]["n_signals"] == duplicated["XLF"]["n_signals"] == 1
    assert single["XLF"]["variance"] == duplicated["XLF"]["variance"]


def test_second_occurrence_in_same_tier_does_not_override_first():
    """同一ティア内の2件目 (別方向の推奨) は無視され、最初の1件が採用される。"""
    long_a = {"priority_actions": [_action("QCOM", atype="add"), _action("QCOM", atype="reduce")]}
    views = analyst._extract_bl_views(long_a, {"priority_actions": []}, {"priority_actions": []})
    # add の期待リターンが採用されている (reduce ではない) ことを間接確認:
    # add は正のリターン、reduce は負のリターンになるはず。
    assert views["QCOM"]["mean_view"] > 0


# ---------------------------------------------------------------------------
# trim/rebalance スキップとの相互作用（既存動作の保持）
# ---------------------------------------------------------------------------


def test_trim_first_then_add_in_same_tier_still_counts_the_add():
    """1件目が trim (スキップ対象) で書き込まれず、2件目の add が採用される
    ケース。dedup ガードは「実際に採用した lineage」だけを見るべきで、
    スキップされた候補まで lineage 済みとして扱ってはいけない。"""
    long_a = {"priority_actions": [_action("COST", atype="trim"), _action("COST", atype="add")]}
    views = analyst._extract_bl_views(long_a, {"priority_actions": []}, {"priority_actions": []})
    assert views["COST"]["n_signals"] == 1
    assert views["COST"]["evidence_lineage_ids"] == ["long"]


# ---------------------------------------------------------------------------
# (b) independent_count == 0 のとき corroboration 文言を出さない
# ---------------------------------------------------------------------------


def test_opus_prompt_omits_black_litterman_framing_when_no_independent_source(tmp_path):
    long_a = {"priority_actions": [_action("AVGO")]}
    medium_a = {"priority_actions": [_action("AVGO")]}
    short_a = {"priority_actions": [_action("AVGO")]}
    analyst._extract_bl_views(long_a, medium_a, short_a)  # bl_mode 未設定 → independent_count=0

    saved = json.loads((tmp_path / "bl_views.json").read_text(encoding="utf-8"))
    assert saved["independent_count"] == 0

    text = analyst._load_bl_views_for_opus()
    assert "Black-Litterman" not in text
    assert "定量モデル" not in text
    assert "独立検証なし" in text
    assert "2/3ティア一致" not in text  # sanity: AVGOは3ティア一致のはず
    assert "3/3ティア一致" in text


def test_opus_prompt_keeps_quant_framing_when_independent_source_present(tmp_path, monkeypatch):
    """independent_count > 0 (真に独立な alpha 源が混入) のときは
    従来通り Black-Litterman の定量的フレーミングを維持する。"""
    def _fake_independent(tickers):
        return {
            t: {
                "bull_view": 0.05, "bear_view": 0.03, "macro_view": 0.04,
                "mean_view": 0.04, "variance": 0.01, "n_signals": 1,
                "avg_confidence": None, "source": "analyst_consensus",
            }
            for t in tickers
        }

    monkeypatch.setenv("BL_USE_INDEPENDENT_ALPHA", "mix")
    monkeypatch.setattr("bl_alpha_sources.compute_independent_views", _fake_independent)

    long_a = {"priority_actions": [_action("AVGO")]}
    analyst._extract_bl_views(long_a, {"priority_actions": []}, {"priority_actions": []})

    saved = json.loads((tmp_path / "bl_views.json").read_text(encoding="utf-8"))
    assert saved["independent_count"] > 0

    text = analyst._load_bl_views_for_opus()
    assert "Black-Litterman" in text
    assert "Ω=" in text


def test_opus_prompt_empty_when_no_bl_views_file():
    assert analyst._load_bl_views_for_opus() == ""
