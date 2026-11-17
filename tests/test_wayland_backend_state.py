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


def test_try_pull_frame_without_pipeline_is_false():
    backend = WaylandBackend()
    assert backend._try_pull_frame(timeout_ns=0) is False


def test_grab_pulls_before_crop(monkeypatch: pytest.MonkeyPatch):
    backend = WaylandBackend()
    backend._session = MagicMock()
    backend._session.closed.is_set.return_value = False
    backend._live_capture = True
    backend._frame = (b"\x00" * 16, 2, 2, 8)
    pulled: list[bool] = []

    def _pull(*, timeout_ns: int = 0) -> bool:
        pulled.append(True)
        return False

    monkeypatch.setattr(backend, "_try_pull_frame", _pull)
    monkeypatch.setattr(backend, "_set_pipeline_playing", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "arc_helper.screen.wayland_backend.crop_bgrx",
        lambda *_a, **_k: MagicMock(name="image"),
    )
    backend.grab((0, 0, 1, 1))
    assert pulled


def test_set_live_capture_toggles_pipeline(monkeypatch: pytest.MonkeyPatch):
    backend = WaylandBackend()
    states: list[bool] = []

    def _track(*, playing: bool) -> None:
        states.append(playing)

    monkeypatch.setattr(backend, "_set_pipeline_playing", _track)
    backend._live_capture = True
    backend.set_live_capture(enabled=False)
    backend.set_live_capture(enabled=False)  # no-op
    backend.set_live_capture(enabled=True)
    assert states == [False, True]


def test_idle_grab_wakes_and_repauses(monkeypatch: pytest.MonkeyPatch):
    backend = WaylandBackend()
    backend._session = MagicMock()
    backend._session.closed.is_set.return_value = False
    backend._live_capture = False
    backend._frame = (b"\x00" * 16, 2, 2, 8)
    play_states: list[bool] = []

    def _track(*, playing: bool) -> None:
        play_states.append(playing)

    monkeypatch.setattr(backend, "_set_pipeline_playing", _track)
    monkeypatch.setattr(backend, "_try_pull_frame", lambda **_k: True)
    monkeypatch.setattr(
        "arc_helper.screen.wayland_backend.crop_bgrx",
        lambda *_a, **_k: MagicMock(name="image"),
    )
    backend.grab((0, 0, 1, 1))
    assert play_states == [True, False]
