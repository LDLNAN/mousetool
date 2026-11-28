use super::{
    AccelConfig, AccelMode, Q32_SCALE, encode_lut_points_sysfs, encode_q32, parse_lut_points_sysfs,
};
use std::{fs, io, path::Path, sync::Mutex};

static BACKEND_LOCK: Mutex<()> = Mutex::new(());

trait ParameterStore {
    fn read(&self, name: &str) -> io::Result<String>;
    fn check_writable(&self, name: &str) -> io::Result<()>;
    fn write(&mut self, name: &str, value: &str) -> io::Result<()>;
}

struct Sysfs<'a>(&'a Path);

impl ParameterStore for Sysfs<'_> {
    fn read(&self, name: &str) -> io::Result<String> {
        fs::read_to_string(self.0.join(name))
    }

    fn check_writable(&self, name: &str) -> io::Result<()> {
        fs::OpenOptions::new().write(true).open(self.0.join(name))?;
        Ok(())
    }

    fn write(&mut self, name: &str, value: &str) -> io::Result<()> {
        fs::write(self.0.join(name), value)
    }
}

fn scalars(cfg: &mut AccelConfig) -> [(&'static str, &mut f64, f64, f64); 13] {
    [
        ("SENS_MULT", &mut cfg.sens_mult, 0.01, 50.0),
        ("YX_RATIO", &mut cfg.yx_ratio, 0.01, 50.0),
        ("INPUT_DPI", &mut cfg.input_dpi, 50.0, 50_000.0),
        ("ANGLE_ROTATION", &mut cfg.angle_rotation, -180.0, 180.0),
        ("ACCEL", &mut cfg.accel, 0.0, 50.0),
        ("OFFSET", &mut cfg.offset, 0.0, 1000.0),
        ("OUTPUT_CAP", &mut cfg.output_cap, 0.0, 100.0),
        ("DECAY_RATE", &mut cfg.decay_rate, 0.0001, 100.0),
        ("LIMIT", &mut cfg.limit, 1.0, 100.0),
        ("GAMMA", &mut cfg.gamma, 0.0001, 100.0),
        ("SMOOTH", &mut cfg.smooth, 0.0, 1.0),
        ("MOTIVITY", &mut cfg.motivity, 1.0001, 100.0),
        ("SYNC_SPEED", &mut cfg.sync_speed, 0.0001, 1000.0),
    ]
}

fn read_value(store: &impl ParameterStore, name: &str) -> Result<String, String> {
    store
        .read(name)
        .map_err(|error| format!("read {name}: {error}"))
}

fn read_lut(store: &impl ParameterStore, mode: AccelMode) -> Result<Vec<[f64; 2]>, String> {
    match store.read("LUT_POINTS") {
        Ok(raw) => parse_lut_points_sysfs(&raw),
        Err(error) if error.kind() == io::ErrorKind::NotFound && mode != AccelMode::Lookup => {
            // Older kernels have no LUT to read. Retain the editable default curve.
            Ok(AccelConfig::default().lut_points)
        }
        Err(error) => Err(format!("read LUT_POINTS: {error}")),
    }
}

fn read_config(store: &impl ParameterStore) -> Result<AccelConfig, String> {
    let raw_mode = read_value(store, "MODE")?;
    let mode = AccelMode::parse_kernel_mode(&raw_mode)
        .ok_or_else(|| format!("invalid MODE: {:?}", raw_mode.trim()))?;
    let mut cfg = AccelConfig {
        mode,
        lut_points: read_lut(store, mode)?,
        ..AccelConfig::default()
    };
    for (name, value, lo, hi) in scalars(&mut cfg) {
        let raw = read_value(store, name)?;
        let encoded = raw
            .trim()
            .parse::<i64>()
            .map_err(|error| format!("invalid {name}: {error}"))?;
        if !(encode_q32(lo)..=encode_q32(hi)).contains(&encoded) {
            return Err(format!("{name} outside supported Q32.32 bounds: {encoded}"));
        }
        *value = encoded as f64 / Q32_SCALE;
    }
    Ok(cfg)
}

pub(super) fn read_config_from_dir(params_dir: &Path) -> Result<AccelConfig, String> {
    let _lock = BACKEND_LOCK
        .lock()
        .map_err(|_| "acceleration backend lock poisoned")?;
    read_config(&Sysfs(params_dir))
}

pub fn read_linux_accel_from_dir(params_dir: &Path) -> Option<AccelConfig> {
    read_config_from_dir(params_dir).ok()
}

struct Write {
    name: &'static str,
    encoded: String,
    previous: String,
}

fn preflight(
    store: &impl ParameterStore,
    cfg: &AccelConfig,
) -> Result<(Vec<Write>, AccelConfig), String> {
    let mut applied = cfg.clone();
    let mut encoded = Vec::new();
    if cfg.mode == AccelMode::Lookup {
        let raw = encode_lut_points_sysfs(&cfg.lut_points)?;
        applied.lut_points = parse_lut_points_sysfs(&raw)?;
        encoded.push(("LUT_POINTS", raw));
    } else {
        applied.lut_points = read_lut(store, cfg.mode)?;
    }
    for (name, value, lo, hi) in scalars(&mut applied) {
        if !(lo..=hi).contains(value) {
            return Err(format!(
                "{name} must be between {lo} and {hi} (got {value})"
            ));
        }
        let raw = encode_q32(*value);
        *value = raw as f64 / Q32_SCALE;
        encoded.push((name, raw.to_string()));
    }
    encoded.push(("MODE", cfg.mode.kernel_index().to_string()));

    let mut plan = Vec::with_capacity(encoded.len());
    for (name, encoded) in encoded {
        let previous = read_value(store, name)?;
        store
            .check_writable(name)
            .map_err(|error| format!("cannot write {name}: {error}"))?;
        plan.push(Write {
            name,
            encoded,
            previous,
        });
    }
    Ok((plan, applied))
}

fn encoded_value(raw: &str) -> &str {
    match raw.strip_suffix('\n') {
        Some(value) => value.strip_suffix('\r').unwrap_or(value),
        None => raw,
    }
}

fn verify(store: &impl ParameterStore, name: &str, expected: &str) -> Result<(), String> {
    let actual = read_value(store, name)?;
    // sysfs getters may append a line ending to the encoded value.
    if encoded_value(&actual) != encoded_value(expected) {
        return Err(format!(
            "{name} readback differs from expected encoded value"
        ));
    }
    Ok(())
}

fn reconcile(store: &mut impl ParameterStore, attempted: &[Write], cause: String) -> String {
    let mut errors = Vec::new();
    for entry in attempted.iter().rev() {
        if let Err(error) = store.write(entry.name, encoded_value(&entry.previous)) {
            errors.push(format!("restore {}: {error}", entry.name));
        }
    }
    for entry in attempted {
        if let Err(error) = verify(store, entry.name, &entry.previous) {
            errors.push(error);
        }
    }
    if errors.is_empty() {
        format!("{cause}. Rollback restored and verified previous values")
    } else {
        format!(
            "{cause}. Rollback left an uncertain live state ({})",
            errors.join(", ")
        )
    }
}

fn apply(store: &mut impl ParameterStore, plan: &[Write]) -> Result<(), String> {
    for (index, entry) in plan.iter().enumerate() {
        if let Err(error) = store.write(entry.name, &entry.encoded) {
            // An I/O error can follow a partial write. Restore the failed entry
            // as well if we cannot confirm it still matches its snapshot.
            let end = if verify(store, entry.name, &entry.previous).is_ok() {
                index
            } else {
                index + 1
            };
            return Err(reconcile(
                store,
                &plan[..end],
                format!("write {}: {error}", entry.name),
            ));
        }
    }
    for entry in plan {
        if let Err(error) = verify(store, entry.name, &entry.encoded) {
            return Err(reconcile(
                store,
                plan,
                format!("apply verification failed: {error}"),
            ));
        }
    }
    Ok(())
}

/// Applies and verifies a Linux configuration with best-effort recovery.
/// The process lock serializes this module's calls. Kernel writes are separate
/// operations and external writers can race them.
pub fn apply_linux_accel_to_dir(
    params_dir: &Path,
    resets_dir: Option<&Path>,
    cfg: &AccelConfig,
) -> Result<AccelConfig, String> {
    let _lock = BACKEND_LOCK
        .lock()
        .map_err(|_| "acceleration backend lock poisoned")?;
    apply_and_persist(&mut Sysfs(params_dir), params_dir, resets_dir, cfg)
}

fn apply_and_persist(
    store: &mut impl ParameterStore,
    params_dir: &Path,
    resets_dir: Option<&Path>,
    cfg: &AccelConfig,
) -> Result<AccelConfig, String> {
    let (plan, applied) =
        preflight(store, cfg).map_err(|error| format!("preflight failed: {error}"))?;
    apply(store, &plan)?;
    if let Some(resets_dir) = resets_dir {
        publish_resets(params_dir, resets_dir, &plan)?;
    }
    Ok(applied)
}

fn publish_resets(params_dir: &Path, resets_dir: &Path, plan: &[Write]) -> Result<(), String> {
    let mut errors = Vec::new();
    for entry in plan.iter().filter(|entry| entry.name != "LUT_POINTS") {
        let name = entry.name;
        let target = params_dir.join(name);
        let target = target.to_string_lossy().replace('\'', "'\\''");
        if let Err(error) = fs::write(
            resets_dir.join(format!("set_last_{name}_value.sh")),
            format!("echo {} > '{target}';", entry.encoded),
        ) {
            errors.push(format!("{name}: {error}"));
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "Live acceleration applied and verified. Reset-script persistence failed ({})",
            errors.join(", ")
        ))
    }
}

#[cfg(test)]
pub(super) fn seed_fixture(params: &Path) {
    let mut cfg = AccelConfig::default();
    for (name, value, _, _) in scalars(&mut cfg) {
        fs::write(params.join(name), encode_q32(*value).to_string()).unwrap();
    }
    fs::write(params.join("MODE"), "0").unwrap();
    fs::write(
        params.join("LUT_POINTS"),
        encode_lut_points_sysfs(&cfg.lut_points).unwrap(),
    )
    .unwrap();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[derive(Default)]
    struct Fake {
        values: HashMap<String, String>,
        writes: Vec<String>,
        fail_at: Vec<usize>,
        partial: bool,
        readback_fault: Option<(&'static str, bool)>,
        ignore_write: Option<usize>,
        unwritable: Option<&'static str>,
    }

    impl ParameterStore for Fake {
        fn read(&self, name: &str) -> io::Result<String> {
            if let Some((parameter, mismatch)) = self.readback_fault {
                if self.writes.len() == ORDER.len() && name == parameter {
                    return if mismatch {
                        Ok("0004\n".into())
                    } else {
                        Err(io::Error::other("injected read error"))
                    };
                }
            }
            self.values
                .get(name)
                .map(|value| format!("{value}\n"))
                .ok_or(io::ErrorKind::NotFound.into())
        }

        fn check_writable(&self, name: &str) -> io::Result<()> {
            if self.unwritable == Some(name) {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            Ok(())
        }

        fn write(&mut self, name: &str, value: &str) -> io::Result<()> {
            let index = self.writes.len();
            let fail = self.fail_at.contains(&index);
            self.writes.push(name.into());
            assert!(
                !value.contains('\n'),
                "kernel setter received a line ending"
            );
            if (!fail || self.partial) && self.ignore_write != Some(index) {
                self.values.insert(name.into(), value.into());
            }
            if fail {
                return Err(io::Error::other("injected write error"));
            }
            Ok(())
        }
    }

    fn fixture() -> (Fake, AccelConfig) {
        let mut cfg = AccelConfig::default();
        let mut store = Fake::default();
        for (name, value, _, _) in scalars(&mut cfg) {
            store
                .values
                .insert(name.into(), encode_q32(*value).to_string());
        }
        store.values.insert("MODE".into(), "0".into());
        store.values.insert(
            "LUT_POINTS".into(),
            encode_lut_points_sysfs(&cfg.lut_points).unwrap(),
        );
        cfg.mode = AccelMode::Lookup;
        cfg.lut_points = vec![[0.0, 0.05], [super::super::LUT_DX_MIN, 10.0]];
        cfg.sens_mult = 0.01;
        cfg.angle_rotation = -180.0;
        (store, cfg)
    }

    const ORDER: [&str; 15] = [
        "LUT_POINTS",
        "SENS_MULT",
        "YX_RATIO",
        "INPUT_DPI",
        "ANGLE_ROTATION",
        "ACCEL",
        "OFFSET",
        "OUTPUT_CAP",
        "DECAY_RATE",
        "LIMIT",
        "GAMMA",
        "SMOOTH",
        "MOTIVITY",
        "SYNC_SPEED",
        "MODE",
    ];

    #[test]
    fn each_write_failure_rolls_back_in_reverse_order() {
        for step in 0..ORDER.len() {
            for partial in [false, true] {
                let (mut store, cfg) = fixture();
                let previous = store.values.clone();
                let (plan, _) = preflight(&store, &cfg).unwrap();
                store.fail_at.push(step);
                store.partial = partial;
                let error = apply(&mut store, &plan).unwrap_err();
                assert!(
                    error.contains(&format!("write {}:", ORDER[step])),
                    "{error}"
                );
                assert!(error.contains("Rollback restored"), "{error}");
                let rollback_end =
                    if partial && plan[step].encoded != encoded_value(&plan[step].previous) {
                        step + 1
                    } else {
                        step
                    };
                let expected: Vec<_> = ORDER[..=step]
                    .iter()
                    .chain(ORDER[..rollback_end].iter().rev())
                    .copied()
                    .collect();
                assert_eq!(store.writes, expected);
                assert_eq!(store.values, previous);
            }
        }
    }

    #[test]
    fn verification_error_and_encoded_mismatch_trigger_full_rollback() {
        for (name, mismatch) in [
            ("MODE", false),
            ("MODE", true),
            ("SENS_MULT", true),
            ("LUT_POINTS", true),
        ] {
            let (mut store, cfg) = fixture();
            let previous = store.values.clone();
            let resets = tempfile::tempdir().unwrap();
            store.readback_fault = Some((name, mismatch));
            let error = apply_and_persist(&mut store, resets.path(), Some(resets.path()), &cfg)
                .unwrap_err();
            assert_eq!(fs::read_dir(resets.path()).unwrap().count(), 0);
            assert!(error.contains(name), "{error}");
            assert!(error.contains("apply verification failed"), "{error}");
            assert!(error.contains("Rollback restored"), "{error}");
            assert_eq!(
                store.writes,
                ORDER
                    .iter()
                    .chain(ORDER.iter().rev())
                    .copied()
                    .collect::<Vec<_>>()
            );
            assert_eq!(store.values, previous);
        }
    }

    #[test]
    fn rollback_write_failure_reports_uncertainty_and_keeps_restoring() {
        let (mut store, cfg) = fixture();
        let (plan, _) = preflight(&store, &cfg).unwrap();
        store.fail_at = vec![2, 3];
        let error = apply(&mut store, &plan).unwrap_err();
        assert!(error.contains("uncertain live state"), "{error}");
        assert!(error.contains("restore SENS_MULT"), "{error}");
        assert_eq!(
            store.writes,
            [
                "LUT_POINTS",
                "SENS_MULT",
                "YX_RATIO",
                "SENS_MULT",
                "LUT_POINTS"
            ]
        );
    }

    #[test]
    fn silently_ignored_restore_leaves_uncertain_live_state() {
        let (mut store, cfg) = fixture();
        let (plan, _) = preflight(&store, &cfg).unwrap();
        store.fail_at.push(2);
        store.ignore_write = Some(3);
        let error = apply(&mut store, &plan).unwrap_err();
        assert!(error.contains("uncertain live state"), "{error}");
        assert!(error.contains("SENS_MULT readback differs"), "{error}");
    }

    #[test]
    fn preflight_rejection_has_no_writes_and_legacy_allows_missing_lut() {
        let (mut store, mut cfg) = fixture();
        store.values.remove("LUT_POINTS");
        assert!(
            preflight(&store, &cfg)
                .err()
                .unwrap()
                .contains("LUT_POINTS")
        );
        cfg.mode = AccelMode::Natural;
        store.unwritable = Some("MODE");
        assert!(
            preflight(&store, &cfg)
                .err()
                .unwrap()
                .contains("cannot write MODE")
        );
        store.unwritable = None;
        let angle = cfg.angle_rotation;
        cfg.angle_rotation = f64::NAN;
        assert!(
            preflight(&store, &cfg)
                .err()
                .unwrap()
                .contains("ANGLE_ROTATION")
        );
        assert!(store.writes.is_empty());
        cfg.angle_rotation = angle;
        let (plan, verified) = preflight(&store, &cfg).unwrap();
        apply(&mut store, &plan).unwrap();
        assert_eq!(store.writes, ORDER[1..]);
        assert_eq!(read_config(&store).unwrap(), verified);
    }

    #[test]
    fn q32_boundaries_round_trip_and_invalid_lut_is_rejected() {
        let (mut store, mut cfg) = fixture();
        for (_, value, lo, _) in scalars(&mut cfg) {
            *value = lo;
        }
        let (plan, expected) = preflight(&store, &cfg).unwrap();
        apply(&mut store, &plan).unwrap();
        assert_eq!(read_config(&store).unwrap(), expected);
        assert_eq!(preflight(&store, &expected).unwrap().1, expected);
        assert_eq!(store.writes, ORDER);
        let spacing = encode_q32(super::super::LUT_DX_MIN);
        assert!(parse_lut_points_sysfs(&format!("0,214748365;{},214748365", spacing - 1)).is_err());
        assert!(parse_lut_points_sysfs(&format!("0,214748364;{spacing},214748365")).is_err());
        assert!(encode_lut_points_sysfs(&[[0.0, 1.0], [0.1 / Q32_SCALE, 1.0]]).is_err());
        for (_, value, _, hi) in scalars(&mut cfg) {
            *value = hi;
        }
        let (plan, expected) = preflight(&store, &cfg).unwrap();
        apply(&mut store, &plan).unwrap();
        assert_eq!(read_config(&store).unwrap(), expected);

        let max_x = encode_q32(1000.0);
        let max_y = encode_q32(10.0);
        assert!(parse_lut_points_sysfs(&format!("0,214748365;{max_x},{max_y}")).is_ok());
        assert!(parse_lut_points_sysfs(&format!("0,214748365;{},{max_y}", max_x + 1)).is_err());
        assert!(parse_lut_points_sysfs(&format!("0,214748365;{max_x},{}", max_y + 1)).is_err());

        let max_points = super::super::LUT_MAX_POINTS;
        let mut points: Vec<_> = (0..max_points).map(|x| [x as f64, 1.0]).collect();
        assert_eq!(
            parse_lut_points_sysfs(&encode_lut_points_sysfs(&points).unwrap()).unwrap(),
            points
        );
        points.push([max_points as f64, 1.0]);
        assert!(encode_lut_points_sysfs(&points).is_err());
        assert!(parse_lut_points_sysfs(&"0".repeat(super::super::LUT_STR_MAX + 1)).is_err());
    }

    #[test]
    fn fallback_reader_surfaces_bad_and_missing_values() {
        for (name, raw) in [
            ("MODE", "unknown"),
            ("ACCEL", "bad"),
            ("LUT_POINTS", "oops"),
            ("INPUT_DPI", "0"),
        ] {
            let (mut store, _) = fixture();
            store.values.insert(name.into(), raw.into());
            assert!(read_config(&store).unwrap_err().contains(name));
        }
        let (mut store, _) = fixture();
        store.values.remove("OFFSET");
        assert!(read_config(&store).unwrap_err().contains("read OFFSET"));
        store.values.insert("OFFSET".into(), "0".into());
        store.values.insert("MODE".into(), "4".into());
        store.values.remove("LUT_POINTS");
        assert!(read_config(&store).unwrap_err().contains("read LUT_POINTS"));
    }

    #[test]
    fn reset_script_errors_preserve_verified_live_values() {
        let params = tempfile::tempdir().unwrap();
        let resets = tempfile::tempdir().unwrap();
        seed_fixture(params.path());
        fs::create_dir(resets.path().join("set_last_SENS_MULT_value.sh")).unwrap();
        let (_, mut cfg) = fixture();
        cfg.sens_mult = f64::NAN;
        let error = apply_linux_accel_to_dir(params.path(), Some(resets.path()), &cfg).unwrap_err();
        assert!(error.contains("preflight failed"), "{error}");
        assert_eq!(fs::read_dir(resets.path()).unwrap().count(), 1);
        assert_eq!(
            read_config_from_dir(params.path()).unwrap().mode,
            AccelMode::Linear
        );

        cfg.sens_mult = 0.01;
        let error = apply_linux_accel_to_dir(params.path(), Some(resets.path()), &cfg).unwrap_err();
        assert!(error.contains("applied and verified"), "{error}");
        assert!(
            error.contains("Reset-script persistence failed (SENS_MULT"),
            "{error}"
        );
        assert_eq!(
            read_linux_accel_from_dir(params.path()).unwrap().mode,
            AccelMode::Lookup
        );
        assert_eq!(
            fs::read_to_string(resets.path().join("set_last_MODE_value.sh")).unwrap(),
            format!("echo 4 > '{}/MODE';", params.path().display())
        );
        assert!(!resets.path().join("set_last_LUT_POINTS_value.sh").exists());
    }
}
