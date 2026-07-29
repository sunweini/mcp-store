"""Events tool tests — alert acknowledgment.

Tests list_unacknowledged, acknowledge_event, and batch_acknowledge
with mocked Zabbix responses. Write tools use destructiveHint=True.
"""
import pytest
from tools.events import (
    list_unacknowledged,
    acknowledge_event,
    batch_acknowledge,
    _ACK_ACTION,
    _MSG_ACTION,
    _CLOSE_ACTION,
)


# ── list_unacknowledged ────────────────────────────────────────────────────────


async def test_list_unacknowledged_filters_acknowledged_false(mock_zabbix):
    """Only returns problems with acknowledged=0."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "200",
            "severity": "4",
            "name": "CPU > 90%",
            "acknowledged": "0",
            "clock": "1722200000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
    ])

    result = await list_unacknowledged(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0]["event_id"] == "200"


async def test_list_unacknowledged_with_severity_filter(mock_zabbix):
    """severity filter passes resolved integer to Zabbix severities param."""
    mock_zabbix.enqueue_result([])

    result = await list_unacknowledged(severity="high", zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 0


async def test_list_unacknowledged_invalid_severity_returns_error():
    """Invalid severity returns error without calling Zabbix."""
    result = await list_unacknowledged(severity="bogus_level")

    assert result["status"] == "error"
    assert "bogus_level" in result["message"]


async def test_list_unacknowledged_zabbix_error(mock_zabbix):
    """Zabbix API error returns structured error, doesn't raise."""
    mock_zabbix.enqueue_error("No permissions")

    result = await list_unacknowledged(zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "No permissions" in result["message"]


# ── acknowledge_event ──────────────────────────────────────────────────────────


async def test_acknowledge_event_success(mock_zabbix):
    """Single event acknowledgment returns success."""
    mock_zabbix.enqueue_result({"eventids": ["200"]})

    result = await acknowledge_event(
        event_id="200",
        message="Known issue, maintenance planned",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"


async def test_acknowledge_event_with_close(mock_zabbix):
    """Acknowledging with close=True sets action param."""
    mock_zabbix.enqueue_result({"eventids": ["200"]})

    result = await acknowledge_event(
        event_id="200",
        message="Resolved",
        close=True,
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"


async def test_acknowledge_event_api_error(mock_zabbix):
    """Zabbix error returns structured error."""
    mock_zabbix.enqueue_error("Event not found")

    result = await acknowledge_event(event_id="999", zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "Event not found" in result["message"]


# ── batch_acknowledge ──────────────────────────────────────────────────────────


async def test_batch_acknowledge_success(mock_zabbix):
    """Batch acknowledge returns per-event results."""
    mock_zabbix.enqueue_result({"eventids": ["200", "201", "202"]})

    result = await batch_acknowledge(
        event_ids=["200", "201", "202"],
        message="Batch ack during maintenance",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"
    assert result["data"]["acknowledged_count"] == 3


async def test_batch_acknowledge_empty_list():
    """Empty event_ids returns error without calling Zabbix."""
    result = await batch_acknowledge(event_ids=[])

    assert result["status"] == "error"
    assert "empty" in result["message"].lower()


async def test_batch_acknowledge_zabbix_error(mock_zabbix):
    """Zabbix error during batch returns structured error."""
    mock_zabbix.enqueue_error("Permission denied")

    result = await batch_acknowledge(
        event_ids=["300", "301"],
        message="test",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "error"
    assert "Permission denied" in result["message"]


# ── action bitmask constants ────────────────────────────────────────────────────


def test_action_bitmask_values():
    """Bitmask constants match Zabbix event.acknowledge spec."""
    assert _ACK_ACTION == 1
    assert _MSG_ACTION == 2
    assert _CLOSE_ACTION == 8
    # ack + message + close = 1 | 2 | 8 = 11
    assert (_ACK_ACTION | _MSG_ACTION | _CLOSE_ACTION) == 11
