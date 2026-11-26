# Native Mouse Tool Architecture & Modular Installer Plan

## Goals

1. **Incredibly lightweight & fast** — tiny binary footprint, instant launch, minimal RAM when open (~15–30 MiB), and ~2 MiB idle RAM if the optional background helper is installed. Zero Electron/browser runtime bundled.
2. **Visual parity with the web UI** — match [web/styles.css](../web/styles.css) 1:1 (warm light `#f6f5f2` / dark `#141413` surfaces, `#c84b20` / `#e05d30` accent, `4px` radius, `960px` centered layout, hairline borders, tabular numerals, interactive DPI track and 64-point acceleration curve graph) while maintaining both the Web UI and Native App.
3. **Zero-install Portable Mode** — ship a single `.exe` (Windows) and `.AppImage` (Linux) that runs completely in-process with zero installation, zero registry/systemd writes, and zero leftover files when deleted.
4. **Modular, painless installation** — users choose exactly what they want:
   - **Core Helper (`mousetool-agent`)** — tiny always-on per-user daemon for logon curve restore, background battery notifications/tray, desktop pointer control, and Web UI pairing.
   - **Desktop App (`mousetool-app`)** — optional native GUI that talks directly to the helper over local IPC (no web server required), or runs in-process when no helper is running.
   - **Web UI option** — uncheck Desktop App to open the hosted HTTPS web UI (paired with the helper over loopback) or serve embedded static assets offline.
   - **Kernel Acceleration Driver (optional)** — `Raw Accel` (Windows) / `mouseaccel` (Linux), clearly gated as the only step requiring admin privileges.

---

## Workspace Layout

```text
Cargo.toml                    # Unified Rust workspace (release: opt-level="z", LTO, strip)
├── crates/mousetool-core/    # Shared zero-GUI library:
│   ├── registry.rs           #   Compiles devices/devices.json (single source of truth)
│   ├── hid.rs                #   HidTransport trait + native hidapi discovery (Linux hidraw / Windows native)
│   ├── accel.rs              #   Exact curve math (linear/natural/synchronous/lookup/no_accel) + OS backends
│   └── lib.rs                #   RuntimeMode (PortableInProcess vs AgentIpc)
├── agent/                    # Tiny always-on helper daemon (mousetool-agent, ~1.8 MB static)
│   └── ...                   #   Loopback HTTP v1 for Web UI + future local IPC for native app
└── crates/mousetool-app/     # True native GUI (iced 0.14 + tiny-skia software renderer, no wgpu bloat)
    ├── theme.rs              #   1:1 light & dark design tokens from web/styles.css
    ├── curve_canvas.rs       #   Custom iced Canvas for the acceleration curve & LUT control points
    └── main.rs               #   App shell (top bar, fleet switcher, hero tiles, 6 tabs, portable mode)
```

### Why `iced` with `tiny-skia` (without `wgpu` by default)?

- **No GPU driver/shader startup lag or 15+ MB Vulkan/DX12 bloat** — `tiny-skia` renders flat cards, hairline borders, text, and anti-aliased Bezier/polyline curves smoothly on CPU on both Wayland/X11 and Windows.
- **Exact custom styling** — `iced` lets every container, button, tab, pill, slider, and canvas use our exact CSS token palette without fighting OS widget theme limitations.
- **Zero WebKitGTK / WebView2 issues** — compiles to a genuinely self-contained native binary suitable for single-file portable `.exe` and `.AppImage` builds.

---

## Phased Rollout

### Phase 1 — Foundation & Styled Shell *(this milestone)*
- Unified Cargo workspace (`mousetool-core`, `mousetool-agent`, `mousetool-app`).
- `mousetool-core`: embedded `devices/devices.json` parser & Tier 0/1/2 classifier, live native `hidapi` mouse discovery, exact 5-mode acceleration curve evaluator + LUT presets, and read-only Linux `mouseaccel`/`maccel` sysfs detection.
- `mousetool-app`: styled `iced` shell matching `web/styles.css` (light + dark themes), live fleet discovery, Device/Tier inspection, and interactive Acceleration curve preview canvas with `--portable` mode.

### Phase 2 — Native HID Drivers & Live Accel Writes
- Port protocol drivers into `mousetool-core::drivers` behind the testable `HidTransport` trait, starting with Razer 90-byte feature reports, Endgame Gear 8k, and CompX/Logitech/Pulsar/Glorious with fixture-verified parity against `mousetool/drivers/*.py` and `web/drivers/*.js`.
- Add validated accel writes + persistence (`mouseaccel` sysfs on Linux, Raw Accel `writer.exe` / `read-rawaccel.ps1` adapter on Windows) and interactive draggable DPI track & LUT control points in `mousetool-app`.

### Phase 3 — Local IPC & Modular Installers / Portable Releases
- Add local IPC transport (Unix domain socket on Linux, Named Pipe on Windows) to `mousetool-agent` so installed `mousetool-app` talks directly to the helper without a web server.
- Add system tray / background battery notifications to `mousetool-agent`.
- Package:
  - **Portable:** `mousetool-portable-x86_64.exe` (Windows) and `mousetool-x86_64.AppImage` (Linux).
  - **Modular Installer:** Windows installer (Inno Setup / WiX + `winget`) and Linux installer (`install.sh` / AUR / `.deb` / `.rpm`) with opt-in toggles for Helper, Desktop App, Web UI shortcut, and Accel Driver.
