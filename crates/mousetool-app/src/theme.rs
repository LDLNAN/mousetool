//! Exact 1:1 visual design tokens and widget styles matching `web/styles.css`.
//!
//! Light palette: warm paper `#f6f5f2` canvas, `#ffffff` cards, `#efeee9` surfaces,
//! `#dcdad2` hairline borders, burnt-orange `#c84b20` accent.
//! Dark palette: warm charcoal `#141413` canvas, `#1c1c1a` cards, `#242421` surfaces,
//! `#2c2c28` hairline borders, `#e05d30` accent.

use iced::widget::{button, container, slider};
use iced::{Background, Border, Color, Theme};

pub const RADIUS: f32 = 4.0;
pub const MAX_CONTENT_WIDTH: f32 = 960.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Appearance {
    Light,
    Dark,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Copy)]
pub struct Tokens {
    pub bg: Color,
    pub surface: Color,
    pub surface_2: Color,
    pub inset: Color,
    pub line: Color,
    pub line_strong: Color,
    pub tick: Color,
    pub ink: Color,
    pub ink_2: Color,
    pub muted: Color,
    pub faint: Color,
    pub accent: Color,
    pub accent_hi: Color,
    pub accent_ink: Color,
    pub accent_soft: Color,
    pub ok: Color,
    pub ok_ink: Color,
    pub ok_soft: Color,
    pub warn: Color,
    pub warn_ink: Color,
    pub warn_soft: Color,
    pub bad: Color,
    pub bad_soft: Color,
}

const fn rgb(r: u8, g: u8, b: u8) -> Color {
    Color::from_rgb(r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0)
}

const fn rgba(r: u8, g: u8, b: u8, a: f32) -> Color {
    Color::from_rgba(r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0, a)
}

pub const LIGHT: Tokens = Tokens {
    bg: rgb(0xf6, 0xf5, 0xf2),
    surface: rgb(0xff, 0xff, 0xff),
    surface_2: rgb(0xef, 0xee, 0xe9),
    inset: rgb(0xea, 0xe8, 0xe1),
    line: rgb(0xdc, 0xda, 0xd2),
    line_strong: rgb(0xc4, 0xc1, 0xb7),
    tick: rgb(0x9c, 0x99, 0x8f),
    ink: rgb(0x16, 0x16, 0x15),
    ink_2: rgb(0x3d, 0x3c, 0x38),
    muted: rgb(0x68, 0x66, 0x60),
    faint: rgb(0x94, 0x91, 0x89),
    accent: rgb(0xc8, 0x4b, 0x20),
    accent_hi: rgb(0xb0, 0x3e, 0x16),
    accent_ink: rgb(0xff, 0xff, 0xff),
    accent_soft: rgba(0xc8, 0x4b, 0x20, 0.10),
    ok: rgb(0x1f, 0x7a, 0x3f),
    ok_ink: rgb(0x14, 0x5a, 0x2c),
    ok_soft: rgba(0x1f, 0x7a, 0x3f, 0.10),
    warn: rgb(0xa8, 0x5d, 0x08),
    warn_ink: rgb(0x82, 0x44, 0x00),
    warn_soft: rgba(0xa8, 0x5d, 0x08, 0.10),
    bad: rgb(0xbd, 0x2c, 0x22),
    bad_soft: rgba(0xbd, 0x2c, 0x22, 0.09),
};

pub const DARK: Tokens = Tokens {
    bg: rgb(0x14, 0x14, 0x13),
    surface: rgb(0x1c, 0x1c, 0x1a),
    surface_2: rgb(0x24, 0x24, 0x21),
    inset: rgb(0x11, 0x11, 0x10),
    line: rgb(0x2c, 0x2c, 0x28),
    line_strong: rgb(0x3d, 0x3d, 0x37),
    tick: rgb(0x60, 0x5f, 0x58),
    ink: rgb(0xe8, 0xe6, 0xdf),
    ink_2: rgb(0xc2, 0xc0, 0xb8),
    muted: rgb(0x8c, 0x8a, 0x82),
    faint: rgb(0x62, 0x60, 0x59),
    accent: rgb(0xe0, 0x5d, 0x30),
    accent_hi: rgb(0xeb, 0x6d, 0x42),
    accent_ink: rgb(0xff, 0xff, 0xff),
    accent_soft: rgba(0xe0, 0x5d, 0x30, 0.14),
    ok: rgb(0x3f, 0xb9, 0x67),
    ok_ink: rgb(0x3f, 0xb9, 0x67),
    ok_soft: rgba(0x3f, 0xb9, 0x67, 0.14),
    warn: rgb(0xe0, 0x9b, 0x3d),
    warn_ink: rgb(0xe0, 0x9b, 0x3d),
    warn_soft: rgba(0xe0, 0x9b, 0x3d, 0.12),
    bad: rgb(0xea, 0x58, 0x4e),
    bad_soft: rgba(0xea, 0x58, 0x4e, 0.12),
};

pub fn tokens(appearance: Appearance) -> Tokens {
    match appearance {
        Appearance::Light => LIGHT,
        Appearance::Dark => DARK,
    }
}

pub fn iced_theme(appearance: Appearance) -> Theme {
    let t = tokens(appearance);
    let palette = iced::theme::Palette {
        background: t.bg,
        text: t.ink,
        primary: t.accent,
        success: t.ok,
        warning: t.warn,
        danger: t.bad,
    };
    let name = match appearance {
        Appearance::Light => "mousetool-light",
        Appearance::Dark => "mousetool-dark",
    };
    Theme::custom(name, palette)
}

pub fn card_style(t: Tokens) -> container::Style {
    container::Style {
        background: Some(Background::Color(t.surface)),
        text_color: Some(t.ink),
        border: Border {
            color: t.line,
            width: 1.0,
            radius: RADIUS.into(),
        },
        ..Default::default()
    }
}

pub fn inset_style(t: Tokens) -> container::Style {
    container::Style {
        background: Some(Background::Color(t.inset)),
        text_color: Some(t.ink),
        border: Border {
            color: t.line,
            width: 1.0,
            radius: RADIUS.into(),
        },
        ..Default::default()
    }
}

pub fn pill_style(bg: Color, border_color: Color, text_color: Color) -> container::Style {
    container::Style {
        background: Some(Background::Color(bg)),
        text_color: Some(text_color),
        border: Border {
            color: border_color,
            width: 1.0,
            radius: RADIUS.into(),
        },
        ..Default::default()
    }
}

pub fn primary_button(t: Tokens) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let bg = match status {
            button::Status::Hovered => t.accent_hi,
            button::Status::Pressed => t.accent_hi,
            _ => t.accent,
        };
        button::Style {
            background: Some(Background::Color(bg)),
            text_color: t.accent_ink,
            border: Border {
                color: bg,
                width: 1.0,
                radius: RADIUS.into(),
            },
            ..Default::default()
        }
    }
}

pub fn secondary_button(
    t: Tokens,
    selected: bool,
) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let (bg, border_color, text_color) = if selected {
            (t.accent_soft, t.accent, t.accent)
        } else {
            match status {
                button::Status::Hovered => (t.surface, t.tick, t.ink),
                button::Status::Pressed => (t.inset, t.line_strong, t.ink),
                _ => (t.surface_2, t.line_strong, t.ink),
            }
        };
        button::Style {
            background: Some(Background::Color(bg)),
            text_color,
            border: Border {
                color: border_color,
                width: 1.0,
                radius: RADIUS.into(),
            },
            ..Default::default()
        }
    }
}

pub fn ghost_button(t: Tokens) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let (bg, border_color) = match status {
            button::Status::Hovered => (t.surface, t.line_strong),
            button::Status::Pressed => (t.inset, t.line_strong),
            _ => (Color::TRANSPARENT, t.line),
        };
        button::Style {
            background: Some(Background::Color(bg)),
            text_color: t.ink_2,
            border: Border {
                color: border_color,
                width: 1.0,
                radius: RADIUS.into(),
            },
            ..Default::default()
        }
    }
}

pub fn tab_button(t: Tokens, active: bool) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_, status| {
        let (bg, text_color, border_color) = if active {
            (t.surface, t.ink, t.accent)
        } else {
            match status {
                button::Status::Hovered => (t.surface_2, t.ink, t.line),
                _ => (Color::TRANSPARENT, t.muted, Color::TRANSPARENT),
            }
        };
        button::Style {
            background: Some(Background::Color(bg)),
            text_color,
            border: Border {
                color: border_color,
                width: if active { 1.0 } else { 0.0 },
                radius: RADIUS.into(),
            },
            ..Default::default()
        }
    }
}

pub fn accent_slider(t: Tokens) -> impl Fn(&Theme, slider::Status) -> slider::Style {
    move |_, status| {
        let handle_bg = match status {
            slider::Status::Hovered | slider::Status::Dragged => t.accent_hi,
            _ => t.accent,
        };
        slider::Style {
            rail: slider::Rail {
                backgrounds: (
                    Background::Color(t.accent),
                    Background::Color(t.line_strong),
                ),
                width: 4.0,
                border: Border {
                    radius: 2.0.into(),
                    ..Default::default()
                },
            },
            handle: slider::Handle {
                shape: slider::HandleShape::Circle { radius: 6.5 },
                background: Background::Color(handle_bg),
                border_width: 1.5,
                border_color: t.surface,
            },
        }
    }
}
