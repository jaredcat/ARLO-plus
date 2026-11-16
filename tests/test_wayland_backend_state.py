"""State-machine tests for WaylandBackend (no portal/pipeline needed)."""

import time
from unittest.mock import MagicMock

import pytest

from arc_helper.screen.base import ScreenCaptureUnavailable
from arc_helper.screen.wayland_backend import RESTART_COOLDOWN_S
from arc_helper.screen.wayland_backend import WaylandBackend


def test_grab_without_session_attempts_reconnect():
    backend = WaylandBackend()
    calls: list[bool] = []

    def _boom() -> None:
        calls.append(True)
        msg = "Screen sharing reconnect failed: not started"
        raise OSError(msg)

    backend._attempt_restart = _boom  # type: ignore[method-assign]
    with pytest.raises(OSError, match="reconnect failed"):
        backend.grab((0, 0, 10, 10))
    assert calls


def test_terminal_error_short_circuits_grab():
    backend = WaylandBackend()
    backend._terminal_error = "gone"
    with pytest.raises(ScreenCaptureUnavailable, match="gone"):
        backend.grab((0, 0, 10, 10))


def test_restart_cooldown_raises_transient_oserror():
    backend = WaylandBackend()
    backend._last_restart_at = time.monotonic()
    with pytest.raises(OSError, match="reconnect"):
        backend._attempt_restart()
    assert backend._terminal_error is None


def test_restart_failure_is_transient_not_terminal():
    backend = WaylandBackend()
    backend._last_restart_at = time.monotonic() - (RESTART_COOLDOWN_S + 1)

    def _fail_start() -> None:
        msg = "portal denied"
        raise OSError(msg)

    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = _fail_start  # type: ignore[method-assign]
    with pytest.raises(OSError, match="reconnect failed"):
        backend._attempt_restart()
    assert backend._terminal_error is None


def test_restart_skips_when_session_already_healthy():
    backend = WaylandBackend()
    session = MagicMock()
    session.closed.is_set.return_value = False
    backend._session = session
    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = MagicMock()  # type: ignore[method-assign]
    backend._attempt_restart()
    backend.stop.assert_not_called()
    backend.start.assert_not_called()


def test_stop_before_start_is_safe():
    backend = WaylandBackend()
    backend.stop()
    backend.stop()
