"""Maintenance tool tests.

Tests create/list/delete maintenance with mocked Zabbix responses.
Write tools annotated destructiveHint=True.
"""
import pytest
from tools.maintenance import (
    create_maintenance,
    list_maintenances,
    delete_maintenance,
    _parse_time,
)


def test_parse_time_valid_iso8601():
    """ISO 8601 datetime parses to Unix timestamp."""
    ts = _parse_time("2026-07-30T02:00:00")
    assert isinstance(ts, int)
    assert ts > 0


def test_parse_time_invalid_raises():
    """Invalid time string raises ValueError."""
    with pytest.raises(ValueError):
        _parse_time("not-a-date")


async def test_create_maintenance_requires_host_or_group():
    """Must provide host_names or host_group_names."""
    result = await create_maintenance(
        name="test",
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
    )
    assert result["status"] == "error"
    assert "host_names" in result["message"] or "host_group_names" in result["message"]


async def test_create_maintenance_resolves_host_names(mock_zabbix):
    """host_names resolved to hostids via host.get, then maintenance.create."""
    # host.get response
    mock_zabbix.enqueue_result([{"hostid": "10", "name": "web-01"}])
    # maintenance.create response
    mock_zabbix.enqueue_result({"maintenanceids": ["100"]})

    result = await create_maintenance(
        name="Web maintenance",
        host_names=["web-01"],
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"
    assert result["data"]["maintenance_id"] == "100"


async def test_create_maintenance_host_not_found(mock_zabbix):
    """Host name not found returns error."""
    mock_zabbix.enqueue_result([])  # host.get returns empty

    result = await create_maintenance(
        name="test",
        host_names=["nonexistent-host"],
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "error"
    assert "nonexistent-host" in result["message"]


async def test_list_maintenances_returns_list(mock_zabbix):
    """list_maintenances returns formatted maintenance list."""
    mock_zabbix.enqueue_result([
        {
            "maintenanceid": "100",
            "name": "Web maintenance",
            "active_since": "1722300000",
            "active_till": "1722314400",
            "description": "Weekly maintenance",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
    ])

    result = await list_maintenances(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0]["name"] == "Web maintenance"


async def test_delete_maintenance_success(mock_zabbix):
    """delete_maintenance returns success."""
    mock_zabbix.enqueue_result({"maintenanceids": ["100"]})

    result = await delete_maintenance(maintenance_id="100", zabbix=mock_zabbix)

    assert result["status"] == "ok"


async def test_delete_maintenance_not_found(mock_zabbix):
    """Delete non-existent maintenance returns error."""
    mock_zabbix.enqueue_error("No maintenance with given IDs")

    result = await delete_maintenance(maintenance_id="999", zabbix=mock_zabbix)

    assert result["status"] == "error"
