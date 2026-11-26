//! Acceleration curve mathematics (exact parity with `web/accel_graph.js` and `rawaccel_export.py`)
//! plus read-only OS acceleration backend detection.

use serde::{Deserialize, Serialize};
use std::path::Path;

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

    pub fn description(self) -> &'static str {
        match self {
            Self::Linear => "Speed rises steadily as you move faster, up to your optional cap.",
            Self::Natural => "Smooth curve that ramps up gently and levels off near your limit.",
            Self::Synchronous => {
                "S-shaped curve that feels slower for fine aim and faster for big swipes."
            }
            Self::Lookup => "Draw your own curve directly on the graph with control points.",
            Self::NoAccel => "No speed boost — constant sensitivity at every hand speed.",
        }
    }

    pub fn from_kernel_mode(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "0" | "linear" => Self::Linear,
            "1" | "natural" => Self::Natural,
            "2" | "synchronous" => Self::Synchronous,
            "3" | "no_accel" | "noaccel" => Self::NoAccel,
            "4" | "lookup" | "lut" => Self::Lookup,
            _ => Self::Linear,
        }
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

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Clone)]
pub struct AccelBackendStatus {
    pub name: String,
    pub label: String,
    pub loaded: bool,
    pub writable: bool,
    pub lut_supported: bool,
    pub detail: String,
    pub active_mode: Option<AccelMode>,
}

/// Detect the active OS kernel acceleration backend (read-only probe).
pub fn detect_accel_backend() -> AccelBackendStatus {
    #[cfg(target_os = "linux")]
    {
        for (name, label) in [("mouseaccel", "mouseaccel (kernel)"), ("maccel", "maccel (legacy)")] {
            let params_dir = Path::new("/sys/module").join(name).join("parameters");
            if params_dir.is_dir() {
                let mode_path = params_dir.join("MODE");
                let active_mode = std::fs::read_to_string(&mode_path)
                    .ok()
                    .map(|raw| AccelMode::from_kernel_mode(&raw));
                let writable = std::fs::OpenOptions::new().write(true).open(&mode_path).is_ok();
                let lut_supported = params_dir.join("LUT_POINTS").is_file();
                let detail = if writable {
                    format!("{label} loaded and writable")
                } else {
                    format!("{label} loaded (read-only — add your user to the `{name}` group)")
                };
                return AccelBackendStatus {
                    name: name.into(),
                    label: label.into(),
                    loaded: true,
                    writable,
                    lut_supported,
                    detail,
                    active_mode,
                };
            }
        }
        AccelBackendStatus {
            name: "none".into(),
            label: "No kernel module loaded".into(),
            loaded: false,
            writable: false,
            lut_supported: false,
            detail: "Preview mode — install `mouseaccel` to apply curves at the kernel level."
                .into(),
            active_mode: None,
        }
    }

    #[cfg(target_os = "windows")]
    {
        let _ = Path::new("");
        AccelBackendStatus {
            name: "rawaccel".into(),
            label: "Raw Accel (Windows)".into(),
            loaded: false,
            writable: false,
            lut_supported: true,
            detail: "Raw Accel native adapter arrives in Phase 2 (previewing curves locally)."
                .into(),
            active_mode: None,
        }
    }

    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        let _ = Path::new("");
        AccelBackendStatus {
            name: "none".into(),
            label: "Preview only".into(),
            loaded: false,
            writable: false,
            lut_supported: false,
            detail: "Curve preview mode.".into(),
            active_mode: None,
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
        // 1 + 0.1 * (2^2) / 4 = 1.1
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
}
