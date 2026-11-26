//! Log-scale DPI stage track Canvas matching `web/index.html` & `web/styles.css`.

use crate::theme::Tokens;
use iced::mouse;
use iced::widget::canvas::{self, Canvas, Event, Frame, Geometry, Path, Stroke, Text};
use iced::{Element, Length, Point, Rectangle, Renderer, Size, Theme};

pub struct DpiTrack<Message> {
    pub stages: Vec<(u32, u32)>,
    pub active_stage: usize, // 1-based
    pub dpi_min: u32,
    pub dpi_max: u32,
    pub dpi_step: u32,
    pub writable: bool,
    pub tokens: Tokens,
    pub on_select: fn(usize) -> Message,
    pub on_drag_dpi: fn(usize, u32) -> Message,
}

#[derive(Default)]
pub struct DpiTrackState {
    dragging: Option<usize>, // 0-based stage index
}

impl<Message: 'static> DpiTrack<Message> {
    pub fn view(self) -> Element<'static, Message> {
        Canvas::new(self)
            .width(Length::Fill)
            .height(Length::Fixed(64.0))
            .into()
    }

    fn log_norm(&self, dpi: u32) -> f32 {
        let lo = (self.dpi_min.max(50) as f64).ln();
        let hi = (self.dpi_max.max(self.dpi_min + 100) as f64).ln();
        let v = (dpi.clamp(self.dpi_min, self.dpi_max).max(50) as f64).ln();
        ((v - lo) / (hi - lo)).clamp(0.0, 1.0) as f32
    }

    fn dpi_from_norm(&self, t: f32) -> u32 {
        let lo = (self.dpi_min.max(50) as f64).ln();
        let hi = (self.dpi_max.max(self.dpi_min + 100) as f64).ln();
        let raw = (lo + (t.clamp(0.0, 1.0) as f64) * (hi - lo)).exp().round() as u32;
        let step = self.dpi_step.max(10);
        let snapped = ((raw + step / 2) / step) * step;
        snapped.clamp(self.dpi_min, self.dpi_max)
    }

    fn track_rect(bounds: Rectangle) -> (f32, f32, f32) {
        let pad = 22.0_f32;
        let width = (bounds.width - pad * 2.0).max(40.0);
        let y = 26.0_f32;
        (pad, width, y)
    }
}

impl<Message: 'static> canvas::Program<Message> for DpiTrack<Message> {
    type State = DpiTrackState;

    fn update(
        &self,
        state: &mut Self::State,
        event: &Event,
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> Option<canvas::Action<Message>> {
        let (pad, width, y) = Self::track_rect(bounds);
        match event {
            Event::Mouse(mouse::Event::ButtonPressed(mouse::Button::Left)) => {
                let pos = cursor.position_in(bounds)?;
                // Find nearest stage marker within 14 px
                let mut best: Option<(usize, f32)> = None;
                for (i, &(x_dpi, _)) in self.stages.iter().enumerate() {
                    let mx = pad + self.log_norm(x_dpi) * width;
                    let dist = ((pos.x - mx).powi(2) + (pos.y - y).powi(2)).sqrt();
                    if dist <= 15.0 && best.is_none_or(|(_, d)| dist < d) {
                        best = Some((i, dist));
                    }
                }
                if let Some((idx, _)) = best {
                    if self.writable {
                        state.dragging = Some(idx);
                    }
                    return Some(
                        canvas::Action::publish((self.on_select)(idx + 1)).and_capture(),
                    );
                }
                None
            }
            Event::Mouse(mouse::Event::CursorMoved { .. }) => {
                let idx = state.dragging?;
                if !self.writable {
                    state.dragging = None;
                    return None;
                }
                let pos = cursor.position_in(bounds)?;
                let norm = ((pos.x - pad) / width).clamp(0.0, 1.0);
                let dpi = self.dpi_from_norm(norm);
                Some(canvas::Action::publish((self.on_drag_dpi)(idx, dpi)).and_capture())
            }
            Event::Mouse(mouse::Event::ButtonReleased(mouse::Button::Left)) => {
                if state.dragging.take().is_some() {
                    return Some(canvas::Action::capture());
                }
                None
            }
            _ => None,
        }
    }

    fn draw(
        &self,
        _state: &Self::State,
        renderer: &Renderer,
        _theme: &Theme,
        bounds: Rectangle,
        _cursor: mouse::Cursor,
    ) -> Vec<Geometry> {
        let mut frame = Frame::new(renderer, bounds.size());
        let t = self.tokens;

        frame.fill_rectangle(Point::ORIGIN, bounds.size(), t.inset);
        let (pad, width, y) = Self::track_rect(bounds);

        // Main horizontal track bar
        frame.fill_rectangle(Point::new(pad, y - 2.0), Size::new(width, 4.0), t.line_strong);

        // Log-scale ticks
        for tick in [100u32, 400, 800, 1600, 3200, 6400, 12800, 26000] {
            if tick < self.dpi_min || tick > self.dpi_max {
                continue;
            }
            let tx = pad + self.log_norm(tick) * width;
            frame.stroke(
                &Path::line(Point::new(tx, y + 6.0), Point::new(tx, y + 11.0)),
                Stroke::default().with_color(t.tick).with_width(1.0),
            );
            let label = if tick >= 1000 {
                if tick % 1000 == 0 {
                    format!("{}k", tick / 1000)
                } else {
                    format!("{:.1}k", tick as f32 / 1000.0)
                }
            } else {
                tick.to_string()
            };
            frame.fill_text(Text {
                content: label,
                position: Point::new(tx - 10.0, y + 14.0),
                color: t.muted,
                size: 10.5.into(),
                ..Text::default()
            });
        }

        // Stage markers (draw inactive first, active on top)
        for pass in [false, true] {
            for (i, &(x_dpi, _)) in self.stages.iter().enumerate() {
                let is_active = i + 1 == self.active_stage;
                if is_active != pass {
                    continue;
                }
                let mx = pad + self.log_norm(x_dpi) * width;
                let center = Point::new(mx, y);
                let radius = if is_active { 8.5 } else { 7.0 };
                let fill = if is_active { t.accent } else { t.surface };
                let stroke = if is_active { t.accent_hi } else { t.ink_2 };
                let num_color = if is_active { t.accent_ink } else { t.ink };

                frame.fill(&Path::circle(center, radius), fill);
                frame.stroke(
                    &Path::circle(center, radius),
                    Stroke::default().with_color(stroke).with_width(1.6),
                );
                frame.fill_text(Text {
                    content: (i + 1).to_string(),
                    position: Point::new(mx - 3.2, y - 6.0),
                    color: num_color,
                    size: 10.5.into(),
                    ..Text::default()
                });
            }
        }

        frame.stroke(
            &Path::rectangle(Point::ORIGIN, bounds.size()),
            Stroke::default().with_color(t.line).with_width(1.0),
        );

        vec![frame.into_geometry()]
    }

    fn mouse_interaction(
        &self,
        state: &Self::State,
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> mouse::Interaction {
        if state.dragging.is_some() {
            return mouse::Interaction::Grabbing;
        }
        if let Some(pos) = cursor.position_in(bounds) {
            let (pad, width, y) = Self::track_rect(bounds);
            for &(x_dpi, _) in &self.stages {
                let mx = pad + self.log_norm(x_dpi) * width;
                if ((pos.x - mx).powi(2) + (pos.y - y).powi(2)).sqrt() <= 15.0 {
                    return if self.writable {
                        mouse::Interaction::Grab
                    } else {
                        mouse::Interaction::Pointer
                    };
                }
            }
        }
        mouse::Interaction::default()
    }
}
