//! Crisp interactive vector Canvas rendering the acceleration curve graph matching `web/accel_graph.js`.
//! Supports dragging custom LUT control points directly on the chart.

use crate::theme::Tokens;
use iced::mouse;
use iced::widget::canvas::{self, Canvas, Event, Frame, Geometry, Path, Stroke, Text};
use iced::{Color, Element, Length, Point, Rectangle, Renderer, Size, Theme};
use mousetool_core::accel::{AccelConfig, AccelMode, evaluate_sensitivity};

pub struct CurveGraph<Message> {
    config: AccelConfig,
    tokens: Tokens,
    on_drag_lut: fn(usize, f64, f64) -> Message,
}

#[derive(Default)]
pub struct CurveGraphState {
    dragging_lut: Option<usize>,
}

impl<Message: 'static> CurveGraph<Message> {
    pub fn new(
        config: AccelConfig,
        tokens: Tokens,
        on_drag_lut: fn(usize, f64, f64) -> Message,
    ) -> Self {
        Self {
            config,
            tokens,
            on_drag_lut,
        }
    }

    pub fn view(self) -> Element<'static, Message> {
        Canvas::new(self)
            .width(Length::Fill)
            .height(Length::Fixed(230.0))
            .into()
    }

    fn layout(bounds: Rectangle, cfg: &AccelConfig) -> (f32, f32, f32, f32, f64, f64) {
        let pad_l = 42.0_f32;
        let pad_r = 16.0_f32;
        let pad_t = 14.0_f32;
        let pad_b = 26.0_f32;
        let plot_w = (bounds.width - pad_l - pad_r).max(40.0);
        let plot_h = (bounds.height - pad_t - pad_b).max(40.0);
        let x_max = 60.0_f64;

        let mut y_max = 2.5_f64;
        for i in 0..=60 {
            let x = (i as f64 / 60.0) * x_max;
            let (sx, sy) = evaluate_sensitivity(x, cfg);
            y_max = y_max.max(sx).max(sy);
        }
        let y_top = (y_max * 1.15).ceil().max(2.5);
        (pad_l, pad_t, plot_w, plot_h, x_max, y_top)
    }
}

impl<Message: 'static> canvas::Program<Message> for CurveGraph<Message> {
    type State = CurveGraphState;

    fn update(
        &self,
        state: &mut Self::State,
        event: &Event,
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> Option<canvas::Action<Message>> {
        if self.config.mode != AccelMode::Lookup {
            state.dragging_lut = None;
            return None;
        }
        let (pad_l, pad_t, plot_w, plot_h, x_max, y_top) = Self::layout(bounds, &self.config);
        let to_px = |x: f64, y: f64| -> Point {
            let nx = (x / x_max).clamp(0.0, 1.0) as f32;
            let ny = (y / y_top).clamp(0.0, 1.0) as f32;
            Point::new(pad_l + nx * plot_w, pad_t + (1.0 - ny) * plot_h)
        };

        match event {
            Event::Mouse(mouse::Event::ButtonPressed(mouse::Button::Left)) => {
                let pos = cursor.position_in(bounds)?;
                let mut best: Option<(usize, f32)> = None;
                for (idx, &[lx, ly]) in self.config.lut_points.iter().enumerate() {
                    let pt = to_px(lx, ly * self.config.sens_mult);
                    let d = ((pos.x - pt.x).powi(2) + (pos.y - pt.y).powi(2)).sqrt();
                    if d <= 13.0 && best.is_none_or(|(_, bd)| d < bd) {
                        best = Some((idx, d));
                    }
                }
                if let Some((idx, _)) = best {
                    state.dragging_lut = Some(idx);
                    return Some(canvas::Action::capture());
                }
                None
            }
            Event::Mouse(mouse::Event::CursorMoved { .. }) => {
                let idx = state.dragging_lut?;
                let pos = cursor.position_in(bounds)?;
                let nx = ((pos.x - pad_l) / plot_w).clamp(0.0, 1.0) as f64;
                let ny = (1.0 - (pos.y - pad_t) / plot_h).clamp(0.0, 1.0) as f64;
                let x = (nx * x_max * 2.0).round() / 2.0;
                let raw_y = (ny * y_top) / self.config.sens_mult.max(0.01);
                let y = ((raw_y * 20.0).round() / 20.0).clamp(0.1, 10.0);
                Some(canvas::Action::publish((self.on_drag_lut)(idx, x, y)).and_capture())
            }
            Event::Mouse(mouse::Event::ButtonReleased(mouse::Button::Left)) => {
                if state.dragging_lut.take().is_some() {
                    return Some(canvas::Action::capture());
                }
                None
            }
            _ => None,
        }
    }

    fn draw(
        &self,
        state: &Self::State,
        renderer: &Renderer,
        _theme: &Theme,
        bounds: Rectangle,
        _cursor: mouse::Cursor,
    ) -> Vec<Geometry> {
        let mut frame = Frame::new(renderer, bounds.size());
        let t = self.tokens;

        frame.fill_rectangle(Point::ORIGIN, bounds.size(), t.inset);
        let (pad_l, pad_t, plot_w, plot_h, x_max, y_top) = Self::layout(bounds, &self.config);
        let y_bot = 0.0_f64;

        let samples = 180usize;
        let mut pts_x = Vec::with_capacity(samples + 1);
        let mut pts_y = Vec::with_capacity(samples + 1);
        for i in 0..=samples {
            let x = (i as f64 / samples as f64) * x_max;
            let (sx, sy) = evaluate_sensitivity(x, &self.config);
            pts_x.push((x, sx));
            pts_y.push((x, sy));
        }

        let to_px = |x: f64, y: f64| -> Point {
            let nx = (x / x_max).clamp(0.0, 1.0) as f32;
            let ny = ((y - y_bot) / (y_top - y_bot)).clamp(0.0, 1.0) as f32;
            Point::new(pad_l + nx * plot_w, pad_t + (1.0 - ny) * plot_h)
        };

        // Horizontal gridlines + labels
        let y_steps = 4usize;
        for i in 0..=y_steps {
            let val = y_bot + (y_top - y_bot) * (i as f64 / y_steps as f64);
            let p0 = to_px(0.0, val);
            let p1 = to_px(x_max, val);
            frame.stroke(
                &Path::line(p0, p1),
                Stroke::default().with_color(t.line).with_width(1.0),
            );
            frame.fill_text(Text {
                content: format!("{val:.1}×"),
                position: Point::new(6.0, p0.y - 6.0),
                color: t.muted,
                size: 11.0.into(),
                ..Text::default()
            });
        }

        // Vertical gridlines + labels
        for tick in [0.0, 15.0, 30.0, 45.0, 60.0] {
            let p0 = to_px(tick, y_bot);
            let p1 = to_px(tick, y_top);
            frame.stroke(
                &Path::line(p0, p1),
                Stroke::default().with_color(t.line).with_width(1.0),
            );
            frame.fill_text(Text {
                content: format!("{tick:.0}"),
                position: Point::new(p0.x - 6.0, pad_t + plot_h + 6.0),
                color: t.muted,
                size: 11.0.into(),
                ..Text::default()
            });
        }

        // 1.0× reference baseline
        if y_top >= 1.0 {
            let b0 = to_px(0.0, 1.0);
            let b1 = to_px(x_max, 1.0);
            frame.stroke(
                &Path::line(b0, b1),
                Stroke::default().with_color(t.line_strong).with_width(1.0),
            );
        }

        // Optional Y-axis curve when Y/X ratio != 1
        if (self.config.yx_ratio - 1.0).abs() > 1e-3 {
            let y_path = Path::new(|b| {
                for (idx, &(x, sy)) in pts_y.iter().enumerate() {
                    let pt = to_px(x, sy);
                    if idx == 0 {
                        b.move_to(pt);
                    } else {
                        b.line_to(pt);
                    }
                }
            });
            frame.stroke(
                &y_path,
                Stroke::default()
                    .with_color(Color::from_rgba(t.muted.r, t.muted.g, t.muted.b, 0.75))
                    .with_width(1.6),
            );
        }

        // Primary X sensitivity curve + soft area fill
        let area = Path::new(|b| {
            b.move_to(to_px(0.0, y_bot));
            for &(x, sx) in &pts_x {
                b.line_to(to_px(x, sx));
            }
            b.line_to(to_px(x_max, y_bot));
            b.close();
        });
        frame.fill(&area, t.accent_soft);

        let curve = Path::new(|b| {
            for (idx, &(x, sx)) in pts_x.iter().enumerate() {
                let pt = to_px(x, sx);
                if idx == 0 {
                    b.move_to(pt);
                } else {
                    b.line_to(pt);
                }
            }
        });
        frame.stroke(
            &curve,
            Stroke::default().with_color(t.accent).with_width(2.2),
        );

        // Draggable Custom LUT control point handles
        if self.config.mode == AccelMode::Lookup {
            for (idx, &[lx, ly]) in self.config.lut_points.iter().enumerate() {
                let pt = to_px(lx, ly * self.config.sens_mult);
                let active = state.dragging_lut == Some(idx);
                let r = if active { 6.5 } else { 5.0 };
                frame.fill(&Path::circle(pt, r), if active { t.accent } else { t.surface });
                frame.stroke(
                    &Path::circle(pt, r),
                    Stroke::default().with_color(t.accent).with_width(2.0),
                );
            }
        }

        frame.stroke(
            &Path::rectangle(Point::new(pad_l, pad_t), Size::new(plot_w, plot_h)),
            Stroke::default().with_color(t.line_strong).with_width(1.0),
        );

        vec![frame.into_geometry()]
    }

    fn mouse_interaction(
        &self,
        state: &Self::State,
        bounds: Rectangle,
        cursor: mouse::Cursor,
    ) -> mouse::Interaction {
        if state.dragging_lut.is_some() {
            return mouse::Interaction::Grabbing;
        }
        if self.config.mode == AccelMode::Lookup {
            if let Some(pos) = cursor.position_in(bounds) {
                let (pad_l, pad_t, plot_w, plot_h, x_max, y_top) =
                    Self::layout(bounds, &self.config);
                for &[lx, ly] in &self.config.lut_points {
                    let nx = (lx / x_max).clamp(0.0, 1.0) as f32;
                    let ny = ((ly * self.config.sens_mult) / y_top).clamp(0.0, 1.0) as f32;
                    let pt = Point::new(pad_l + nx * plot_w, pad_t + (1.0 - ny) * plot_h);
                    if ((pos.x - pt.x).powi(2) + (pos.y - pt.y).powi(2)).sqrt() <= 13.0 {
                        return mouse::Interaction::Grab;
                    }
                }
            }
        }
        mouse::Interaction::default()
    }
}
