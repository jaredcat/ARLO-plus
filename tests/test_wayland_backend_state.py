"""State-machine tests for WaylandBackend (no portal/pipeline needed)."""

import time
from unittest.mock import MagicMock

import pytest

from arc_helper.screen.base import FrameUnavailableError
from arc_helper.screen.base import ScreenCaptureUnavailable
from arc_helper.screen.wayland_backend import RESTART_COOLDOWN_S
from arc_helper.screen.wayland_backend import STALE_AFTER_S
from arc_helper.screen.wayland_backend import STATS_INTERVAL_S
from arc_helper.screen.wayland_backend import WaylandBackend

_FRAME = (b"\x00" * 16, 2, 2, 8)


def _backend_with_open_session() -> WaylandBackend:
    backend = WaylandBackend()
    backend._session = MagicMock()
    backend._session.closed.is_set.return_value = False
    return backend


def _stub_crop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "arc_helper.screen.wayland_backend.crop_bgrx",
        lambda *_a, **_k: MagicMock(name="image"),
    )


def test_grab_without_session_attempts_reconnect():
    backend = WaylandBackend()
    calls: list[bool] = []

    def _boom(**_k: object) -> None:
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


def test_restart_cooldown_raises_no_frame():
    backend = WaylandBackend()
    backend._last_restart_at = time.monotonic()
    with pytest.raises(FrameUnavailableError, match="reconnect"):
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
    with pytest.raises(FrameUnavailableError, match="reconnect failed"):
        backend._attempt_restart()
    assert backend._terminal_error is None


def test_restart_skips_when_session_already_healthy():
    backend = _backend_with_open_session()
    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = MagicMock()  # type: ignore[method-assign]
    backend._attempt_restart()
    backend.stop.assert_not_called()
    backend.start.assert_not_called()


def test_force_restart_rebuilds_open_session():
    backend = _backend_with_open_session()
    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = MagicMock()  # type: ignore[method-assign]
    backend._attempt_restart(force=True)
    backend.stop.assert_called_once()
    backend.start.assert_called_once()


def test_force_restart_skipped_when_another_thread_already_recovered():
    backend = _backend_with_open_session()
    backend._frame = _FRAME
    backend._frame_at = time.monotonic()
    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = MagicMock()  # type: ignore[method-assign]
    backend._attempt_restart(force=True)
    backend.stop.assert_not_called()
    backend.start.assert_not_called()


def test_repeated_restarts_back_off():
    backend = _backend_with_open_session()
    backend.stop = MagicMock()  # type: ignore[method-assign]
    backend.start = MagicMock()  # type: ignore[method-assign]
    backend._attempt_restart(force=True)
    # Past the base cooldown, but the second reconnect in a row waits twice as long.
    backend._last_restart_at = time.monotonic() - (RESTART_COOLDOWN_S + 0.5)
    with pytest.raises(FrameUnavailableError, match="retry in"):
        backend._attempt_restart(force=True)
    backend._last_restart_at = time.monotonic() - (2 * RESTART_COOLDOWN_S + 0.5)
    backend._attempt_restart(force=True)
    assert backend.start.call_count == 2


def test_stop_before_start_is_safe():
    backend = WaylandBackend()
    backend.stop()
    backend.stop()


def test_grab_serves_fresh_frame_without_reconnect(monkeypatch: pytest.MonkeyPatch):
    backend = _backend_with_open_session()
    backend._frame = _FRAME
    backend._frame_at = time.monotonic() - (STALE_AFTER_S - 0.5)
    restarts: list[bool] = []
    monkeypatch.setattr(backend, "_attempt_restart", lambda **_k: restarts.append(True))
    _stub_crop(monkeypatch)
    assert backend.grab((0, 0, 1, 1)) is not None
    assert restarts == []


def test_grab_reconnects_when_stream_went_silent(monkeypatch: pytest.MonkeyPatch):
    backend = _backend_with_open_session()
    backend._frame = _FRAME
    backend._frame_at = time.monotonic() - (STALE_AFTER_S + 0.5)
    restarts: list[bool] = []

    def _restart(*, force: bool = False) -> None:
        restarts.append(force)
        backend._frame = (b"\x01" * 16, 2, 2, 8)
        backend._frame_at = time.monotonic()

    monkeypatch.setattr(backend, "_attempt_restart", _restart)
    _stub_crop(monkeypatch)
    assert backend.grab((0, 0, 1, 1)) is not None
    assert restarts == [True]


def test_grab_raises_no_frame_when_reconnect_brings_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend_with_open_session()
    backend._frame = _FRAME
    backend._frame_at = time.monotonic() - (STALE_AFTER_S + 0.5)
    monkeypatch.setattr(backend, "_attempt_restart", lambda **_k: None)
    with pytest.raises(FrameUnavailableError):
        backend.grab((0, 0, 1, 1))


def test_live_capture_and_invalidate_are_noops():
    backend = WaylandBackend()
    backend._pipeline = MagicMock()
    backend._frame = _FRAME
    backend._frame_at = time.monotonic()
    backend.set_live_capture(enabled=False)
    backend.set_live_capture(enabled=True)
    backend.invalidate_cached_frame()
    assert backend._frame == _FRAME
    backend._pipeline.set_state.assert_not_called()


def test_store_frame_records_sequence_and_arrival():
    backend = WaylandBackend()
    assert backend.frame_age() is None
    before = time.monotonic()
    backend._store_frame(_FRAME, 7)
    backend._store_frame(_FRAME, 8)
    assert backend._frame_seq == 2
    assert backend._frame_at >= before
    age = backend.frame_age()
    assert age is not None
    assert age < STALE_AFTER_S


def test_stats_separate_new_frames_from_keepalive_repeats():
    backend = WaylandBackend()
    backend._store_frame(_FRAME, 7)
    backend._store_frame(_FRAME, 7)  # keepalive repeat of the same frame
    backend._store_frame(_FRAME, 8)
    assert backend._stats.samples == 3
    assert backend._stats.new_frames == 2


def test_stats_line_logged_once_per_interval():
    backend = WaylandBackend()
    assert backend._store_frame(_FRAME, 1) is None
    backend._stats.started_at -= STATS_INTERVAL_S + 1
    line = backend._store_frame(_FRAME, 2)
    assert line is not None
    assert "ScreenCast stats" in line
    assert "max frame age" in line
    # The window restarts, so the next sample does not log again.
    assert backend._store_frame(_FRAME, 3) is None


def test_on_new_sample_ingests(monkeypatch: pytest.MonkeyPatch):
    backend = WaylandBackend()
    ingested: list[object] = []
    monkeypatch.setattr(backend, "_ingest_sample", ingested.append)
    sample = object()
    sink = MagicMock()
    sink.emit.return_value = sample
    backend._on_new_sample(sink)
    sink.emit.assert_called_once_with("pull-sample")
    assert ingested == [sample]
