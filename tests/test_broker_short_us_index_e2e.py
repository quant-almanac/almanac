"""US 売建可否の基準ベース近似(index∪時価総額≥$5B)の受入 E2E。

楽天証券 米株信用の売建対象基準「S&P500/NASDAQ100/NYダウ構成 OR 時価総額≥$5B」を
近似実装し、broker_short_us.json を自動生成する。機械可読な公式リストが無いための近似で、
発注画面が最終確認(human_execution_only)。基準外/時価総額不明は fail-closed で除外。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync_broker_short_us_index as ix

NOW = datetime(2026, 6, 27, 9, 0, 0)
INDEX = {"AAPL", "NVDA", "QCOM"}


def test_index_member_is_eligible():
    e = ix.classify_us_eligibility("AAPL", market_cap=None, index_members=INDEX)
    assert e is not None
    assert e["rakuten"] is True
    assert e["borrow_cost_annual_pct"] == ix.DEFAULT_BORROW_COST


def test_large_cap_non_index_is_eligible():
    e = ix.classify_us_eligibility("ARM", market_cap=1.2e11, index_members=INDEX)
    assert e is not None and e["rakuten"] is True


def test_small_cap_non_index_excluded():
    assert ix.classify_us_eligibility("TINY", market_cap=1e9, index_members=INDEX) is None


def test_unknown_mktcap_non_index_fails_closed():
    assert ix.classify_us_eligibility("HUH", market_cap=None, index_members=INDEX) is None


def test_build_outputs_only_eligible():
    out = ix.build_broker_us(
        ["AAPL", "ARM", "TINY", "HUH"],
        index_members=INDEX,
        market_caps={"ARM": 1.2e11, "TINY": 1e9, "HUH": None},
    )
    assert set(out) == {"AAPL", "ARM"}, "基準外は黙って除外(fail-closed)"
    assert out["AAPL"]["eligibility"] == "rule_based_index_or_mktcap"


def test_sync_writes_file_and_builder_marks_shortable(tmp_path):
    import short_universe as su
    ix.sync(
        base_dir=tmp_path, tickers=["AAPL", "TINY"], now=NOW,
        index_members=INDEX, market_caps={"TINY": 1e9},
    )
    f = tmp_path / "data" / "broker_short_us.json"
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["tickers"]["AAPL"]["rakuten"] is True
    assert "TINY" not in data["tickers"]

    led = su.build_short_universe(["AAPL", "TINY"], now=NOW, base_dir=tmp_path)
    assert led["tickers"]["AAPL"]["shortable"] is True, led["tickers"]["AAPL"]["reasons"]
    assert led["tickers"]["TINY"]["shortable"] is False  # 売建可否 未供給=fail-closed
    # 近似でも human_execution_only / 自動発注なしは不変
    assert led["tickers"]["AAPL"]["human_execution_only"] is True
    assert led["tickers"]["AAPL"]["executable"] is False
