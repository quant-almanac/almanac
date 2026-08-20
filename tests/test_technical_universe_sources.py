"""テクニカル取得対象の候補ソース配線の検証。

2026-08-21: technical_signals は "screen_results_us.json" を読もうとしていたが、
screener.py はそんなファイルを書かない (US は screen_results.json / 朝バッチは
screen_results_morning.json)。load_json が既定値 {} を返すだけなので例外も
警告も出ず、米国スクリーナー候補が丸ごとテクニカル取得対象から漏れ続けていた。
結果として、その候補が発注候補に上がると technical_data_missing で止まる。
"""
from __future__ import annotations

import json

import technical_signals


class TestCandidateFileWiring:
    def test_the_phantom_us_filename_is_gone(self):
        # 誰も書かないファイル名。参照が残っていると欠落に気づけない。
        assert "screen_results_us.json" not in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_the_us_screener_outputs_are_actually_read(self):
        # screener.py が実際に書く2つの US 出力先。
        for name in ("screen_results.json", "screen_results_morning.json"):
            assert name in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_the_jp_screener_output_is_still_read(self):
        assert "screen_results_jp.json" in technical_signals.CANDIDATE_UNIVERSE_FILES

    def test_every_configured_filename_is_one_a_producer_writes(self):
        """設定名が実在の書き手を持つこと。

        screen_results_us.json が長期間放置されたのは、名前と書き手の対応を
        誰も検査していなかったため。
        """
        from pathlib import Path

        producers = "\n".join(
            Path(p).read_text(encoding="utf-8")
            for p in ("screener.py", "short_screener.py", "margin_long_screener.py")
        )
        # ペアトレ/スクイーズは別レーンで書き手が分かれるため対象外。
        checked = {
            "screen_results.json", "screen_results_morning.json",
            "screen_results_jp.json", "short_candidates.json",
            "margin_long_candidates.json",
        }
        for name in checked:
            assert name in technical_signals.CANDIDATE_UNIVERSE_FILES
            assert name in producers, f"{name} を書くコードが見当たらない"


class TestUniverseAssembly:
    def _universe(self, tmp_path, monkeypatch, candidates):
        monkeypatch.setattr(technical_signals, "BASE_DIR", tmp_path)
        (tmp_path / "holdings.json").write_text(
            json.dumps({"AAPL": {"ticker": "AAPL"}}), encoding="utf-8")
        (tmp_path / "scenario_playbook.json").write_text("{}", encoding="utf-8")
        for name, rows in candidates.items():
            (tmp_path / name).write_text(
                json.dumps({"candidates": rows}), encoding="utf-8")
        return technical_signals._build_ticker_universe()

    def test_a_us_screener_candidate_reaches_the_universe(self, tmp_path, monkeypatch):
        universe = self._universe(
            tmp_path, monkeypatch, {"screen_results.json": [{"ticker": "MRNA"}]})
        assert "MRNA" in universe

    def test_a_morning_batch_candidate_reaches_the_universe(self, tmp_path, monkeypatch):
        universe = self._universe(
            tmp_path, monkeypatch, {"screen_results_morning.json": [{"ticker": "TJX"}]})
        assert "TJX" in universe

    def test_holdings_are_still_included(self, tmp_path, monkeypatch):
        assert "AAPL" in self._universe(tmp_path, monkeypatch, {})

    def test_a_missing_candidate_file_is_not_fatal(self, tmp_path, monkeypatch):
        # その日走らなかったレーンがあるのは正常。
        assert self._universe(tmp_path, monkeypatch, {})

    def test_a_completely_absent_candidate_set_is_warned_about(self, tmp_path, monkeypatch, caplog):
        # 個々の不在は正常でも、全滅は配線が壊れている証拠。
        with caplog.at_level("WARNING"):
            self._universe(tmp_path, monkeypatch, {})
        assert any("候補ユニバース" in r.message for r in caplog.records)
