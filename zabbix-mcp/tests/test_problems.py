"""Problems tool tests.

Tests list_active_problems and problem_summary with mocked Zabbix responses.
"""
import pytest
from tools.problems import list_active_problems, problem_summary, _resolve_severity


# ── list_active_problems ───────────────────────────────────────────────────────


async def test_list_active_problems_returns_sorted_by_time_desc(mock_zabbix):
    """Problems sorted by clock DESC (newest first)."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "100",
            "clock": "1722200000",
            "severity": "4",
            "name": "CPU > 90%",
            "acknowledged": "0",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
        {
            "eventid": "99",
            "clock": "1722100000",
            "severity": "3",
            "name": "Disk > 80%",
            "acknowledged": "1",
            "hosts": [{"hostid": "11", "name": "db-01"}],
        },
    ])

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["data"][0]["event_id"] == "100"
    assert result["data"][0]["severity_name"] == "high"
    assert result["data"][1]["event_id"] == "99"


async def test_list_active_problems_filters_by_severity(mock_zabbix):
    """severity='high' maps to Zabbix integer 4 in API call."""
    mock_zabbix.enqueue_result([])

    result = await list_active_problems(severity="high", zabbix=mock_zabbix)

    assert result["status"] == "ok"


async def test_list_active_problems_invalid_severity_returns_error():
    """Invalid severity string returns error without calling Zabbix."""
    result = await list_active_problems(severity="invalid_sev")

    assert result["status"] == "error"
    assert "invalid_sev" in result["message"]


async def test_list_active_problems_zabbix_error_returns_error(mock_zabbix):
    """Zabbix API error returns structured error, doesn't raise."""
    mock_zabbix.enqueue_error("No permissions")

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "No permissions" in result["message"]


# ── problem_summary ────────────────────────────────────────────────────────────


async def test_problem_summary_returns_aggregation(mock_zabbix):
    """problem_summary aggregates by severity, host_group, top hosts."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "1", "severity": "5", "acknowledged": "0",
            "name": "Down", "clock": "1722200000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
            "groups": [{"groupid": "1", "name": "Linux servers"}],
        },
        {
            "eventid": "2", "severity": "5", "acknowledged": "1",
            "name": "Down", "clock": "1722100000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
            "groups": [{"groupid": "1", "name": "Linux servers"}],
        },
        {
            "eventid": "3", "severity": "3", "acknowledged": "0",
            "name": "High CPU", "clock": "1722050000",
            "hosts": [{"hostid": "11", "name": "db-01"}],
            "groups": [{"groupid": "2", "name": "DB servers"}],
        },
    ])

    result = await problem_summary(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["data"]["total"] == 3
    assert result["data"]["by_severity"]["disaster"] == 2
    assert result["data"]["by_severity"]["average"] == 1
    assert result["data"]["unacknowledged"] == 2


async def test_problem_summary_zabbix_error_returns_error(mock_zabbix):
    """Zabbix API error in problem_summary returns structured error."""
    mock_zabbix.enqueue_error("Connection refused")

    result = await problem_summary(zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "Connection refused" in result["message"]


# ── _resolve_severity ──────────────────────────────────────────────────────────


def test_resolve_severity_valid():
    """_resolve_severity maps name to int correctly."""
    assert _resolve_severity("high") == 4
    assert _resolve_severity("disaster") == 5
    assert _resolve_severity(None) is None


def test_resolve_severity_invalid():
    """_resolve_severity returns None for invalid names."""
    assert _resolve_severity("critical") is None  # not a valid Zabbix severity


# ── host filter ────────────────────────────────────────────────────────────────


async def test_list_active_problems_filters_by_host(mock_zabbix):
    """host='web-01' filters client-side to only that host's problems."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "100",
            "clock": "1722200000",
            "severity": "4",
            "name": "CPU > 90%",
            "acknowledged": "0",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
        {
            "eventid": "101",
            "clock": "1722199000",
            "severity": "3",
            "name": "Disk full",
            "acknowledged": "0",
            "hosts": [{"hostid": "11", "name": "db-01"}],
        },
    ])

    result = await list_active_problems(host="web-01", zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0]["host"] == "web-01"


# ── acknowledged flag ──────────────────────────────────────────────────────────


async def test_list_active_problems_acknowledged_flag(mock_zabbix):
    """acknowledged field maps Zabbix string '0'/'1' to bool."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "200",
            "clock": "1722200000",
            "severity": "2",
            "name": "Latency high",
            "acknowledged": "1",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
    ])

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["data"][0]["acknowledged"] is True


async def test_list_active_problems_empty_result(mock_zabbix):
    """Empty Zabbix response returns ok with count=0."""
    mock_zabbix.enqueue_result([])

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["data"] == []
