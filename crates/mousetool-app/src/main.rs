//! Lightweight native Mouse Tool GUI (`iced` + `tiny-skia`) matching `web/styles.css`.
//!
//! Runs self-contained in-process via `mousetool-core` (portable mode — zero install,
//! zero files written to disk), with live native HID drivers and kernel acceleration control.

mod curve_canvas;
mod dpi_track;
mod theme;

use curve_canvas::CurveGraph;
use dpi_track::DpiTrack;
use iced::widget::{Space, button, column, container, row, scrollable, slider, text};
use iced::{Alignment, Element, Length, Padding, Task, Theme};
use mousetool_core::RuntimeMode;
use mousetool_core::accel::{
    AccelBackendStatus, AccelConfig, AccelMode, LutPreset, apply_live_accel, detect_accel_backend,
};
use mousetool_core::drivers::{
    LodState, MouseStatus, MouseUpdates, apply_mouse_updates, has_native_driver, read_mouse_status,
};
use mousetool_core::hid::{DetectedMouse, discover_mice};
use mousetool_core::registry::DeviceRegistry;
use theme::{Appearance, MAX_CONTENT_WIDTH, Tokens};

pub fn main() -> iced::Result {
    if std::env::args().any(|a| a == "--probe") {
        run_readonly_probe();
        return Ok(());
    }
    let portable_only = std::env::args().any(|a| a == "--portable");
    iced::application(move || App::new(portable_only), App::update, App::view)
        .title("Mouse Tool")
        .theme(App::theme)
        .window_size((1020.0, 780.0))
        .run()
}

fn run_readonly_probe() {
    let reg = DeviceRegistry::load_embedded().expect("embedded devices.json is valid");
    println!(
        "Registry: {} models, {} profiles, {} matches",
        reg.model_count(),
        reg.profile_count(),
        reg.match_count()
    );
    match discover_mice(&reg) {
        Ok(mice) => {
            println!("Detected mice: {}", mice.len());
            for m in &mice {
                println!(
                    "  - {} [{}] ({}, {}, path={})",
                    m.display_name(),
                    m.classified.driver,
                    m.vid_pid(),
                    m.classified.tier_label(),
                    m.path
                );
                if m.classified.tier > 0 && has_native_driver(&m.classified.driver) {
                    match read_mouse_status(m) {
                        Ok(st) => {
                            println!(
                                "      live read: dpi={:?} active={:?} stages={:?} poll={:?}Hz lod={:?} fw={:?} motion_sync={:?} spdt={:?}",
                                st.dpi,
                                st.active_stage,
                                st.stages,
                                st.polling_hz,
                                st.lod,
                                st.firmware,
                                st.motion_sync,
                                st.spdt
                            );
                        }
                        Err(e) => println!("      live read error: {e}"),
                    }
                }
            }
        }
        Err(e) => println!("HID discovery error: {e}"),
    }
    let accel = detect_accel_backend();
    println!(
        "Accel backend: {} (loaded={}, writable={}, lut_supported={}, mode={:?})",
        accel.label, accel.loaded, accel.writable, accel.lut_supported, accel.active_mode
    );
    if let Some(cfg) = accel.live_config {
        println!(
            "  live accel: mode={} sens_mult={:.3} yx_ratio={:.3} input_dpi={:.0} accel={:.4} cap={:.2} lut_pts={}",
            cfg.mode.wire_name(),
            cfg.sens_mult,
            cfg.yx_ratio,
            cfg.input_dpi,
            cfg.accel,
            cfg.output_cap,
            cfg.lut_points.len()
        );
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tab {
    Sensitivity,
    Buttons,
    Power,
    Dongle,
    Device,
    Acceleration,
}

impl Tab {
    const ALL: [Self; 6] = [
        Self::Sensitivity,
        Self::Buttons,
        Self::Power,
        Self::Dongle,
        Self::Device,
        Self::Acceleration,
    ];

    fn label(self) -> &'static str {
        match self {
            Self::Sensitivity => "Sensitivity",
            Self::Buttons => "Buttons",
            Self::Power => "Power",
            Self::Dongle => "Dongle",
            Self::Device => "Device",
            Self::Acceleration => "Acceleration",
        }
    }
}

#[derive(Debug, Clone)]
enum Message {
    Refresh,
    SelectMouse(usize),
    SelectTab(Tab),
    ToggleAppearance,

    // Mouse Sensitivity / Tuning
    SelectStage(usize),
    DragStageDpi(usize, u32),
    NudgeStageDpi(usize, i32),
    AddStage,
    RemoveStage(usize),
    RevertStages,
    SaveStages,
    SetPollingHz(u32),
    SetLodIndex(u8),
    SetLodPreset(String),
    ToggleMotionSync,
    ToggleAngleSnapping,
    ToggleRippleControl,
    ToggleSlamclick,
    ToggleMotionJitter,
    ToggleLeftHanded,
    SetSpdt(String),

    // Acceleration
    SetAccelMode(AccelMode),
    SetSensMult(f64),
    SetYxRatio(f64),
    SetInputDpi(f64),
    SetAccel(f64),
    SetOffset(f64),
    SetOutputCap(f64),
    SetDecayRate(f64),
    SetLimit(f64),
    SetSyncSpeed(f64),
    SetMotivity(f64),
    SetGamma(f64),
    SetSmooth(f64),
    DragLutPoint(usize, f64, f64),
    AddLutPoint,
    RemoveLutPoint,
    ApplyLutPreset(LutPreset),
    UseMouseDpiForAccel,
    SaveAccel,
    ResetAccelOff,
    RevertAccel,
}

struct App {
    registry: DeviceRegistry,
    runtime_mode: RuntimeMode,
    appearance: Appearance,
    mice: Vec<DetectedMouse>,
    selected_mouse: Option<usize>,
    mouse_status: Option<MouseStatus>,
    mouse_error: Option<String>,
    stage_draft: Vec<(u32, u32)>,
    active_stage_draft: usize,
    stages_dirty: bool,
    status_toast: Option<(String, bool)>,
    hid_error: Option<String>,
    active_tab: Tab,
    accel_backend: AccelBackendStatus,
    accel_live_cfg: AccelConfig,
    accel_cfg: AccelConfig,
}

impl App {
    fn new(_portable_only: bool) -> (Self, Task<Message>) {
        let registry = DeviceRegistry::load_embedded().expect("embedded devices.json is valid");
        let (mice, hid_error) = match discover_mice(&registry) {
            Ok(list) => (list, None),
            Err(err) => (Vec::new(), Some(err)),
        };
        let selected_mouse = (!mice.is_empty()).then_some(0);
        let accel_backend = detect_accel_backend();
        let accel_live_cfg = accel_backend
            .live_config
            .clone()
            .unwrap_or_default();
        let active_tab = if selected_mouse.is_some() {
            Tab::Sensitivity
        } else {
            Tab::Acceleration
        };

        let mut app = Self {
            registry,
            runtime_mode: RuntimeMode::PortableInProcess,
            appearance: Appearance::Dark,
            mice,
            selected_mouse,
            mouse_status: None,
            mouse_error: None,
            stage_draft: Vec::new(),
            active_stage_draft: 1,
            stages_dirty: false,
            status_toast: None,
            hid_error,
            active_tab,
            accel_backend,
            accel_cfg: accel_live_cfg.clone(),
            accel_live_cfg,
        };
        app.reload_selected_mouse();
        (app, Task::none())
    }

    fn can_write_accel(&self) -> bool {
        self.accel_backend.writable && self.accel_backend.live_config.is_some()
    }

    fn theme(&self) -> Theme {
        theme::iced_theme(self.appearance)
    }

    fn current_mouse(&self) -> Option<&DetectedMouse> {
        self.selected_mouse.and_then(|i| self.mice.get(i))
    }

    fn sync_stage_draft_from_status(&mut self) {
        if let Some(st) = &self.mouse_status {
            self.stage_draft = st.stages.clone();
            self.active_stage_draft = st.active_stage.unwrap_or(1);
            self.stages_dirty = false;
        } else {
            self.stage_draft.clear();
            self.active_stage_draft = 1;
            self.stages_dirty = false;
        }
    }

    fn reload_selected_mouse(&mut self) {
        self.mouse_status = None;
        self.mouse_error = None;
        let Some(mouse) = self.current_mouse().cloned() else {
            self.sync_stage_draft_from_status();
            return;
        };
        if mouse.classified.tier == 0 {
            self.sync_stage_draft_from_status();
            return;
        }
        if !has_native_driver(&mouse.classified.driver) {
            self.mouse_error = Some(format!(
                "Native Rust driver for `{}` is queued next; device metadata is shown below.",
                mouse.classified.driver
            ));
            self.sync_stage_draft_from_status();
            return;
        }
        match read_mouse_status(&mouse) {
            Ok(st) => {
                self.mouse_status = Some(st);
                self.sync_stage_draft_from_status();
            }
            Err(err) => {
                self.mouse_error = Some(err);
                self.sync_stage_draft_from_status();
            }
        }
    }

    fn apply_mouse_update(&mut self, updates: MouseUpdates, ok_msg: &str) {
        let Some(mouse) = self.current_mouse().cloned() else {
            return;
        };
        match apply_mouse_updates(&mouse, &updates) {
            Ok(st) => {
                self.mouse_status = Some(st);
                self.mouse_error = None;
                self.sync_stage_draft_from_status();
                self.status_toast = Some((ok_msg.to_string(), false));
            }
            Err(err) => {
                self.status_toast = Some((err, true));
            }
        }
    }

    fn accel_dirty(&self) -> bool {
        self.accel_cfg != self.accel_live_cfg
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Refresh => {
                let prev_id = self.current_mouse().map(|m| m.id.clone());
                match discover_mice(&self.registry) {
                    Ok(list) => {
                        self.mice = list;
                        self.hid_error = None;
                        self.selected_mouse = prev_id
                            .and_then(|id| self.mice.iter().position(|m| m.id == id))
                            .or_else(|| (!self.mice.is_empty()).then_some(0));
                    }
                    Err(err) => {
                        self.hid_error = Some(err);
                    }
                }
                self.reload_selected_mouse();
                self.accel_backend = detect_accel_backend();
                if let Some(live) = self.accel_backend.live_config.clone() {
                    self.accel_live_cfg = live.clone();
                    self.accel_cfg = live;
                }
                self.status_toast =
                    if self.accel_backend.loaded && self.accel_backend.live_config.is_none() {
                        Some((self.accel_backend.detail.clone(), true))
                    } else {
                        Some(("Refreshed live hardware state".into(), false))
                    };
            }
            Message::SelectMouse(idx) => {
                if idx < self.mice.len() {
                    self.selected_mouse = Some(idx);
                    self.status_toast = None;
                    self.reload_selected_mouse();
                }
            }
            Message::SelectTab(tab) => self.active_tab = tab,
            Message::ToggleAppearance => {
                self.appearance = match self.appearance {
                    Appearance::Dark => Appearance::Light,
                    Appearance::Light => Appearance::Dark,
                };
            }

            // Mouse DPI & stages
            Message::SelectStage(stage1) => {
                if stage1 >= 1 && stage1 <= self.stage_draft.len() {
                    self.active_stage_draft = stage1;
                    self.stages_dirty = true;
                }
            }
            Message::DragStageDpi(idx, dpi) => {
                if let Some(slot) = self.stage_draft.get_mut(idx) {
                    *slot = (dpi, dpi);
                    self.stages_dirty = true;
                }
            }
            Message::NudgeStageDpi(idx, delta) => {
                let (lo, hi) = self
                    .mouse_status
                    .as_ref()
                    .map(|s| (s.dpi_min, s.dpi_max))
                    .unwrap_or((100, 30_000));
                if let Some(slot) = self.stage_draft.get_mut(idx) {
                    let next = (slot.0 as i32 + delta).clamp(lo as i32, hi as i32) as u32;
                    *slot = (next, next);
                    self.stages_dirty = true;
                }
            }
            Message::AddStage => {
                let max_stages = self
                    .mouse_status
                    .as_ref()
                    .map(|s| s.max_stages)
                    .unwrap_or(4);
                if self.stage_draft.len() < max_stages {
                    let next_dpi = self
                        .stage_draft
                        .last()
                        .map(|&(x, _)| (x + 800).min(16_000))
                        .unwrap_or(800);
                    self.stage_draft.push((next_dpi, next_dpi));
                    self.stages_dirty = true;
                }
            }
            Message::RemoveStage(idx) => {
                if self.stage_draft.len() > 1 && idx < self.stage_draft.len() {
                    self.stage_draft.remove(idx);
                    if self.active_stage_draft > self.stage_draft.len() {
                        self.active_stage_draft = self.stage_draft.len();
                    }
                    self.stages_dirty = true;
                }
            }
            Message::RevertStages => self.sync_stage_draft_from_status(),
            Message::SaveStages => {
                let updates = MouseUpdates {
                    stages: Some(self.stage_draft.clone()),
                    active_stage: Some(self.active_stage_draft),
                    ..Default::default()
                };
                self.apply_mouse_update(updates, "Saved DPI stages to mouse onboard memory");
            }
            Message::SetPollingHz(hz) => {
                let updates = MouseUpdates {
                    polling_hz: Some(hz),
                    ..Default::default()
                };
                self.apply_mouse_update(updates, &format!("Polling rate set to {hz} Hz"));
            }
            Message::SetLodIndex(idx) => {
                let updates = MouseUpdates {
                    lod_index: Some(idx),
                    ..Default::default()
                };
                self.apply_mouse_update(updates, "Updated lift-off distance");
            }
            Message::SetLodPreset(level) => {
                let updates = MouseUpdates {
                    lod_preset: Some(level.clone()),
                    ..Default::default()
                };
                self.apply_mouse_update(updates, &format!("Lift-off set to {level}"));
            }
            Message::ToggleMotionSync => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.motion_sync) {
                    let updates = MouseUpdates {
                        motion_sync: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Motion Sync");
                }
            }
            Message::ToggleAngleSnapping => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.angle_snapping) {
                    let updates = MouseUpdates {
                        angle_snapping: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Angle Snapping");
                }
            }
            Message::ToggleRippleControl => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.ripple_control) {
                    let updates = MouseUpdates {
                        ripple_control: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Ripple Control");
                }
            }
            Message::ToggleSlamclick => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.slamclick) {
                    let updates = MouseUpdates {
                        slamclick: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Slamclick Filter");
                }
            }
            Message::ToggleMotionJitter => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.motion_jitter) {
                    let updates = MouseUpdates {
                        motion_jitter: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Motion Jitter Filter");
                }
            }
            Message::ToggleLeftHanded => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.left_handed) {
                    let updates = MouseUpdates {
                        left_handed: Some(!cur),
                        ..Default::default()
                    };
                    self.apply_mouse_update(updates, "Updated Left-Handed button swap");
                }
            }
            Message::SetSpdt(mode) => {
                let updates = MouseUpdates {
                    spdt: Some(mode.clone()),
                    ..Default::default()
                };
                self.apply_mouse_update(updates, &format!("SPDT mode set to {mode}"));
            }

            // Acceleration
            Message::SetAccelMode(mode) => self.accel_cfg.mode = mode,
            Message::SetSensMult(v) => self.accel_cfg.sens_mult = (v * 100.0).round() / 100.0,
            Message::SetYxRatio(v) => self.accel_cfg.yx_ratio = (v * 50.0).round() / 50.0,
            Message::SetInputDpi(v) => self.accel_cfg.input_dpi = (v / 50.0).round() * 50.0,
            Message::SetAccel(v) => self.accel_cfg.accel = (v * 200.0).round() / 200.0,
            Message::SetOffset(v) => self.accel_cfg.offset = (v * 4.0).round() / 4.0,
            Message::SetOutputCap(v) => self.accel_cfg.output_cap = (v * 20.0).round() / 20.0,
            Message::SetDecayRate(v) => self.accel_cfg.decay_rate = (v * 1000.0).round() / 1000.0,
            Message::SetLimit(v) => self.accel_cfg.limit = (v * 20.0).round() / 20.0,
            Message::SetSyncSpeed(v) => self.accel_cfg.sync_speed = (v * 20.0).round() / 20.0,
            Message::SetMotivity(v) => self.accel_cfg.motivity = (v * 100.0).round() / 100.0,
            Message::SetGamma(v) => self.accel_cfg.gamma = (v * 20.0).round() / 20.0,
            Message::SetSmooth(v) => self.accel_cfg.smooth = (v * 50.0).round() / 50.0,
            Message::DragLutPoint(idx, x, y) => {
                if idx < self.accel_cfg.lut_points.len() {
                    let lo_x = if idx == 0 {
                        0.0
                    } else {
                        self.accel_cfg.lut_points[idx - 1][0] + 0.5
                    };
                    let hi_x = if idx + 1 == self.accel_cfg.lut_points.len() {
                        60.0
                    } else {
                        self.accel_cfg.lut_points[idx + 1][0] - 0.5
                    };
                    let clamped_x = if idx == 0 {
                        0.0
                    } else {
                        x.clamp(lo_x, hi_x.max(lo_x))
                    };
                    self.accel_cfg.lut_points[idx] = [clamped_x, y];
                }
            }
            Message::AddLutPoint => {
                let pts = &mut self.accel_cfg.lut_points;
                if pts.len() < 64 && pts.len() >= 2 {
                    let n = pts.len();
                    let [x0, y0] = pts[n - 2];
                    let [x1, y1] = pts[n - 1];
                    let mx = ((x0 + x1) * 0.5 * 2.0).round() / 2.0;
                    let my = ((y0 + y1) * 0.5 * 20.0).round() / 20.0;
                    if mx > x0 + 0.1 && mx < x1 - 0.1 {
                        pts.insert(n - 1, [mx, my]);
                    }
                }
            }
            Message::RemoveLutPoint => {
                let pts = &mut self.accel_cfg.lut_points;
                if pts.len() > 2 {
                    pts.remove(pts.len() - 2);
                }
            }
            Message::ApplyLutPreset(preset) => {
                self.accel_cfg.mode = AccelMode::Lookup;
                self.accel_cfg.lut_points = preset.points();
            }
            Message::UseMouseDpiForAccel => {
                if let Some((dpi_x, _)) = self.mouse_status.as_ref().and_then(|s| s.dpi) {
                    self.accel_cfg.input_dpi = dpi_x as f64;
                }
            }
            Message::SaveAccel | Message::ResetAccelOff if !self.can_write_accel() => {
                self.status_toast = Some((self.accel_backend.detail.clone(), true));
            }
            Message::SaveAccel => match apply_live_accel(&self.accel_cfg) {
                Ok(readback) => {
                    self.accel_live_cfg = readback.clone();
                    self.accel_cfg = readback;
                    self.accel_backend = detect_accel_backend();
                    self.status_toast = Some(("Acceleration curve saved to kernel driver".into(), false));
                }
                Err(err) => {
                    self.accel_backend = detect_accel_backend();
                    self.status_toast = Some((err, true));
                }
            },
            Message::ResetAccelOff => {
                let mut off = self.accel_cfg.clone();
                off.mode = AccelMode::NoAccel;
                match apply_live_accel(&off) {
                    Ok(readback) => {
                        self.accel_live_cfg = readback.clone();
                        self.accel_cfg = readback;
                        self.accel_backend = detect_accel_backend();
                        self.status_toast = Some(("Acceleration reset to Off".into(), false));
                    }
                    Err(err) => {
                        self.accel_backend = detect_accel_backend();
                        self.status_toast = Some((err, true));
                    }
                }
            }
            Message::RevertAccel => {
                self.accel_cfg = self.accel_live_cfg.clone();
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Message> {
        let t = theme::tokens(self.appearance);

        let mut body = column![
            self.view_header(t),
            self.view_fleet(t),
            self.view_hero(t),
            self.view_tabs(t),
        ]
        .spacing(14)
        .max_width(MAX_CONTENT_WIDTH)
        .width(Length::Fill);

        if let Some((msg, is_err)) = &self.status_toast {
            let (bg, border, fg) = if *is_err {
                (t.bad_soft, t.bad, t.bad)
            } else {
                (t.ok_soft, t.ok, t.ok_ink)
            };
            body = body.push(
                container(text(msg).size(12.5).color(fg))
                    .padding([8, 12])
                    .width(Length::Fill)
                    .style(move |_| theme::pill_style(bg, border, fg)),
            );
        }

        let panel = match self.active_tab {
            Tab::Sensitivity => self.view_sensitivity_panel(t),
            Tab::Acceleration => self.view_accel_panel(t),
            Tab::Device => self.view_device_panel(t),
            other => self.view_coming_soon_panel(t, other),
        };
        body = body.push(panel);

        let centered = container(body)
            .width(Length::Fill)
            .padding(Padding {
                top: 16.0,
                right: 20.0,
                bottom: 40.0,
                left: 20.0,
            })
            .align_x(Alignment::Center);

        scrollable(centered)
            .width(Length::Fill)
            .height(Length::Fill)
            .into()
    }

    fn view_header(&self, t: Tokens) -> Element<'_, Message> {
        let (conn_text, conn_bg, conn_border, conn_fg) = if let Some(m) = self.current_mouse() {
            (
                format!("{} ({})", m.display_name(), m.vid_pid()),
                t.ok_soft,
                t.ok,
                t.ok_ink,
            )
        } else {
            ("No mouse detected".into(), t.surface_2, t.line_strong, t.muted)
        };

        let mut left = row![
            text("mousetool").size(18).color(t.ink),
            container(text(conn_text).size(12).color(conn_fg))
                .padding([4, 10])
                .style(move |_| theme::pill_style(conn_bg, conn_border, conn_fg)),
        ]
        .spacing(10)
        .align_y(Alignment::Center);

        if let Some(m) = self.current_mouse() {
            let (bg, border, fg) = match m.classified.tier {
                2 => (t.ok_soft, t.ok, t.ok_ink),
                1 => (t.warn_soft, t.warn, t.warn_ink),
                _ => (t.surface_2, t.line_strong, t.muted),
            };
            left = left.push(
                container(text(m.classified.tier_label()).size(12).color(fg))
                    .padding([4, 9])
                    .style(move |_| theme::pill_style(bg, border, fg)),
            );
        }

        let theme_label = match self.appearance {
            Appearance::Dark => "Light",
            Appearance::Light => "Dark",
        };

        let right = row![
            container(
                text(self.runtime_mode.badge_label())
                    .size(11.5)
                    .color(t.muted),
            )
            .padding([4, 8])
            .style(move |_| theme::pill_style(t.surface_2, t.line, t.muted)),
            button(text(theme_label).size(12.5))
                .padding([6, 10])
                .style(theme::ghost_button(t))
                .on_press(Message::ToggleAppearance),
            button(text("Refresh").size(12.5))
                .padding([6, 11])
                .style(theme::primary_button(t))
                .on_press(Message::Refresh),
        ]
        .spacing(8)
        .align_y(Alignment::Center);

        row![left, Space::new().width(Length::Fill), right]
            .align_y(Alignment::Center)
            .into()
    }

    fn view_fleet(&self, t: Tokens) -> Element<'_, Message> {
        if self.mice.is_empty() {
            let msg = self.hid_error.as_deref().unwrap_or(
                "No USB or Bluetooth mouse found yet. Connect a mouse and click Refresh, or tune Acceleration below.",
            );
            return container(text(msg).size(12.5).color(t.muted))
                .padding([10, 14])
                .width(Length::Fill)
                .style(move |_| theme::card_style(t))
                .into();
        }

        let mut bar = row![].spacing(8).align_y(Alignment::Center);
        for (i, m) in self.mice.iter().enumerate() {
            let selected = self.selected_mouse == Some(i);
            let label = format!("{} · {}", m.display_name(), m.classified.proto_tag);
            bar = bar.push(
                button(text(label).size(12.5))
                    .padding([6, 11])
                    .style(theme::secondary_button(t, selected))
                    .on_press(Message::SelectMouse(i)),
            );
        }

        container(bar)
            .padding([8, 10])
            .width(Length::Fill)
            .style(move |_| theme::card_style(t))
            .into()
    }

    fn view_hero(&self, t: Tokens) -> Element<'_, Message> {
        let m = self.current_mouse();
        let st = self.mouse_status.as_ref();

        let dpi_text = st
            .and_then(|s| s.dpi)
            .map(|(x, y)| if x == y { format!("{x}") } else { format!("{x}×{y}") })
            .unwrap_or_else(|| "–".into());
        let dpi_sub = st
            .and_then(|s| s.active_stage.map(|a| format!("Stage {a}/{}", s.stages.len())))
            .unwrap_or_else(|| {
                m.map(|d| format!("max {}", d.classified.dpi_max))
                    .unwrap_or_else(|| "No sensor read".into())
            });

        let poll_text = st
            .and_then(|s| s.polling_hz)
            .map(|hz| format!("{hz} Hz"))
            .unwrap_or_else(|| "–".into());
        let poll_sub = st
            .and_then(|s| s.lod.as_ref())
            .map(|l| match l {
                LodState::Preset(p) => format!("LOD: {p}"),
                LodState::Index { label, .. } => format!("LOD: {label}"),
                LodState::Async { lift, land } => format!("LOD: {lift}/{land}"),
            })
            .unwrap_or_else(|| "USB report rate".into());

        let batt_or_accel_k = if st.and_then(|s| s.battery_percent).is_some() {
            "Battery"
        } else {
            "Acceleration"
        };
        let batt_or_accel_v = if let Some(pct) = st.and_then(|s| s.battery_percent) {
            format!("{pct}%")
        } else {
            self.accel_cfg.mode.label().into()
        };
        let batt_or_accel_s = if let Some(chg) = st.and_then(|s| s.charging) {
            if chg { "Charging".into() } else { "Wireless".into() }
        } else {
            self.accel_backend.label.clone()
        };

        let device_text = m
            .map(|d| d.display_name().to_string())
            .unwrap_or_else(|| "None".into());
        let link_text = st
            .and_then(|s| s.firmware.as_ref())
            .map(|fw| format!("{fw} · {}", m.map(|d| d.classified.link.as_str()).unwrap_or("")))
            .unwrap_or_else(|| {
                m.map(|d| format!("{} · {}", d.classified.link, d.vid_pid()))
                    .unwrap_or_else(|| "Portable native core".into())
            });

        let tile = |k: &'static str, v: String, sub: String| {
            container(
                column![
                    text(k).size(11.5).color(t.muted),
                    text(v).size(20).color(t.ink),
                    text(sub).size(11.5).color(t.faint),
                ]
                .spacing(4),
            )
            .padding([12, 14])
            .width(Length::FillPortion(1))
            .style(move |_| theme::card_style(t))
        };

        row![
            tile("DPI", dpi_text, dpi_sub),
            tile("Polling rate", poll_text, poll_sub),
            tile(batt_or_accel_k, batt_or_accel_v, batt_or_accel_s),
            tile("Device", device_text, link_text),
        ]
        .spacing(10)
        .into()
    }

    fn view_tabs(&self, t: Tokens) -> Element<'_, Message> {
        let mut bar = row![].spacing(6);
        for tab in Tab::ALL {
            bar = bar.push(
                button(text(tab.label()).size(13))
                    .padding([7, 13])
                    .style(theme::tab_button(t, self.active_tab == tab))
                    .on_press(Message::SelectTab(tab)),
            );
        }
        container(bar)
            .padding([6, 8])
            .width(Length::Fill)
            .style(move |_| theme::inset_style(t))
            .into()
    }

    fn view_sensitivity_panel(&self, t: Tokens) -> Element<'_, Message> {
        let Some(mouse) = self.current_mouse() else {
            return container(
                text("Connect a mouse and click Refresh to configure DPI, polling rate, LOD, and sensor tuning.")
                    .size(12.5)
                    .color(t.muted),
            )
            .padding(16)
            .width(Length::Fill)
            .style(move |_| theme::card_style(t))
            .into();
        };

        let mut sections = column![].spacing(12);

        if let Some(err) = &self.mouse_error {
            sections = sections.push(
                container(text(err).size(12.5).color(t.warn_ink))
                    .padding([10, 14])
                    .width(Length::Fill)
                    .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
            );
        }

        if let Some(hint) = &mouse.permission_hint {
            sections = sections.push(
                container(text(hint).size(12.5).color(t.warn_ink))
                    .padding([10, 14])
                    .width(Length::Fill)
                    .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
            );
        }

        let Some(st) = &self.mouse_status else {
            let note = if mouse.classified.tier == 0 {
                mouse.classified.status_note.as_str()
            } else {
                "Waiting for live hardware readout. Click Refresh once permissions are ready."
            };
            sections = sections.push(
                container(text(note).size(12.5).color(t.muted))
                    .padding(16)
                    .width(Length::Fill)
                    .style(move |_| theme::card_style(t)),
            );
            return sections.into();
        };

        let writable = mouse.classified.tier >= 2 && mouse.classified.write_enabled;

        // 01.1 DPI Stages
        if !self.stage_draft.is_empty() {
            let track = DpiTrack {
                stages: self.stage_draft.clone(),
                active_stage: self.active_stage_draft,
                dpi_min: st.dpi_min,
                dpi_max: st.dpi_max,
                dpi_step: st.dpi_step,
                writable,
                tokens: t,
                on_select: Message::SelectStage,
                on_drag_dpi: Message::DragStageDpi,
            }
            .view();

            let mut stage_cards = row![].spacing(8);
            for (i, &(x, _y)) in self.stage_draft.iter().enumerate() {
                let is_active = i + 1 == self.active_stage_draft;
                let step = st.dpi_step.max(50) as i32;
                let mut card_col = column![
                    row![
                        text(format!("Stage {}", i + 1))
                            .size(11.5)
                            .color(if is_active { t.accent } else { t.muted }),
                        Space::new().width(Length::Fill),
                        text(if is_active { "Active" } else { "" })
                            .size(11)
                            .color(t.accent),
                    ],
                    text(format!("{x} DPI")).size(17).color(t.ink),
                ]
                .spacing(6);

                if writable {
                    let mut btns = row![
                        button(text("-").size(12))
                            .padding([3, 8])
                            .style(theme::secondary_button(t, false))
                            .on_press(Message::NudgeStageDpi(i, -step)),
                        button(text("+").size(12))
                            .padding([3, 8])
                            .style(theme::secondary_button(t, false))
                            .on_press(Message::NudgeStageDpi(i, step)),
                        button(text(if is_active { "Selected" } else { "Activate" }).size(11.5))
                            .padding([3, 8])
                            .style(theme::secondary_button(t, is_active))
                            .on_press(Message::SelectStage(i + 1)),
                    ]
                    .spacing(4);
                    if self.stage_draft.len() > 1 {
                        btns = btns.push(
                            button(text("×").size(12))
                                .padding([3, 7])
                                .style(theme::ghost_button(t))
                                .on_press(Message::RemoveStage(i)),
                        );
                    }
                    card_col = card_col.push(btns);
                }

                stage_cards = stage_cards.push(
                    container(card_col)
                        .padding([10, 12])
                        .width(Length::FillPortion(1))
                        .style(move |_| {
                            if is_active {
                                theme::pill_style(t.surface, t.accent, t.ink)
                            } else {
                                theme::card_style(t)
                            }
                        }),
                );
            }

            let mut foot = row![
                text(format!(
                    "Drag markers on the scale · up to {} stages · {}–{} DPI",
                    st.max_stages, st.dpi_min, st.dpi_max
                ))
                .size(11.5)
                .color(t.muted),
                Space::new().width(Length::Fill),
            ]
            .spacing(8)
            .align_y(Alignment::Center);

            if writable && self.stage_draft.len() < st.max_stages {
                foot = foot.push(
                    button(text("+ Add stage").size(12))
                        .padding([5, 10])
                        .style(theme::ghost_button(t))
                        .on_press(Message::AddStage),
                );
            }
            if writable && self.stages_dirty {
                foot = foot
                    .push(
                        container(text("Unsaved").size(11.5).color(t.warn_ink))
                            .padding([4, 8])
                            .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
                    )
                    .push(
                        button(text("Revert").size(12))
                            .padding([5, 10])
                            .style(theme::ghost_button(t))
                            .on_press(Message::RevertStages),
                    )
                    .push(
                        button(text("Save stages").size(12))
                            .padding([5, 12])
                            .style(theme::primary_button(t))
                            .on_press(Message::SaveStages),
                    );
            }

            sections = sections.push(
                container(
                    column![
                        row![
                            text("01.1").size(11.5).color(t.faint),
                            text("DPI stages").size(15).color(t.ink),
                            Space::new().width(Length::Fill),
                            text(if writable {
                                "Stored directly in onboard mouse memory"
                            } else {
                                "Read-only safety lock"
                            })
                            .size(12)
                            .color(t.muted),
                        ]
                        .spacing(8)
                        .align_y(Alignment::Center),
                        track,
                        stage_cards,
                        foot,
                    ]
                    .spacing(12),
                )
                .padding(16)
                .width(Length::Fill)
                .style(move |_| theme::card_style(t)),
            );
        }

        // 01.2 Polling Rate & 01.3 LOD
        if !st.supported_poll_rates.is_empty() || st.lod.is_some() {
            let mut card = column![
                row![
                    text("01.2").size(11.5).color(t.faint),
                    text("Tracking & Lift-Off").size(15).color(t.ink),
                ]
                .spacing(8)
                .align_y(Alignment::Center),
            ]
            .spacing(12);

            if !st.supported_poll_rates.is_empty() {
                let mut poll_btns = row![].spacing(6);
                for &hz in &st.supported_poll_rates {
                    let active = st.polling_hz == Some(hz);
                    let mut b = button(text(format!("{hz} Hz")).size(12.5))
                        .padding([6, 11])
                        .style(theme::secondary_button(t, active));
                    if writable {
                        b = b.on_press(Message::SetPollingHz(hz));
                    }
                    poll_btns = poll_btns.push(b);
                }
                card = card.push(
                    row![
                        column![
                            text("Polling rate").size(13).color(t.ink),
                            text("How often the mouse reports position to your PC.")
                                .size(11.5)
                                .color(t.muted),
                        ]
                        .spacing(2),
                        Space::new().width(Length::Fill),
                        poll_btns,
                    ]
                    .align_y(Alignment::Center),
                );
            }

            if let Some(lod) = &st.lod {
                let mut lod_btns = row![].spacing(6);
                match lod {
                    LodState::Index { index, options, .. } => {
                        for (i, label) in options.iter().enumerate() {
                            let active = *index as usize == i;
                            let mut b = button(text(label.clone()).size(12.5))
                                .padding([6, 11])
                                .style(theme::secondary_button(t, active));
                            if writable {
                                b = b.on_press(Message::SetLodIndex(i as u8));
                            }
                            lod_btns = lod_btns.push(b);
                        }
                    }
                    LodState::Preset(cur) => {
                        for level in ["low", "medium", "high"] {
                            let active = cur.eq_ignore_ascii_case(level);
                            let mut b = button(text(level).size(12.5))
                                .padding([6, 11])
                                .style(theme::secondary_button(t, active));
                            if writable {
                                b = b.on_press(Message::SetLodPreset(level.into()));
                            }
                            lod_btns = lod_btns.push(b);
                        }
                    }
                    LodState::Async { lift, land } => {
                        lod_btns = lod_btns.push(
                            text(format!("Asymmetric lift {lift} / land {land}"))
                                .size(12.5)
                                .color(t.ink_2),
                        );
                    }
                }
                card = card.push(
                    row![
                        column![
                            text("Lift-off distance").size(13).color(t.ink),
                            text("Tracking cutoff height when lifting the mouse.")
                                .size(11.5)
                                .color(t.muted),
                        ]
                        .spacing(2),
                        Space::new().width(Length::Fill),
                        lod_btns,
                    ]
                    .align_y(Alignment::Center),
                );
            }

            sections = sections.push(
                container(card)
                    .padding(16)
                    .width(Length::Fill)
                    .style(move |_| theme::card_style(t)),
            );
        }

        // 01.3 Sensor & Click Tuning (capability-driven)
        if st.motion_sync.is_some()
            || st.angle_snapping.is_some()
            || st.ripple_control.is_some()
            || st.slamclick.is_some()
            || st.spdt.is_some()
        {
            let mut tune = column![
                row![
                    text("01.3").size(11.5).color(t.faint),
                    text("Sensor & click tuning").size(15).color(t.ink),
                ]
                .spacing(8)
                .align_y(Alignment::Center),
            ]
            .spacing(10);

            if let Some(v) = st.motion_sync {
                tune = tune.push(toggle_row(
                    t,
                    "Motion Sync",
                    "Synchronizes sensor frames to USB polling intervals.",
                    v,
                    writable.then_some(Message::ToggleMotionSync),
                ));
            }
            if let Some(v) = st.angle_snapping {
                tune = tune.push(toggle_row(
                    t,
                    "Angle Snapping",
                    "Predicts straight horizontal/vertical lines.",
                    v,
                    writable.then_some(Message::ToggleAngleSnapping),
                ));
            }
            if let Some(v) = st.ripple_control {
                tune = tune.push(toggle_row(
                    t,
                    "Ripple Control",
                    "Smooths high-DPI jitter at the cost of slight latency.",
                    v,
                    writable.then_some(Message::ToggleRippleControl),
                ));
            }
            if let Some(v) = st.slamclick {
                tune = tune.push(toggle_row(
                    t,
                    "Slamclick Filter",
                    "Prevents accidental clicks when setting the mouse down.",
                    v,
                    writable.then_some(Message::ToggleSlamclick),
                ));
            }
            if let Some(v) = st.motion_jitter {
                tune = tune.push(toggle_row(
                    t,
                    "Motion Jitter Filter",
                    "Filters micro-jitter from optical sensor noise.",
                    v,
                    writable.then_some(Message::ToggleMotionJitter),
                ));
            }
            if let Some(v) = st.left_handed {
                tune = tune.push(toggle_row(
                    t,
                    "Left-handed swap",
                    "Swaps primary Left and Right mouse buttons in hardware.",
                    v,
                    writable.then_some(Message::ToggleLeftHanded),
                ));
            }
            if let Some(spdt) = &st.spdt {
                let mut spdt_btns = row![].spacing(6);
                for (id, label) in [
                    ("off", "Off"),
                    ("gx_safe", "GX Safe"),
                    ("gx_speed", "GX Speed"),
                ] {
                    let active = spdt == id;
                    let mut b = button(text(label).size(12.5))
                        .padding([6, 11])
                        .style(theme::secondary_button(t, active));
                    if writable {
                        b = b.on_press(Message::SetSpdt(id.into()));
                    }
                    spdt_btns = spdt_btns.push(b);
                }
                tune = tune.push(
                    row![
                        column![
                            text("GX SPDT click mode").size(13).color(t.ink),
                            text("Ultra-low-latency SPDT switch processing.")
                                .size(11.5)
                                .color(t.muted),
                        ]
                        .spacing(2),
                        Space::new().width(Length::Fill),
                        spdt_btns,
                    ]
                    .align_y(Alignment::Center),
                );
            }

            sections = sections.push(
                container(tune)
                    .padding(16)
                    .width(Length::Fill)
                    .style(move |_| theme::card_style(t)),
            );
        }

        sections.into()
    }

    fn view_accel_panel(&self, t: Tokens) -> Element<'_, Message> {
        let mut modes = row![].spacing(6);
        for mode in AccelMode::ALL {
            modes = modes.push(
                button(text(mode.label()).size(12.5))
                    .padding([6, 12])
                    .style(theme::secondary_button(t, self.accel_cfg.mode == mode))
                    .on_press(Message::SetAccelMode(mode)),
            );
        }

        let graph = CurveGraph::new(self.accel_cfg.clone(), t, Message::DragLutPoint).view();

        let mut controls = column![
            row![
                text("06.1").size(11.5).color(t.faint),
                text("Acceleration curve").size(15).color(t.ink),
                Space::new().width(Length::Fill),
                text(&self.accel_backend.detail).size(12).color(
                    if self.accel_backend.loaded && self.accel_backend.live_config.is_none() {
                        t.bad
                    } else {
                        t.muted
                    }
                ),
            ]
            .spacing(8)
            .align_y(Alignment::Center),
            modes,
            text(self.accel_cfg.mode.description())
                .size(12.5)
                .color(t.muted),
            graph,
        ]
        .spacing(12);

        match self.accel_cfg.mode {
            AccelMode::Linear => {
                controls = controls
                    .push(slider_row(
                        t,
                        "Accel",
                        self.accel_cfg.accel,
                        0.0..=0.5,
                        0.005,
                        format!("{:.3}", self.accel_cfg.accel),
                        Message::SetAccel,
                    ))
                    .push(slider_row(
                        t,
                        "Offset",
                        self.accel_cfg.offset,
                        0.0..=40.0,
                        0.25,
                        format!("{:.2}", self.accel_cfg.offset),
                        Message::SetOffset,
                    ))
                    .push(slider_row(
                        t,
                        "Output cap",
                        self.accel_cfg.output_cap,
                        0.0..=6.0,
                        0.05,
                        format!("{:.2}×", self.accel_cfg.output_cap),
                        Message::SetOutputCap,
                    ));
            }
            AccelMode::Natural => {
                controls = controls
                    .push(slider_row(
                        t,
                        "Decay rate",
                        self.accel_cfg.decay_rate,
                        0.001..=1.0,
                        0.001,
                        format!("{:.3}", self.accel_cfg.decay_rate),
                        Message::SetDecayRate,
                    ))
                    .push(slider_row(
                        t,
                        "Offset",
                        self.accel_cfg.offset,
                        0.0..=40.0,
                        0.25,
                        format!("{:.2}", self.accel_cfg.offset),
                        Message::SetOffset,
                    ))
                    .push(slider_row(
                        t,
                        "Limit",
                        self.accel_cfg.limit,
                        1.0..=6.0,
                        0.05,
                        format!("{:.2}×", self.accel_cfg.limit),
                        Message::SetLimit,
                    ));
            }
            AccelMode::Synchronous => {
                controls = controls
                    .push(slider_row(
                        t,
                        "Sync speed",
                        self.accel_cfg.sync_speed,
                        0.1..=40.0,
                        0.05,
                        format!("{:.2}", self.accel_cfg.sync_speed),
                        Message::SetSyncSpeed,
                    ))
                    .push(slider_row(
                        t,
                        "Motivity",
                        self.accel_cfg.motivity,
                        1.01..=6.0,
                        0.01,
                        format!("{:.2}×", self.accel_cfg.motivity),
                        Message::SetMotivity,
                    ))
                    .push(slider_row(
                        t,
                        "Gamma",
                        self.accel_cfg.gamma,
                        0.05..=5.0,
                        0.05,
                        format!("{:.2}", self.accel_cfg.gamma),
                        Message::SetGamma,
                    ))
                    .push(slider_row(
                        t,
                        "Smooth",
                        self.accel_cfg.smooth,
                        0.0..=1.0,
                        0.02,
                        format!("{:.2}", self.accel_cfg.smooth),
                        Message::SetSmooth,
                    ));
            }
            AccelMode::Lookup => {
                let mut preset_row = row![
                    text(format!("Control points: {}/64 · Presets:", self.accel_cfg.lut_points.len()))
                        .size(12.5)
                        .color(t.muted),
                ]
                .spacing(6)
                .align_y(Alignment::Center);
                for preset in LutPreset::ALL {
                    preset_row = preset_row.push(
                        button(text(preset.label()).size(12))
                            .padding([5, 10])
                            .style(theme::secondary_button(t, false))
                            .on_press(Message::ApplyLutPreset(preset)),
                    );
                }
                preset_row = preset_row
                    .push(Space::new().width(Length::Fill))
                    .push(
                        button(text("+ Point").size(12))
                            .padding([5, 9])
                            .style(theme::ghost_button(t))
                            .on_press(Message::AddLutPoint),
                    )
                    .push(
                        button(text("− Point").size(12))
                            .padding([5, 9])
                            .style(theme::ghost_button(t))
                            .on_press(Message::RemoveLutPoint),
                    );
                controls = controls.push(preset_row);
            }
            AccelMode::NoAccel => {}
        }

        // Global curve scaling parameters
        controls = controls
            .push(slider_row(
                t,
                "Sensitivity multiplier",
                self.accel_cfg.sens_mult,
                0.05..=5.0,
                0.01,
                format!("{:.2}×", self.accel_cfg.sens_mult),
                Message::SetSensMult,
            ))
            .push(slider_row(
                t,
                "Y / X ratio",
                self.accel_cfg.yx_ratio,
                0.2..=3.0,
                0.02,
                format!("{:.2}", self.accel_cfg.yx_ratio),
                Message::SetYxRatio,
            ))
            .push(slider_row(
                t,
                "Mouse DPI",
                self.accel_cfg.input_dpi,
                100.0..=16_000.0,
                50.0,
                format!("{:.0}", self.accel_cfg.input_dpi),
                Message::SetInputDpi,
            ));

        // Action footer (Use mouse DPI, Revert, Reset to Off, Save curve)
        let mut actions = row![].spacing(8).align_y(Alignment::Center);
        if self.mouse_status.as_ref().and_then(|s| s.dpi).is_some() {
            actions = actions.push(
                button(text("Use mouse DPI").size(12))
                    .padding([6, 10])
                    .style(theme::ghost_button(t))
                    .on_press(Message::UseMouseDpiForAccel),
            );
        }
        actions = actions.push(Space::new().width(Length::Fill));

        if self.accel_dirty() {
            actions = actions
                .push(
                    container(text("Unsaved").size(11.5).color(t.warn_ink))
                        .padding([4, 8])
                        .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
                )
                .push(
                    button(text("Revert").size(12.5))
                        .padding([6, 10])
                        .style(theme::ghost_button(t))
                        .on_press(Message::RevertAccel),
                );
        }

        if self.accel_backend.writable
            || (self.accel_backend.loaded && self.accel_backend.live_config.is_none())
        {
            let can_write = self.can_write_accel();
            let disabled_style = move |theme: &Theme, status| {
                let mut style = theme::secondary_button(t, false)(theme, status);
                style.text_color = t.muted;
                style
            };
            actions = actions
                .push(
                    button(text("Reset to Off").size(12.5))
                        .padding([6, 11])
                        .style(move |theme, status| {
                            if can_write {
                                theme::secondary_button(t, false)(theme, status)
                            } else {
                                disabled_style(theme, status)
                            }
                        })
                        .on_press_maybe(can_write.then_some(Message::ResetAccelOff)),
                )
                .push(
                    button(text("Save curve").size(12.5))
                        .padding([6, 14])
                        .style(move |theme, status| {
                            if can_write {
                                theme::primary_button(t)(theme, status)
                            } else {
                                disabled_style(theme, status)
                            }
                        })
                        .on_press_maybe(can_write.then_some(Message::SaveAccel)),
                );
        } else {
            actions = actions.push(
                text("Read-only / Preview")
                    .size(12)
                    .color(t.muted),
            );
        }

        controls = controls.push(actions);

        container(controls)
            .padding(16)
            .width(Length::Fill)
            .style(move |_| theme::card_style(t))
            .into()
    }

    fn view_device_panel(&self, t: Tokens) -> Element<'_, Message> {
        let mut content = column![
            row![
                text("05.1").size(11.5).color(t.faint),
                text("Device & Registry Inspector").size(15).color(t.ink),
                Space::new().width(Length::Fill),
                text(format!(
                    "Embedded registry: {} models · {} profiles · {} matches",
                    self.registry.model_count(),
                    self.registry.profile_count(),
                    self.registry.match_count()
                ))
                .size(12)
                .color(t.muted),
            ]
            .spacing(8)
            .align_y(Alignment::Center),
        ]
        .spacing(12);

        if let Some(m) = self.current_mouse() {
            let mut details = column![
                kv_row(t, "Model", m.display_name().to_string()),
                kv_row(t, "USB / HID ID", m.vid_pid()),
                kv_row(t, "Support Tier", m.classified.tier_label().to_string()),
                kv_row(
                    t,
                    "Driver / Protocol",
                    format!("{} ({})", m.classified.proto_label, m.classified.driver),
                ),
                kv_row(t, "Connection", m.classified.link.clone()),
                kv_row(t, "Device Path", m.path.clone()),
            ]
            .spacing(6);

            if let Some(fw) = self.mouse_status.as_ref().and_then(|s| s.firmware.as_ref()) {
                details = details.push(kv_row(t, "Firmware", fw.clone()));
            }
            if let Some(sn) = self.mouse_status.as_ref().and_then(|s| s.serial.as_ref()) {
                details = details.push(kv_row(t, "Serial", sn.clone()));
            }

            if let Some(hint) = &m.permission_hint {
                details = details.push(
                    container(text(hint).size(12).color(t.warn_ink))
                        .padding([8, 10])
                        .width(Length::Fill)
                        .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
                );
            }

            if !m.classified.status_note.is_empty() {
                details = details.push(text(&m.classified.status_note).size(12).color(t.muted));
            }
            content = content.push(details);
        } else {
            content = content.push(
                text("No HID mice detected yet. Plug in a mouse and click Refresh.")
                    .size(12.5)
                    .color(t.muted),
            );
        }

        content = content.push(
            container(
                text("Portable mode active: running in-process via mousetool-core with zero installation and zero files written to disk.")
                    .size(12)
                    .color(t.muted),
            )
            .padding([8, 12])
            .width(Length::Fill)
            .style(move |_| theme::inset_style(t)),
        );

        container(content)
            .padding(16)
            .width(Length::Fill)
            .style(move |_| theme::card_style(t))
            .into()
    }

    fn view_coming_soon_panel(&self, t: Tokens, tab: Tab) -> Element<'_, Message> {
        let desc = format!(
            "{} controls are queued next as the remaining protocol drivers (CompX, Logitech HID++, Pulsar, Glorious) are ported into mousetool-core alongside Razer and Endgame Gear 8k.",
            tab.label()
        );
        container(
            column![
                text(tab.label()).size(15).color(t.ink),
                text(desc).size(12.5).color(t.muted),
            ]
            .spacing(8),
        )
        .padding(16)
        .width(Length::Fill)
        .style(move |_| theme::card_style(t))
        .into()
    }
}

fn slider_row<'a>(
    t: Tokens,
    label: &'static str,
    value: f64,
    range: std::ops::RangeInclusive<f64>,
    step: f64,
    formatted: String,
    on_change: fn(f64) -> Message,
) -> Element<'a, Message> {
    row![
        text(label)
            .size(12.5)
            .color(t.ink_2)
            .width(Length::Fixed(160.0)),
        slider(range, value, on_change)
            .step(step)
            .style(theme::accent_slider(t))
            .width(Length::Fill),
        text(formatted)
            .size(12.5)
            .color(t.ink)
            .width(Length::Fixed(64.0)),
    ]
    .spacing(12)
    .align_y(Alignment::Center)
    .into()
}

fn toggle_row<'a>(
    t: Tokens,
    title: &'static str,
    desc: &'static str,
    enabled: bool,
    on_press: Option<Message>,
) -> Element<'a, Message> {
    let mut btn = button(text(if enabled { "On" } else { "Off" }).size(12.5))
        .padding([6, 14])
        .style(theme::secondary_button(t, enabled));
    if let Some(msg) = on_press {
        btn = btn.on_press(msg);
    }
    row![
        column![
            text(title).size(13).color(t.ink),
            text(desc).size(11.5).color(t.muted),
        ]
        .spacing(2),
        Space::new().width(Length::Fill),
        btn,
    ]
    .align_y(Alignment::Center)
    .into()
}

fn kv_row<'a>(t: Tokens, key: &'static str, val: String) -> Element<'a, Message> {
    row![
        text(key)
            .size(12.5)
            .color(t.muted)
            .width(Length::Fixed(160.0)),
        text(val).size(12.5).color(t.ink),
    ]
    .spacing(12)
    .align_y(Alignment::Center)
    .into()
}
