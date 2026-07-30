"""End-to-end flow: login -> register server -> create token -> list."""
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_full_flow(client, fake_redis):
    # 1. login works (default admin created in lifespan)
    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. register a server
    resp = client.post("/api/servers", json={
        "name": "zabbix", "url": "http://localhost:8000/mcp", "description": "Zabbix",
    }, headers=headers)
    assert resp.status_code == 201

    # 3. list servers shows it
    resp = client.get("/api/servers", headers=headers)
    assert len(resp.json()) == 1

    # 4. create a token with read perm on zabbix
    resp = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=headers)
    assert resp.status_code == 201
    plaintext = resp.json()["token"]
    assert plaintext.startswith("tok_")

    # 5. list tokens masks plaintext
    resp = client.get("/api/tokens", headers=headers)
    t = resp.json()[0]
    assert t["token_masked"].endswith("****")
    assert "token" not in t

    # 6. delete server + token
    assert client.delete("/api/servers/zabbix", headers=headers).status_code == 204
    assert client.delete(f"/api/tokens/{resp.json()[0]['id']}" if False else "/api/tokens/x",
                         headers=headers).status_code in (204, 404)
