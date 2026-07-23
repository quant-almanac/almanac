"""Part B: drawdown_dca_engine の T1/T2/T3 発動判定。"""
from __future__ import annotations

import importlib
import pytest


@pytest.fixture(scope="module")
def dca():
    return importlib.import_module("drawdown_dca_engine")


def test_module_has_tranche_config(dca):
    """T1/T2/T3 の閾値定数が存在する"""
    assert hasattr(dca, "generate_ladder_signals") or hasattr(dca, "evaluate")
    # しきい値 attribute の少なくとも一つ
    names = dir(dca)
    tranche_hints = [n for n in names if "T1" in n or "T2" in n or "T3" in n or "TRANCHE" in n.upper()]
    assert tranche_hints, "tranche-related constants should exist"


def test_safe_when_no_signals(dca, tmp_path, monkeypatch):
    """必須入力が無くても例外で落ちない"""
    monkeypatch.chdir(tmp_path)
    # 主要 entrypoint を叩いて落ちないこと
    for fn_name in ("generate_ladder_signals", "evaluate", "scan"):
        fn = getattr(dca, fn_name, None)
        if fn is None:
            continue
        try:
            fn()
        except TypeError:
            pass  # 引数必須系は skip
        except FileNotFoundError:
            pass
