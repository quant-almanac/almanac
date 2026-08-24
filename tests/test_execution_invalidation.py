"""Stage -1: execution_invalidation の単一権威テスト。

背景: 2026-07-27 06:24 の分析 (analysis_id=a64491d6-...) は、n_test=0 の
未検証 GINN モデルの値を tier プロンプトへ混入させ、AVGO/XLF の trim を
execution_readiness="ready" として出力した。対象ポジションの証券会社照合も
7/14 で止まっていた。

同じ推奨を Today (ai_portfolio_analysis.json) と注文API (最新 analysis 優先、
古い推薦は action_state.json へフォールバック) の2経路が読むため、
片方だけ直すと他方から通ってしまう。本テストは、overlay-only の
execution_invalidation_state.json が両経路・dedup・通知の4箇所すべてで
効くことを確認する。

必須テスト (計画書 Stage -1 より):
  - Today で review になる            -> test_today系 (別ファイルの today テストで確認)
  - analysis ID 指定の新規注文が409   -> test_linked_ai_readiness_flips_to_review
  - action state ID 指定でも409       -> 同上
  - 過去の実約定 fill 報告は拒否しない -> test_fill_timing_classification, test_sync_execution_status_tags_fill_timing
  - 再起動後も維持                    -> test_state_persists_across_reload
  - 次の analysis ID に継承されない    -> test_different_analysis_id_not_invalidated
  - 新分析の dedup で復活しない        -> test_dedup_skips_invalidated_pending
  - 通知の実行候補から除外される       -> test_get_all_pending_excludes_invalidated
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import execution_invalidation as ei  # noqa: E402
import action_state_tracker as ast  # noqa: E402


ANALYSIS_ID = "a64491d6-54e8-45c7-beab-b9d52487478a"
AVGO_ID = "c168f75003a3"
XLF_ID = "5185b2d8b12e"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "STATE_PATH", tmp_path / "execution_invalidation_state.json")
    monkeypatch.setattr(ast, "STATE_FILE", tmp_path / "action_state.json")
    # ei._state_path() は ALMANAC_STATE_DIR が環境にあれば STATE_PATH より
    # 優先する。これを差し替えないと、ALMANAC_STATE_DIR を明示指定した
    # 受け入れ実行 (フレッシュな全体テスト比較など) では全テストが同じ
    # 共有ファイルを読み書きし、無効化状態が前のテストから漏れて残る
    # (Codex レビュー 2026-08-24 で再現: before_ids が空集合になった)。
    monkeypatch.setenv("ALMANAC_STATE_DIR", str(tmp_path))
    return tmp_path


def _seed_action_state(path: Path, *, analysis_id: str = ANALYSIS_ID) -> None:
    import json
    actions = {
        AVGO_ID: {
            "id": AVGO_ID, "ticker": "AVGO", "action_type": "trim",
            "status": "pending", "analysis_id": analysis_id,
            "recommended_at": "2026-07-27T06:24:10.876530",
            "execution_readiness": "ready", "execution_block_reasons": [],
            "execution_account": "一般",
        },
        XLF_ID: {
            "id": XLF_ID, "ticker": "XLF", "action_type": "trim",
            "status": "pending", "analysis_id": analysis_id,
            "recommended_at": "2026-07-27T06:24:10.876530",
            "execution_readiness": "ready", "execution_block_reasons": [],
            "execution_account": "Medium",
        },
    }
    path.write_text(json.dumps({"actions": actions, "last_updated": ""}, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# コア: invalidate / resolve
# ---------------------------------------------------------------------------


def test_invalidate_requires_approval():
    with pytest.raises(ValueError, match="approved_by"):
        ei.invalidate_analysis(
            analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
            reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT],
            approved_by="", source_incident="test",
        )


def test_resolve_matches_by_analysis_id_or_action_state_id():
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID, XLF_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )
    assert ei.resolve_execution_invalidation(analysis_id=ANALYSIS_ID) is not None
    assert ei.resolve_execution_invalidation(action_state_id=AVGO_ID) is not None
    assert ei.resolve_execution_invalidation(action_state_id=XLF_ID) is not None
    assert ei.resolve_execution_invalidation(action_state_id="unrelated-id") is None
    assert ei.resolve_execution_invalidation() is None


def test_different_analysis_id_not_invalidated():
    """次の analysis ID には無効化が継承されない。"""
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )
    assert ei.resolve_execution_invalidation(analysis_id="some-new-analysis-id") is None


def test_state_persists_across_reload(tmp_path):
    """プロセス再起動相当 (モジュール再読込) しても無効化が維持される。"""
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )
    assert ei.STATE_PATH.exists()
    # 新しいプロセスを模して state を read し直す (グローバルはキャッシュしない設計)
    fresh = ei._load_state()
    assert any(e.get("analysis_id") == ANALYSIS_ID for e in fresh["entries"])


def test_subprocess_honors_state_dir(tmp_path):
    """CLI/subprocess callers must not fall back to the production overlay."""
    state_dir = tmp_path / "isolated-state"
    env = os.environ.copy()
    env["ALMANAC_STATE_DIR"] = str(state_dir)
    code = f"""
import json
import execution_invalidation as ei
ei.invalidate_analysis(
    analysis_id={ANALYSIS_ID!r},
    action_state_ids={[AVGO_ID]!r},
    reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT],
    approved_by="user:test",
    source_incident="subprocess-test",
)
print(json.dumps(ei.resolve_execution_invalidation(analysis_id={ANALYSIS_ID!r})))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["analysis_id"] == ANALYSIS_ID
    assert (state_dir / "execution_invalidation_state.json").exists()


def test_reinvalidate_replaces_not_duplicates():
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:a",
        source_incident="first",
    )
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID, XLF_ID],
        reason_codes=[ei.REASON_POSITION_FRESHNESS_UNVERIFIED], approved_by="user:b",
        source_incident="second",
    )
    state = ei._load_state()
    matching = [e for e in state["entries"] if e.get("analysis_id") == ANALYSIS_ID]
    assert len(matching) == 1
    assert matching[0]["approved_by"] == "user:b"
    assert XLF_ID in matching[0]["action_state_ids"]


# ---------------------------------------------------------------------------
# fill 境界: 過去の実約定は拒否しない
# ---------------------------------------------------------------------------


def test_fill_timing_classification():
    entry = {"invalidated_at": "2026-07-27T12:00:00+00:00"}
    assert ei.classify_fill_timing(entry, "2026-07-27T06:00:00+00:00") == "pre_invalidation_fact"
    assert ei.classify_fill_timing(entry, "2026-07-27T18:00:00+00:00") == "post_invalidation_fill"
    # 不明な時刻は安全側 (取引の事実を握りつぶさない) の分類にする
    assert ei.classify_fill_timing(entry, None) == "post_invalidation_unknown_timing"


def test_sync_execution_status_tags_fill_timing_but_never_blocks(tmp_path):
    """fill 記録そのものは無効化後も拒否されず、印だけが付く。"""
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)

    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )

    action_id = ast.sync_execution_status(
        ticker="AVGO", direction="sell", execution_status="filled",
        action_state_id=AVGO_ID,
    )
    assert action_id == AVGO_ID
    state = ast._load()
    entry = state["actions"][AVGO_ID]
    assert entry["status"] == "filled"
    assert entry["filled_at"] is not None
    assert entry["fill_timing_vs_invalidation"] == "post_invalidation_unknown_timing" or \
        entry["fill_timing_vs_invalidation"] in ("pre_invalidation_fact", "post_invalidation_fill")


# ---------------------------------------------------------------------------
# 注文API 配線: 409 になること (実際の関数を直接呼ぶ)
# ---------------------------------------------------------------------------


def test_linked_ai_readiness_flips_to_review(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    import api.routes.actions as actions_mod
    monkeypatch.setattr(actions_mod, "BASE_DIR", tmp_path)

    def _strict_load(path):
        import json
        return json.loads(Path(path).read_text(encoding="utf-8"))
    monkeypatch.setattr(actions_mod, "_load_json_strict", _strict_load)
    monkeypatch.setattr(actions_mod, "ANALYSIS_FILE", tmp_path / "ai_portfolio_analysis.json")
    (tmp_path / "ai_portfolio_analysis.json").write_text("{}", encoding="utf-8")

    readiness_before, _ = actions_mod._linked_ai_readiness_values(
        analysis_id=ANALYSIS_ID, action_state_id=AVGO_ID, ticker="AVGO", direction="sell",
    )
    assert readiness_before == "ready"

    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID, XLF_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )

    readiness_after, reasons_after = actions_mod._linked_ai_readiness_values(
        analysis_id=ANALYSIS_ID, action_state_id=AVGO_ID, ticker="AVGO", direction="sell",
    )
    assert readiness_after == "review"
    assert reasons_after[0]["code"] == ei.REASON_UNVALIDATED_GINN_INPUT

    # action_state_id だけの指定でも同様にブロックされる (analysis_id 経由の抜け道を防ぐ)
    readiness_by_id_only, _ = actions_mod._linked_ai_readiness_values(
        analysis_id=None, action_state_id=XLF_ID, ticker="XLF", direction="sell",
    )
    assert readiness_by_id_only == "review"


def test_original_action_state_record_is_never_mutated(tmp_path, monkeypatch):
    """overlay-only: action_state.json の原レコードを書き換えない。"""
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    import json
    before = json.loads(state_path.read_text(encoding="utf-8"))

    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID, XLF_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )
    # 何らかの読み取りパスを経由しても action_state.json 自体は不変
    ast.get_all_pending(days_threshold=0)

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert before == after


# ---------------------------------------------------------------------------
# dedup 配線: 無効化済み pending は新分析で再利用されない
# ---------------------------------------------------------------------------


def test_dedup_skips_invalidated_pending(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    dedup_key = ast.dedup_key_for_action({
        "ticker": "AVGO", "type": "trim", "execution_account": "一般",
    })
    state = ast._load()
    assert ast._find_existing_pending(state, dedup_key) == AVGO_ID

    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )
    state = ast._load()
    assert ast._find_existing_pending(state, dedup_key) is None


def test_new_analysis_after_invalidation_gets_a_new_action_state_id(tmp_path, monkeypatch):
    """同日・同intentでも旧IDへ衝突せず successor を作る。"""
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)
    monkeypatch.setattr(ast, "STATE_FILE", state_path)
    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )

    added = ast.record_recommendations([{
        "analysis_id": "new-analysis-id",
        "ticker": "AVGO", "type": "trim", "urgency": "medium",
        "action": "再照合後の新しい候補",
        "execution_owner": "husband", "execution_broker": "rakuten",
        "execution_account": "一般",
    }])
    rows = ast._load()["actions"]
    successors = [
        row for key, row in rows.items()
        if key != AVGO_ID and row.get("ticker") == "AVGO"
    ]
    assert added == 1
    assert len(successors) == 1
    assert successors[0]["id"] != AVGO_ID
    assert successors[0]["analysis_id"] == "new-analysis-id"


# ---------------------------------------------------------------------------
# 通知/プロンプト配線: get_all_pending から除外される
# ---------------------------------------------------------------------------


def test_get_all_pending_excludes_invalidated(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _seed_action_state(state_path)
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    before_ids = {p["id"] for p in ast.get_all_pending(days_threshold=0)}
    assert {AVGO_ID, XLF_ID} <= before_ids

    ei.invalidate_analysis(
        analysis_id=ANALYSIS_ID, action_state_ids=[AVGO_ID, XLF_ID],
        reason_codes=[ei.REASON_UNVALIDATED_GINN_INPUT], approved_by="user:test",
        source_incident="test",
    )

    after_ids = {p["id"] for p in ast.get_all_pending(days_threshold=0)}
    assert AVGO_ID not in after_ids
    assert XLF_ID not in after_ids
