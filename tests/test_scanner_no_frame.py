"""A grab with no current frame skips the scan instead of reading as a miss."""

import queue
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from arc_helper import main as main_module
from arc_helper.main import Scanner
from arc_helper.main import ScannerState
from arc_helper.map_sync import MapSyncEngine
from arc_helper.ocr import OCREngine
from arc_helper.screen import FrameUnavailableError
from arc_helper.screen import ScreenCaptureUnavailable


def _scanner() -> Scanner:
    scanner = Scanner(
        root=MagicMock(),
        overlay=MagicMock(),
        status=MagicMock(),
        db=MagicMock(),
        ui_queue=queue.Queue(),
    )
    scanner._wayland_stable = True
    return scanner


def _region() -> MagicMock:
    region = MagicMock()
    region.bbox = (0, 0, 10, 10)
    region.width = 10
    region.height = 10
    return region


def _fail_grab(error: Exception):
    def _grab(_bbox: tuple[int, int, int, int]):
        raise error

    return _grab


def test_capture_region_reports_no_frame_instead_of_black_image():
    region = _region()
    with (
        patch("arc_helper.ocr.grab_screen", _fail_grab(OSError("stream stalled"))),
        pytest.raises(FrameUnavailableError, match="stream stalled"),
    ):
        OCREngine.capture_region(region)


def test_capture_region_keeps_terminal_error_distinct():
    region = _region()
    with (
        patch(
            "arc_helper.ocr.grab_screen",
            _fail_grab(ScreenCaptureUnavailable("revoked")),
        ),
        pytest.raises(ScreenCaptureUnavailable),
    ):
        OCREngine.capture_region(region)


def test_detect_map_open_propagates_no_frame():
    settings = MagicMock()
    settings.map_trigger_region.width = 50
    settings.map_trigger_region.height = 20
    settings.map_trigger_region.bbox = (0, 0, 50, 20)
    with (
        patch("arc_helper.map_sync.get_settings", return_value=settings),
        patch("arc_helper.map_sync.get_ocr_engine", return_value=MagicMock()),
        patch(
            "arc_helper.map_sync.grab_screen",
            _fail_grab(FrameUnavailableError("reconnecting")),
        ),
        pytest.raises(FrameUnavailableError),
    ):
        MapSyncEngine.detect_map_open()


def test_no_frame_skips_scan_without_leaving_inventory():
    scanner = _scanner()
    scanner.state = ScannerState.ACTIVE
    scanner._running = True
    ticks: list[int] = []

    def _tick(_ocr: object, _settings: object) -> bool:
        ticks.append(1)
        if len(ticks) <= 2:
            msg = "reconnecting"
            raise FrameUnavailableError(msg)
        scanner._running = False
        return True

    with (
        patch.object(Scanner, "_scan_tick", side_effect=_tick),
        patch("arc_helper.main.get_settings", return_value=MagicMock()),
        patch("arc_helper.main.get_ocr_engine", return_value=MagicMock()),
        patch("arc_helper.main.time.sleep") as sleep,
    ):
        scanner._scan_loop()

    assert len(ticks) == 3
    assert scanner.state == ScannerState.ACTIVE
    assert scanner._trigger_miss_streak == 0
    assert scanner._no_frame_streak == 0  # reset once a scan succeeds again
    assert [c.args[0] for c in sleep.call_args_list] == [
        main_module._NO_FRAME_RETRY_S,
        main_module._NO_FRAME_RETRY_S,
    ]
    assert scanner.ui_queue.empty()  # no "error" status, no overlay hide


def test_open_map_is_polled_faster_than_idle_interval():
    scanner = _scanner()
    settings = MagicMock()
    settings.scan.idle_scan_interval = 1.0
    with (
        patch.object(MapSyncEngine, "detect_map_open", return_value=True),
        patch("arc_helper.main.time.sleep") as sleep,
    ):
        assert scanner._idle_phase(hotkey_now=False, ocr=MagicMock(), settings=settings)

    sleep.assert_called_once_with(main_module._MAP_OPEN_POLL_S)
    assert settings.scan.idle_scan_interval > main_module._MAP_OPEN_POLL_S
