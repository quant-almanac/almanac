"""analyst._log_red_team_verdicts: run_analysis から red_team_ledger への配線テスト"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyst as analyst_mod  # noqa: E402
import red_team_ledger as rtl  # noqa: E402


def test_logs_valid_verdicts_and_skips_malformed(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "VERDICT_LOG_PATH", tmp_path / "verdicts.jsonl")

    synthesis = {
        "red_team_verdict": [
            {"ticker": "AVGO", "action": "buy 5株", "verdict": "reject", "verdict_reason": "流動性懸念"},
            {"ticker": "NVDA", "action": "add 3株", "verdict": "adopt", "verdict_reason": "妥当"},
            {"ticker": "", "action": "buy", "verdict": "reject", "verdict_reason": "ticker欠落"},  # skip
            {"ticker": "META", "action": "buy", "verdict": "unknown"},  # skip: invalid verdict
            "not_a_dict",  # skip: 型不正
        ]
    }

    analyst_mod._log_red_team_verdicts(synthesis)

    rows = rtl._read_jsonl(tmp_path / "verdicts.jsonl")
    assert len(rows) == 2
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AVGO", "NVDA"}


def test_no_red_team_verdict_field_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(rtl, "VERDICT_LOG_PATH", tmp_path / "verdicts.jsonl")
    analyst_mod._log_red_team_verdicts({})  # red_team_verdict キーが無い
    assert not (tmp_path / "verdicts.jsonl").exists()


def test_never_raises_even_if_ledger_import_fails(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _broken_import(name, *args, **kwargs):
        if name == "red_team_ledger":
            raise ImportError("simulated failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _broken_import)
    # 例外を出さないこと (本分析フローを止めない設計)
    analyst_mod._log_red_team_verdicts({"red_team_verdict": [{"ticker": "X", "action": "y", "verdict": "adopt"}]})
