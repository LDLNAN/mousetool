mod state;
mod wire;
use serde::Deserialize;
use serde_json::{Value, json};
pub use state::Session;
use std::time::{Duration, Instant};
use url::Url;
pub use wire::serve_one;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub origins: Vec<String>,
}
impl Config {
    pub fn validate(&self) -> bool {
        !self.origins.is_empty()
            && self.origins.iter().all(|s| {
                Url::parse(s).is_ok_and(|u| {
                    !s.contains('*')
                        && u.origin().ascii_serialization() == *s
                        && (u.scheme() == "https"
                            || (u.scheme() == "http"
                                && matches!(u.host_str(), Some("127.0.0.1" | "localhost"))))
                })
            })
    }
}
pub trait Backend {
    fn capabilities(&self) -> Vec<&'static str>;
}
pub struct Fixture;
impl Backend for Fixture {
    fn capabilities(&self) -> Vec<&'static str> {
        vec![]
    }
}

struct Window {
    start: Instant,
    used: usize,
    limit: usize,
}
impl Window {
    fn new(limit: usize) -> Self {
        Self {
            start: Instant::now(),
            used: 0,
            limit,
        }
    }
    fn admit(&mut self) -> bool {
        if self.start.elapsed() >= Duration::from_secs(60) {
            self.start = Instant::now();
            self.used = 0;
        }
        if self.used >= self.limit {
            return false;
        }
        self.used += 1;
        true
    }
}
pub struct Api<B> {
    config: Config,
    host: String,
    session: Session,
    backend: B,
    requests: Window,
    pairs: Window,
}
impl<B: Backend> Api<B> {
    pub fn new(
        config: Config,
        port: u16,
        session: Session,
        backend: B,
    ) -> Result<Self, &'static str> {
        if !config.validate() {
            return Err("expected exact canonical HTTPS or HTTP loopback origins");
        }
        Ok(Self {
            config,
            host: format!("127.0.0.1:{port}"),
            session,
            backend,
            requests: Window::new(120),
            pairs: Window::new(5),
        })
    }
    fn handle(&mut self, req: &httparse::Request, body: &[u8]) -> Reply {
        let header = |name: &str| wire::header(req, name);
        if header("host") != Some(&self.host) {
            return Reply::err(403, "bad_host");
        }
        let origin = header("origin").unwrap_or("");
        if !self.config.origins.iter().any(|o| o == origin) {
            return Reply::err(403, "bad_origin");
        }
        let mut reply = if self.requests.admit() {
            self.route(req, body, origin)
        } else {
            Reply::err(429, "rate_limit")
        };
        reply
            .headers
            .push(("Access-Control-Allow-Origin", origin.into()));
        reply
    }
    fn route(&mut self, req: &httparse::Request, body: &[u8], origin: &str) -> Reply {
        let header = |name: &str| wire::header(req, name);
        let path = req.path.unwrap_or("");
        let method = req.method.unwrap_or("");
        let expected = match path {
            "/api/v1/status" | "/api/v1/capabilities" => "GET",
            "/api/v1/pair" => "POST",
            _ => "",
        };
        if method == "OPTIONS" {
            let private_network = header("access-control-request-private-network");
            if expected.is_empty()
                || header("access-control-request-method") != Some(expected)
                || private_network.is_some_and(|v| v != "true")
                || header("access-control-request-headers").is_some_and(|h| {
                    !h.split(',').all(|s| {
                        ["authorization", "content-type"]
                            .iter()
                            .any(|a| s.trim().eq_ignore_ascii_case(a))
                    })
                })
            {
                return Reply::err(403, "bad_preflight");
            }
            let mut headers = vec![
                ("Access-Control-Allow-Methods", expected.into()),
                (
                    "Access-Control-Allow-Headers",
                    "Authorization, Content-Type".into(),
                ),
            ];
            if private_network.is_some() {
                headers.push(("Access-Control-Allow-Private-Network", "true".into()));
            }
            return Reply {
                status: 204,
                body: Value::Null,
                headers,
            };
        }
        if path == "/api/v1/pair" && method == "POST" {
            if !self.pairs.admit() {
                return Reply::err(429, "rate_limit");
            }
            if header("content-type") != Some("application/json") {
                return Reply::err(415, "content_type");
            }
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Pair {
                code: String,
            }
            let Ok(pair) = serde_json::from_slice::<Pair>(body) else {
                return Reply::err(400, "invalid_json");
            };
            return match self.session.exchange(&pair.code, origin) {
                Ok(Some((token, expires))) => {
                    Reply::ok(json!({"token":token,"expires_at":expires}))
                }
                Ok(None) => Reply::err(401, "invalid_code"),
                Err(_) => Reply::err(500, "state_error"),
            };
        }
        if path == "/api/v1/status" && method == "GET" {
            return Reply::ok(json!({"api_version":1,"service":"mousetool-agent"}));
        }
        if !header("authorization")
            .and_then(|h| h.strip_prefix("Bearer "))
            .is_some_and(|t| self.session.authorized(t, origin))
        {
            return Reply::err(401, "unauthorized");
        }
        match (path, method) {
            ("/api/v1/capabilities", "GET") => {
                Reply::ok(json!({"capabilities":self.backend.capabilities()}))
            }
            _ if expected.is_empty() => Reply::err(404, "not_found"),
            _ => Reply::err(405, "method"),
        }
    }
}
struct Reply {
    status: u16,
    body: Value,
    headers: Vec<(&'static str, String)>,
}
impl Reply {
    fn ok(body: Value) -> Self {
        Self {
            status: 200,
            body,
            headers: vec![],
        }
    }
    fn err(status: u16, code: &str) -> Self {
        Self {
            status,
            body: json!({"error":code}),
            headers: vec![],
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn origins() {
        for s in [
            "*",
            "https://*.example",
            "null",
            "http://example.org",
            "https://example.org/",
            "https://a@b",
            "https://example.org:443",
            "file:///x",
        ] {
            assert!(
                !Config {
                    origins: vec![s.into()]
                }
                .validate(),
                "{s}"
            );
        }
    }
    #[test]
    fn window_recovers() {
        let mut w = Window::new(1);
        assert!(w.admit());
        assert!(!w.admit());
        w.start -= Duration::from_secs(60);
        assert!(w.admit());
    }
}
