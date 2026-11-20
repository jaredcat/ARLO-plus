# Changelog

All notable changes to this project will be documented in this file.

## Versions

### [1.0.4]

- Abort database updates if any of the sources are down
- Show update status in ARLO's status pill
- Wayland: the screen-share stream now runs the whole time and is never paused; pausing it between scans could leave it delivering no frames at all after a resume, which showed up as stale inventory/map reads and repeated reconnects
- Wayland: add `WAYLAND_CAPTURE_FPS` (default 10) — the compositor is asked to send at most this many frames per second, which bounds the capture cost during gameplay
- Wayland: one dead-stream rule (no frame for 1.5s rebuilds the share, with back-off) replaces the frame-reuse tiers and pipeline nudges; a stats line is logged once a minute
- A screen grab that returns no frame now skips that scan instead of being read as "inventory closed"
- In-game map pins hide sooner after the map closes (the open map is re-checked every 0.25s instead of every `IDLE_SCAN_INTERVAL`)
- Add `python -m arc_helper.screen.streamprobe` to measure what the compositor delivers at different capture rates

### [1.0.3]

- Add icon

### [1.0.2]

- Wayland: auto-reconnect ScreenCast after the portal session drops (no permanent Error latch)
- Wayland: cut gameplay frame-time cost — on-demand frame pulls, leaky pre-convert queue, pause capture while idle
- Wayland: cap ScreenCast frame reuse at 1s; wait ~keepalive for samples; on real stalls nudge then force portal reconnect (session can look open while PipeWire is dead)
- Add `IDLE_SCAN_INTERVAL` (default 1s) so inventory/map probes are slower during active play
- Add `DISABLE_PAUSED_PROJECTS` (default on) to omit PAUSED catalog projects (e.g. Expedition) from keep reasons; when off, paused projects sort last
- Refactor oversized `update_db.py` catalog helpers

### [1.0.1]

- Fix SonarQube findings (complexity/smells) and wire coverage + Python version into the scan

### [1.0.0]

- Migrate UI from Tk to Qt (PySide6): tooltip, status pill, in-game map pins, F8 map, calibrate
- Linux Wayland overlays via XWayland (`xcb` + override-redirect); in-game pins enabled again
- Map overlay feature to help find things in-game

### [0.4.0]

Stable cut of this fork ([jaredcat/arlo-plus](https://github.com/jaredcat/arlo-plus)), following Soygen’s `v0.3.0` line.

- Hybrid catalog sync: MetaForge for items/quests/prices; wiki for workshop/projects/traders
- Player progress import from MetaForge (bookmarklet → `account-sync.json`) filters finished keep reasons
- Show sell price on the overlay only when the in-raid inventory trigger matches
- Linux X11 / Wayland capture support
- Absorb Soygen and Pabosik fork ancestry
- GitHub Releases: `main` = stable, `dev` = beta pre-releases

### [0.1.1]

- Performance improvements
- Code cleanup

### [0.1.0]

- Initial version of the project
- Base OCR tool
- Calibration tool
- Resolution profiles
- Documentation
- Items database + management (via calibration tool)
