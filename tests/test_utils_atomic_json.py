import json
import math

import pytest

from utils import atomic_write_json


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_atomic_write_json_rejects_nonstandard_numbers_without_replacing_file(
    tmp_path, invalid
) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"status": "original"}), encoding="utf-8")

    with pytest.raises(ValueError):
        atomic_write_json(target, {"amount": invalid})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "original"}


def test_positive_finite_rejects_bool():
    """True/False は int のサブクラスで True==1 が通ってしまうため明示的に拒否する
    （2026-09 レビュー Codex 指摘 #5: portfolio_value=True が保存され得た）。"""
    from utils import positive_finite
    with pytest.raises(ValueError, match="not numeric"):
        positive_finite(True, label="x")
    with pytest.raises(ValueError, match="not numeric"):
        positive_finite(False, label="x")


def test_positive_finite_rejects_zero_and_negative():
    from utils import positive_finite
    with pytest.raises(ValueError, match="positive and finite"):
        positive_finite(0, label="x")
    with pytest.raises(ValueError, match="positive and finite"):
        positive_finite(-5, label="x")


def test_positive_finite_rejects_nan_and_inf():
    from utils import positive_finite
    with pytest.raises(ValueError, match="positive and finite"):
        positive_finite(math.nan, label="x")
    with pytest.raises(ValueError, match="positive and finite"):
        positive_finite(math.inf, label="x")


def test_positive_finite_accepts_positive_number():
    from utils import positive_finite
    assert positive_finite(29_000_000, label="x") == 29_000_000.0
    assert positive_finite("29000000.5", label="x") == 29_000_000.5
