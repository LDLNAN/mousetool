# Changelog

Notable user-visible changes to Mouse Tool. Updated with every merge to local `dev`; dates are merge dates (2026). Ticket IDs refer to the Tephra ledger.

## Unreleased

### Added
- **Battery on any desktop**: a background watcher reads each wireless mouse's battery once, shares it with the page and the command line, and sends a desktop notification at 20% and again at 10% (re-armed after charging). `mousetool battery --waybar` prints one line of JSON for Waybar-style bars. Notifications can be turned off from the Power settings (MT-009, 11-21, verified 85.7/100, cross-provider reviewer).
- **Raw Accel export (Windows step 1)**: an offline preview and export of your acceleration curve in Raw Accel's format, with an equivalence check that says plainly when the curve cannot be verified (MT-003, 11-21, verified 88.3/100, same-family reviewer).
- **Pointer settings on more desktops**: Plasma, Hyprland, COSMIC and GNOME backends alongside niri, with plain-language messages when a tool is missing or the desktop isn't supported (MT-005, 11-20, verified 87.9/100). Negative speeds on Plasma and niri detection when the helper starts early are fixed.
- **Live speed and swipe indicator** on the acceleration curve graph, plotting where your current mouse speed sits on the curve and showing swipes (MT-001, 11-20, verified 86.9/100). The reconnect and accelerated-sample issues found in review are fixed.

### Changed
- **mouseaccel support**: mousetool now uses the `mouseaccel` kernel module when it is loaded and falls back to `maccel` otherwise, with the module in use shown on the Acceleration tab. Saving a stale draft after the backend changes is refused instead of overwriting the other module, the privileged helper no longer follows symlinks, and unreadable parameters now say so (MA-002, 11-21, verified 89.9/100, cross-provider reviewer). Install the new udev rule with `./install.sh --mouseaccel-perms`; nothing is loaded or swapped for you.
- **README screenshots**: six full-size screenshots of the sensitivity, acceleration, desktop pointer and tuning views, a script that regenerates them, and corrected hardware and command-line notes (MT-004, 11-21, verified 90.2/100, cross-provider reviewer).
- **Mouse outline and device text**: a redrawn mouse wireframe shape and a cleaner device-name font that fits at narrow widths (MT-008/010, 11-21, verified 93.3/91.4, same-family reviewer).


## Before 2026-11-19

- Installer, systemd service, noctalia widget and tests (11-18); branding generalised from Viper to Mouse Tool (11-13); initial release for the Razer Viper V2 Pro (11-13).
