//! Lightweight native Mouse Tool GUI (`iced` + `tiny-skia`) matching `web/styles.css`.
//!
//! Runs self-contained in-process via `mousetool-core` (portable mode — zero install,
//! zero files written to disk), with live native HID drivers and kernel acceleration control.

mod curve_canvas;
mod device_io;
mod dpi_track;
mod theme;

use curve_canvas::CurveGraph;
use device_io::{DeviceKey, Job, Outcome, Reply, Tag, Worker};
use dpi_track::DpiTrack;
use iced::widget::{Space, button, column, container, row, scrollable, slider, text};
use iced::{Alignment, Element, Length, Padding, Task, Theme};
use mousetool_core::RuntimeMode;
use mousetool_core::accel::{
    AccelBackendStatus, AccelConfig, AccelMode, LutPreset, apply_live_accel, detect_accel_backend,
};
use mousetool_core::drivers::{LodState, MouseStatus, MouseUpdates, has_native_driver};
use mousetool_core::hid::DetectedMouse;
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
    let worker = Worker::native(reg.into());
    let run = |device, sequence, job| {
        iced::futures::executor::block_on(worker.submit(
            Tag {
                device,
                generation: 0,
                sequence,
            },
            job,
        ))
        .outcome
    };
    match run(None, 0, Job::Discover) {
        Outcome::Fleet(mice) => {
            println!("Detected mice: {}", mice.len());
            for (i, m) in mice.iter().enumerate() {
                println!(
                    "  - {} [{}] ({}, {})",
                    m.display_name(),
                    m.classified.driver,
                    m.vid_pid(),
                    m.classified.tier_label()
                );
                if m.classified.tier > 0 && has_native_driver(&m.classified.driver) {
                    match run(Some(DeviceKey::of(m)), i as u64 + 1, Job::Read(m.clone())) {
                        Outcome::Readback { status: st, .. } => {
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
                        _ => println!("      live read unavailable"),
                    }
                }
            }
        }
        _ => println!("HID discovery failed"),
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
    DeviceReply(Reply),
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DeviceState {
    Ready,
    Busy,
    Disconnected,
    DiscoveryFailed,
    Stale { after_write: bool },
    WorkerUnavailable,
}

impl DeviceState {
    fn presentation(self, t: Tokens) -> (&'static str, iced::Color, iced::Color, iced::Color) {
        match self {
            DeviceState::Ready => ("Connected / Ready", t.ok_soft, t.ok, t.ok_ink),
            DeviceState::Busy => ("Device busy", t.warn_soft, t.warn, t.warn_ink),
            DeviceState::Disconnected => ("Disconnected", t.surface_2, t.line_strong, t.ink),
            DeviceState::DiscoveryFailed => ("Discovery failed", t.warn_soft, t.warn, t.warn_ink),
            DeviceState::Stale { after_write: true } => (
                "Stale / Write outcome unknown",
                t.warn_soft,
                t.warn,
                t.warn_ink,
            ),
            DeviceState::Stale { after_write: false } => {
                ("Stale / Readback failed", t.warn_soft, t.warn, t.warn_ink)
            }
            DeviceState::WorkerUnavailable => {
                ("Device worker unavailable", t.warn_soft, t.warn, t.warn_ink)
            }
        }
    }
}

struct App {
    worker: Worker,
    registry: std::sync::Arc<DeviceRegistry>,
    device_state: DeviceState,
    generation: u64,
    sequence: u64,
    pending: Option<Tag>,
    discover_requested: bool,
    discovering: bool,
    draft_device: Option<DeviceKey>,
    write_uncertain: bool,
    runtime_mode: RuntimeMode,
    appearance: Appearance,
    mice: Vec<DetectedMouse>,
    selected_mouse: Option<usize>,
    mouse_status: Option<MouseStatus>,
    stage_draft: Vec<(u32, u32)>,
    active_stage_draft: usize,
    stages_dirty: bool,
    status_toast: Option<(String, bool)>,
    active_tab: Tab,
    tab_chosen: bool,
    accel_backend: AccelBackendStatus,
    accel_live_cfg: AccelConfig,
    accel_cfg: AccelConfig,
}

impl App {
    fn new(_portable_only: bool) -> (Self, Task<Message>) {
        let registry = std::sync::Arc::new(
            DeviceRegistry::load_embedded().expect("embedded devices.json is valid"),
        );
        Self::with_worker(
            Worker::native(registry.clone()),
            registry,
            detect_accel_backend(),
        )
    }

    fn with_worker(
        worker: Worker,
        registry: std::sync::Arc<DeviceRegistry>,
        accel_backend: AccelBackendStatus,
    ) -> (Self, Task<Message>) {
        let accel_live_cfg = accel_backend.live_config.clone().unwrap_or_default();
        let mut app = Self {
            worker,
            registry,
            device_state: DeviceState::Busy,
            generation: 0,
            sequence: 0,
            pending: None,
            discover_requested: true,
            discovering: false,
            draft_device: None,
            write_uncertain: false,
            runtime_mode: RuntimeMode::PortableInProcess,
            appearance: Appearance::Dark,
            mice: Vec::new(),
            selected_mouse: None,
            mouse_status: None,
            stage_draft: Vec::new(),
            active_stage_draft: 1,
            stages_dirty: false,
            status_toast: None,
            active_tab: Tab::Sensitivity,
            tab_chosen: false,
            accel_backend,
            accel_cfg: accel_live_cfg.clone(),
            accel_live_cfg,
        };
        let task = app.schedule_device_job();
        (app, task)
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

    fn device_key(&self) -> Option<DeviceKey> {
        self.current_mouse().map(DeviceKey::of)
    }

    fn dispatch(&mut self, job: Job) -> Task<Message> {
        self.sequence += 1;
        let tag = Tag {
            device: self.device_key(),
            generation: self.generation,
            sequence: self.sequence,
        };
        self.pending = Some(tag.clone());
        self.discovering = matches!(job, Job::Discover);
        self.device_state = DeviceState::Busy;
        Task::perform(self.worker.submit(tag, job), Message::DeviceReply)
    }

    fn schedule_device_job(&mut self) -> Task<Message> {
        if self.pending.is_some() {
            return Task::none();
        }
        if self.discover_requested {
            self.discover_requested = false;
            return self.dispatch(Job::Discover);
        }
        let Some(mouse) = self.current_mouse().cloned() else {
            self.device_state = DeviceState::Disconnected;
            return Task::none();
        };
        let key = Some(DeviceKey::of(&mouse));
        if self.draft_device != key {
            self.draft_device = key;
            self.write_uncertain = false;
            self.sync_stage_draft_from_status();
        }
        if mouse.classified.tier > 0 && has_native_driver(&mouse.classified.driver) {
            return self.dispatch(Job::Read(mouse));
        }
        self.device_state = DeviceState::Ready;
        Task::none()
    }

    fn finish_device_job(&mut self, reply: Reply) -> Task<Message> {
        if self.pending.as_ref() != Some(&reply.tag) {
            return Task::none();
        }
        self.pending = None;
        self.discovering = false;
        if reply.tag.generation != self.generation || reply.tag.device != self.device_key() {
            return self.schedule_device_job();
        }

        if reply.tag.sequence == 1
            && !self.tab_chosen
            && !matches!(&reply.outcome, Outcome::Fleet(mice) if !mice.is_empty())
        {
            self.active_tab = Tab::Acceleration;
        }
        match reply.outcome {
            Outcome::Fleet(mice) => {
                let previous = self.device_key().or_else(|| self.draft_device.clone());
                self.selected_mouse = previous
                    .and_then(|key| mice.iter().position(|mouse| DeviceKey::of(mouse) == key))
                    .or_else(|| (!mice.is_empty()).then_some(0));
                self.mice = mice;
                return self.schedule_device_job();
            }
            Outcome::DiscoveryFailed => self.device_state = DeviceState::DiscoveryFailed,
            Outcome::Disconnected => {
                if let Some(index) = self.selected_mouse.take() {
                    self.mice.remove(index);
                }
                self.device_state = DeviceState::Disconnected;
            }
            Outcome::Readback {
                status,
                write_verified,
            } => {
                self.mouse_status = Some(status);
                self.write_uncertain = false;
                self.device_state = DeviceState::Ready;
                if !self.stages_dirty
                    || self.mouse_status.as_ref().is_some_and(|status| {
                        status.stages == self.stage_draft
                            && status.active_stage == Some(self.active_stage_draft)
                    })
                {
                    self.sync_stage_draft_from_status();
                }
                if let Some(verified) = write_verified {
                    let message = if verified {
                        "Settings verified on the mouse."
                    } else {
                        "The mouse readback differs from the requested settings."
                    };
                    self.status_toast = Some((message.into(), !verified));
                }
            }
            Outcome::Stale { after_write } => {
                self.device_state = DeviceState::Stale {
                    after_write: after_write || self.write_uncertain,
                };
            }
            Outcome::WorkerUnavailable => self.device_state = DeviceState::WorkerUnavailable,
        }
        Task::none()
    }

    fn can_write_mouse(&self) -> bool {
        self.device_state == DeviceState::Ready
            && self.pending.is_none()
            && self.mouse_status.is_some()
            && self
                .current_mouse()
                .is_some_and(|mouse| mouse.classified.tier >= 2 && mouse.classified.write_enabled)
    }

    fn apply_mouse_update(&mut self, updates: MouseUpdates) -> Task<Message> {
        if !self.can_write_mouse() {
            return Task::none();
        }
        let mouse = self.current_mouse().expect("writable selection").clone();
        self.status_toast = None;
        self.mouse_status = None;
        self.write_uncertain = true;
        self.dispatch(Job::Write(mouse, updates))
    }

    fn invalidate_device_status(&mut self) {
        self.generation += 1;
        self.mouse_status = None;
        self.status_toast = None;
        self.device_state = DeviceState::Busy;
    }

    fn accel_dirty(&self) -> bool {
        self.accel_cfg != self.accel_live_cfg
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Refresh => {
                self.invalidate_device_status();
                self.discover_requested = true;
                self.accel_backend = detect_accel_backend();
                if let Some(live) = self.accel_backend.live_config.clone() {
                    self.accel_live_cfg = live.clone();
                    self.accel_cfg = live;
                }
                return self.schedule_device_job();
            }
            Message::DeviceReply(reply) => return self.finish_device_job(reply),
            Message::SelectMouse(idx) => {
                if idx < self.mice.len() {
                    // Reissue discovery under the latest selection if Refresh was in flight.
                    self.discover_requested |=
                        self.device_state == DeviceState::DiscoveryFailed || self.discovering;
                    self.selected_mouse = Some(idx);
                    self.invalidate_device_status();
                    return self.schedule_device_job();
                }
            }
            Message::SelectTab(tab) => {
                self.active_tab = tab;
                self.tab_chosen = true;
            }
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
            Message::RevertStages => {
                if self.mouse_status.is_some() {
                    self.sync_stage_draft_from_status();
                }
            }
            Message::SaveStages => {
                let updates = MouseUpdates {
                    stages: Some(self.stage_draft.clone()),
                    active_stage: Some(self.active_stage_draft),
                    ..Default::default()
                };
                return self.apply_mouse_update(updates);
            }
            Message::SetPollingHz(hz) => {
                let updates = MouseUpdates {
                    polling_hz: Some(hz),
                    ..Default::default()
                };
                return self.apply_mouse_update(updates);
            }
            Message::SetLodIndex(idx) => {
                let updates = MouseUpdates {
                    lod_index: Some(idx),
                    ..Default::default()
                };
                return self.apply_mouse_update(updates);
            }
            Message::SetLodPreset(level) => {
                let updates = MouseUpdates {
                    lod_preset: Some(level.clone()),
                    ..Default::default()
                };
                return self.apply_mouse_update(updates);
            }
            Message::ToggleMotionSync => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.motion_sync) {
                    let updates = MouseUpdates {
                        motion_sync: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::ToggleAngleSnapping => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.angle_snapping) {
                    let updates = MouseUpdates {
                        angle_snapping: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::ToggleRippleControl => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.ripple_control) {
                    let updates = MouseUpdates {
                        ripple_control: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::ToggleSlamclick => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.slamclick) {
                    let updates = MouseUpdates {
                        slamclick: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::ToggleMotionJitter => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.motion_jitter) {
                    let updates = MouseUpdates {
                        motion_jitter: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::ToggleLeftHanded => {
                if let Some(cur) = self.mouse_status.as_ref().and_then(|s| s.left_handed) {
                    let updates = MouseUpdates {
                        left_handed: Some(!cur),
                        ..Default::default()
                    };
                    return self.apply_mouse_update(updates);
                }
            }
            Message::SetSpdt(mode) => {
                let updates = MouseUpdates {
                    spdt: Some(mode.clone()),
                    ..Default::default()
                };
                return self.apply_mouse_update(updates);
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
            Message::SaveAccel => match apply_live_accel(&self.accel_cfg) {
                Ok(readback) => {
                    self.accel_live_cfg = readback.clone();
                    self.accel_cfg = readback;
                    self.accel_backend = detect_accel_backend();
                    self.status_toast = Some(("Acceleration curve saved to kernel driver".into(), false));
                }
                Err(err) => {
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
                (t.bad_soft, t.bad, t.ink)
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
        let (conn_text, conn_bg, conn_border, conn_fg) = self.device_state.presentation(t);

        let mut left = row![
            text("mousetool").size(18).color(t.ink),
            container(text(conn_text).size(12).color(conn_fg))
                .width(200)
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
            let msg = match self.device_state {
                DeviceState::Busy => "Checking for USB and Bluetooth mice.",
                DeviceState::DiscoveryFailed => "Device discovery failed. Click Refresh to retry.",
                _ => "Connect a mouse and click Refresh, or tune Acceleration below.",
            };
            return container(text(msg).size(12.5).color(t.ink_2))
                .center_y(46)
                .padding([8, 14])
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

        container(scrollable(bar).direction(scrollable::Direction::Horizontal(
            scrollable::Scrollbar::new(),
        )))
        .center_y(46)
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
            .map(|(x, y)| {
                if x == y {
                    format!("{x}")
                } else {
                    format!("{x}×{y}")
                }
            })
            .unwrap_or_else(|| "–".into());
        let dpi_sub = st
            .and_then(|s| {
                s.active_stage
                    .map(|a| format!("Stage {a}/{}", s.stages.len()))
            })
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
            .map(|fw| {
                format!(
                    "{fw} · {}",
                    m.map(|d| d.classified.link.as_str()).unwrap_or("")
                )
            })
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

        if mouse.permission_hint.is_some() {
            sections = sections.push(
                container(
                    text("HID access denied. Apply the udev rules and reconnect the mouse.")
                        .size(12.5)
                        .color(t.warn_ink),
                )
                .padding([10, 14])
                .width(Length::Fill)
                .style(move |_| theme::pill_style(t.warn_soft, t.warn, t.warn_ink)),
            );
        }

        let Some(st) = &self.mouse_status else {
            let note = if mouse.classified.tier == 0 {
                mouse.classified.status_note.as_str()
            } else if !has_native_driver(&mouse.classified.driver) {
                "A native settings driver is unavailable for this mouse."
            } else if self.device_state == DeviceState::Busy {
                "Waiting for device readback."
            } else {
                "Click Refresh to read the current settings and enable supported controls."
            };
            sections = sections.push(
                container(text(note).size(12.5).color(t.muted))
                    .padding(16)
                    .width(Length::Fill)
                    .style(move |_| theme::card_style(t)),
            );
            return sections.into();
        };

        let writable = self.can_write_mouse();

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
                text(&self.accel_backend.detail).size(12).color(t.muted),
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

        if self.accel_backend.writable {
            actions = actions
                .push(
                    button(text("Reset to Off").size(12.5))
                        .padding([6, 11])
                        .style(theme::secondary_button(t, false))
                        .on_press(Message::ResetAccelOff),
                )
                .push(
                    button(text("Save curve").size(12.5))
                        .padding([6, 14])
                        .style(theme::primary_button(t))
                        .on_press(Message::SaveAccel),
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
            ]
            .spacing(6);

            if let Some(fw) = self.mouse_status.as_ref().and_then(|s| s.firmware.as_ref()) {
                details = details.push(kv_row(t, "Firmware", fw.clone()));
            }
            if m.permission_hint.is_some() {
                details = details.push(
                    container(
                        text("HID access denied. Apply the udev rules and reconnect the mouse.")
                            .size(12)
                            .color(t.warn_ink),
                    )
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::{Receiver, channel};
    use std::time::Duration;

    const WAIT: Duration = Duration::from_secs(5);

    fn mouse(id: &str) -> DetectedMouse {
        DetectedMouse {
            id: id.into(),
            path: format!("fixture-{id}"),
            vid: 0x3367,
            pid: 0x1964,
            manufacturer: String::new(),
            product: String::new(),
            serial: String::new(),
            usage_page: 1,
            usage: 2,
            interface_number: 0,
            classified: DeviceRegistry::load_embedded()
                .unwrap()
                .classify_usb(0x3367, 0x1964, None),
            permission_hint: None,
        }
    }

    fn status(dpi: u32) -> MouseStatus {
        MouseStatus {
            dpi: Some((dpi, dpi)),
            dpi_min: 50,
            dpi_max: 30_000,
            dpi_step: 50,
            active_stage: Some(1),
            stages: vec![(dpi, dpi)],
            max_stages: 4,
            ..Default::default()
        }
    }

    fn readback(dpi: u32) -> Outcome {
        Outcome::Readback {
            status: status(dpi),
            write_verified: None,
        }
    }

    fn app(worker: Worker) -> App {
        let backend = AccelBackendStatus {
            name: "fixture".into(),
            label: "Fixture".into(),
            loaded: false,
            writable: false,
            lut_supported: false,
            lut_writable: false,
            detail: String::new(),
            active_mode: None,
            live_config: None,
            params_dir: None,
            resets_dir: None,
        };
        App::with_worker(
            worker,
            DeviceRegistry::load_embedded().unwrap().into(),
            backend,
        )
        .0
    }

    fn receive(app: &mut App) {
        let reply = app.worker.replies.recv_timeout(WAIT).unwrap();
        let _ = app.update(Message::DeviceReply(reply));
    }

    fn wait_for_job(gate: &Receiver<()>) {
        gate.recv_timeout(WAIT).unwrap();
    }

    #[test]
    fn delayed_write_allows_navigation_and_blocks_duplicate_apply() {
        let (started, jobs) = channel();
        let (release, gate) = channel();
        let mut app = app(Worker::start(move |job| match job {
            Job::Discover => Outcome::Fleet(vec![mouse("a")]),
            Job::Read(_) => readback(800),
            Job::Write(mouse, updates) => {
                started.send(()).unwrap();
                wait_for_job(&gate);
                device_io::assess_readback(&mouse, Some(&updates), Err("readback failed".into()))
            }
        }));
        receive(&mut app);
        receive(&mut app);
        assert!(app.can_write_mouse());
        let _ = app.update(Message::DragStageDpi(0, 1600));
        let _ = app.update(Message::SaveStages);
        wait_for_job(&jobs);
        let sequence = app.sequence;
        let _ = app.update(Message::SaveStages);
        assert_eq!(app.sequence, sequence);
        assert!(!app.can_write_mouse());
        assert_eq!(app.device_state, DeviceState::Busy);

        let _ = app.update(Message::ToggleAppearance);
        let _ = app.update(Message::SelectTab(Tab::Device));
        assert_eq!(app.appearance, Appearance::Light);
        assert_eq!(app.active_tab, Tab::Device);
        let _ = app.view();

        release.send(()).unwrap();
        receive(&mut app);
        assert!(jobs.try_recv().is_err());
        assert_eq!(app.device_state, DeviceState::Stale { after_write: true });
        assert!(app.mouse_status.is_none());
        assert_eq!(app.stage_draft, vec![(1600, 1600)]);
        assert!(app.stages_dirty);
        assert!(!app.can_write_mouse());

        let _ = app.update(Message::Refresh);
        receive(&mut app);
        receive(&mut app);
        assert!(app.can_write_mouse());
        assert_eq!(app.stage_draft, vec![(1600, 1600)]);
    }

    #[test]
    fn reselection_discards_delayed_read() {
        let (started, jobs) = channel();
        let (release, gate) = channel();
        let mut app = app(Worker::start(move |job| match job {
            Job::Discover => Outcome::Fleet(vec![mouse("a"), mouse("b")]),
            Job::Read(mouse) if mouse.id == "a" => {
                started.send(()).unwrap();
                wait_for_job(&gate);
                readback(800)
            }
            Job::Read(_) => readback(3200),
            Job::Write(..) => panic!("unexpected write"),
        }));
        receive(&mut app);
        wait_for_job(&jobs);
        let _ = app.update(Message::SelectMouse(1));
        release.send(()).unwrap();
        let stale = app.worker.replies.recv_timeout(WAIT).unwrap();
        let _ = app.update(Message::DeviceReply(stale.clone()));
        assert!(app.mouse_status.is_none());
        receive(&mut app);
        let _ = app.update(Message::DeviceReply(stale));
        assert_eq!(app.current_mouse().unwrap().id, "b");
        assert_eq!(app.stage_draft, vec![(3200, 3200)]);
    }

    #[test]
    fn refresh_during_write_discards_completion_after_unplug() {
        let (started, jobs) = channel();
        let (release, gate) = channel();
        let mut unplugged = false;
        let mut app = app(Worker::start(move |job| match job {
            Job::Discover if unplugged => Outcome::Fleet(vec![]),
            Job::Discover => Outcome::Fleet(vec![mouse("a")]),
            Job::Read(_) => readback(800),
            Job::Write(..) => {
                started.send(()).unwrap();
                wait_for_job(&gate);
                unplugged = true;
                Outcome::Readback {
                    status: status(1600),
                    write_verified: Some(true),
                }
            }
        }));
        receive(&mut app);
        receive(&mut app);
        let _ = app.update(Message::SetPollingHz(1000));
        wait_for_job(&jobs);
        let _ = app.update(Message::Refresh);
        release.send(()).unwrap();
        receive(&mut app);
        assert_eq!(app.device_state, DeviceState::Busy);
        assert!(app.mouse_status.is_none());
        assert!(app.status_toast.is_none());
        receive(&mut app);
        assert_eq!(app.device_state, DeviceState::Disconnected);
        assert!(app.current_mouse().is_none());
        assert!(app.mouse_status.is_none());
        assert!(!app.can_write_mouse());
    }

    #[test]
    fn failed_refresh_invalidates_previous_snapshot() {
        let mut discovered = false;
        let mut app = app(Worker::start(move |job| match job {
            Job::Discover if discovered => Outcome::DiscoveryFailed,
            Job::Discover => {
                discovered = true;
                Outcome::Fleet(vec![mouse("a")])
            }
            Job::Read(_) => readback(800),
            Job::Write(..) => panic!("unexpected write"),
        }));
        receive(&mut app);
        receive(&mut app);
        let _ = app.update(Message::Refresh);
        receive(&mut app);
        assert_eq!(app.device_state, DeviceState::DiscoveryFailed);
        assert!(app.status_toast.is_none());
        assert!(app.mouse_status.is_none());
        assert!(!app.can_write_mouse());
    }

    #[test]
    fn device_status_text_meets_aa_in_both_themes() {
        fn luminance(rgb: [f32; 3]) -> f32 {
            let [r, g, b] = rgb.map(|value| {
                if value <= 0.04045 {
                    value / 12.92
                } else {
                    ((value + 0.055) / 1.055).powf(2.4)
                }
            });
            0.2126 * r + 0.7152 * g + 0.0722 * b
        }

        for appearance in [Appearance::Dark, Appearance::Light] {
            let tokens = theme::tokens(appearance);
            for state in [
                DeviceState::Ready,
                DeviceState::Busy,
                DeviceState::Disconnected,
                DeviceState::DiscoveryFailed,
                DeviceState::Stale { after_write: false },
                DeviceState::Stale { after_write: true },
                DeviceState::WorkerUnavailable,
            ] {
                let (_, bg, _, fg) = state.presentation(tokens);
                let background = luminance([
                    bg.r * bg.a + tokens.bg.r * (1.0 - bg.a),
                    bg.g * bg.a + tokens.bg.g * (1.0 - bg.a),
                    bg.b * bg.a + tokens.bg.b * (1.0 - bg.a),
                ]);
                let foreground = luminance([fg.r, fg.g, fg.b]);
                let ratio =
                    (background.max(foreground) + 0.05) / (background.min(foreground) + 0.05);
                assert!(ratio >= 4.5, "{appearance:?} {state:?}: {ratio:.2}");
            }
        }
    }
}
