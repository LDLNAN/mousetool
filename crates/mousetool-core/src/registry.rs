//! Shared device registry loaded from `devices/devices.json` (single source of truth).

use serde::Deserialize;
use std::collections::HashMap;

pub const EMBEDDED_REGISTRY_JSON: &str = include_str!("../../../devices/devices.json");

#[derive(Debug, Clone, Deserialize)]
pub struct RegistryDoc {
    pub schema_version: u32,
    pub models: Vec<ModelEntry>,
    pub profiles: HashMap<String, ProfileEntry>,
    pub matches: Vec<MatchEntry>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelEntry {
    pub key: String,
    pub name: String,
    pub brand: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    pub kind: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct OperationCaps {
    #[serde(default)]
    pub read: bool,
    #[serde(default)]
    pub write: bool,
    #[serde(default)]
    pub dpi_max: Option<u32>,
    #[serde(default)]
    pub txn: Option<String>,
    #[serde(default)]
    pub storage: Option<String>,
    #[serde(default)]
    pub cmd_class: Option<String>,
    #[serde(default)]
    pub cmd_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ProfileEntry {
    pub driver: String,
    pub proto_label: String,
    pub proto_tag: String,
    pub tier: u8,
    pub write_enabled: bool,
    #[serde(default)]
    pub status_note: Option<String>,
    #[serde(default)]
    pub operations: HashMap<String, OperationCaps>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MatchEntry {
    pub id: String,
    pub model_key: String,
    pub profile_id: String,
    pub bus: String,
    pub id_namespace: String,
    pub vid: String,
    pub pid: Option<String>,
    pub wpid: Option<String>,
    #[serde(default)]
    pub link: Option<String>,
    #[serde(default)]
    pub prio: i32,
    #[serde(default)]
    pub wireless: bool,
    #[serde(default)]
    pub hyper: bool,
    #[serde(default)]
    pub meta: serde_json::Value,
}

#[derive(Debug, Clone)]
pub struct ClassifiedDevice {
    pub model_key: Option<String>,
    pub name: String,
    pub brand: String,
    pub driver: String,
    pub proto_label: String,
    pub proto_tag: String,
    pub tier: u8,
    pub write_enabled: bool,
    pub status_note: String,
    pub link: String,
    pub wireless: bool,
    pub hyper: bool,
    pub dpi_max: u32,
    pub operations: HashMap<String, OperationCaps>,
    pub meta: serde_json::Value,
}

pub fn parse_hex_u8(val: Option<&str>, default: u8) -> u8 {
    val.and_then(|s| {
        let trimmed = s.trim();
        let hex = trimmed
            .strip_prefix("0x")
            .or_else(|| trimmed.strip_prefix("0X"))
            .unwrap_or(trimmed);
        u8::from_str_radix(hex, 16).ok()
    })
    .unwrap_or(default)
}

impl ClassifiedDevice {
    pub fn tier_label(&self) -> &'static str {
        match self.tier {
            2 => "Tier 2 · Verified",
            1 => "Tier 1 · Read-only",
            _ => "Tier 0 · Pointer only",
        }
    }

    pub fn can_read(&self, op: &str) -> bool {
        self.operations.get(op).is_some_and(|c| c.read)
    }

    pub fn can_write(&self, op: &str) -> bool {
        self.tier >= 2
            && self.write_enabled
            && self.operations.get(op).is_some_and(|c| c.write)
    }

    pub fn op_txn(&self, op: &str, default: u8) -> u8 {
        parse_hex_u8(self.operations.get(op).and_then(|c| c.txn.as_deref()), default)
    }

    pub fn op_storage(&self, op: &str, default: u8) -> u8 {
        parse_hex_u8(
            self.operations.get(op).and_then(|c| c.storage.as_deref()),
            default,
        )
    }

    /// Enforce Tier 0/1 allowlist for Razer 90-byte feature commands.
    pub fn is_razer_cmd_allowed(&self, cls: u8, cmd: u8, txn: u8) -> bool {
        if self.tier == 0 {
            return false;
        }
        if self.tier >= 2 && self.write_enabled {
            return true;
        }
        // Tier 1 read-only: only commands explicitly allowlisted in operations with read=true & write=false.
        self.operations.values().any(|spec| {
            if !spec.read || spec.write {
                return false;
            }
            let Some(c_str) = spec.cmd_class.as_deref() else {
                return false;
            };
            let Some(i_str) = spec.cmd_id.as_deref() else {
                return false;
            };
            let allowed_cls = parse_hex_u8(Some(c_str), 0xFF);
            let allowed_cmd = parse_hex_u8(Some(i_str), 0xFF);
            let allowed_txn = parse_hex_u8(spec.txn.as_deref(), txn);
            cls == allowed_cls && cmd == allowed_cmd && txn == allowed_txn
        })
    }
}

pub struct DeviceRegistry {
    doc: RegistryDoc,
    models: HashMap<String, usize>,
    by_usb: HashMap<(u16, u16), usize>,
    by_bt: HashMap<(u16, u16), usize>,
    by_wpid: HashMap<(u16, u16), usize>,
}

impl DeviceRegistry {
    pub fn load_embedded() -> Result<Self, serde_json::Error> {
        Self::from_json(EMBEDDED_REGISTRY_JSON)
    }

    pub fn from_json(json: &str) -> Result<Self, serde_json::Error> {
        let doc: RegistryDoc = serde_json::from_str(json)?;
        let mut models = HashMap::with_capacity(doc.models.len());
        for (i, m) in doc.models.iter().enumerate() {
            models.insert(m.key.clone(), i);
        }
        let mut by_usb = HashMap::new();
        let mut by_bt = HashMap::new();
        let mut by_wpid = HashMap::new();

        for (i, m) in doc.matches.iter().enumerate() {
            let Ok(vid) = u16::from_str_radix(&m.vid, 16) else {
                continue;
            };
            match m.id_namespace.as_str() {
                "usb_pid" => {
                    if let Some(pid) = m.pid.as_deref().and_then(|s| u16::from_str_radix(s, 16).ok())
                    {
                        by_usb.insert((vid, pid), i);
                    }
                }
                "bluetooth_pid" => {
                    if let Some(pid) = m.pid.as_deref().and_then(|s| u16::from_str_radix(s, 16).ok())
                    {
                        by_bt.insert((vid, pid), i);
                    }
                }
                "logitech_wpid" => {
                    if let Some(wpid) =
                        m.wpid.as_deref().and_then(|s| u16::from_str_radix(s, 16).ok())
                    {
                        by_wpid.insert((vid, wpid), i);
                    }
                }
                _ => {}
            }
        }

        Ok(Self {
            doc,
            models,
            by_usb,
            by_bt,
            by_wpid,
        })
    }

    pub fn model_count(&self) -> usize {
        self.doc.models.len()
    }

    pub fn profile_count(&self) -> usize {
        self.doc.profiles.len()
    }

    pub fn match_count(&self) -> usize {
        self.doc.matches.len()
    }

    pub fn wpid_match_count(&self) -> usize {
        self.by_wpid.len()
    }

    pub fn classify_usb(&self, vid: u16, pid: u16, fallback_name: Option<&str>) -> ClassifiedDevice {
        if let Some(&idx) = self.by_usb.get(&(vid, pid)) {
            if let Some(dev) = self.build_classified(idx) {
                return dev;
            }
        }
        self.generic_tier0(vid, pid, fallback_name, "USB")
    }

    pub fn classify_bluetooth(
        &self,
        vid: u16,
        pid: u16,
        fallback_name: Option<&str>,
    ) -> ClassifiedDevice {
        if let Some(&idx) = self.by_bt.get(&(vid, pid)) {
            if let Some(dev) = self.build_classified(idx) {
                return dev;
            }
        }
        self.generic_tier0(vid, pid, fallback_name, "Bluetooth")
    }

    /// Look up by USB PID first, then Bluetooth PID (never cross-matches Logitech WPIDs).
    pub fn classify_any(&self, vid: u16, pid: u16, fallback_name: Option<&str>) -> ClassifiedDevice {
        if let Some(&idx) = self.by_usb.get(&(vid, pid)).or_else(|| self.by_bt.get(&(vid, pid))) {
            if let Some(dev) = self.build_classified(idx) {
                return dev;
            }
        }
        self.generic_tier0(vid, pid, fallback_name, "HID")
    }

    fn build_classified(&self, match_idx: usize) -> Option<ClassifiedDevice> {
        let m = &self.doc.matches[match_idx];
        let model = &self.doc.models[*self.models.get(&m.model_key)?];
        let prof = self.doc.profiles.get(&m.profile_id)?;
        let dpi_max = m
            .meta
            .get("dpi_max")
            .and_then(|v| v.as_u64())
            .or_else(|| prof.operations.get("dpi").and_then(|d| d.dpi_max).map(u64::from))
            .unwrap_or(30_000) as u32;

        Some(ClassifiedDevice {
            model_key: Some(model.key.clone()),
            name: model.name.clone(),
            brand: model.brand.clone(),
            driver: prof.driver.clone(),
            proto_label: prof.proto_label.clone(),
            proto_tag: prof.proto_tag.clone(),
            tier: prof.tier,
            write_enabled: prof.write_enabled,
            status_note: prof.status_note.clone().unwrap_or_default(),
            link: m.link.clone().unwrap_or_default(),
            wireless: m.wireless,
            hyper: m.hyper,
            dpi_max,
            operations: prof.operations.clone(),
            meta: m.meta.clone(),
        })
    }

    fn generic_tier0(
        &self,
        vid: u16,
        pid: u16,
        fallback_name: Option<&str>,
        link: &str,
    ) -> ClassifiedDevice {
        let name = fallback_name
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| format!("Generic HID Mouse ({vid:04x}:{pid:04x})"));
        let status_note = format!(
            "Tier 0 · Pointer only ({vid:04x}:{pid:04x}): no reviewed protocol profile matches this device. Zero vendor HID reports are sent."
        );
        ClassifiedDevice {
            model_key: None,
            name,
            brand: "generic".into(),
            driver: "generic".into(),
            proto_label: "Generic USB / HID Mouse".into(),
            proto_tag: "HID".into(),
            tier: 0,
            write_enabled: false,
            status_note,
            link: link.into(),
            wireless: link == "Bluetooth",
            hyper: false,
            dpi_max: 30_000,
            operations: HashMap::new(),
            meta: serde_json::Value::Null,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_registry_loads_and_classifies_tiers() {
        let reg = DeviceRegistry::load_embedded().expect("embedded devices.json should parse");
        assert_eq!(reg.doc.schema_version, 1);
        assert!(reg.model_count() >= 80);
        assert!(reg.profile_count() >= 15);
        assert!(reg.match_count() >= 120);

        // Tier 2 Razer Viper V2 Pro
        let viper = reg.classify_usb(0x1532, 0x00a5, None);
        assert_eq!(viper.tier, 2);
        assert_eq!(viper.driver, "razer");
        assert!(viper.can_read("dpi"));
        assert!(viper.can_write("dpi"));

        // Tier 1 read-only Razer Viper Mini — writes MUST be blocked
        let mini = reg.classify_usb(0x1532, 0x008a, None);
        assert_eq!(mini.tier, 1);
        assert_eq!(mini.driver, "razer");
        assert!(!mini.write_enabled);
        assert!(mini.can_read("dpi"));
        assert!(!mini.can_write("dpi"));

        // Tier 2 Endgame Gear OP1 8k
        let op1 = reg.classify_usb(0x3367, 0x1964, None);
        assert_eq!(op1.tier, 2);
        assert_eq!(op1.driver, "egg8k");

        // Unknown Razer VID must fall back to Tier 0 (never silent razer fallback)
        let unknown_razer = reg.classify_usb(0x1532, 0x9999, Some("Mystery Razer"));
        assert_eq!(unknown_razer.tier, 0);
        assert_eq!(unknown_razer.driver, "generic");
        assert!(!unknown_razer.can_write("dpi"));
    }
}
