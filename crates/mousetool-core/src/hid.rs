//! Cross-platform HID transport abstraction, live `hidapi` implementation, and mouse discovery.

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

#[cfg(feature = "hid")]
pub struct HidApiTransport {
    device: hidapi::HidDevice,
}

#[cfg(feature = "hid")]
impl HidApiTransport {
    pub fn open_path(path: &str) -> Result<Self, String> {
        let api = hidapi::HidApi::new().map_err(|e| format!("HID initialization failed: {e}"))?;
        let cpath = std::ffi::CString::new(path)
            .map_err(|_| format!("invalid HID device path: {path}"))?;
        let device = api
            .open_path(&cpath)
            .map_err(|e| format!("failed to open HID device {path}: {e}"))?;
        Ok(Self { device })
    }
}

#[cfg(feature = "hid")]
impl HidTransport for HidApiTransport {
    fn send_feature_report(&mut self, data: &[u8]) -> Result<(), String> {
        self.device
            .send_feature_report(data)
            .map_err(|e| format!("send_feature_report failed: {e}"))
    }

    fn get_feature_report(&mut self, buf: &mut [u8]) -> Result<usize, String> {
        self.device
            .get_feature_report(buf)
            .map_err(|e| format!("get_feature_report failed: {e}"))
    }

    fn write(&mut self, data: &[u8]) -> Result<usize, String> {
        self.device
            .write(data)
            .map_err(|e| format!("HID write failed: {e}"))
    }

    fn read_timeout(&mut self, buf: &mut [u8], timeout_ms: i32) -> Result<usize, String> {
        self.device
            .read_timeout(buf, timeout_ms)
            .map_err(|e| format!("HID read failed: {e}"))
    }
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
/// and picks the protocol-specific control interface (matching `mousetool/hid_discovery.py`).
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

        if classified.tier == 0 && !is_generic_mouse {
            continue;
        }

        let path = info.path().to_string_lossy().into_owned();
        let dedupe_key = if !serial.is_empty() {
            format!("{vid:04x}:{pid:04x}:{serial}")
        } else {
            format!("{vid:04x}:{pid:04x}")
        };

        let score = interface_score(
            &classified.driver,
            classified.tier,
            &path,
            usage_page,
            usage,
            info.interface_number(),
            is_vendor_collection,
            is_generic_mouse,
        );

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

#[cfg(feature = "hid")]
#[allow(clippy::too_many_arguments)]
fn interface_score(
    driver: &str,
    tier: u8,
    path: &str,
    usage_page: u16,
    usage: u16,
    interface_number: i32,
    is_vendor_collection: bool,
    is_generic_mouse: bool,
) -> u8 {
    if tier > 0 {
        if let Some(rd) = read_linux_report_descriptor(path) {
            if driver == "egg8k"
                && contains_bytes(&rd, &[0x06, 0x01, 0xFF])
                && contains_bytes(&rd, &[0x85, 0xA0])
                && contains_bytes(&rd, &[0x85, 0xA1])
            {
                return 6;
            }
            if driver == "razer" && contains_bytes(&rd, &[0x95, 0x5A, 0xB1]) {
                return 6;
            }
        }
        if driver == "egg8k" && usage_page == 0xFF01 {
            return 5;
        }
        if driver == "razer" && interface_number == 0 && usage_page == 0x0001 && usage == 0x0002 {
            return 5;
        }
        if is_vendor_collection {
            return 4;
        }
        if interface_number > 0 {
            return 2;
        }
    }
    if is_generic_mouse { 1 } else { 0 }
}

#[cfg(all(feature = "hid", target_os = "linux"))]
fn read_linux_report_descriptor(path: &str) -> Option<Vec<u8>> {
    let dev_name = path.strip_prefix("/dev/")?;
    if !dev_name.starts_with("hidraw") {
        return None;
    }
    std::fs::read(format!("/sys/class/hidraw/{dev_name}/device/report_descriptor")).ok()
}

#[cfg(all(feature = "hid", not(target_os = "linux")))]
fn read_linux_report_descriptor(_path: &str) -> Option<Vec<u8>> {
    None
}

#[cfg(feature = "hid")]
fn contains_bytes(haystack: &[u8], needle: &[u8]) -> bool {
    !needle.is_empty() && haystack.windows(needle.len()).any(|w| w == needle)
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
