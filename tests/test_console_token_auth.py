from pathlib import Path

from fastapi.testclient import TestClient

from nth_dao.web import create_app


def _auth_headers(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.nth_console_token}"}


def test_api_requires_console_bearer_token(tmp_path: Path):
    app = create_app(tmp_path, require_console_auth=True)
    client = TestClient(app)

    missing = client.get("/api/summary", params={"actor_id": "admin"})
    assert missing.status_code == 401

    wrong = client.get(
        "/api/summary",
        params={"actor_id": "admin"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401

    ok = client.get(
        "/api/summary",
        params={"actor_id": "admin"},
        headers=_auth_headers(app),
    )
    assert ok.status_code == 200


def test_actor_id_remains_authorization_not_authentication(tmp_path: Path):
    app = create_app(tmp_path, require_console_auth=True)
    client = TestClient(app)

    response = client.get(
        "/api/build_id",
        params={"actor_id": "stranger"},
        headers=_auth_headers(app),
    )
    assert response.status_code == 403


def test_frontend_html_injects_console_token(tmp_path: Path):
    app = create_app(tmp_path, require_console_auth=True)
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "window.__NTH_CONSOLE_TOKEN__" in response.text
    assert app.state.nth_console_token in response.text
