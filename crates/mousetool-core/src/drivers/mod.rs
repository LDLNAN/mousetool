//! Native HID mouse protocol drivers behind `HidTransport`, with strict Tier 0/1/2 dispatch guards.

pub mod egg8k;
pub mod razer;

use crate::hid::{DetectedMouse, HidTransport};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LodState {
    Preset(String),
    Index {
        index: u8,
        label: String,
        options: Vec<String>,
    },
    Async {
        lift: u8,
        land: u8,
    },
}

#[derive(Debug, Clone, Default)]
pub struct MouseStatus {
    pub firmware: Option<String>,
    pub serial: Option<String>,
    pub dpi: Option<(u32, u32)>,
    pub dpi_min: u32,
    pub dpi_max: u32,
    pub dpi_step: u32,
    pub max_stages: usize,
    pub active_stage: Option<usize>,
    pub stages: Vec<(u32, u32)>,
    pub polling_hz: Option<u32>,
    pub supported_poll_rates: Vec<u32>,
    pub lod: Option<LodState>,
    pub battery_percent: Option<u8>,
    pub charging: Option<bool>,
    pub idle_sec: Option<u16>,
    pub low_battery_pct: Option<u8>,
    pub motion_sync: Option<bool>,
    pub angle_snapping: Option<bool>,
    pub ripple_control: Option<bool>,
    pub slamclick: Option<bool>,
    pub motion_jitter: Option<bool>,
    pub glass_mode: Option<bool>,
    pub left_handed: Option<bool>,
    pub spdt: Option<String>,
    pub angle_tuning: Option<i8>,
    pub force_max_fps: Option<bool>,
    pub led_liftoff_disabled: Option<bool>,
}

#[derive(Debug, Clone, Default)]
pub struct MouseUpdates {
    pub dpi: Option<(u32, u32)>,
    pub stages: Option<Vec<(u32, u32)>>,
    pub active_stage: Option<usize>,
    pub polling_hz: Option<u32>,
    pub lod_index: Option<u8>,
    pub lod_preset: Option<String>,
    pub lod_async: Option<(u8, u8)>,
    pub motion_sync: Option<bool>,
    pub angle_snapping: Option<bool>,
    pub ripple_control: Option<bool>,
    pub slamclick: Option<bool>,
    pub motion_jitter: Option<bool>,
    pub glass_mode: Option<bool>,
    pub left_handed: Option<bool>,
    pub spdt: Option<String>,
    pub idle_sec: Option<u16>,
    pub low_battery_pct: Option<u8>,
}

/// Returns true if a native Rust protocol driver is implemented for this device's driver ID.
pub fn has_native_driver(driver: &str) -> bool {
    matches!(driver, "egg8k" | "razer")
}

/// Read live hardware status from a detected mouse over HID (enforcing Tier 0/1/2 rules).
#[cfg(feature = "hid")]
pub fn read_mouse_status(mouse: &DetectedMouse) -> Result<MouseStatus, String> {
    if mouse.classified.tier == 0 {
        return Err(format!(
            "{} is Tier 0 · Pointer only (zero vendor HID commands are sent).",
            mouse.display_name()
        ));
    }
    if !has_native_driver(&mouse.classified.driver) {
        return Err(format!(
            "Native Rust driver for `{}` is queued next; use the Web UI or CLI for now.",
            mouse.classified.driver
        ));
    }
    let mut transport = crate::hid::HidApiTransport::open_path(&mouse.path)?;
    read_mouse_status_with_transport(&mut transport, mouse)
}

#[cfg(not(feature = "hid"))]
pub fn read_mouse_status(_mouse: &DetectedMouse) -> Result<MouseStatus, String> {
    Err("HID support is disabled in this build.".into())
}

pub fn read_mouse_status_with_transport<T: HidTransport>(
    transport: &mut T,
    mouse: &DetectedMouse,
) -> Result<MouseStatus, String> {
    if mouse.classified.tier == 0 {
        return Err(format!(
            "Blocked at dispatch: {} is Tier 0 (zero vendor reports).",
            mouse.display_name()
        ));
    }
    match mouse.classified.driver.as_str() {
        "egg8k" => egg8k::read_status(transport, &mouse.classified),
        "razer" => razer::read_status(transport, &mouse.classified),
        other => Err(format!("Unsupported native driver `{other}`")),
    }
}

/// Apply validated updates to a Tier 2 mouse over HID and return refreshed status.
#[cfg(feature = "hid")]
pub fn apply_mouse_updates(
    mouse: &DetectedMouse,
    updates: &MouseUpdates,
) -> Result<MouseStatus, String> {
    if mouse.classified.tier < 2 || !mouse.classified.write_enabled {
        return Err(format!(
            "Blocked at dispatch: {} ({}) is read-only.",
            mouse.display_name(),
            mouse.classified.tier_label()
        ));
    }
    if !has_native_driver(&mouse.classified.driver) {
        return Err(format!(
            "Native Rust driver for `{}` is not implemented yet.",
            mouse.classified.driver
        ));
    }
    let mut transport = crate::hid::HidApiTransport::open_path(&mouse.path)?;
    apply_mouse_updates_with_transport(&mut transport, mouse, updates)
}

#[cfg(not(feature = "hid"))]
pub fn apply_mouse_updates(
    _mouse: &DetectedMouse,
    _updates: &MouseUpdates,
) -> Result<MouseStatus, String> {
    Err("HID support is disabled in this build.".into())
}

pub fn apply_mouse_updates_with_transport<T: HidTransport>(
    transport: &mut T,
    mouse: &DetectedMouse,
    updates: &MouseUpdates,
) -> Result<MouseStatus, String> {
    if mouse.classified.tier < 2 || !mouse.classified.write_enabled {
        return Err(format!(
            "Blocked at dispatch: {} ({}) does not allow writes.",
            mouse.display_name(),
            mouse.classified.tier_label()
        ));
    }
    match mouse.classified.driver.as_str() {
        "egg8k" => egg8k::apply_updates(transport, &mouse.classified, updates),
        "razer" => razer::apply_updates(transport, &mouse.classified, updates),
        other => Err(format!("Unsupported native driver `{other}`")),
    }
}
