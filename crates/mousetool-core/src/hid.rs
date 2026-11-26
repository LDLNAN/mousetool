//! Cross-platform HID transport abstraction and live mouse discovery.

use crate::registry::{ClassifiedDevice, DeviceRegistry};
use std::collections::BTreeMap;

/// Low-level HID report transport so mouse protocol drivers can be unit-tested
/// against in-memory mocks on any OS.
pub trait HidTransport {
    fn send_feature_report(&mut self, data: &[u8]) -> Result<(), String>;
    fn get_feature_report(&mut self, buf: &mut [u8]) -> Result<usize, String>;
    fn write(&mut self, data: &[u8]) -> Result<usize, String>;
    fn read_timeout(&mut self, buf: &mut [u8], timeout_ms: i32) -> Result<usize, String>;
}

#[derive(Debug, Clone)]
pub struct DetectedMouse {
    pub id: String,
    pub path: String,
    pub vid: u16,
    pub pid: u16,
    pub manufacturer: String,
    pub product: String,
    pub serial: String,
    pub usage_page: u16,
    pub usage: u16,
    pub interface_number: i32,
    pub classified: ClassifiedDevice,
    pub permission_hint: Option<String>,
}

impl DetectedMouse {
    pub fn vid_pid(&self) -> String {
        format!("{:04x}:{:04x}", self.vid, self.pid)
    }

    pub fn display_name(&self) -> &str {
        if self.classified.tier > 0 {
            &self.classified.name
        } else if !self.product.is_empty() {
            &self.product
        } else {
            &self.classified.name
        }
    }
}

/// Enumerate connected HID mice and classify each via `devices/devices.json`.
///
/// Deduplicates multi-interface USB mice (e.g. pointer + vendor control interface)
/// and prefers the vendor-specific control collection when one is present.
#[cfg(feature = "hid")]
pub fn discover_mice(registry: &DeviceRegistry) -> Result<Vec<DetectedMouse>, String> {
    let api = hidapi::HidApi::new().map_err(|e| format!("HID initialization failed: {e}"))?;
    let mut best_by_key: BTreeMap<String, (u8, DetectedMouse)> = BTreeMap::new();

    for info in api.device_list() {
        let vid = info.vendor_id();
        let pid = info.product_id();
        let usage_page = info.usage_page();
        let usage = info.usage();
        let product = info.product_string().unwrap_or("").trim().to_string();
        let manufacturer = info.manufacturer_string().unwrap_or("").trim().to_string();
        let serial = info.serial_number().unwrap_or("").trim().to_string();

        let classified = registry.classify_any(vid, pid, Some(&product));
        let is_generic_mouse = usage_page == 0x0001 && usage == 0x0002;
        let is_vendor_collection = usage_page >= 0xff00;

        // Include every registry-matched mouse/receiver, plus generic HID mice (Usage 0x01:0x02).
        if classified.tier == 0 && !is_generic_mouse {
            continue;
        }

        let path = info.path().to_string_lossy().into_owned();
        let dedupe_key = if !serial.is_empty() {
            format!("{vid:04x}:{pid:04x}:{serial}")
        } else {
            format!("{vid:04x}:{pid:04x}")
        };

        // Rank interfaces: prefer vendor control collection for Tier 1/2 devices.
        let score = if classified.tier > 0 && is_vendor_collection {
            3
        } else if classified.tier > 0 && info.interface_number() > 0 {
            2
        } else if is_generic_mouse {
            1
        } else {
            0
        };

        let permission_hint = probe_permission_hint(&path, classified.tier);

        let candidate = DetectedMouse {
            id: dedupe_key.clone(),
            path,
            vid,
            pid,
            manufacturer,
            product,
            serial,
            usage_page,
            usage,
            interface_number: info.interface_number(),
            classified,
            permission_hint,
        };

        match best_by_key.get(&dedupe_key) {
            Some((existing_score, _)) if *existing_score >= score => {}
            _ => {
                best_by_key.insert(dedupe_key, (score, candidate));
            }
        }
    }

    let mut mice: Vec<DetectedMouse> = best_by_key.into_values().map(|(_, m)| m).collect();
    mice.sort_by(|a, b| {
        b.classified
            .tier
            .cmp(&a.classified.tier)
            .then_with(|| a.display_name().cmp(b.display_name()))
    });
    Ok(mice)
}

#[cfg(not(feature = "hid"))]
pub fn discover_mice(_registry: &DeviceRegistry) -> Result<Vec<DetectedMouse>, String> {
    Ok(Vec::new())
}

#[cfg(all(feature = "hid", target_os = "linux"))]
fn probe_permission_hint(path: &str, tier: u8) -> Option<String> {
    if tier == 0 || !path.starts_with("/dev/hidraw") {
        return None;
    }
    match std::fs::OpenOptions::new().read(true).write(true).open(path) {
        Ok(_) => None,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => Some(format!(
            "No write permission on {path}. Run `./install.sh --udev` (or apply udev rules) and reconnect your mouse."
        )),
        Err(_) => None,
    }
}

#[cfg(all(feature = "hid", not(target_os = "linux")))]
fn probe_permission_hint(_path: &str, _tier: u8) -> Option<String> {
    None
}
