"""Measure how the Wayland ScreenCast stream behaves on this machine.

Stop ARLO first (two ScreenCast clients on the same share can interfere).
A static desktop is enough to measure delivered rate, gaps and this
process's CPU. Put the game on screen only if you also want to watch
in-game frame time while the probe runs — inventory does not need to
be open.

    uv run python -m arc_helper.screen.streamprobe
    uv run python -m arc_helper.screen.streamprobe --fps 20,10,5 --seconds 15
    uv run python -m arc_helper.screen.streamprobe --only steady,cap5 --seconds 20

Each scenario opens its own portal session (the compositor drops a stream when
its consumer disconnects), builds the capture pipeline with different options
and reports what actually arrived:

* ``samples/s``  every buffer that reached appsink, keepalive repeats included
* ``frames/s``   buffers carrying a new compositor frame (sequence changed)
* ``max gap``    longest stretch with no buffer at all
* ``cpu``        this process's CPU time as a share of one core

Nothing is saved; frames are only counted.
"""

from __future__ import annotations

import argparse
import itertools
import statistics
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from dataclasses import field

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib  # pyright: ignore[reportAttributeAccessIssue]  # ruff: ignore[module-import-not-at-top-of-file]
from gi.repository import Gst  # pyright: ignore[reportAttributeAccessIssue]  # ruff: ignore[module-import-not-at-top-of-file]
from gi.repository import GstVideo  # pyright: ignore[reportAttributeAccessIssue]  # ruff: ignore[module-import-not-at-top-of-file]

from .portal import CURSOR_MODE_HIDDEN  # ruff: ignore[module-import-not-at-top-of-file]
from .portal import CURSOR_MODE_METADATA  # ruff: ignore[module-import-not-at-top-of-file]
from .portal import PortalError  # ruff: ignore[module-import-not-at-top-of-file]
from .portal import ScreenCastSession  # ruff: ignore[module-import-not-at-top-of-file]
from .token_store import load_token  # ruff: ignore[module-import-not-at-top-of-file]
from .token_store import save_token  # ruff: ignore[module-import-not-at-top-of-file]

KEEPALIVE_MS = 500
FIRST_FRAME_TIMEOUT_S = 10.0
# How long the old idle probe waited for a frame after un-pausing.
WAKE_TIMEOUT_S = 0.55
# Sparse sample of the frame for a cheap "did the picture change" check.
_DIGEST_STEP = 4099
_SESSION_SETTLE_S = 0.5


@dataclass(frozen=True)
class Scenario:
    name: str
    max_fps: int = 0  # 0 = no cap requested
    provide_clock: bool = True
    cycles: int = 0  # PAUSED/PLAYING cycles before the measured window
    cursor_metadata: bool = False


@dataclass
class Recorder:
    """Arrival log filled from the appsink streaming thread."""

    arrivals: list[float] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    digests: list[int] = field(default_factory=list)
    cursors: list[tuple[int, int] | None] = field(default_factory=list)
    _cond: threading.Condition = field(default_factory=threading.Condition)

    def on_sample(self, sink: object) -> Gst.FlowReturn:
        emit = getattr(sink, "emit", None)
        sample = emit("pull-sample") if emit is not None else None
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        if buf is None:
            return Gst.FlowReturn.OK
        # Copy the frame like the real backend does, so the CPU figure is honest.
        digest = 0
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(mapinfo.data)
                digest = zlib.adler32(data[::_DIGEST_STEP])
            finally:
                buf.unmap(mapinfo)
        cursor = _cursor_from_buffer(buf)
        with self._cond:
            self.arrivals.append(time.monotonic())
            self.offsets.append(int(buf.offset))
            self.digests.append(digest)
            self.cursors.append(cursor)
            self._cond.notify_all()
        return Gst.FlowReturn.OK

    def count(self) -> int:
        with self._cond:
            return len(self.arrivals)

    def wait_for_count(self, target: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: len(self.arrivals) >= target, timeout)

    def snapshot(
        self,
    ) -> tuple[list[float], list[int], list[int], list[tuple[int, int] | None]]:
        with self._cond:
            return (
                list(self.arrivals),
                list(self.offsets),
                list(self.digests),
                list(self.cursors),
            )


def _cursor_from_buffer(buf: object) -> tuple[int, int] | None:
    """Cursor position pipewiresrc attaches when the session uses metadata mode."""
    try:
        meta = GstVideo.buffer_get_video_region_of_interest_meta_id(buf, 0)
    except Exception:  # ruff: ignore[blind-except] - GI binding differences
        return None
    if meta is None:
        return None
    try:
        if GLib.quark_to_string(meta.roi_type) != "cursor":
            return None
        return int(meta.x), int(meta.y)
    except Exception:  # ruff: ignore[blind-except]
        return None


def _changes(values: list[int]) -> int:
    """Number of distinct runs in ``values`` (1 for a constant list)."""
    if not values:
        return 0
    return 1 + sum(1 for a, b in itertools.pairwise(values) if a != b)


def window_stats(rec: Recorder, t0: float, t1: float) -> dict[str, float | int | None]:
    """Rates and the longest silent stretch inside ``[t0, t1]``."""
    arrivals, offsets, digests, _cursors = rec.snapshot()
    idx = [i for i, t in enumerate(arrivals) if t0 <= t <= t1]
    span = max(t1 - t0, 1e-9)
    times = [arrivals[i] for i in idx]
    edges = [t0, *times, t1]
    max_gap = max(b - a for a, b in itertools.pairwise(edges))
    seqs = [offsets[i] for i in idx]
    # Without a header meta every buffer reports the same "no offset" value.
    seq_known = len(set(seqs)) > 1
    return {
        "samples": len(idx),
        "samples_per_s": len(idx) / span,
        "frames_per_s": (_changes(seqs) / span) if seq_known else None,
        "content_changes_per_s": _changes([digests[i] for i in idx]) / span,
        "max_gap_s": max_gap,
    }


def _pipeline_desc(session: ScreenCastSession, sc: Scenario) -> str:
    clock = "" if sc.provide_clock else "provide-clock=false "
    cap = f"! video/x-raw,max-framerate={sc.max_fps}/1 " if sc.max_fps else ""
    return (
        f"pipewiresrc name=src fd={session.pipewire_fd} path={session.node_id} "
        f"keepalive-time={KEEPALIVE_MS} {clock}"
        f"{cap}"
        "! queue max-size-buffers=1 leaky=downstream "
        "! videoconvert n-threads=2 ! video/x-raw,format=BGRx "
        "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )


def _drain_bus(pipeline: Gst.Element) -> list[str]:
    bus = pipeline.get_bus()
    out: list[str] = []
    mask = Gst.MessageType.ERROR | Gst.MessageType.WARNING
    while True:
        message = bus.timed_pop_filtered(0, mask)
        if message is None:
            return out
        if message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            out.append(f"ERROR {err.message}")
        else:
            err, _debug = message.parse_warning()
            out.append(f"WARNING {err.message}")


def _negotiated_caps(pipeline: Gst.Element) -> str:
    src = pipeline.get_by_name("src")
    pad = src.get_static_pad("src") if src is not None else None
    caps = pad.get_current_caps() if pad is not None else None
    return caps.to_string() if caps is not None else "(none)"


def _run_cycles(
    pipeline: Gst.Element, rec: Recorder, sc: Scenario, pause_s: float
) -> dict[str, float | int | None]:
    """Replay the old idle probe: PAUSED, then wake for one frame, repeat."""
    latencies: list[float] = []
    misses = 0
    failures = 0
    pipeline.set_state(Gst.State.PAUSED)
    for _ in range(sc.cycles):
        time.sleep(pause_s)
        before = rec.count()
        t0 = time.monotonic()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            failures += 1
        if rec.wait_for_count(before + 1, WAKE_TIMEOUT_S):
            latencies.append(time.monotonic() - t0)
        else:
            misses += 1
        if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            failures += 1
    # Then go live, as the scanner did on entering inventory/map.
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        failures += 1
    return {
        "cycles": sc.cycles,
        "wake_misses": misses,
        "wake_median_s": statistics.median(latencies) if latencies else None,
        "wake_max_s": max(latencies) if latencies else None,
        "state_failures": failures,
    }


def _cursor_report(rec: Recorder) -> dict[str, object]:
    _arrivals, _offsets, _digests, cursors = rec.snapshot()
    seen = [c for c in cursors if c is not None]
    report: dict[str, object] = {
        "buffers_with_cursor": len(seen),
        "buffers_total": len(cursors),
        "distinct_positions": len(set(seen)),
        "last_position": seen[-1] if seen else None,
    }
    try:
        from pynput import mouse as pynput_mouse

        report["xwayland_position"] = tuple(pynput_mouse.Controller().position)
    except Exception as e:  # ruff: ignore[blind-except]
        report["xwayland_position"] = f"unavailable ({e})"
    return report


def run_scenario(
    sc: Scenario, *, seconds: float, pause_s: float, steady_after_s: float
) -> dict[str, object]:
    result: dict[str, object] = {"name": sc.name}
    mode = CURSOR_MODE_METADATA if sc.cursor_metadata else CURSOR_MODE_HIDDEN
    session = ScreenCastSession(restore_token=load_token(), cursor_mode=mode)
    pipeline: Gst.Element | None = None
    try:
        session.open()
        if session.new_restore_token:
            save_token(session.new_restore_token)
        rec = Recorder()
        pipeline = Gst.parse_launch(_pipeline_desc(session, sc))
        sink = pipeline.get_by_name("sink") if pipeline is not None else None
        if pipeline is None or sink is None:
            result["error"] = "pipeline was not created"
            return result
        sink.connect("new-sample", rec.on_sample)

        t_play = time.monotonic()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            result["error"] = "pipeline refused PLAYING: " + "; ".join(
                _drain_bus(pipeline)
            )
            return result
        if not rec.wait_for_count(1, FIRST_FRAME_TIMEOUT_S):
            result["error"] = "no first frame: " + "; ".join(_drain_bus(pipeline))
            return result
        result["first_frame_s"] = rec.snapshot()[0][0] - t_play
        result["caps"] = _negotiated_caps(pipeline)

        window = seconds
        if sc.cycles:
            result["cycling"] = _run_cycles(pipeline, rec, sc, pause_s)
            window = steady_after_s

        cpu0 = time.process_time()
        t0 = time.monotonic()
        time.sleep(window)
        t1 = time.monotonic()
        cpu1 = time.process_time()

        result["window_s"] = t1 - t0
        result["stats"] = window_stats(rec, t0, t1)
        result["cpu_pct"] = 100.0 * (cpu1 - cpu0) / max(t1 - t0, 1e-9)
        if sc.cursor_metadata:
            result["cursor"] = _cursor_report(rec)
        bus = _drain_bus(pipeline)
        if bus:
            result["bus"] = bus
    except (PortalError, GLib.Error, OSError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        session.close()
        time.sleep(_SESSION_SETTLE_S)
    return result


def _fmt(value: object, spec: str = ".1f") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return format(value, spec)
    return str(value)


def print_result(result: dict[str, object]) -> None:
    print(f"\n== {result['name']} ==")
    if "error" in result:
        print(f"  FAILED: {result['error']}")
        return
    print(f"  caps        : {result.get('caps')}")
    print(f"  first frame : {_fmt(result.get('first_frame_s'), '.2f')} s")
    cycling = result.get("cycling")
    if isinstance(cycling, dict):
        print(
            f"  cycling     : {cycling['cycles']} cycles, "
            f"{cycling['wake_misses']} wakes got no frame within "
            f"{WAKE_TIMEOUT_S:.2f}s, wake median "
            f"{_fmt(cycling['wake_median_s'], '.3f')}s max "
            f"{_fmt(cycling['wake_max_s'], '.3f')}s, "
            f"{cycling['state_failures']} state-change failures"
        )
    stats = result.get("stats")
    if isinstance(stats, dict):
        print(
            f"  window      : {_fmt(result.get('window_s'))} s, "
            f"{stats['samples']} samples"
        )
        print(f"  samples/s   : {_fmt(stats['samples_per_s'])}")
        print(f"  frames/s    : {_fmt(stats['frames_per_s'])}")
        print(f"  changed/s   : {_fmt(stats['content_changes_per_s'])}")
        print(f"  max gap     : {_fmt(stats['max_gap_s'], '.3f')} s")
    print(f"  cpu         : {_fmt(result.get('cpu_pct'))} % of one core")
    cursor = result.get("cursor")
    if isinstance(cursor, dict):
        print(f"  cursor meta : {cursor}")
    for line in result.get("bus") or []:  # pyright: ignore[reportGeneralTypeIssues]
        print(f"  bus         : {line}")


def _parse_fps_list(raw: str) -> tuple[int, ...]:
    """Comma-separated positive ints → unique rates, order preserved."""
    rates: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        piece = token.strip()
        if not piece:
            continue
        try:
            rate = int(piece)
        except ValueError:
            msg = f"invalid --fps value {piece!r} (expected integers)"
            raise argparse.ArgumentTypeError(msg) from None
        if rate < 1:
            msg = f"fps must be >= 1, got {rate}"
            raise argparse.ArgumentTypeError(msg)
        if rate in seen:
            continue
        seen.add(rate)
        rates.append(rate)
    return tuple(rates)


def build_scenarios(cycles: int, caps: tuple[int, ...]) -> list[Scenario]:
    scenarios = [Scenario("steady")]
    scenarios.extend(Scenario(f"cap{fps}", max_fps=fps) for fps in caps)
    scenarios.extend(
        [
            Scenario("steady-noclock", provide_clock=False),
            Scenario("cycle", cycles=cycles),
            Scenario("cycle-noclock", cycles=cycles, provide_clock=False),
            # Cursor metadata check; rate only bounds cost, not the result.
            Scenario("cursor", max_fps=10, cursor_metadata=True),
        ]
    )
    return scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--steady-after", type=float, default=15.0)
    parser.add_argument(
        "--fps",
        type=_parse_fps_list,
        default=_parse_fps_list("10,5,2"),
        help="comma-separated max-framerate caps to probe (each becomes scenario capN)",
    )
    parser.add_argument(
        "--only", default="", help="comma-separated scenario names to run"
    )
    args = parser.parse_args(argv)

    Gst.init(None)
    scenarios = build_scenarios(args.cycles, args.fps)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {sc.name for sc in scenarios}
        if unknown:
            hint = ""
            missing_caps = sorted(
                name
                for name in unknown
                if name.startswith("cap") and name[3:].isdigit()
            )
            if missing_caps:
                rates = ",".join(name[3:] for name in missing_caps)
                hint = f" (add them with --fps {rates},…)"
            parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}{hint}")
        scenarios = [sc for sc in scenarios if sc.name in wanted]

    for sc in scenarios:
        result = run_scenario(
            sc,
            seconds=args.seconds,
            pause_s=args.pause,
            steady_after_s=args.steady_after,
        )
        print_result(result)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
