//! Lightweight native Mouse Tool GUI (`iced` + `tiny-skia`) matching `web/styles.css`.

mod curve_canvas;
mod theme;

use curve_canvas::CurveGraph;
use iced::widget::{Space, button, column, container, row, scrollable, slider, text};
use iced::{Alignment, Element, Length, Padding, Task, Theme};
use mousetool_core::RuntimeMode;
use mousetool_core::accel::{
    AccelBackendStatus, AccelConfig, AccelMode, LutPreset, detect_accel_backend,
};
use mousetool_core::hid::{DetectedMouse, discover_mice};
use mousetool_core::registry::DeviceRegistry;
use theme::{Appearance, MAX_CONTENT_WIDTH, Tokens};

pub fn main() -> iced::Result {
    let portable_only = std::env::args().any(|a| a == "--portable");
    iced::application(
        move || App::new(portable_only),
        App::update,
        App::view,
    )
    .title("Mouse Tool")
    .theme(App::theme)
    .window_size((1020.0, 760.0))
    .run()
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
    SetAccelMode(AccelMode),
    SetSensMult(f64),
    SetYxRatio(f64),
    SetAccel(f64),
    SetOffset(f64),
    SetOutputCap(f64),
    SetDecayRate(f64),
    SetLimit(f64),
    SetSyncSpeed(f64),
    SetMotivity(f64),
    SetGamma(f64),
    SetSmooth(f64),
    ApplyLutPreset(LutPreset),
}

struct App {
    registry: DeviceRegistry,
    runtime_mode: RuntimeMode,
    appearance: Appearance,
    mice: Vec<DetectedMouse>,
    selected_mouse: Option<usize>,
    hid_error: Option<String>,
    active_tab: Tab,
    accel_backend: AccelBackendStatus,
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
        let mut accel_cfg = AccelConfig::default();
        if let Some(active) = accel_backend.active_mode {
            accel_cfg.mode = active;
        }
        let active_tab = if selected_mouse.is_some() {
            Tab::Device
        } else {
            Tab::Acceleration
        };
        (
            Self {
                registry,
                runtime_mode: RuntimeMode::PortableInProcess,
                appearance: Appearance::Dark,
                mice,
                selected_mouse,
                hid_error,
                active_tab,
                accel_backend,
                accel_cfg,
            },
            Task::none(),
        )
    }

    fn theme(&self) -> Theme {
        theme::iced_theme(self.appearance)
    }

    fn current_mouse(&self) -> Option<&DetectedMouse> {
        self.selected_mouse.and_then(|i| self.mice.get(i))
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Refresh => {
                match discover_mice(&self.registry) {
                    Ok(list) => {
                        self.mice = list;
                        self.hid_error = None;
                        self.selected_mouse = (!self.mice.is_empty()).then_some(0);
                    }
                    Err(err) => {
                        self.hid_error = Some(err);
                    }
                }
                self.accel_backend = detect_accel_backend();
            }
            Message::SelectMouse(idx) => {
                if idx < self.mice.len() {
                    self.selected_mouse = Some(idx);
                }
            }
            Message::SelectTab(tab) => {
                self.active_tab = tab;
            }
            Message::ToggleAppearance => {
                self.appearance = match self.appearance {
                    Appearance::Dark => Appearance::Light,
                    Appearance::Light => Appearance::Dark,
                };
            }
            Message::SetAccelMode(mode) => self.accel_cfg.mode = mode,
            Message::SetSensMult(v) => self.accel_cfg.sens_mult = (v * 100.0).round() / 100.0,
            Message::SetYxRatio(v) => self.accel_cfg.yx_ratio = (v * 50.0).round() / 50.0,
            Message::SetAccel(v) => self.accel_cfg.accel = (v * 200.0).round() / 200.0,
            Message::SetOffset(v) => self.accel_cfg.offset = (v * 4.0).round() / 4.0,
            Message::SetOutputCap(v) => self.accel_cfg.output_cap = (v * 20.0).round() / 20.0,
            Message::SetDecayRate(v) => self.accel_cfg.decay_rate = (v * 1000.0).round() / 1000.0,
            Message::SetLimit(v) => self.accel_cfg.limit = (v * 20.0).round() / 20.0,
            Message::SetSyncSpeed(v) => self.accel_cfg.sync_speed = (v * 20.0).round() / 20.0,
            Message::SetMotivity(v) => self.accel_cfg.motivity = (v * 100.0).round() / 100.0,
            Message::SetGamma(v) => self.accel_cfg.gamma = (v * 20.0).round() / 20.0,
            Message::SetSmooth(v) => self.accel_cfg.smooth = (v * 50.0).round() / 50.0,
            Message::ApplyLutPreset(preset) => {
                self.accel_cfg.mode = AccelMode::Lookup;
                self.accel_cfg.lut_points = preset.points();
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Message> {
        let t = theme::tokens(self.appearance);

        let header = self.view_header(t);
        let fleet = self.view_fleet(t);
        let hero = self.view_hero(t);
        let tabs = self.view_tabs(t);
        let panel = match self.active_tab {
            Tab::Acceleration => self.view_accel_panel(t),
            Tab::Device => self.view_device_panel(t),
            other => self.view_coming_soon_panel(t, other),
        };

        let body = column![header, fleet, hero, tabs, panel]
            .spacing(14)
            .max_width(MAX_CONTENT_WIDTH)
            .width(Length::Fill);

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
            Appearance::Dark => "Light theme",
            Appearance::Light => "Dark theme",
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
        let dpi_text = m
            .map(|d| format!("up to {}", d.classified.dpi_max))
            .unwrap_or_else(|| "–".into());
        let proto_text = m
            .map(|d| d.classified.proto_tag.clone())
            .unwrap_or_else(|| "–".into());
        let device_text = m
            .map(|d| d.display_name().to_string())
            .unwrap_or_else(|| "None".into());
        let link_text = m
            .map(|d| format!("{} · {}", d.classified.link, d.vid_pid()))
            .unwrap_or_else(|| "Portable native core".into());

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
            tile("DPI", dpi_text, "Native HID Phase 2".into()),
            tile("Protocol", proto_text, "Registry-matched".into()),
            tile(
                "Acceleration",
                self.accel_cfg.mode.label().into(),
                self.accel_backend.label.clone(),
            ),
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

        let graph = CurveGraph::new(self.accel_cfg.clone(), t).view();

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
                let mut preset_row = row![text("Presets:").size(12.5).color(t.muted)]
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
                controls = controls.push(preset_row);
            }
            AccelMode::NoAccel => {}
        }

        // Global curve scaling parameters (exact same defaults/steps as web/accel_graph.js)
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
            ));

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
            "{} controls are wired next in Phase 2 as native HID protocol drivers move into mousetool-core (behind HidTransport, matching web/drivers/*.js and mousetool/drivers/*.py).",
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
        text(label).size(12.5).color(t.ink_2).width(Length::Fixed(160.0)),
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

fn kv_row<'a>(t: Tokens, key: &'static str, val: String) -> Element<'a, Message> {
    row![
        text(key).size(12.5).color(t.muted).width(Length::Fixed(160.0)),
        text(val).size(12.5).color(t.ink),
    ]
    .spacing(12)
    .align_y(Alignment::Center)
    .into()
}
