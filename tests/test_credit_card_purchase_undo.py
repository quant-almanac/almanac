"""credit_card_investment.remove_purchase: 誤POST復旧用undoの回帰テスト"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import credit_card_investment as cci  # noqa: E402


def test_undo_single_purchase_returns_to_pristine_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")

    cci.record_monthly_purchase("husband", amount=10_000, nav=20_000, purchase_date="2026-07-01")
    result = cci.remove_purchase("husband", "2026-07-01")

    assert "error" not in result
    acc = result["account"]
    assert acc["current_units"] == 0.0
    assert acc["avg_nav"] == 0.0
    assert acc["total_invested"] == 0.0
    assert acc["total_points"] == 0.0
    assert acc["purchase_history"] == []


def test_undo_middle_purchase_recomputes_weighted_avg_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")

    cci.record_monthly_purchase("husband", amount=10_000, nav=20_000, purchase_date="2026-05-01")
    cci.record_monthly_purchase("husband", amount=10_000, nav=25_000, purchase_date="2026-06-01")  # 誤送信（取り消す対象）
    cci.record_monthly_purchase("husband", amount=10_000, nav=22_000, purchase_date="2026-07-01")

    result = cci.remove_purchase("husband", "2026-06-01")
    assert "error" not in result

    # 6月分を除いた5月+7月のみで再計算した値と一致すること。
    # record_monthly_purchase は units を history 保存時に4桁丸めするため
    # (既存の挙動)、再計算もその丸め済み値を基準にする。
    expected_units_5 = round(10_000 / 20_000, 4)
    expected_units_7 = round(10_000 / 22_000, 4)
    expected_total_units = expected_units_5 + expected_units_7
    expected_avg_nav = (20_000 * expected_units_5 + 22_000 * expected_units_7) / expected_total_units

    acc = result["account"]
    assert abs(acc["current_units"] - expected_total_units) < 1e-9
    assert abs(acc["avg_nav"] - expected_avg_nav) < 1e-6
    assert acc["total_invested"] == 20_000  # 10,000 x 2
    assert len(acc["purchase_history"]) == 2
    assert [p["date"] for p in acc["purchase_history"]] == ["2026-05-01", "2026-07-01"]


def test_undo_nonexistent_date_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")
    cci.record_monthly_purchase("husband", amount=10_000, nav=20_000, purchase_date="2026-07-01")

    result = cci.remove_purchase("husband", "2026-01-01")
    assert "error" in result
    # 存在しない日付を指定しても既存レコードは無傷
    data = cci.load_cc_data()
    assert len(data["husband"]["purchase_history"]) == 1


def test_undo_invalid_person_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")
    result = cci.remove_purchase("nobody", "2026-07-01")
    assert "error" in result


def test_undo_same_day_duplicate_removes_only_latest(tmp_path, monkeypatch):
    """同日2重POSTからの復旧: 末尾(最新)の1件だけ取り消される"""
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")
    cci.record_monthly_purchase("wife", amount=10_000, nav=20_000, purchase_date="2026-07-01")
    cci.record_monthly_purchase("wife", amount=10_000, nav=20_000, purchase_date="2026-07-01")  # 二重送信

    result = cci.remove_purchase("wife", "2026-07-01")
    assert "error" not in result
    assert len(result["account"]["purchase_history"]) == 1
    assert result["account"]["total_invested"] == 10_000


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("ALLOW_UNAUTH", raising=False)
    monkeypatch.delenv("KAIROS_API_KEY", raising=False)
    monkeypatch.setenv("ALMANAC_API_KEY", "test-key-abc123")
    monkeypatch.setattr(cci, "DATA_PATH", tmp_path / "credit_card_plans.json")

    import importlib
    import api.main
    from fastapi.testclient import TestClient

    importlib.reload(api.main)
    return TestClient(api.main.app)


def test_purchase_then_undo_via_http(app_client):
    headers = {"X-API-Key": "test-key-abc123"}

    r = app_client.post(
        "/api/admin/credit-card/purchase",
        json={"person": "husband", "amount": 10_000, "nav": 20_000, "purchase_date": "2026-07-01"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["account"]["current_units"] == 0.5

    r = app_client.post(
        "/api/admin/credit-card/purchase/undo",
        json={"person": "husband", "purchase_date": "2026-07-01"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["account"]["current_units"] == 0.0
    assert body["account"]["purchase_history"] == []


def test_undo_requires_api_key(app_client):
    r = app_client.post(
        "/api/admin/credit-card/purchase/undo",
        json={"person": "husband", "purchase_date": "2026-07-01"},
    )
    assert r.status_code == 403


def test_undo_nonexistent_date_returns_400_via_http(app_client):
    r = app_client.post(
        "/api/admin/credit-card/purchase/undo",
        json={"person": "husband", "purchase_date": "2099-01-01"},
        headers={"X-API-Key": "test-key-abc123"},
    )
    assert r.status_code == 400
