from fastapi.testclient import TestClient

from api.main import app


def test_ipv4_loopback_frontend_origin_is_allowed():
    response = TestClient(app).get(
        "/health",
        headers={"Origin": "http://127.0.0.1:3000"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
