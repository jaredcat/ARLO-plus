"""Wayland screen backend: portal ScreenCast stream consumed via GStreamer.

Pipeline: pipewiresrc → rate cap → leaky queue → videoconvert → appsink.

The stream runs for the whole process lifetime and is never paused. Its cost is
bounded at the source instead: the capsfilter right after ``pipewiresrc`` asks
the compositor for at most ``WAYLAND_CAPTURE_FPS`` frames per second, so frames
that would only be thrown away are never produced or copied.

Pausing the pipeline between scans (the previous design) could leave the stream
delivering nothing at all after a resume, not even ``keepalive-time`` repeats.
``screen/streamprobe.py`` reproduces that and measures the capped rates.

``grab`` never waits for the compositor: the appsink callback keeps the latest
frame, and ``grab`` crops it. There is one freshness rule. While the stream is
healthy a sample arrives at least every ``KEEPALIVE_MS``, so a newest sample
older than ``STALE_AFTER_S`` means the stream is dead and the portal session is
rebuilt.
"""

import threading
import time
from dataclasses import dataclass

from PIL import Image

from arc_helper.config import get_settings
from arc_helper.config import logger

from .base import FrameUnavailableError
from .base import Point
from .base import ScreenCaptureUnavailable
from .base import compute_scale
from .frame import crop_bgrx
from .token_store import load_token
from .token_store import save_token

_INSTALL_HINT = (
    "Wayland capture needs PyGObject + GStreamer.\n"
    "  Arch:        sudo pacman -S --needed cairo pkgconf gcc gstreamer gst-plugins-base gst-plugin-pipewire\n"
    "  Debian:      sudo apt install libcairo2-dev libgirepository1.0-dev pkg-config gcc gstreamer1.0-pipewire gstreamer1.0-plugins-base\n"
    "  then:        uv sync\n"
    "Or force X11 capture with SCREEN_BACKEND=x11 in .env (requires an X11 session)."
)

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]  # GI stubs
    from gi.repository import Gst  # pyright: ignore[reportAttributeAccessIssue]  # GI stubs
    from gi.repository import GstVideo  # pyright: ignore[reportAttributeAccessIssue]  # GI stubs
except (ImportError, ValueError) as _e:
    raise ImportError(_INSTALL_HINT) from _e

# portal imports gi itself, so it must stay behind the guard above
from .portal import PortalError  # ruff: ignore[module-import-not-at-top-of-file]
from .portal import ScreenCastSession  # ruff: ignore[module-import-not-at-top-of-file]

FIRST_FRAME_TIMEOUT_S = 10.0
# pipewiresrc re-sends the last buffer at this interval when the screen is
# static, so a healthy stream is never silent for longer than this.
KEEPALIVE_MS = 500
# The single freshness rule: three missed keepalives means the stream is dead.
STALE_AFTER_S = 3 * KEEPALIVE_MS / 1000.0
MIN_CAPTURE_FPS = 1
# Avoid spamming the portal when the compositor flaps the session
# (monitor hotplug, tray "Stop sharing", competing ScreenCast clients).
RESTART_COOLDOWN_S = 2.0
# Reconnects that keep failing to hold back off up to this long, and the
# back-off resets once the stream has gone this long without needing one.
RESTART_COOLDOWN_MAX_S = 30.0
RESTART_STREAK_RESET_S = 60.0
STATS_INTERVAL_S = 60.0

# (data, width, height, stride) of a BGRx frame.
_Frame = tuple[bytes, int, int, int]


@dataclass
class _StreamStats:
    """Counters for one stats window. Guarded by ``WaylandBackend._lock``."""

    started_at: float = 0.0
    samples: int = 0
    # Samples carrying a new compositor frame (keepalive repeats excluded).
    # Stays 0 when the stream has no per-buffer sequence to tell them apart.
    new_frames: int = 0
    max_gap_s: float = 0.0
    grabs: int = 0
    max_grab_age_s: float = 0.0


class WaylandBackend:
    """ScreenBackend implementation for Wayland sessions."""

    name = "wayland"

    def __init__(self) -> None:
        self._session: ScreenCastSession | None = None
        self._pipeline: Gst.Element | None = None
        self._lock = threading.Lock()
        self._frame: _Frame | None = None
        # Arrival time (monotonic) and running count of the newest sample.
        self._frame_at = 0.0
        self._frame_seq = 0
        self._last_stream_seq: int | None = None
        self._stats = _StreamStats()
        self._first_frame = threading.Event()
        self._size: tuple[int, int] = (0, 0)
        self._tk_scale = 1.0
        self._capture_fps = 0
        self._restart_lock = threading.Lock()
        self._last_restart_at = 0.0
        self._restart_streak = 0
        self._terminal_error: str | None = None

    def start(self) -> None:
        Gst.init(None)
        self._clear_frame()
        # Floor at 1: a zero/negative max-framerate is not a usable capsfilter.
        # No upper clamp — the compositor will only deliver what its refresh
        # rate allows, so a high request is harmless if someone wants to try.
        self._capture_fps = max(MIN_CAPTURE_FPS, get_settings().wayland_capture_fps)

        try:
            self._session = ScreenCastSession(restore_token=load_token())
            try:
                self._session.open()
            except PortalError as e:
                msg = (
                    f"Screen sharing setup failed: {e}\n"
                    "ARLO needs screen-share permission on Wayland (a one-time dialog).\n"
                    "If no dialog appeared, check that xdg-desktop-portal and your "
                    "desktop's portal backend are running.\n"
                    "Alternatively use an X11 session with SCREEN_BACKEND=x11."
                )
                raise OSError(msg) from e
            if self._session.new_restore_token:
                try:
                    save_token(self._session.new_restore_token)
                except OSError as e:
                    logger.warning(f"Could not save screen-share restore token: {e}")

            # The rate cap must sit directly after the source: it is negotiated
            # with the compositor, which then throttles its own recording.
            desc = (
                f"pipewiresrc fd={self._session.pipewire_fd} "
                f"path={self._session.node_id} keepalive-time={KEEPALIVE_MS} "
                f"! video/x-raw,max-framerate={self._capture_fps}/1 "
                "! queue max-size-buffers=1 leaky=downstream "
                "! videoconvert n-threads=2 ! video/x-raw,format=BGRx "
                "! appsink name=sink emit-signals=true max-buffers=1 "
                "drop=true sync=false"
            )
            try:
                self._pipeline = Gst.parse_launch(desc)
            except GLib.Error as e:
                msg = (
                    f"GStreamer pipeline creation failed: {e.message}\n"
                    "Is gst-plugin-pipewire installed?\n" + _INSTALL_HINT
                )
                raise OSError(msg) from e
            pipeline = self._pipeline
            if pipeline is None:
                msg = "GStreamer pipeline was not created"
                raise OSError(msg)  # ruff: ignore[raise-within-try]
            sink = pipeline.get_by_name("sink")
            if sink is None:
                msg = "GStreamer appsink missing from pipeline"
                raise OSError(msg)  # ruff: ignore[raise-within-try]
            sink.connect("new-sample", self._on_new_sample)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                msg = f"GStreamer pipeline refused to start.{self._collect_bus_error()}"
                raise OSError(msg)  # ruff: ignore[raise-within-try]

            if not self._first_frame.wait(FIRST_FRAME_TIMEOUT_S):
                bus_error = self._collect_bus_error()
                msg = (
                    "No frame arrived from the compositor within "
                    f"{FIRST_FRAME_TIMEOUT_S:.0f}s.{bus_error}\n"
                    "If the screen-share dialog was approved, try moving the mouse "
                    "(some compositors only send frames on screen changes)."
                )
                raise OSError(msg)  # ruff: ignore[raise-within-try]
        except Exception:
            self.stop()
            raise

        with self._lock:
            frame = self._frame
        if frame is None:
            msg = "First frame vanished unexpectedly"
            raise OSError(msg)
        _data, width, height, _stride = frame
        self._size = (width, height)
        self._tk_scale = compute_scale(width, self._x11_logical_width())
        self._terminal_error = None
        logger.info(
            f"Wayland capture: {width}x{height}, tk scale {self._tk_scale:.4g}, "
            f"capped at {self._capture_fps} fps"
        )

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def set_live_capture(self, *, enabled: bool) -> None:
        """No-op: the stream always runs, throttled by the compositor."""

    def invalidate_cached_frame(self) -> None:
        """No-op: the newest sample is always current to within the keepalive."""

    def grab(self, bbox: tuple[int, int, int, int]) -> Image.Image:
        if self._terminal_error is not None:
            raise ScreenCaptureUnavailable(self._terminal_error)
        session = self._session
        if session is None or session.closed.is_set():
            self._attempt_restart()

        frame = self._fresh_frame()
        if frame is None:
            self._attempt_restart(force=True)
            frame = self._fresh_frame()
        if frame is None:
            msg = "No current frame from the compositor"
            raise FrameUnavailableError(msg)
        data, width, height, stride = frame
        return crop_bgrx(data, width, height, stride, bbox)

    def frame_age(self) -> float | None:
        """Seconds since the newest sample arrived, or None before the first."""
        with self._lock:
            at = self._frame_at
        return time.monotonic() - at if at > 0 else None

    def cursor_position(self) -> Point:
        from pynput import mouse as pynput_mouse

        pos = pynput_mouse.Controller().position
        return Point(
            x=round(pos[0] * self._tk_scale),
            y=round(pos[1] * self._tk_scale),
        )

    def resolution(self) -> tuple[int, int]:
        return self._size

    @property
    def tk_scale(self) -> float:
        return self._tk_scale

    # ------------------------------------------------------------------

    def _fresh_frame(self) -> _Frame | None:
        """Return the newest frame, or None when the stream has gone silent."""
        now = time.monotonic()
        with self._lock:
            frame = self._frame
            if frame is None or self._frame_at <= 0:
                return None
            age = now - self._frame_at
            if age > STALE_AFTER_S:
                return None
            stats = self._stats
            stats.grabs += 1
            stats.max_grab_age_s = max(stats.max_grab_age_s, age)
        return frame

    def _clear_frame(self) -> None:
        with self._lock:
            self._frame = None
            self._frame_at = 0.0
            self._last_stream_seq = None
            self._stats = _StreamStats()
        self._first_frame.clear()

    def _on_new_sample(self, sink: object) -> Gst.FlowReturn:
        emit = getattr(sink, "emit", None)
        if emit is None:
            return Gst.FlowReturn.OK
        sample = emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        self._ingest_sample(sample)
        return Gst.FlowReturn.OK

    def _ingest_sample(self, sample: object) -> bool:
        get_buffer = getattr(sample, "get_buffer", None)
        get_caps = getattr(sample, "get_caps", None)
        if get_buffer is None or get_caps is None:
            return False
        buf = get_buffer()
        caps = get_caps()
        if buf is None or caps is None:
            return False
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        meta = GstVideo.buffer_get_video_meta(buf)
        stride = int(meta.stride[0] if meta else width * 4)
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        if len(data) < height * stride and height > 0:
            stride = len(data) // height
        # pipewiresrc stores the PipeWire sequence number in the buffer offset;
        # a keepalive repeat carries the same one as the frame it repeats.
        offset = int(buf.offset)
        stream_seq = None if offset == Gst.BUFFER_OFFSET_NONE else offset
        stats_line = self._store_frame((data, width, height, stride), stream_seq)
        self._first_frame.set()
        if stats_line:
            logger.info(stats_line)
        return True

    def _store_frame(self, frame: _Frame, stream_seq: int | None) -> str | None:
        """Publish ``frame`` as the newest; return a stats line once a minute."""
        now = time.monotonic()
        with self._lock:
            stats = self._stats
            if stats.started_at <= 0:
                stats.started_at = now
            elif self._frame_at > 0:
                stats.max_gap_s = max(stats.max_gap_s, now - self._frame_at)
            stats.samples += 1
            if stream_seq is not None and stream_seq != self._last_stream_seq:
                stats.new_frames += 1
            self._last_stream_seq = stream_seq
            self._frame = frame
            self._frame_at = now
            self._frame_seq += 1
            elapsed = now - stats.started_at
            if elapsed < STATS_INTERVAL_S:
                return None
            self._stats = _StreamStats(started_at=now)
        return _format_stats(stats, elapsed)

    def _collect_bus_error(self) -> str:
        if self._pipeline is None:
            return ""
        bus = self._pipeline.get_bus()
        parts = []
        while True:
            message = bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
            if message is None:
                break
            err, debug = message.parse_error()
            parts.append(f"{err.message} ({debug})")
        return f"\nGStreamer errors: {'; '.join(parts)}" if parts else ""

    @staticmethod
    def _x11_logical_width() -> int:
        try:
            from Xlib import display as xlib_display

            xdisplay = xlib_display.Display()
            width = xdisplay.screen().width_in_pixels
            xdisplay.close()
        except Exception as e:  # ruff: ignore[blind-except] - no XWayland: degrade gracefully
            logger.warning(f"XWayland not reachable ({e}); assuming scale 1.0")
            return 0
        return width

    def _attempt_restart(self, *, force: bool = False) -> None:
        """Rebuild the portal session and pipeline.

        Without ``force`` this only acts when the portal closed the session.
        ``force`` is the dead-stream path: the session looks open but no sample
        has arrived within ``STALE_AFTER_S``.
        """
        with self._restart_lock:
            session = self._session
            if not force and session is not None and not session.closed.is_set():
                return
            if force and self._has_fresh_frame():
                return  # another thread reconnected while we waited for the lock
            now = time.monotonic()
            elapsed = now - self._last_restart_at
            if self._last_restart_at <= 0 or elapsed > RESTART_STREAK_RESET_S:
                self._restart_streak = 0
            cooldown = min(
                RESTART_COOLDOWN_S * 2**self._restart_streak, RESTART_COOLDOWN_MAX_S
            )
            if self._last_restart_at > 0 and elapsed < cooldown:
                remaining = cooldown - elapsed
                msg = f"Screen share reconnecting (retry in {remaining:.1f}s)"
                raise FrameUnavailableError(msg)
            self._last_restart_at = now
            self._restart_streak += 1
            if force:
                age = self.frame_age()
                silent = f"{age:.1f}s" if age is not None else "ever"
                logger.warning(
                    "ScreenCast delivered no frame for %s — forcing reconnect%s",
                    silent,
                    self._collect_bus_error(),
                )
            else:
                logger.warning("Portal session closed - reconnecting screen share")
            self.stop()
            try:
                self.start()
            except Exception as e:
                logger.error(f"Screen share reconnect failed: {e}")
                msg = f"Screen sharing reconnect failed: {e}"
                raise FrameUnavailableError(msg) from e

    def _has_fresh_frame(self) -> bool:
        age = self.frame_age()
        return age is not None and age <= STALE_AFTER_S


def _format_stats(stats: _StreamStats, elapsed: float) -> str:
    """One log line describing a finished stats window."""
    per_s = stats.samples / elapsed if elapsed > 0 else 0.0
    if stats.new_frames:
        rate = f"{stats.new_frames / elapsed:.1f} new frames/s ({per_s:.1f} samples/s with keepalive)"
    else:
        rate = f"{per_s:.1f} samples/s"
    return (
        f"ScreenCast stats: {rate}, longest gap {stats.max_gap_s * 1000:.0f}ms, "
        f"max frame age at grab {stats.max_grab_age_s * 1000:.0f}ms over "
        f"{stats.grabs} grabs"
    )
