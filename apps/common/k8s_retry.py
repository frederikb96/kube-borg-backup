"""Retry wrapper for transient Kubernetes API failures.

Handles etcd timeouts (HTTP 500), gateway errors (502/503/504),
connection failures, and 409 Conflict (idempotent create).
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from kubernetes.client.rest import ApiException

from common.pod_monitor import log_msg

T = TypeVar("T")

# HTTP status codes that indicate transient server-side failures
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}

# Retry schedule: (attempt_number, delay_seconds)
RETRY_DELAYS = [5, 10, 20]
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1  # 4 total: 1 initial + 3 retries


def _is_transient_exception(exc: Exception) -> bool:
    """Check if an exception represents a transient failure worth retrying.

    Args:
        exc: The exception to evaluate

    Returns:
        True if the exception is transient and the call should be retried
    """
    # Kubernetes API exceptions with transient HTTP status codes
    if isinstance(exc, ApiException) and exc.status in TRANSIENT_STATUS_CODES:
        return True

    # urllib3 connection failures (underlying transport errors)
    try:
        from urllib3.exceptions import MaxRetryError
        if isinstance(exc, MaxRetryError):
            return True
    except ImportError:
        pass

    # Standard library connection/timeout errors
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    return False


def _is_conflict(exc: Exception) -> bool:
    """Check if exception is a 409 Conflict (resource already exists)."""
    return isinstance(exc, ApiException) and exc.status == 409


def k8s_api_retry(
    operation: Callable[[], T],
    context: str,
    on_conflict: Callable[[], T] | None = None,
) -> T:
    """Execute a K8s API call with retry logic for transient failures.

    Args:
        operation: Callable that performs the K8s API call
        context: Human-readable description for log messages
            (e.g., "creating clone PVC my-pvc")
        on_conflict: Optional callable to handle 409 Conflict. When provided
            and a 409 occurs, this function is called instead of failing.
            Typically reads and returns the existing resource.

    Returns:
        The return value of operation() or on_conflict()

    Raises:
        ApiException: On permanent API failures (4xx except 409 with handler)
        Exception: On non-API failures after all retries exhausted
    """
    last_exc: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return operation()

        except Exception as exc:
            last_exc = exc

            # 409 Conflict: resource already exists (idempotent create)
            if _is_conflict(exc) and on_conflict is not None:
                log_msg(
                    f"[k8s-retry] {context}: 409 Conflict "
                    f"(resource already exists), returning existing"
                )
                return on_conflict()

            # Non-retryable errors: raise immediately
            if not _is_transient_exception(exc):
                raise

            # Retryable error: log and potentially retry
            if attempt >= MAX_ATTEMPTS:
                log_msg(
                    f"[k8s-retry] {context}: attempt {attempt}/{MAX_ATTEMPTS} "
                    f"failed, no retries left: {exc}"
                )
                raise

            delay = RETRY_DELAYS[attempt - 1]
            log_msg(
                f"[k8s-retry] {context}: attempt {attempt}/{MAX_ATTEMPTS} "
                f"failed (transient), retrying in {delay}s: {exc}"
            )
            time.sleep(delay)

    # Should never reach here, but satisfy type checker
    assert last_exc is not None
    raise last_exc
