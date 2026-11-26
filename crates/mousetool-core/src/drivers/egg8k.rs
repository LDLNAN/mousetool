//! Endgame Gear 8k (`0xA0`/`0xA1` 1041-byte NVRAM) protocol driver.
//! Exact wire parity with `mousetool/drivers/egg.py`.

use crate::drivers::{LodState, MouseStatus, MouseUpdates};
use crate::hid::HidTransport;
use crate::registry::ClassifiedDevice;
use std::thread;
use std::time::Duration;

pub const EGG_CFG_LEN: usize = 1041;
pub const EGG_CMD_LEN: usize = 64;
pub const EGG_POLL_RATES: [u32; 4] = [1000, 2000, 4000, 8000];

#[derive(Debug, Clone)]
pub struct EggProfile {
    pub family: String,
    pub cpi_min: u32,
    pub cpi_max: u32,
    pub cpi_step_lo: u32,
    pub cpi_step_hi: u32,
    pub lod_options: Vec<String>,
    pub lod_glass: bool,
    pub angle_tuning: bool,
    pub force_max_fps: bool,
    pub led_liftoff: bool,
}

impl EggProfile {
    pub fn from_device(dev: &ClassifiedDevice) -> Self {
        let m = &dev.meta;
        let lod_options = m
            .get("lod_options")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|x| x.as_str().map(ToOwned::to_owned))
                    .collect::<Vec<_>>()
            })
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| vec!["0.7 mm".into(), "1 mm".into(), "2 mm".into()]);

        Self {
            family: m
                .get("family")
                .and_then(|v| v.as_str())
                .unwrap_or("v1")
                .to_string(),
            cpi_min: m.get("cpi_min").and_then(|v| v.as_u64()).unwrap_or(50) as u32,
            cpi_max: m
                .get("cpi_max")
                .and_then(|v| v.as_u64())
                .unwrap_or(dev.dpi_max as u64) as u32,
            cpi_step_lo: m.get("cpi_step_lo").and_then(|v| v.as_u64()).unwrap_or(50) as u32,
            cpi_step_hi: m.get("cpi_step_hi").and_then(|v| v.as_u64()).unwrap_or(50) as u32,
            lod_options,
            lod_glass: m.get("lod_glass").and_then(|v| v.as_bool()).unwrap_or(false),
            angle_tuning: m
                .get("angle_tuning")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            force_max_fps: m
                .get("force_max_fps")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            led_liftoff: m
                .get("led_liftoff")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        }
    }

    pub fn clamp_cpi(&self, val: u32) -> u32 {
        let v = val.clamp(self.cpi_min, self.cpi_max);
        let step = if v <= 10_000 {
            self.cpi_step_lo.max(1)
        } else {
            self.cpi_step_hi.max(1)
        };
        let q = ((v + step / 2) / step) * step;
        q.clamp(self.cpi_min, self.cpi_max)
    }
}

pub fn format_version(buf: &[u8]) -> Option<String> {
    for off in [17usize, 16, 18, 2, 4, 6] {
        if off + 1 >= buf.len() {
            continue;
        }
        let patch_b = buf[off];
        let major_b = buf[off + 1];
        let hi_n = patch_b >> 4;
        let lo_n = patch_b & 0x0F;
        if (1..=9).contains(&major_b) && hi_n <= 9 && lo_n <= 9 {
            return Some(format!("v{major_b}.{hi_n}{lo_n}"));
        }
    }
    for off in [17usize, 16, 18, 2, 4, 6] {
        if off + 1 >= buf.len() {
            continue;
        }
        let v = u16::from_le_bytes([buf[off], buf[off + 1]]);
        if (50..=5000).contains(&v) {
            return Some(format!("v{:.2}", v as f64 / 100.0));
        }
    }
    None
}

pub fn blob_ok(buf: &[u8]) -> bool {
    if buf.len() < 131 || buf[1] != 0x01 {
        return false;
    }
    let stages = buf[30];
    let active = buf[29];
    let div = buf[21];
    let cpi1 = u16::from_le_bytes([buf[52], buf[53]]);
    (1..=4).contains(&stages) && active <= 3 && (1..=64).contains(&div) && (10..=30_000).contains(&cpi1)
}

pub fn parse_config(blob: &[u8], prof: &EggProfile, firmware: Option<String>) -> MouseStatus {
    let n = (blob[30] as usize).clamp(1, 4);
    let active0 = (blob[29] as usize).min(n - 1);
    let mut stages = Vec::with_capacity(n);
    for i in 0..n {
        let o = 51 + i * 5;
        let x = u16::from_le_bytes([blob[o + 1], blob[o + 2]]) as u32;
        let y = u16::from_le_bytes([blob[o + 3], blob[o + 4]]) as u32;
        stages.push((x, y));
    }

    let div = if blob[21] == 0 { 1 } else { blob[21] as u32 };
    let polling_hz = if 8000 % div == 0 {
        Some(8000 / div)
    } else {
        None
    };

    let glass = prof.lod_glass && blob[127] != 0;
    let lod_list: Vec<String> = if glass {
        vec!["1.0 mm".into(), "2.0 mm".into()]
    } else {
        prof.lod_options.clone()
    };
    let lod_idx = blob[25];
    let lod_label = lod_list
        .get(lod_idx as usize)
        .cloned()
        .unwrap_or_else(|| format!("index {lod_idx}"));

    let ho = &blob[71..77];
    let left_handed = !(ho[0] == 0 && ho[1] == 1 && ho[2..6].iter().all(|&b| b == 0))
        && ho.iter().any(|&b| b != 0);

    let ctl0 = blob[77];
    let spdt = match ctl0 {
        0xF0 => "gx_safe",
        0xF1 => "gx_speed",
        _ => "off",
    }
    .to_string();

    let angle_raw = blob[128] as i8;

    MouseStatus {
        firmware,
        serial: None,
        dpi: Some(stages[active0]),
        dpi_min: prof.cpi_min,
        dpi_max: prof.cpi_max,
        dpi_step: prof.cpi_step_lo,
        max_stages: 4,
        active_stage: Some(active0 + 1),
        stages,
        polling_hz,
        supported_poll_rates: EGG_POLL_RATES.to_vec(),
        lod: Some(LodState::Index {
            index: lod_idx,
            label: lod_label,
            options: lod_list,
        }),
        battery_percent: None,
        charging: None,
        idle_sec: None,
        low_battery_pct: None,
        motion_sync: Some(blob[28] != 0),
        angle_snapping: Some(blob[26] != 0),
        ripple_control: Some(blob[27] != 0),
        slamclick: Some((blob[22] & 0x01) != 0),
        motion_jitter: Some((blob[22] & 0x10) != 0),
        glass_mode: prof.lod_glass.then_some(glass),
        left_handed: Some(left_handed),
        spdt: Some(spdt),
        angle_tuning: prof.angle_tuning.then_some(angle_raw),
        force_max_fps: prof.force_max_fps.then_some(blob[129] != 0),
        led_liftoff_disabled: prof.led_liftoff.then_some(blob[24] == 0),
    }
}

pub fn serialize_config(
    base: &[u8],
    updates: &MouseUpdates,
    prof: &EggProfile,
) -> Result<Vec<u8>, String> {
    let mut out = vec![0u8; EGG_CFG_LEN];
    let n = base.len().min(EGG_CFG_LEN);
    out[..n].copy_from_slice(&base[..n]);
    out[0] = 0xA0;
    out[1] = 0x11;

    if let Some(hz) = updates.polling_hz {
        if !matches!(hz, 125 | 250 | 500 | 1000 | 2000 | 4000 | 8000) {
            return Err("polling rate must be 1000, 2000, 4000, or 8000 Hz".into());
        }
        out[21] = (8000 / hz) as u8;
    }
    if let Some(v) = updates.slamclick {
        out[22] = if v { out[22] | 0x01 } else { out[22] & !0x01 };
    }
    if let Some(v) = updates.motion_jitter {
        out[22] = if v { out[22] | 0x10 } else { out[22] & !0x10 };
    }
    if let Some(idx) = updates.lod_index {
        out[25] = idx;
    }
    if let Some(v) = updates.angle_snapping {
        out[26] = u8::from(v);
    }
    if let Some(v) = updates.ripple_control {
        out[27] = u8::from(v);
    }
    if let Some(v) = updates.motion_sync {
        out[28] = u8::from(v);
    }
    if let Some(stages) = &updates.stages {
        if !(1..=4).contains(&stages.len()) {
            return Err("Endgame Gear 8k mice support 1 to 4 DPI stages".into());
        }
        out[30] = stages.len() as u8;
        for (i, &(x, y)) in stages.iter().enumerate() {
            let cx = prof.clamp_cpi(x) as u16;
            let cy = prof.clamp_cpi(y) as u16;
            let o = 51 + i * 5;
            out[o] = u8::from(cx != cy);
            out[o + 1..o + 3].copy_from_slice(&cx.to_le_bytes());
            out[o + 3..o + 5].copy_from_slice(&cy.to_le_bytes());
        }
    }
    if let Some(act) = updates.active_stage {
        let cnt = (out[30] as usize).clamp(1, 4);
        if !(1..=cnt).contains(&act) {
            return Err(format!("active stage must be between 1 and {cnt}"));
        }
        out[29] = (act - 1) as u8;
    }
    if let Some((dx, dy)) = updates.dpi {
        let cx = prof.clamp_cpi(dx) as u16;
        let cy = prof.clamp_cpi(dy) as u16;
        let act0 = (out[29] as usize).min(3);
        let o = 51 + act0 * 5;
        out[o] = u8::from(cx != cy);
        out[o + 1..o + 3].copy_from_slice(&cx.to_le_bytes());
        out[o + 3..o + 5].copy_from_slice(&cy.to_le_bytes());
    }
    if prof.lod_glass {
        if let Some(v) = updates.glass_mode {
            out[127] = u8::from(v);
        }
    }
    if let Some(lh) = updates.left_handed {
        let ho = &out[71..77];
        let cur_lh = !(ho[0] == 0 && ho[1] == 1 && ho[2..6].iter().all(|&b| b == 0))
            && ho.iter().any(|&b| b != 0);
        if lh != cur_lh {
            if lh {
                let mut rec0 = [0u8; 6];
                rec0.copy_from_slice(&out[78..84]);
                if rec0.iter().all(|&b| b == 0) {
                    rec0 = [0x00, 0x02, 0x00, 0x00, 0x00, 0x00];
                }
                out[71..77].copy_from_slice(&rec0);
                out[78..84].copy_from_slice(&[0x00, 0x01, 0x00, 0x00, 0x00, 0x00]);
            } else {
                let mut left_act = [0u8; 6];
                left_act.copy_from_slice(&out[71..77]);
                if (left_act[0] == 0 && left_act[1] == 1) || left_act.iter().all(|&b| b == 0) {
                    left_act = [0x00, 0x02, 0x00, 0x00, 0x00, 0x00];
                }
                out[71..77].copy_from_slice(&[0x00, 0x01, 0x00, 0x00, 0x00, 0x00]);
                out[78..84].copy_from_slice(&left_act);
            }
        }
    }
    if let Some(spdt) = &updates.spdt {
        let code = match spdt.to_ascii_lowercase().as_str() {
            "gx_safe" | "safe" => 0xF0,
            "gx_speed" | "speed" => 0xF1,
            "off" => 8,
            _ => return Err("spdt must be 'off', 'gx_safe', or 'gx_speed'".into()),
        };
        out[77] = code;
        out[84] = code;
    }

    Ok(out)
}

fn send_cmd<T: HidTransport>(transport: &mut T, op: u8) -> Result<(), String> {
    let mut buf = [0u8; EGG_CMD_LEN];
    buf[0] = 0xA1;
    buf[1] = op;
    transport.send_feature_report(&buf)
}

pub fn read_version<T: HidTransport>(transport: &mut T) -> Option<String> {
    send_cmd(transport, 0x02).ok()?;
    thread::sleep(Duration::from_millis(50));
    let mut last = [0u8; EGG_CMD_LEN];
    for _ in 0..4 {
        let mut buf = [0u8; EGG_CMD_LEN];
        buf[0] = 0xA1;
        if transport.get_feature_report(&mut buf).is_ok() {
            last = buf;
            if buf[1] == 0x01 {
                return format_version(&buf);
            }
        }
        thread::sleep(Duration::from_millis(60));
    }
    format_version(&last)
}

pub fn read_config<T: HidTransport>(transport: &mut T) -> Result<Vec<u8>, String> {
    for _ in 0..3 {
        send_cmd(transport, 0x12)?;
        thread::sleep(Duration::from_millis(80));
        let mut delay_ms = 60u64;
        for _ in 0..6 {
            for rid in [0xA0u8, 0xA1u8] {
                let mut buf = vec![0u8; EGG_CFG_LEN];
                buf[0] = rid;
                if transport.get_feature_report(&mut buf).is_ok() {
                    buf[0] = 0xA0;
                    if blob_ok(&buf) {
                        return Ok(buf);
                    }
                }
            }
            thread::sleep(Duration::from_millis(delay_ms));
            delay_ms = (delay_ms * 2).min(400);
        }
    }
    Err("failed to read Endgame Gear 8k config blob".into())
}

pub fn write_config<T: HidTransport>(transport: &mut T, blob: &[u8]) -> Result<(), String> {
    let mut payload = vec![0u8; EGG_CFG_LEN];
    let n = blob.len().min(EGG_CFG_LEN);
    payload[..n].copy_from_slice(&blob[..n]);
    payload[0] = 0xA0;
    payload[1] = 0x11;

    for attempt in 0..4 {
        match transport.send_feature_report(&payload) {
            Ok(()) => break,
            Err(e) if attempt == 3 => return Err(e),
            Err(_) => thread::sleep(Duration::from_millis(50)),
        }
    }
    thread::sleep(Duration::from_millis(300));
    let mut delay_ms = 60u64;
    for _ in 0..8 {
        let mut ack = [0u8; EGG_CMD_LEN];
        ack[0] = 0xA1;
        if transport.get_feature_report(&mut ack).is_ok() && ack[1] == 0x01 {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(delay_ms));
        delay_ms = (delay_ms * 2).min(400);
    }
    Err("Endgame Gear 8k write was not acknowledged".into())
}

pub fn read_status<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<MouseStatus, String> {
    let prof = EggProfile::from_device(dev);
    let blob = read_config(transport)?;
    let fw = read_version(transport);
    Ok(parse_config(&blob, &prof, fw))
}

pub fn apply_updates<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    updates: &MouseUpdates,
) -> Result<MouseStatus, String> {
    let prof = EggProfile::from_device(dev);
    let base = read_config(transport)?;
    let next = serialize_config(&base, updates, &prof)?;
    write_config(transport, &next)?;
    let reread = read_config(transport)?;
    let fw = read_version(transport);
    Ok(parse_config(&reread, &prof, fw))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry::DeviceRegistry;

    fn sample_egg_blob() -> Vec<u8> {
        let mut b = vec![0u8; EGG_CFG_LEN];
        b[0] = 0xA0;
        b[1] = 0x01;
        b[21] = 2; // 4000 Hz
        b[22] = 0x01; // slamclick on
        b[25] = 1; // 1 mm
        b[26] = 0; // angle snapping off
        b[27] = 1; // ripple on
        b[28] = 1; // motion sync on
        b[29] = 1; // active stage index 1 -> stage 2
        b[30] = 3; // 3 stages
        for (i, cpi) in [800u16, 1600, 3200].into_iter().enumerate() {
            let o = 51 + i * 5;
            b[o] = 0;
            b[o + 1..o + 3].copy_from_slice(&cpi.to_le_bytes());
            b[o + 3..o + 5].copy_from_slice(&cpi.to_le_bytes());
        }
        b[77] = 0xF1; // gx_speed
        b[84] = 0xF1;
        b
    }

    #[test]
    fn parse_and_serialize_roundtrip_preserves_fields() {
        let reg = DeviceRegistry::load_embedded().unwrap();
        let dev = reg.classify_usb(0x3367, 0x1964, None);
        let prof = EggProfile::from_device(&dev);
        let base = sample_egg_blob();
        assert!(blob_ok(&base));

        let parsed = parse_config(&base, &prof, Some("v1.23".into()));
        assert_eq!(parsed.polling_hz, Some(4000));
        assert_eq!(parsed.active_stage, Some(2));
        assert_eq!(parsed.dpi, Some((1600, 1600)));
        assert_eq!(parsed.stages, vec![(800, 800), (1600, 1600), (3200, 3200)]);
        assert_eq!(parsed.motion_sync, Some(true));
        assert_eq!(parsed.slamclick, Some(true));
        assert_eq!(parsed.spdt.as_deref(), Some("gx_speed"));

        let updates = MouseUpdates {
            polling_hz: Some(8000),
            stages: Some(vec![(400, 400), (1625, 1625)]),
            active_stage: Some(2),
            motion_sync: Some(false),
            spdt: Some("gx_safe".into()),
            ..Default::default()
        };
        let mut next = serialize_config(&base, &updates, &prof).unwrap();
        next[1] = 0x01; // simulate readback status
        let parsed2 = parse_config(&next, &prof, None);
        assert_eq!(parsed2.polling_hz, Some(8000));
        assert_eq!(parsed2.active_stage, Some(2));
        assert_eq!(parsed2.stages, vec![(400, 400), (1650, 1650)]); // clamped to 50-step
        assert_eq!(parsed2.motion_sync, Some(false));
        assert_eq!(parsed2.spdt.as_deref(), Some("gx_safe"));
        assert_eq!(parsed2.slamclick, Some(true)); // untouched field preserved
    }

    #[test]
    fn bcd_version_format_matches_op1_8k() {
        let mut buf = [0u8; EGG_CMD_LEN];
        buf[1] = 0x01;
        buf[17] = 0x23;
        buf[18] = 0x01;
        assert_eq!(format_version(&buf).as_deref(), Some("v1.23"));
    }
}
