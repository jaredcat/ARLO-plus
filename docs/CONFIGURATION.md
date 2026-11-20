
## Configuration

All configuration is stored in the `.env` file in the application directory. You can edit this file with any text editor.

### Configuration Options

```env
# =============================================================================
# TRIGGER REGIONS - Where "INVENTORY" text appears
# =============================================================================

# Region 1 - Menu mode
TRIGGER_REGION_X=1450
TRIGGER_REGION_Y=23
TRIGGER_REGION_WIDTH=173
TRIGGER_REGION_HEIGHT=44

# Region 2 - In-raid mode
TRIGGER_REGION2_X=1295
TRIGGER_REGION2_Y=38
TRIGGER_REGION2_WIDTH=173
TRIGGER_REGION2_HEIGHT=44

# =============================================================================
# TOOLTIP CAPTURE - Area captured around cursor to find tooltip
# =============================================================================

TOOLTIP_CAPTURE_WIDTH=550
TOOLTIP_CAPTURE_HEIGHT=550
TOOLTIP_CAPTURE_OFFSET_X=50      # Horizontal offset from cursor
TOOLTIP_CAPTURE_OFFSET_Y=-500    # Vertical offset from cursor (negative = above)

# =============================================================================
# OVERLAY SETTINGS - How the recommendation overlay appears
# =============================================================================

OVERLAY_X=100                    # Overlay X position on screen
OVERLAY_Y=100                    # Overlay Y position on screen
OVERLAY_DISPLAY_TIME=4.0         # How long the overlay stays visible (seconds)
OVERLAY_COOLDOWN=2.0             # Minimum time between showing same item (seconds)

# =============================================================================
# SCANNING SETTINGS - How often the app scans the screen
# =============================================================================

TRIGGER_SCAN_INTERVAL=0.5        # Seconds between trigger scans (legacy; idle uses IDLE_SCAN_INTERVAL)
IDLE_SCAN_INTERVAL=1.0           # Seconds between inventory/map probes while idle (gameplay)
TOOLTIP_SCAN_INTERVAL=0.3        # Seconds between tooltip scans

# =============================================================================
# PLATFORM SETTINGS (Linux)
# =============================================================================

SCREEN_BACKEND=auto              # auto | x11 | wayland
WAYLAND_CAPTURE_FPS=10           # Wayland only: screen capture rate cap, see below

# =============================================================================
# PROJECTS
# =============================================================================

DISABLE_PAUSED_PROJECTS=true     # Omit PAUSED catalog projects (e.g. Expedition); false = show last

# =============================================================================
# COMPANION MAP
# =============================================================================

MAP_HOTKEY=F8                    # Toggle companion map window
MAP_FIND_HOTKEY=F9               # Find last hovered item on the map

# Optional MAP-tab OCR region (leave width/height at 1 to use heuristic)
MAP_TRIGGER_REGION_X=0
MAP_TRIGGER_REGION_Y=0
MAP_TRIGGER_REGION_WIDTH=1
MAP_TRIGGER_REGION_HEIGHT=1

# Optional center map terrain capture (exclude sidebars)
MAP_VIEWPORT_X=0
MAP_VIEWPORT_Y=0
MAP_VIEWPORT_WIDTH=1
MAP_VIEWPORT_HEIGHT=1

# =============================================================================
# DEBUG SETTINGS
# =============================================================================

DEBUG_MODE=false                 # Enable to save debug images and logs
SHOW_CAPTURE_AREA=false          # Show red rectangle around capture area
```

See [MAP.md](MAP.md) for companion map usage and Metaforge attribution.

### Wayland capture rate

On Wayland, ARLO reads the screen through one screen-share stream that runs the whole time ARLO is open. `WAYLAND_CAPTURE_FPS` is the most frames per second the compositor is asked to send. The frame ARLO scans is at most `1 / WAYLAND_CAPTURE_FPS` seconds old, so 10 means at most 0.1 s.

- Lower it (5, or 2) if you see a frame-time cost in game while ARLO is running.
- Raise it if tooltips or map pins feel late. Values below 1 are clamped to 1; there is no upper limit (the compositor will not deliver more than its refresh rate).

Once a minute the terminal prints a line such as `ScreenCast stats: 9.8 new frames/s (10.0 samples/s with keepalive), longest gap 105ms, max frame age at grab 98ms over 180 grabs`. A longest gap well above 500 ms, or `forcing reconnect` lines, means the stream stalled.

To measure what your compositor delivers at different caps, close ARLO and run `python -m arc_helper.screen.streamprobe` (about four minutes). It reports the delivered rate, longest gap and ARLO's own CPU use for each cap.

### Debug Mode

Enable debug mode to troubleshoot OCR issues:

1. Set `DEBUG_MODE=true` in `.env`
2. Run the application
3. Check the `debug/` folder for:
   - `trigger_raw.png` / `trigger_processed.png` - Trigger region captures
   - `tooltip_capture_raw.png` / `tooltip_capture_processed.png` - Tooltip captures
   - `tooltip_mask.png`, `tooltip_cropped.png`, etc. - OCR preprocessing steps
   - `arc_helper.log` - Detailed log file with all operations
