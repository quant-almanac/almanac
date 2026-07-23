"""データ供給 E2E: 手動 CSV → sync → builder で実際に shortable=true が surface する。

Step C/D で fail-closed パイプラインは通ったが、live データが無いと全 shortable=false。
ここでは「借株可否データを供給すれば実際に空売り候補が surface する」ことを
端から端で確認する(手動 CSV fixture 経由、外部 API 不要)。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync_broker_short_us import parse_broker_short_csv, sync as sync_broker
from sync_jsf_lending import sync as sync_jsf

NOW = datetime(2026, 6, 26, 9, 0, 0)


# ── US broker CSV パーサ(fail-closed)──

def test_broker_csv_parses_explicit_availability():
    parsed = parse_broker_short_csv(
        "ticker,rakuten,sbi,borrow_cost_annual_pct\n"
        "TSLA,yes,no,0.05\n"
        "AAPL,no,no,\n"
    )
    assert parsed["TSLA"] == {"rakuten": True, "sbi": False, "borrow_cost_annual_pct": 0.05}
    assert parsed["AAPL"] == {"rakuten": False, "sbi": False, "borrow_cost_annual_pct": None}


def test_broker_csv_rejects_unknown_headers():
    with pytest.raises(ValueError, match="unrecognized"):
        parse_broker_short_csv("foo,bar\n1,2\n")


def test_broker_sync_writes_json(tmp_path):
    out = tmp_path / "broker_short_us.json"
    payload = sync_broker(
        output_path=out,
        source_text="ticker,rakuten,sbi,borrow_cost_annual_pct\nTSLA,yes,no,0.05\n",
    )
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tickers"]["TSLA"]["rakuten"] is True
    assert "generated_at" in data


# ── 端から端: 供給後に shortable=true が出る ──

def test_supplied_data_makes_jp_and_us_shortable(tmp_path):
    import short_universe as su

    # US broker fixture
    broker_path = tmp_path / "broker_short_us.json"
    sync_broker(
        output_path=broker_path,
        source_text="ticker,rakuten,sbi,borrow_cost_annual_pct\nTSLA,yes,no,0.05\n",
    )
    broker_file = json.loads(broker_path.read_text(encoding="utf-8"))
    broker_map = dict(broker_file.get("tickers") or {})
    broker_map["_as_of"] = broker_file.get("generated_at")

    # JSF fixture
    jsf_path = tmp_path / "jsf_lending_state.json"
    sync_jsf(
        output_path=jsf_path,
        source_text="ticker,loan_ratio,reverse_daily_fee\n7203.T,1.6,inactive\n",
    )
    jsf_state = json.loads(jsf_path.read_text(encoding="utf-8"))
    # sync は generated_at を UTC 実時刻で書く → builder 鮮度判定のため now を合わせる
    now = datetime.fromisoformat(jsf_state["generated_at"].replace("Z", "+00:00")).replace(tzinfo=None)

    led = su.build_short_universe(
        ["7203.T", "TSLA"], now=now,
        sources={
            "loanable_map": {"7203.T": True},
            "pinned_at": now.isoformat(),
            "jsf_state": jsf_state,
            "broker_us_map": broker_map,
            "squeeze_map": {},
        },
    )
    assert led["tickers"]["7203.T"]["shortable"] is True, led["tickers"]["7203.T"]["reasons"]
    assert led["tickers"]["TSLA"]["shortable"] is True, led["tickers"]["TSLA"]["reasons"]
    assert led["shortable_count"] == 2
    # 供給後も human_execution_only / 自動発注なしは不変
    for t in ("7203.T", "TSLA"):
        assert led["tickers"][t]["human_execution_only"] is True
        assert led["tickers"][t]["executable"] is False
