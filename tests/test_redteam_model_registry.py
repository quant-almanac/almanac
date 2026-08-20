"""Red Team のモデル配線が静かに劣化しないことの検証。

2026-08-20 時点で 6 プロバイダ中 4 つが無応答だった:
  llama-3.3-70b-versatile  → Groq が提供終了、404
  qwen/qwen3-235b-a22b-2507 → OpenRouter クレジット不足、402
  groq / qwen / haiku       → 上記の結果 attacks=0
実際に反証を出していたのは deepseek と gemini だけで、設計の 1/3 に落ちて
いた。個々の失敗はログに出ていたが行ごとに散っており気づけなかった。
"""
from __future__ import annotations

import llm_cost_accounting
from model_router import MODEL_REGISTRY


class TestGroqModel:
    def test_the_retired_llama_id_is_not_wired_anywhere(self):
        # 提供終了した ID が現役の配線に残っていないこと。
        assert MODEL_REGISTRY.get("groq_open") != "llama-3.3-70b-versatile"

    def test_a_groq_model_is_registered_centrally(self):
        # 呼び出し側の直書きに戻すと、次の提供終了でまた静かに枠が減る。
        assert MODEL_REGISTRY.get("groq_open")

    def test_the_registered_model_has_a_cost_entry(self):
        # 未登録だと cost_usd が None になり「不明」と「無料」の区別が消える。
        assert MODEL_REGISTRY["groq_open"] in llm_cost_accounting.DEFAULT_PRICES_PER_MILLION

    def test_the_retired_model_keeps_its_cost_entry_for_historical_logs(self):
        # 過去ログの集計が壊れないよう、消さずに残す。
        assert "llama-3.3-70b-versatile" in llm_cost_accounting.DEFAULT_PRICES_PER_MILLION


class TestCallSitesUseTheRegistry:
    def _source(self, path):
        from pathlib import Path
        return Path(path).read_text(encoding="utf-8")

    def test_analyst_redteam_does_not_hardcode_the_retired_model(self):
        assert "llama-3.3-70b-versatile" not in self._source("analyst/__init__.py")

    def test_llm_adapters_does_not_hardcode_the_retired_model(self):
        assert "llama-3.3-70b-versatile" not in self._source("llm_adapters.py")
