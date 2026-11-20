# Changelog

Notable user-visible changes to Mouse Tool. Updated with every merge to local `dev`; dates are merge dates (2026). Ticket IDs refer to the Tephra ledger.

## Unreleased

### Changed
- **Mouse outline and device text**: a redrawn mouse wireframe shape and a cleaner device-name font that fits at narrow widths (MT-008/010, 11-21, verified 93.3/91.4, same-family reviewer).

### Added
- **Pointer settings on more desktops**: Plasma, Hyprland, COSMIC and GNOME backends alongside niri, with plain-language messages when a tool is missing or the desktop isn't supported (MT-005, 11-20, verified 87.9/100). Negative speeds on Plasma and niri detection when the helper starts early are fixed.
- **Live speed and swipe indicator** on the acceleration curve graph, plotting where your current mouse speed sits on the curve and showing swipes (MT-001, 11-20, verified 86.9/100). The reconnect and accelerated-sample issues found in review are fixed.

## Before 2026-11-19

- Installer, systemd service, noctalia widget and tests (11-18); branding generalised from Viper to Mouse Tool (11-13); initial release for the Razer Viper V2 Pro (11-13).
