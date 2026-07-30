"""Verify the CRITICAL metrics snapshot bug is fixed.

Before the fix, permission_middleware.py did `from observability import
REQUESTS_TOTAL, ...` which snapshotted the values (all None) at import time.
When init_telemetry() later rebound the names in the observability module,
the middleware's local references still pointed to None -> NO metrics ever
recorded.

After the fix, permission_middleware does `import observability` (module
import) and accesses `observability.REQUESTS_TOTAL` at call time, which
resolves to the post-init value.
"""
import pytest

import observability
import permission_middleware


def test_metrics_are_none_before_init():
    """Before init_telemetry(), the instruments are None."""
    assert observability.REQUESTS_TOTAL is None
    assert observability.REQUEST_LATENCY is None
    assert observability.AUTH_FAILURES is None


def test_metrics_snapshot_bug_fixed():
    """The CRITICAL test: after init_telemetry(), the middleware's attribute
    access sees the SAME non-None object as the observability module.

    Before the fix (from-import), permission_middleware.REQUESTS_TOTAL would
    still be None here. After the fix (module import), attribute access
    resolves at call time and picks up the rebound value.
    """
    # Use a non-default port to avoid conflict with any running server.
    import os
    os.environ["PROMETHEUS_PORT"] = "19464"

    # Reset globals to None so the test is deterministic even if a prior
    # test already called init_telemetry().
    observability.REQUESTS_TOTAL = None
    observability.REQUEST_LATENCY = None
    observability.AUTH_FAILURES = None

    # Before init: middleware sees None via attribute access.
    assert permission_middleware.observability.REQUESTS_TOTAL is None

    # Run init - this rebinds the names in the observability module.
    observability.init_telemetry()

    try:
        # The middleware's attribute access must now see the non-None objects.
        assert permission_middleware.observability.REQUESTS_TOTAL is not None, (
            "SNAPSHOT BUG: middleware still sees None after init_telemetry()"
        )
        assert permission_middleware.observability.REQUEST_LATENCY is not None, (
            "SNAPSHOT BUG: middleware still sees None after init_telemetry()"
        )
        assert permission_middleware.observability.AUTH_FAILURES is not None, (
            "SNAPSHOT BUG: middleware still sees None after init_telemetry()"
        )

        # Verify it's the SAME object (identity, not just truthiness).
        assert (
            permission_middleware.observability.REQUESTS_TOTAL
            is observability.REQUESTS_TOTAL
        )
        assert (
            permission_middleware.observability.REQUEST_LATENCY
            is observability.REQUEST_LATENCY
        )
        assert (
            permission_middleware.observability.AUTH_FAILURES
            is observability.AUTH_FAILURES
        )

        # Verify the instruments actually work (can record without error).
        permission_middleware.observability.REQUESTS_TOTAL.add(1, {"status": "ok"})
        permission_middleware.observability.REQUEST_LATENCY.record(0.05)
        permission_middleware.observability.AUTH_FAILURES.add(1, {"error_type": "invalid_token"})
    finally:
        # Clean up: reset globals so other tests aren't affected.
        observability.REQUESTS_TOTAL = None
        observability.REQUEST_LATENCY = None
        observability.AUTH_FAILURES = None
        del os.environ["PROMETHEUS_PORT"]
