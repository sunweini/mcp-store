import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_create_server(client, fake_redis, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "zabbix", "url": "http://localhost:8000/mcp",
        "description": "Zabbix MCP",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "zabbix"
    assert data["url"] == "http://localhost:8000/mcp"


def test_create_server_invalid_name_underscore(client, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "my_server", "url": "http://x", "description": "",
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_list_servers(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "zabbix"


def test_delete_server(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.delete("/api/servers/zabbix", headers=auth_headers)
    assert resp.status_code == 204
    # gone
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.json() == []


def test_unauth_rejected(client):
    assert client.get("/api/servers").status_code == 401
