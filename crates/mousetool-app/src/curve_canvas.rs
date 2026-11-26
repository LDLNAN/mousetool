//! Crisp vector Canvas widget rendering the acceleration curve graph matching `web/accel_graph.js`.

use crate::theme::Tokens;
use iced::mouse;
use iced::widget::canvas::{self, Canvas, Frame, Geometry, Path, Stroke, Text};
use iced::{Color, Element, Length, Point, Rectangle, Renderer, Size, Theme};
use mousetool_core::accel::{AccelConfig, AccelMode, evaluate_sensitivity};

pub struct CurveGraph {
    config: AccelConfig,
    tokens: Tokens,
}

impl CurveGraph {
    pub fn new(config: AccelConfig, tokens: Tokens) -> Self {
        Self { config, tokens }
    }

    pub fn view<Message: 'static>(self) -> Element<'static, Message> {
        Canvas::new(self)
            .width(Length::Fill)
            .height(Length::Fixed(230.0))
            .into()
    }
}

impl<Message> canvas::Program<Message> for CurveGraph {
    type State = ();

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

        // Inset chart background
        frame.fill_rectangle(Point::ORIGIN, bounds.size(), t.inset);

        let pad_l = 42.0_f32;
        let pad_r = 16.0_f32;
        let pad_t = 14.0_f32;
        let pad_b = 26.0_f32;
        let plot_w = (bounds.width - pad_l - pad_r).max(40.0);
        let plot_h = (bounds.height - pad_t - pad_b).max(40.0);

        let x_max = 60.0_f64;
        let samples = 180usize;

        let mut pts_x = Vec::with_capacity(samples + 1);
        let mut pts_y = Vec::with_capacity(samples + 1);
        let mut y_max = 2.0_f64;

        for i in 0..=samples {
            let x = (i as f64 / samples as f64) * x_max;
            let (sx, sy) = evaluate_sensitivity(x, &self.config);
            y_max = y_max.max(sx).max(sy);
            pts_x.push((x, sx));
            pts_y.push((x, sy));
        }
        // Nice headroom above peak
        let y_top = (y_max * 1.15).ceil().max(2.0);
        let y_bot = 0.0_f64;

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

        // Vertical gridlines + labels (hand speed)
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

        // Custom LUT control point handles
        if self.config.mode == AccelMode::Lookup {
            for &[lx, ly] in &self.config.lut_points {
                let pt = to_px(lx, ly * self.config.sens_mult);
                frame.fill(&Path::circle(pt, 5.0), t.surface);
                frame.stroke(
                    &Path::circle(pt, 5.0),
                    Stroke::default().with_color(t.accent).with_width(2.0),
                );
            }
        }

        // Subtle outer plot border
        frame.stroke(
            &Path::rectangle(Point::new(pad_l, pad_t), Size::new(plot_w, plot_h)),
            Stroke::default().with_color(t.line_strong).with_width(1.0),
        );

        vec![frame.into_geometry()]
    }
}
