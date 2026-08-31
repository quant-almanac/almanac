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
