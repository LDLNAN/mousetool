//! Razer 90-byte HID feature report protocol driver (`VID 0x1532`).
//! Exact wire parity with `mousetool/drivers/razer.py`, including Tier 1 read-only enforcement.

use crate::drivers::{LodState, MouseStatus, MouseUpdates};
use crate::hid::HidTransport;
use crate::registry::ClassifiedDevice;
use std::thread;
use std::time::{Duration, Instant};

const POLL_CODES: &[(u8, u32)] = &[(1, 1000), (2, 500), (4, 250), (8, 125)];
const POLL2_CODES: &[(u8, u32)] = &[
    (0x01, 8000),
    (0x02, 4000),
    (0x04, 2000),
    (0x08, 1000),
    (0x10, 500),
    (0x20, 250),
    (0x40, 125),
];
const LOD_NAMES: [&str; 3] = ["low", "medium", "high"];

pub fn build_packet(cls: u8, cmd: u8, size: u8, args: &[u8], txn: u8) -> [u8; 90] {
    let mut r = [0u8; 90];
    r[1] = txn;
    r[5] = size;
    r[6] = cls;
    r[7] = cmd;
    let len = args.len().min(80);
    r[8..8 + len].copy_from_slice(&args[..len]);
    let mut crc = 0u8;
    for &b in &r[2..88] {
        crc ^= b;
    }
    r[88] = crc;
    r
}

pub fn xfer<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    cls: u8,
    cmd: u8,
    size: u8,
    args: &[u8],
    txn: u8,
) -> Result<Vec<u8>, String> {
    if !dev.is_razer_cmd_allowed(cls, cmd, txn) {
        return Err(format!(
            "Blocked at dispatch: {} ({}) does not allow Razer command cls=0x{cls:02x} cmd=0x{cmd:02x} txn=0x{txn:02x}",
            dev.name,
            dev.tier_label()
        ));
    }

    let pkt = build_packet(cls, cmd, size, args, txn);
    let mut out = [0u8; 91];
    out[0] = 0x00; // report ID 0
    out[1..].copy_from_slice(&pkt);

    let mut last_status = None;
    for _ in 0..3 {
        transport.send_feature_report(&out)?;
        let start = Instant::now();
        while start.elapsed() < Duration::from_millis(400) {
            thread::sleep(Duration::from_millis(8));
            let mut buf = [0u8; 91];
            buf[0] = 0x00;
            if transport.get_feature_report(&mut buf).is_err() {
                continue;
            }
            let r = &buf[1..];
            if r[6] != cls || r[7] != cmd || r[0] < 2 {
                continue;
            }
            last_status = Some(r[0]);
            return match r[0] {
                2 => Ok(r[8..].to_vec()),
                5 => Err("command unsupported by firmware".into()),
                3 => Err("mouse rejected command".into()),
                _ => break,
            };
        }
        thread::sleep(Duration::from_millis(120));
    }
    if last_status == Some(4) {
        return Err("mouse didn't answer (asleep or out of range)".into());
    }
    Err(format!("no reply (status {last_status:?})"))
}

pub fn get_firmware<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<String, String> {
    if !dev.can_read("firmware") {
        return Err("firmware read not supported".into());
    }
    let txn = dev.op_txn("firmware", 0x1F);
    let a = xfer(transport, dev, 0x00, 0x81, 0x02, &[], txn)?;
    Ok(format!("v{}.{}", a[0], a[1]))
}

pub fn get_serial<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<String, String> {
    if !dev.can_read("serial") {
        return Err("serial read not supported".into());
    }
    let txn = dev.op_txn("serial", 0x1F);
    let a = xfer(transport, dev, 0x00, 0x82, 0x16, &[], txn)?;
    let end = a.iter().take(22).position(|&b| b == 0).unwrap_or(22.min(a.len()));
    Ok(String::from_utf8_lossy(&a[..end]).trim().to_string())
}

pub fn get_dpi<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<(u32, u32), String> {
    if !dev.can_read("dpi") {
        return Err("DPI read not supported".into());
    }
    let txn = dev.op_txn("dpi", 0x1F);
    let storage = dev.op_storage("dpi", 0x00);
    let a = xfer(transport, dev, 0x04, 0x85, 0x07, &[storage], txn)?;
    let x = u16::from_be_bytes([a[1], a[2]]) as u32;
    let y = u16::from_be_bytes([a[3], a[4]]) as u32;
    Ok((x, y))
}

pub fn set_dpi<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    x: u32,
    y: u32,
) -> Result<(), String> {
    if !dev.can_write("dpi") {
        return Err(format!("DPI write blocked for {} ({})", dev.name, dev.tier_label()));
    }
    if !(100..=dev.dpi_max).contains(&x) || !(100..=dev.dpi_max).contains(&y) {
        return Err(format!("DPI must be between 100 and {}", dev.dpi_max));
    }
    let txn = dev.op_txn("dpi", 0x1F);
    let [xh, xl] = (x as u16).to_be_bytes();
    let [yh, yl] = (y as u16).to_be_bytes();
    xfer(transport, dev, 0x04, 0x05, 0x07, &[0x01, xh, xl, yh, yl, 0, 0], txn)?;
    Ok(())
}

pub fn get_stages<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<(usize, Vec<(u32, u32)>), String> {
    if !dev.can_read("stages") {
        return Err("stages read not supported".into());
    }
    let txn = dev.op_txn("stages", 0x1F);
    let a = xfer(transport, dev, 0x04, 0x86, 0x26, &[0x01], txn)?;
    let active = a[1] as usize;
    let n = (a[2] as usize).min(5);
    let mut stages = Vec::with_capacity(n);
    for i in 0..n {
        let o = 3 + i * 7;
        let x = u16::from_be_bytes([a[o + 1], a[o + 2]]) as u32;
        let y = u16::from_be_bytes([a[o + 3], a[o + 4]]) as u32;
        stages.push((x, y));
    }
    Ok((active, stages))
}

pub fn set_stages<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    active: usize,
    stages: &[(u32, u32)],
) -> Result<(), String> {
    if !dev.can_write("stages") {
        return Err(format!("stages write blocked for {} ({})", dev.name, dev.tier_label()));
    }
    if !(1..=5).contains(&stages.len()) {
        return Err("must have between 1 and 5 stages".into());
    }
    if !(1..=stages.len()).contains(&active) {
        return Err(format!("active stage must be between 1 and {}", stages.len()));
    }
    let mut args = vec![0x01u8, active as u8, stages.len() as u8];
    for (idx, &(x, y)) in stages.iter().enumerate() {
        if !(100..=dev.dpi_max).contains(&x) || !(100..=dev.dpi_max).contains(&y) {
            return Err(format!(
                "stage {} DPI must be between 100 and {}",
                idx + 1,
                dev.dpi_max
            ));
        }
        let [xh, xl] = (x as u16).to_be_bytes();
        let [yh, yl] = (y as u16).to_be_bytes();
        args.extend_from_slice(&[(idx + 1) as u8, xh, xl, yh, yl, 0, 0]);
    }
    let txn = dev.op_txn("stages", 0x1F);
    xfer(transport, dev, 0x04, 0x06, 0x26, &args, txn)?;
    Ok(())
}

pub fn get_poll<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<Option<u32>, String> {
    if !dev.can_read("polling") {
        return Err("polling read not supported".into());
    }
    let txn = dev.op_txn("polling", 0x1F);
    if dev.hyper {
        let a = xfer(transport, dev, 0x00, 0xC0, 0x01, &[], txn)?;
        Ok(POLL2_CODES.iter().find(|&&(c, _)| c == a[1]).map(|&(_, hz)| hz))
    } else {
        let a = xfer(transport, dev, 0x00, 0x85, 0x01, &[], txn)?;
        Ok(POLL_CODES.iter().find(|&&(c, _)| c == a[0]).map(|&(_, hz)| hz))
    }
}

pub fn set_poll<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    hz: u32,
) -> Result<(), String> {
    if !dev.can_write("polling") {
        return Err(format!("polling write blocked for {} ({})", dev.name, dev.tier_label()));
    }
    let txn = dev.op_txn("polling", 0x1F);
    if dev.hyper {
        let code = POLL2_CODES
            .iter()
            .find(|&&(_, r)| r == hz)
            .map(|&(c, _)| c)
            .ok_or_else(|| format!("unsupported polling rate {hz}"))?;
        xfer(transport, dev, 0x00, 0x40, 0x02, &[0x00, code], txn)?;
        let _ = xfer(transport, dev, 0x00, 0x40, 0x02, &[0x01, code], txn);
    } else {
        let code = POLL_CODES
            .iter()
            .find(|&&(_, r)| r == hz)
            .map(|&(c, _)| c)
            .ok_or_else(|| format!("unsupported polling rate {hz}"))?;
        xfer(transport, dev, 0x00, 0x05, 0x01, &[code], txn)?;
    }
    Ok(())
}

pub fn get_lod<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<Option<LodState>, String> {
    if !dev.can_read("lod") {
        return Ok(None);
    }
    let txn = dev.op_txn("lod", 0x1F);
    for _ in 0..3 {
        let a = xfer(transport, dev, 0x0B, 0x85, 0x01, &[], txn)?;
        if a[2] == 0x01 && (a[3] as usize) < LOD_NAMES.len() {
            return Ok(Some(LodState::Preset(LOD_NAMES[a[3] as usize].into())));
        }
        if a[2] == 0x04 {
            return Ok(Some(LodState::Async {
                lift: a[4] + 1,
                land: a[5] + 1,
            }));
        }
        thread::sleep(Duration::from_millis(60));
    }
    Ok(None)
}

pub fn set_lod_preset<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    level: &str,
) -> Result<(), String> {
    if !dev.can_write("lod") {
        return Err("LOD write blocked".into());
    }
    let code = match level.to_ascii_lowercase().as_str() {
        "low" => 0u8,
        "med" | "medium" => 1u8,
        "high" => 2u8,
        _ => return Err("LOD preset must be low, medium, or high".into()),
    };
    let txn = dev.op_txn("lod", 0x1F);
    xfer(transport, dev, 0x0B, 0x03, 0x03, &[0x00, 0x04, 0x28], txn)?;
    xfer(transport, dev, 0x0B, 0x0B, 0x04, &[0x00, 0x04, 0x01, code], txn)?;
    Ok(())
}

pub fn get_battery<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<(u8, bool), String> {
    if !dev.can_read("battery") {
        return Err("battery read not supported".into());
    }
    let txn = dev.op_txn("battery", 0x1F);
    let bat = xfer(transport, dev, 0x07, 0x80, 0x02, &[], txn)?;
    let chg = xfer(transport, dev, 0x07, 0x84, 0x02, &[], txn)?;
    let pct = ((bat[1] as f64 / 255.0) * 100.0).round().clamp(0.0, 100.0) as u8;
    Ok((pct, chg[1] != 0))
}

pub fn read_status<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
) -> Result<MouseStatus, String> {
    let firmware = get_firmware(transport, dev).ok();
    let serial = get_serial(transport, dev).ok();
    let dpi = get_dpi(transport, dev).ok();
    let (active_stage, stages) = match get_stages(transport, dev) {
        Ok((a, s)) => (Some(a), s),
        Err(_) => (None, Vec::new()),
    };
    let polling_hz = get_poll(transport, dev).ok().flatten();
    let lod = get_lod(transport, dev).ok().flatten();
    let (battery_percent, charging) = if dev.wireless {
        match get_battery(transport, dev) {
            Ok((p, c)) => (Some(p), Some(c)),
            Err(_) => (None, None),
        }
    } else {
        (None, None)
    };
    let supported_poll_rates = if dev.can_read("polling") {
        if dev.hyper {
            vec![125, 250, 500, 1000, 2000, 4000, 8000]
        } else {
            vec![125, 500, 1000]
        }
    } else {
        Vec::new()
    };

    Ok(MouseStatus {
        firmware,
        serial,
        dpi,
        dpi_min: 100,
        dpi_max: dev.dpi_max,
        dpi_step: 50,
        max_stages: 5,
        active_stage,
        stages,
        polling_hz,
        supported_poll_rates,
        lod,
        battery_percent,
        charging,
        ..Default::default()
    })
}

pub fn apply_updates<T: HidTransport>(
    transport: &mut T,
    dev: &ClassifiedDevice,
    updates: &MouseUpdates,
) -> Result<MouseStatus, String> {
    if let Some(stages) = &updates.stages {
        let active = updates.active_stage.unwrap_or(1);
        set_stages(transport, dev, active, stages)?;
    } else if let Some((x, y)) = updates.dpi {
        set_dpi(transport, dev, x, y)?;
    }
    if let Some(hz) = updates.polling_hz {
        set_poll(transport, dev, hz)?;
    }
    if let Some(level) = &updates.lod_preset {
        set_lod_preset(transport, dev, level)?;
    }
    read_status(transport, dev)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry::DeviceRegistry;

    struct MockRazer {
        sent: Vec<Vec<u8>>,
        last_cls: u8,
        last_cmd: u8,
    }

    impl MockRazer {
        fn new() -> Self {
            Self {
                sent: Vec::new(),
                last_cls: 0,
                last_cmd: 0,
            }
        }
    }

    impl HidTransport for MockRazer {
        fn send_feature_report(&mut self, data: &[u8]) -> Result<(), String> {
            self.sent.push(data.to_vec());
            self.last_cls = data[7];
            self.last_cmd = data[8];
            Ok(())
        }
        fn get_feature_report(&mut self, buf: &mut [u8]) -> Result<usize, String> {
            buf.fill(0);
            buf[1] = 2; // status OK
            buf[7] = self.last_cls;
            buf[8] = self.last_cmd;
            match (self.last_cls, self.last_cmd) {
                (0x00, 0x81) => {
                    buf[9] = 2;
                    buf[10] = 5;
                }
                (0x04, 0x85) => {
                    buf[10..12].copy_from_slice(&1600u16.to_be_bytes());
                    buf[12..14].copy_from_slice(&1600u16.to_be_bytes());
                }
                _ => {}
            }
            Ok(buf.len())
        }
        fn write(&mut self, _data: &[u8]) -> Result<usize, String> {
            Ok(0)
        }
        fn read_timeout(&mut self, _buf: &mut [u8], _timeout_ms: i32) -> Result<usize, String> {
            Ok(0)
        }
    }

    #[test]
    fn packet_crc_matches_xor_of_bytes_2_through_87() {
        let p = build_packet(0x04, 0x05, 0x07, &[0x01, 0x06, 0x40, 0x06, 0x40, 0, 0], 0x1F);
        let expected: u8 = p[2..88].iter().fold(0u8, |acc, &b| acc ^ b);
        assert_eq!(p[88], expected);
    }

    #[test]
    fn tier1_viper_mini_allows_dpi_read_and_blocks_all_writes() {
        let reg = DeviceRegistry::load_embedded().unwrap();
        let mini = reg.classify_usb(0x1532, 0x008a, None);
        let mut mock = MockRazer::new();

        assert_eq!(get_firmware(&mut mock, &mini).unwrap(), "v2.5");
        assert_eq!(get_dpi(&mut mock, &mini).unwrap(), (1600, 1600));
        let sent_before = mock.sent.len();

        // Any mutating write MUST be blocked before touching transport
        assert!(set_dpi(&mut mock, &mini, 800, 800).is_err());
        assert!(xfer(&mut mock, &mini, 0x04, 0x05, 0x07, &[0x01], 0xFF).is_err());
        assert_eq!(mock.sent.len(), sent_before);
    }
}
