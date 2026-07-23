"""T10: API 認証 — POST/PUT/DELETE/PATCH は X-API-Key 必須、GET は緩和"""
import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """API キーを固定して TestClient を作成"""
    # ALLOW_UNAUTH を無効化、API_KEY を固定
    monkeypatch.delenv('ALLOW_UNAUTH', raising=False)
    monkeypatch.delenv('KAIROS_API_KEY', raising=False)
    monkeypatch.setenv('ALMANAC_API_KEY', 'test-key-abc123')

    # api.main を再インポートして環境変数を反映
    import importlib
    import api.main
    importlib.reload(api.main)

    client = TestClient(api.main.app)
    return client


def test_get_health_no_auth(app_client):
    """GET /health は認証不要"""
    r = app_client.get('/health')
    assert r.status_code == 200


def test_get_root_no_auth(app_client):
    """GET / は認証不要"""
    r = app_client.get('/')
    # ルートが存在すれば 200、しなければ 404 だが 403 ではない
    assert r.status_code in (200, 404)


def test_post_without_key_returns_403(app_client):
    """POST は X-API-Key 必須 — 無ければ 403"""
    r = app_client.post('/api/actions/execute', json={'ticker': 'NVDA'})
    assert r.status_code == 403


def test_post_wrong_key_returns_403(app_client):
    r = app_client.post('/api/actions/execute',
                         json={'ticker': 'NVDA'},
                         headers={'X-API-Key': 'wrong-key'})
    assert r.status_code == 403


def test_delete_without_key_returns_403(app_client):
    r = app_client.delete('/api/actions/executions/some-id')
    assert r.status_code == 403
