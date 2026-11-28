//! Acceleration curve mathematics (exact parity with `web/accel_graph.js` and `rawaccel_export.py`)
//! plus live Linux `mouseaccel`/`maccel` sysfs reader & validated writer.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

pub const Q32_SCALE: f64 = 4_294_967_296.0; // 1 << 32
pub const LUT_MIN_POINTS: usize = 2;
pub const LUT_MAX_POINTS: usize = 64;
pub const LUT_STR_MAX: usize = 3072;
pub const LUT_DX_MIN: f64 = 0.001;
const LUT_X_MAX: f64 = 1000.0;
const LUT_Y_MIN: f64 = 0.05;
const LUT_Y_MAX: f64 = 10.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AccelMode {
    Linear,
    Natural,
    Synchronous,
    Lookup,
    NoAccel,
}

impl AccelMode {
    pub const ALL: [Self; 5] = [
        Self::Linear,
        Self::Natural,
        Self::Synchronous,
        Self::Lookup,
        Self::NoAccel,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Linear => "Linear",
            Self::Natural => "Natural",
            Self::Synchronous => "Synchronous",
            Self::Lookup => "Custom",
            Self::NoAccel => "Off",
        }
    }

    pub fn wire_name(self) -> &'static str {
        match self {
            Self::Linear => "linear",
            Self::Natural => "natural",
            Self::Synchronous => "synchronous",
            Self::Lookup => "lookup",
            Self::NoAccel => "no_accel",
        }
    }

    pub fn kernel_index(self) -> u8 {
        match self {
            Self::Linear => 0,
            Self::Natural => 1,
            Self::Synchronous => 2,
            Self::NoAccel => 3,
            Self::Lookup => 4,
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Linear => "Speed rises steadily as you move faster, up to your optional cap.",
            Self::Natural => "Smooth curve that ramps up gently and levels off near your limit.",
            Self::Synchronous => {
                "S-shaped curve that feels slower for fine aim and faster for big swipes."
            }
            Self::Lookup => {
                "Draw your own curve directly on the graph with draggable control points."
            }
            Self::NoAccel => "No speed boost — constant sensitivity at every hand speed.",
        }
    }

    pub fn from_kernel_mode(raw: &str) -> Self {
        Self::parse_kernel_mode(raw).unwrap_or(Self::Linear)
    }

    fn parse_kernel_mode(raw: &str) -> Option<Self> {
        Some(match raw.trim().to_ascii_lowercase().as_str() {
            "0" | "linear" => Self::Linear,
            "1" | "natural" => Self::Natural,
            "2" | "synchronous" => Self::Synchronous,
            "3" | "no_accel" | "noaccel" => Self::NoAccel,
            "4" | "lookup" | "lut" => Self::Lookup,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LutPreset {
    Flat,
    Ramp,
    SCurve,
    StepCap,
}

impl LutPreset {
    pub const ALL: [Self; 4] = [Self::Flat, Self::Ramp, Self::SCurve, Self::StepCap];

    pub fn label(self) -> &'static str {
        match self {
            Self::Flat => "Flat 1×",
            Self::Ramp => "Ramp",
            Self::SCurve => "S-Curve",
            Self::StepCap => "Step Cap",
        }
    }

    pub fn points(self) -> Vec<[f64; 2]> {
        match self {
            Self::Flat => vec![[0.0, 1.0], [60.0, 1.0]],
            Self::Ramp => vec![[0.0, 1.0], [10.0, 1.0], [35.0, 1.6], [60.0, 2.0]],
            Self::SCurve => vec![
                [0.0, 0.85],
                [8.0, 0.9],
                [20.0, 1.2],
                [38.0, 1.7],
                [60.0, 1.85],
            ],
            Self::StepCap => vec![[0.0, 1.0], [15.0, 1.0], [20.0, 1.5], [60.0, 1.5]],
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AccelConfig {
    pub mode: AccelMode,
    pub sens_mult: f64,
    pub yx_ratio: f64,
    pub input_dpi: f64,
    pub angle_rotation: f64,
    pub accel: f64,
    pub offset: f64,
    pub output_cap: f64,
    pub decay_rate: f64,
    pub limit: f64,
    pub gamma: f64,
    pub smooth: f64,
    pub motivity: f64,
    pub sync_speed: f64,
    pub lut_points: Vec<[f64; 2]>,
}

impl Default for AccelConfig {
    fn default() -> Self {
        Self {
            mode: AccelMode::Linear,
            sens_mult: 1.0,
            yx_ratio: 1.0,
            input_dpi: 1000.0,
            angle_rotation: 0.0,
            accel: 0.02,
            offset: 0.0,
            output_cap: 2.0,
            decay_rate: 0.1,
            limit: 1.5,
            gamma: 1.0,
            smooth: 0.5,
            motivity: 1.5,
            sync_speed: 5.0,
            lut_points: vec![[0.0, 1.0], [10.0, 1.0], [30.0, 1.5], [60.0, 1.8]],
        }
    }
}

/// Evaluate `(sens_x, sens_y)` at normalized hand speed `x` (counts/ms at 1000 DPI).
/// Matches `maccelEval()` in `web/accel_graph.js`.
pub fn evaluate_sensitivity(x: f64, cfg: &AccelConfig) -> (f64, f64) {
    let mut s = 1.0_f64;
    match cfg.mode {
        AccelMode::Linear => {
            if x > cfg.offset && x > 0.0 {
                let mut b = cfg.accel * ((x - cfg.offset) * (x - cfg.offset)) / x;
                let mut sign = 1.0;
                if cfg.output_cap > 0.0 {
                    let mut c = cfg.output_cap - 1.0;
                    if c < 0.0 {
                        c = -c;
                        sign = -1.0;
                    }
                    b = b.min(c);
                }
                s = 1.0 + sign * b;
            }
        }
        AccelMode::Natural => {
            if x > cfg.offset && cfg.limit > 1.0 && cfg.decay_rate > 0.0 {
                let l = cfg.limit - 1.0;
                let a = cfg.decay_rate / l.abs();
                let c = -l / a;
                let ox = cfg.offset - x;
                let dec = (a * ox).exp();
                let out = l * (dec / a - ox) + c;
                s = out / x + 1.0;
            }
        }
        AccelMode::Synchronous => {
            let mot = cfg.motivity.max(1.0001);
            let sync = cfg.sync_speed.max(0.0001);
            let log_m = mot.ln();
            let gc = cfg.gamma / log_m;
            let log_s = sync.ln();
            let sharp = if cfg.smooth == 0.0 {
                16.0
            } else {
                0.5 / cfg.smooth
            };
            if sharp >= 16.0 {
                let ls = (gc * (x.max(1e-6).ln() - log_s)).clamp(-1.0, 1.0);
                s = (ls * log_m).exp();
            } else if (x - sync).abs() < 1e-9 {
                s = 1.0;
            } else {
                let ld = x.max(1e-6).ln() - log_s;
                let sign = if ld > 0.0 { 1.0 } else { -1.0 };
                let ls = gc * ld.abs();
                let exp = sign * ls.powf(sharp).tanh().powf(1.0 / sharp);
                s = (exp * log_m).exp();
            }
        }
        AccelMode::Lookup => {
            let pts = &cfg.lut_points;
            if pts.len() >= 2 {
                if x <= pts[0][0] {
                    s = pts[0][1];
                } else if x >= pts[pts.len() - 1][0] {
                    s = pts[pts.len() - 1][1];
                } else {
                    let mut lo = 0;
                    let mut hi = pts.len() - 1;
                    while hi - lo > 1 {
                        let mid = (lo + hi) >> 1;
                        if pts[mid][0] <= x {
                            lo = mid;
                        } else {
                            hi = mid;
                        }
                    }
                    let [x0, y0] = pts[lo];
                    let [x1, y1] = pts[lo + 1];
                    let span = (x1 - x0).max(1e-9);
                    s = y0 + ((x - x0) * (y1 - y0)) / span;
                }
            }
        }
        AccelMode::NoAccel => {
            s = 1.0;
        }
    }
    let sx = s * cfg.sens_mult;
    (sx, sx * cfg.yx_ratio)
}

fn encode_q32(value: f64) -> i64 {
    (value * Q32_SCALE).round() as i64
}

pub fn parse_lut_points_sysfs(raw: &str) -> Result<Vec<[f64; 2]>, String> {
    let text = raw.trim();
    if text.is_empty() || text.len() > LUT_STR_MAX {
        return Err("LUT_POINTS sysfs value is empty or too long".into());
    }
    let mut out = Vec::new();
    for tok in text.split(';') {
        let mut parts = tok.split(',');
        let (Some(xs), Some(ys), None) = (parts.next(), parts.next(), parts.next()) else {
            return Err(format!("malformed LUT_POINTS entry: {tok:?}"));
        };
        let xr: i64 = xs.parse().map_err(|_| format!("invalid LUT x: {xs:?}"))?;
        let yr: i64 = ys.parse().map_err(|_| format!("invalid LUT y: {ys:?}"))?;
        out.push([xr as f64 / Q32_SCALE, yr as f64 / Q32_SCALE]);
    }
    // Decoded values are exact here because the LUT bounds fit within f64's
    // integer precision in Q32.32.
    validate_lut_bounds(&out, encode_q32(LUT_Y_MIN) as f64 / Q32_SCALE)?;
    Ok(out)
}

pub fn validate_lut_points(points: &[[f64; 2]]) -> Result<(), String> {
    validate_lut_bounds(points, LUT_Y_MIN)
}

fn validate_lut_bounds(points: &[[f64; 2]], min_y: f64) -> Result<(), String> {
    if !(LUT_MIN_POINTS..=LUT_MAX_POINTS).contains(&points.len()) {
        return Err(format!(
            "LUT_POINTS must have between {LUT_MIN_POINTS} and {LUT_MAX_POINTS} points"
        ));
    }
    // Use the exact kernel threshold so a decoded boundary can be re-encoded.
    let min_dx = encode_q32(LUT_DX_MIN) as f64 / Q32_SCALE;
    let mut prev_x = None;
    for &[x, y] in points {
        if !x.is_finite() || !y.is_finite() {
            return Err("LUT_POINTS values must be finite".into());
        }
        if !(0.0..=LUT_X_MAX).contains(&x) || !(min_y..=LUT_Y_MAX).contains(&y) {
            return Err(format!("LUT point ({x}, {y}) out of bounds"));
        }
        if let Some(px) = prev_x {
            if x - px < min_dx {
                return Err(format!(
                    "LUT_POINTS x values must increase by at least {LUT_DX_MIN}"
                ));
            }
        }
        prev_x = Some(x);
    }
    Ok(())
}

pub fn encode_lut_points_sysfs(points: &[[f64; 2]]) -> Result<String, String> {
    validate_lut_points(points)?;
    let text = points
        .iter()
        .map(|&[x, y]| format!("{},{}", encode_q32(x), encode_q32(y)))
        .collect::<Vec<_>>()
        .join(";");
    parse_lut_points_sysfs(&text)?;
    Ok(text)
}

#[derive(Debug, Clone)]
pub struct AccelBackendStatus {
    pub name: String,
    pub label: String,
    pub loaded: bool,
    pub writable: bool,
    pub lut_supported: bool,
    pub lut_writable: bool,
    pub detail: String,
    pub active_mode: Option<AccelMode>,
    pub live_config: Option<AccelConfig>,
    pub params_dir: Option<PathBuf>,
    pub resets_dir: Option<PathBuf>,
}

#[path = "accel/linux_io.rs"]
mod linux_io;
pub use linux_io::{apply_linux_accel_to_dir, read_linux_accel_from_dir};

/// Apply `cfg` to the active OS acceleration backend.
pub fn apply_live_accel(cfg: &AccelConfig) -> Result<AccelConfig, String> {
    let status = detect_accel_backend();
    if !status.loaded {
        return Err("No kernel acceleration backend is loaded.".into());
    }
    if !status.writable || status.live_config.is_none() {
        return Err(status.detail);
    }
    #[cfg(target_os = "linux")]
    {
        let params_dir = status
            .params_dir
            .as_deref()
            .ok_or_else(|| "missing sysfs parameters directory".to_string())?;
        return apply_linux_accel_to_dir(params_dir, status.resets_dir.as_deref(), cfg);
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = cfg;
        Err("Live native accel writes on this OS arrive in the next slice.".into())
    }
}

/// Detect the active OS kernel acceleration backend (read-only probe + live config read).
pub fn detect_accel_backend() -> AccelBackendStatus {
    #[cfg(target_os = "linux")]
    {
        for (name, label) in [
            ("mouseaccel", "mouseaccel (kernel)"),
            ("maccel", "maccel (legacy)"),
        ] {
            let params_dir = PathBuf::from("/sys/module").join(name).join("parameters");
            if params_dir.is_dir() {
                let mode_path = params_dir.join("MODE");
                let config_result = linux_io::read_config_from_dir(&params_dir);
                let live_config = config_result.as_ref().ok().cloned();
                let active_mode = live_config.as_ref().map(|c| c.mode);
                let writable = std::fs::OpenOptions::new()
                    .write(true)
                    .open(&mode_path)
                    .is_ok();
                let lut_path = params_dir.join("LUT_POINTS");
                let lut_supported = lut_path.is_file();
                let lut_writable = lut_supported
                    && std::fs::OpenOptions::new()
                        .write(true)
                        .open(&lut_path)
                        .is_ok();
                let resets_dir = PathBuf::from("/var/opt").join(name).join("resets");
                let detail = if writable {
                    format!("{label} loaded and writable")
                } else {
                    format!("{label} loaded (read-only — add your user to the `{name}` group)")
                };
                let detail = match config_result {
                    Ok(_) => detail,
                    Err(error) => format!("{detail}. Live configuration unavailable: {error}"),
                };
                return AccelBackendStatus {
                    name: name.into(),
                    label: label.into(),
                    loaded: true,
                    writable,
                    lut_supported,
                    lut_writable,
                    detail,
                    active_mode,
                    live_config,
                    params_dir: Some(params_dir),
                    resets_dir: resets_dir.is_dir().then_some(resets_dir),
                };
            }
        }
        AccelBackendStatus {
            name: "none".into(),
            label: "No kernel module loaded".into(),
            loaded: false,
            writable: false,
            lut_supported: false,
            lut_writable: false,
            detail: "Preview mode — install `mouseaccel` to apply curves at the kernel level."
                .into(),
            active_mode: None,
            live_config: None,
            params_dir: None,
            resets_dir: None,
        }
    }

    #[cfg(target_os = "windows")]
    {
        AccelBackendStatus {
            name: "rawaccel".into(),
            label: "Raw Accel (Windows)".into(),
            loaded: false,
            writable: false,
            lut_supported: true,
            lut_writable: false,
            detail: "Raw Accel native adapter coming next (previewing curves locally).".into(),
            active_mode: None,
            live_config: None,
            params_dir: None,
            resets_dir: None,
        }
    }

    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        AccelBackendStatus {
            name: "none".into(),
            label: "Preview only".into(),
            loaded: false,
            writable: false,
            lut_supported: false,
            lut_writable: false,
            detail: "Curve preview mode.".into(),
            active_mode: None,
            live_config: None,
            params_dir: None,
            resets_dir: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linear_curve_respects_offset_and_cap() {
        let cfg = AccelConfig {
            mode: AccelMode::Linear,
            accel: 0.1,
            offset: 2.0,
            output_cap: 1.5,
            sens_mult: 1.0,
            yx_ratio: 1.0,
            ..Default::default()
        };
        assert_eq!(evaluate_sensitivity(1.0, &cfg), (1.0, 1.0));
        let (mid, _) = evaluate_sensitivity(4.0, &cfg);
        assert!((mid - 1.1).abs() < 1e-9);
        let (capped, _) = evaluate_sensitivity(100.0, &cfg);
        assert!((capped - 1.5).abs() < 1e-9);
    }

    #[test]
    fn natural_synchronous_lookup_and_no_accel_match_expected() {
        let nat = AccelConfig {
            mode: AccelMode::Natural,
            decay_rate: 0.1,
            offset: 0.0,
            limit: 1.5,
            ..Default::default()
        };
        let (n0, _) = evaluate_sensitivity(0.01, &nat);
        let (n_hi, _) = evaluate_sensitivity(200.0, &nat);
        assert!(n0 >= 1.0 && n_hi <= 1.5 && n_hi > 1.4);

        let sync = AccelConfig {
            mode: AccelMode::Synchronous,
            sync_speed: 5.0,
            motivity: 1.5,
            gamma: 1.0,
            smooth: 0.5,
            ..Default::default()
        };
        let (at_sync, _) = evaluate_sensitivity(5.0, &sync);
        assert!((at_sync - 1.0).abs() < 1e-9);

        let lut = AccelConfig {
            mode: AccelMode::Lookup,
            sens_mult: 1.2,
            yx_ratio: 0.5,
            lut_points: vec![[0.0, 1.0], [20.0, 2.0]],
            ..Default::default()
        };
        let (lx, ly) = evaluate_sensitivity(10.0, &lut);
        assert!((lx - 1.8).abs() < 1e-9);
        assert!((ly - 0.9).abs() < 1e-9);

        let off = AccelConfig {
            mode: AccelMode::NoAccel,
            sens_mult: 0.8,
            yx_ratio: 1.25,
            ..Default::default()
        };
        let (ox, oy) = evaluate_sensitivity(42.0, &off);
        assert!((ox - 0.8).abs() < 1e-9);
        assert!((oy - 1.0).abs() < 1e-9);
    }

    #[test]
    fn sysfs_q32_and_lut_roundtrip_and_reset_scripts() {
        let tmp = tempfile::tempdir().unwrap();
        let params = tmp.path().join("parameters");
        let resets = tmp.path().join("resets");
        std::fs::create_dir_all(&params).unwrap();
        std::fs::create_dir_all(&resets).unwrap();
        linux_io::seed_fixture(&params);

        let cfg = AccelConfig {
            mode: AccelMode::Lookup,
            sens_mult: 1.15,
            yx_ratio: 1.25,
            input_dpi: 3200.0,
            lut_points: vec![[0.0, 1.0], [15.0, 1.2], [45.0, 1.75]],
            ..Default::default()
        };
        let readback = apply_linux_accel_to_dir(&params, Some(&resets), &cfg).unwrap();
        assert_eq!(readback.mode, AccelMode::Lookup);
        assert!((readback.sens_mult - 1.15).abs() < 1e-6);
        assert!((readback.yx_ratio - 1.25).abs() < 1e-6);
        assert!((readback.input_dpi - 3200.0).abs() < 1e-6);
        assert_eq!(readback.lut_points.len(), 3);
        assert!((readback.lut_points[2][1] - 1.75).abs() < 1e-6);

        // Reset script exists for SENS_MULT and MODE, NEVER for LUT_POINTS
        assert!(resets.join("set_last_SENS_MULT_value.sh").is_file());
        assert!(resets.join("set_last_MODE_value.sh").is_file());
        assert!(!resets.join("set_last_LUT_POINTS_value.sh").exists());
    }
}
