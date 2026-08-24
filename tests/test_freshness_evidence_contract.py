"""保有・現金の鮮度契約: 停止条件を壁時計から乖離の証拠へ移した分の検証。

2026-08 に holdings/cash が 96h の壁時計で失効し、6営業日連続で発注候補が
0件になった。holdings/cash には定期更新の生産者がいない (楽天CSV取込か
本人の表明でしか動かない) ため、時間が経つこと自体が停止条件になっていた。

新しい契約:
  - 壁時計は助言 (refresh 96h) に降格。最終防衛線として stale 720h を残す
  - 停止は「記録済みなのに holdings へ反映されていない約定」を掴んだとき
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import analysis_snapshot
import holdings_freshness
from freshness_policy import SOURCE_FRESHNESS_POLICIES, stale_after_hours


class TestPolicyShape:
    def test_wall_clock_no_longer_stops_within_a_few_days(self):
        # 4日放置しただけで停止するのが自己ロックの直接の原因だった。
        for source in ("holdings", "cash"):
            assert stale_after_hours(source) > 96.0

    def test_a_backstop_still_exists(self):
        # 「証拠が無い＝永遠に fresh」にはしない。何も分からない状態が
        # 続けば、いずれ止まらなければならない。
        for source in ("holdings", "cash"):
            assert stale_after_hours(source) <= 720.0

    def test_refresh_advice_comes_well_before_the_backstop(self):
        for source in ("holdings", "cash"):
            policy = SOURCE_FRESHNESS_POLICIES[source]
            assert policy.refresh_after_hours is not None
            assert policy.refresh_after_hours < policy.stale_after_hours
            policy.validate(source)


class TestDivergenceDetection:
    def test_no_recorded_activity_is_not_divergence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            holdings_freshness, "plan_rollforward",
            lambda **kw: {"planned": [], "skipped": []},
        )
        result = holdings_freshness.holdings_divergence(base_dir=tmp_path)
        assert result["diverged"] is False
        assert result["unapplied_count"] == 0
        assert result["unresolved_count"] == 0

    def test_an_unapplied_confirmed_fill_is_divergence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            holdings_freshness, "plan_rollforward",
            lambda **kw: {"planned": [{"execution_id": "e1", "ticker": "V"}], "skipped": []},
        )
        result = holdings_freshness.holdings_divergence(base_dir=tmp_path)
        assert result["diverged"] is True
        assert result["unapplied_count"] == 1

    def test_an_unresolvable_fill_is_also_divergence(self, tmp_path, monkeypatch):
        # 差分に変換できない約定は rollforward では解消しない。それでも
        # holdings が事実を表していない証拠なので止める対象。
        monkeypatch.setattr(
            holdings_freshness, "plan_rollforward",
            lambda **kw: {"planned": [], "skipped": [
                {"execution_id": "e2", "reason": "would_go_negative"},
            ]},
        )
        result = holdings_freshness.holdings_divergence(base_dir=tmp_path)
        assert result["diverged"] is True
        assert result["unresolved_count"] == 1

    def test_it_reports_without_touching_anything(self, tmp_path, monkeypatch):
        # 検出は判定に使うだけ。ここで holdings を書き換えたら、
        # 「読んだだけで状態が変わる」ことになる。
        holdings = tmp_path / "holdings.json"
        holdings.write_text(json.dumps({"V": {"shares": 3}}), encoding="utf-8")
        before = holdings.read_bytes()
        monkeypatch.setattr(
            holdings_freshness, "plan_rollforward",
            lambda **kw: {"planned": [{"execution_id": "e1"}], "skipped": []},
        )
        holdings_freshness.holdings_divergence(base_dir=tmp_path)
        assert holdings.read_bytes() == before


class TestSnapshotGating:
    def _provenance(self, tmp_path, *, age_hours: float, diverged: bool):
        path = tmp_path / "holdings.json"
        path.write_text(json.dumps({"V": {"shares": 1}}), encoding="utf-8")
        now = datetime(2026, 8, 17, 12, 0, 0)
        as_of = now - timedelta(hours=age_hours)
        import os
        os.utime(path, (as_of.timestamp(), as_of.timestamp()))
        return analysis_snapshot._provenance_for_file(
            path, ts_keys=("__mtime__",),
            max_age_hours=stale_after_hours("holdings"),
            now=now, source_label="holdings.json",
            diverged=diverged, base_dir=tmp_path,
        )

    def test_four_days_idle_is_not_stale_anymore(self, tmp_path):
        # これが 2026-08 に全候補を review へ落としていたケース。
        assert self._provenance(tmp_path, age_hours=100, diverged=False).freshness_status != "stale"

    def test_divergence_is_stale_even_when_the_file_is_fresh(self, tmp_path):
        # 今朝CSVを取り込んでいても、その後に記録された未反映の約定が
        # あれば holdings は事実を表していない。
        prov = self._provenance(tmp_path, age_hours=1, diverged=True)
        assert prov.freshness_status == "stale"

    def test_the_reason_is_visible_in_the_source_label(self, tmp_path):
        # 「なぜ止まったか」が読めないと、時間切れと乖離を取り違える。
        prov = self._provenance(tmp_path, age_hours=1, diverged=True)
        assert "diverged" in prov.source

    def test_the_backstop_still_fires_without_any_divergence(self, tmp_path):
        prov = self._provenance(tmp_path, age_hours=1000, diverged=False)
        assert prov.freshness_status == "stale"

    def test_divergence_does_not_relabel_an_already_stale_source(self, tmp_path):
        # 既に時間切れなら、乖離の有無で理由を上書きしない。
        prov = self._provenance(tmp_path, age_hours=1000, diverged=True)
        assert prov.freshness_status == "stale"
        assert "diverged" not in prov.source


class TestFailureIsolation:
    def test_an_unreadable_ledger_does_not_crash_the_analysis(self, tmp_path, monkeypatch):
        # 判定できないことを理由に分析全体を落としてはいけない —— これは
        # 変わらない。ただし答えは 2026-08-24 のレビューで反転した:
        # attestation は壁時計での失効という後ろ盾を外すため、この判定は
        # attested な holdings/cash に対する唯一の停止条件になっている。
        # 台帳が一時的に読めないだけで「乖離なし」を仮定すると、attest 済み
        # の古い holdings が無期限に fresh を名乗り続ける
        # (Codex レビューで再現: 800h前 + attestation + 台帳読込エラー →
        # decision snapshot が fresh のまま)。分析を落とさないことと
        # fail-open にすることは別の要求で、後者だけを諦める。
        def boom(**kw):
            raise RuntimeError("ledger unreadable")

        monkeypatch.setattr(holdings_freshness, "holdings_divergence", boom)
        assert analysis_snapshot._holdings_diverged(Path(tmp_path)) is True

    def test_the_answer_is_fixed_for_one_snapshot(self, tmp_path, monkeypatch):
        # 同じスナップショット内で holdings と cash が違う答えを見ると、
        # 片方だけ止まる不整合が起きる。build 側が1回だけ評価すること。
        calls = []

        def counted(**kw):
            calls.append(1)
            return {"diverged": False}

        monkeypatch.setattr(holdings_freshness, "holdings_divergence", counted)
        for name in ("holdings.json", "account.json", "technical_state.json"):
            (tmp_path / name).write_text("{}", encoding="utf-8")
        analysis_snapshot.build_base_snapshot(base_dir=tmp_path)
        assert len(calls) == 1

    def test_no_module_level_cache_leaks_between_runs(self, tmp_path, monkeypatch):
        # 台帳が変われば答えも変わらなければならない。
        answers = iter([{"diverged": False}, {"diverged": True}])
        monkeypatch.setattr(
            holdings_freshness, "holdings_divergence", lambda **kw: next(answers),
        )
        assert analysis_snapshot._holdings_diverged(Path(tmp_path)) is False
        assert analysis_snapshot._holdings_diverged(Path(tmp_path)) is True
