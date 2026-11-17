"""Wayland screen backend: portal ScreenCast stream consumed via GStreamer.

Pipeline: pipewiresrc → leaky queue → videoconvert → appsink.

Frames are pulled on demand from ``grab()`` (OCR scan rate), not copied on
every compositor damage. The leaky queue drops excess frames *before*
videoconvert so a 60–144 Hz game does not burn CPU converting unused frames.
"""

import threading
import time
from typing import cast

from PIL import Image

from arc_helper.config import logger

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
KEEPALIVE_MS = 500
# Avoid spamming the portal when the compositor flaps the session
# (monitor hotplug, tray "Stop sharing", competing ScreenCast clients).
RESTART_COOLDOWN_S = 2.0
_FIRST_FRAME_POLL_S = 0.1
# After unpausing for an idle probe, wait briefly for a fresh sample.
_WAKE_PULL_TIMEOUT_S = 0.25


class WaylandBackend:
    """ScreenBackend implementation for Wayland sessions."""

    name = "wayland"

    def __init__(self) -> None:
        self._session: ScreenCastSession | None = None
        self._pipeline: Gst.Element | None = None
        self._lock = threading.Lock()
        self._frame: tuple[bytes, int, int, int] | None = None  # data, w, h, stride
        self._size: tuple[int, int] = (0, 0)
        self._tk_scale = 1.0
        self._restart_lock = threading.Lock()
        self._pipeline_state_lock = threading.Lock()
        self._last_restart_at = 0.0
        # True while inventory/map scanning needs a steady stream; False during
        # gameplay idle (pipeline PAUSED between sparse trigger probes).
        self._live_capture = True
        # Only set for truly fatal setup failures callers may latch on;
        # portal session closes recover via _attempt_restart instead.
        self._terminal_error: str | None = None

    def start(self) -> None:
        Gst.init(None)
        self._frame = None

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

            # leaky queue before videoconvert: drop damaged frames we will never
            # pull, so CPU convert only runs for samples grab() actually needs.
            desc = (
                f"pipewiresrc fd={self._session.pipewire_fd} "
                f"path={self._session.node_id} keepalive-time={KEEPALIVE_MS} "
                "! queue max-size-buffers=1 leaky=downstream "
                "! videoconvert n-threads=2 ! video/x-raw,format=BGRx "
                "! appsink name=sink max-buffers=1 drop=true sync=false"
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
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                msg = f"GStreamer pipeline refused to start.{self._collect_bus_error()}"
                raise OSError(msg)  # ruff: ignore[raise-within-try]

            deadline = time.monotonic() + FIRST_FRAME_TIMEOUT_S
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                timeout_ns = int(min(remaining, _FIRST_FRAME_POLL_S) * Gst.SECOND)
                if self._try_pull_frame(timeout_ns=max(timeout_ns, 1)):
                    break
            else:
                bus_error = self._collect_bus_error()
                msg = (
                    "No frame arrived from the compositor within "
                    f"{FIRST_FRAME_TIMEOUT_S:.0f}s.{bus_error}\n"
                    "If the screen-share dialog was approved, try moving the mouse "
                    "(some compositors only send frames on screen changes)."
                )
                raise OSError(msg)  # ruff: ignore[raise-within-try]
        except Exception:
            self.stop()  # release partial session/pipeline; safe and idempotent
            raise

        with self._lock:
            frame = self._frame
        if frame is None:
            msg = "First frame vanished unexpectedly"
            raise OSError(msg)
        # Local copy after None-guard (pyright can lose the tuple narrow here).
        _data, width, height, _stride = cast("tuple[bytes, int, int, int]", frame)
        # Single-monitor assumption: XWayland reports the total logical
        # screen width, the stream is one monitor. Fine for the primary-
        # monitor use case; multi-monitor scaling is a known limitation.
        # Plain attribute stores (atomic in CPython); readers may see one stale frame's worth during restart - harmless.
        self._size = (width, height)
        self._tk_scale = compute_scale(width, self._x11_logical_width())
        self._terminal_error = None
        # Default to paused-between-grabs; scanner enables live for inventory/map.
        self._live_capture = False
        self._set_pipeline_playing(playing=False)
        logger.info(f"Wayland capture: {width}x{height}, tk scale {self._tk_scale:.4g}")

    def stop(self) -> None:
        # Order matters: NULL the pipeline first (joins streaming threads),
        # then close the session (which closes our pipewire fd).
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def set_live_capture(self, *, enabled: bool) -> None:
        """Keep the pipeline PLAYING (inventory/map) or PAUSED between probes."""
        with self._pipeline_state_lock:
            if self._live_capture == enabled:
                return
            self._live_capture = enabled
            self._set_pipeline_playing(playing=enabled)

    def grab(self, bbox: tuple[int, int, int, int]) -> Image.Image:
        if self._terminal_error is not None:
            raise ScreenCaptureUnavailable(self._terminal_error)
        session = self._session  # local ref: stop() may null it concurrently
        if session is None or session.closed.is_set():
            self._attempt_restart()
        with self._pipeline_state_lock:
            live = self._live_capture
            if not live:
                self._set_pipeline_playing(playing=True)
            try:
                if live:
                    # Steady stream: take a newer sample if waiting; else reuse.
                    self._try_pull_frame(timeout_ns=0)
                else:
                    # Brief wake for an idle inventory/map probe.
                    timeout_ns = int(_WAKE_PULL_TIMEOUT_S * Gst.SECOND)
                    if not self._try_pull_frame(timeout_ns=timeout_ns):
                        self._try_pull_frame(timeout_ns=0)
                with self._lock:
                    frame = self._frame
            finally:
                if not live:
                    self._set_pipeline_playing(playing=False)
        if frame is None:
            msg = "No frame received from the compositor yet"
            raise OSError(msg)
        data, width, height, stride = frame
        return crop_bgrx(data, width, height, stride, bbox)

    def cursor_position(self) -> Point:
        # Wayland has no global cursor query. Xlib coordinates are valid
        # while the pointer is over an XWayland window (the Proton game is
        # one); scale them up to physical/stream pixels.
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

    def _set_pipeline_playing(self, *, playing: bool) -> None:
        """PLAYING vs PAUSED. Caller holds ``_pipeline_state_lock`` when racing grab."""
        pipeline = self._pipeline
        if pipeline is None:
            return
        state = Gst.State.PLAYING if playing else Gst.State.PAUSED
        if pipeline.set_state(state) == Gst.StateChangeReturn.FAILURE:
            label = "PLAYING" if playing else "PAUSED"
            logger.warning(
                "GStreamer failed to enter %s%s",
                label,
                self._collect_bus_error(),
            )

    def _try_pull_frame(self, *, timeout_ns: int = 0) -> bool:
        """Pull one appsink sample into ``_frame``. Return True if updated."""
        pipeline = self._pipeline
        if pipeline is None:
            return False
        sink = pipeline.get_by_name("sink")
        if sink is None:
            return False
        sample = sink.emit("try-pull-sample", timeout_ns)
        if sample is None:
            return False
        return self._ingest_sample(sample)

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
            stride = len(data) // height  # defensive: tightly packed buffer
        with self._lock:
            self._frame = (data, width, height, stride)
        return True

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
            return 0  # compute_scale(.., 0) -> 1.0
        return width

    def _attempt_restart(self) -> None:
        """Reconnect ScreenCast after the compositor/user closes the session.

        Retries indefinitely with a cooldown. Failures raise plain OSError so
        OCR can soft-fail (black frame) and the next scan tick tries again —
        typically via the restore token without another dialog.
        """
        with self._restart_lock:
            session = self._session
            if session is not None and not session.closed.is_set():
                return  # another grab() already recovered
            now = time.monotonic()
            elapsed = now - self._last_restart_at
            if self._last_restart_at > 0 and elapsed < RESTART_COOLDOWN_S:
                remaining = RESTART_COOLDOWN_S - elapsed
                msg = f"Screen share reconnecting (retry in {remaining:.1f}s)"
                raise OSError(msg)
            self._last_restart_at = now
            logger.warning("Portal session closed - reconnecting screen share")
            self.stop()
            try:
                self.start()
            except Exception as e:
                # Leave session/pipeline None so the next grab retries.
                logger.error(f"Screen share reconnect failed: {e}")
                msg = f"Screen sharing reconnect failed: {e}"
                raise OSError(msg) from e
