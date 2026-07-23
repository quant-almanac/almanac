import json
from datetime import datetime

from api.routes import market


def test_market_snapshot_persists_full_as_of_timestamp(monkeypatch, tmp_path):
    cache = tmp_path / "market_snapshot.json"
    monkeypatch.setattr(market, "CACHE_PATH", str(cache))
    monkeypatch.setattr(
        market,
        "_fetch_market_data",
        lambda: {"NK225": {"price": 70_000, "ma50_diff": 1.5}},
    )

    data = market._get_cached_or_fetch()

    as_of = datetime.fromisoformat(data["as_of"])
    assert as_of.tzinfo is not None
    persisted = json.loads(cache.read_text(encoding="utf-8"))
    assert persisted["as_of"] == data["as_of"]
