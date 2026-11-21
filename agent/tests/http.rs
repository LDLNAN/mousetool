//! Real loopback wire format, ephemeral listener and private throwaway state per test.
use mousetool_agent::{Api, Config, Fixture, Session, serve_one};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::Path,
    time::Duration,
};
use tempfile::{TempDir, tempdir};

const ORIGIN: &str = "https://ui.example";
struct Server {
    listener: TcpListener,
    api: std::sync::Mutex<Api<Fixture>>,
    dir: TempDir,
}
impl Server {
    fn new() -> Self {
        Self::start(tempdir().unwrap(), 300)
    }
    fn start(dir: TempDir, seconds: u64) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let cfg = Config {
            origins: vec![ORIGIN.into(), "https://other.example".into()],
        };
        let session = Session::open(dir.path(), Duration::from_secs(seconds)).unwrap();
        let api = Api::new(cfg, listener.local_addr().unwrap().port(), session, Fixture).unwrap();
        Self {
            listener,
            api: std::sync::Mutex::new(api),
            dir,
        }
    }
    fn restart(self) -> Self {
        drop(self.api);
        Self::start(self.dir, 300)
    }
    fn path(&self, name: &str) -> std::path::PathBuf {
        self.dir.path().join("mousetool-agent").join(name)
    }
    fn code(&self) -> String {
        fs::read_to_string(self.path("pair-code")).unwrap()
    }
    fn raw(&mut self, request: &str) -> String {
        let addr = self.listener.local_addr().unwrap();
        let mut client = TcpStream::connect(addr).unwrap();
        client
            .write_all(request.replace("HOST", &addr.to_string()).as_bytes())
            .unwrap();
        client.shutdown(Shutdown::Write).unwrap();
        serve_one(self.listener.accept().unwrap().0, &self.api).unwrap();
        let mut out = String::new();
        client.read_to_string(&mut out).unwrap();
        out
    }
    fn call(&mut self, method: &str, path: &str, extra: &str, body: &str) -> String {
        self.raw(&format!("{method} /api/v1/{path} HTTP/1.1\r\nHost: HOST\r\nOrigin: {ORIGIN}\r\n{extra}Content-Length: {}\r\n\r\n{body}",body.len()))
    }
    fn pair(&mut self, body: &str) -> String {
        self.call("POST", "pair", "Content-Type: application/json\r\n", body)
    }
    fn issue(&mut self) -> String {
        let res = self.pair(&json!({"code":self.code()}).to_string());
        status(&res, 200);
        let body: Value = serde_json::from_str(res.split_once("\r\n\r\n").unwrap().1).unwrap();
        body["token"].as_str().unwrap().into()
    }
}
fn status(s: &str, n: u16) {
    assert!(
        s.starts_with(&format!("HTTP/1.1 {n} ")),
        "expected {n}, received {s}"
    );
    assert!(!s.contains("Allow-Origin: *"));
}
fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}
#[test]
fn origin_host_and_framing() {
    let mut s = Server::new();
    for (headers, n) in [
        ("Host: HOST", 403),
        ("Origin: https://ui.example", 403),
        ("Host: evil.example\r\nOrigin: https://ui.example", 403),
        ("Host: HOST\r\nOrigin: *", 403),
        ("Host: HOST\r\nOrigin: null", 403),
        ("Host: HOST\r\nOrigin: https://ui.example.evil", 403),
        ("Host: HOST\r\nHost: HOST", 400),
        ("Origin: *\r\nOrigin: https://ui.example", 400),
        ("Host: HOST\r\nContent-Length: 4097", 413),
        ("Content-Length: -1", 400),
        ("Transfer-Encoding: chunked", 400),
        ("Content-Length: 10", 400),
        ("bad header", 400),
    ] {
        let res = s.raw(&format!("GET /api/v1/status HTTP/1.1\r\n{headers}\r\n\r\n"));
        status(&res, n);
        assert!(!res.contains("Access-Control-Allow-Origin"));
    }
    let res = s.call("GET", "status", "", "");
    status(&res, 200);
    assert!(res.contains("Access-Control-Allow-Origin: https://ui.example\r\n"));
}
#[test]
fn preflight() {
    let mut s = Server::new();
    let method = "Access-Control-Request-Method: GET\r\n";
    let pna = "Access-Control-Request-Private-Network: true\r\n";
    for h in [
        pna.into(),
        format!("{method}Access-Control-Request-Private-Network: false\r\n"),
        format!("{pna}Access-Control-Request-Method: POST\r\n"),
        format!("{method}{pna}Access-Control-Request-Headers: *\r\n"),
    ] {
        let r = s.call("OPTIONS", "capabilities", &h, "");
        status(&r, 403);
        assert!(!r.contains("Access-Control-Allow-Private-Network"));
    }
    for (path, method, hdr) in [
        ("pair", "POST", "content-type"),
        ("capabilities", "GET", "authorization"),
    ] {
        let r = s.call("OPTIONS", path, &format!("Access-Control-Request-Method: {method}\r\nAccess-Control-Request-Headers: {hdr}\r\n"), "");
        status(&r, 204);
        assert!(r.contains("Access-Control-Allow-Origin: https://ui.example\r\n"));
        assert!(r.contains(&format!("Access-Control-Allow-Methods: {method}\r\n")));
        assert!(!r.contains("Access-Control-Allow-Private-Network"));
    }
    let r = s.call(
        "OPTIONS",
        "capabilities",
        &format!("{method}{pna}Access-Control-Request-Headers: authorization\r\n"),
        "",
    );
    status(&r, 204);
    assert!(r.contains("Access-Control-Allow-Private-Network: true\r\n"));
}
#[test]
fn grant_lifecycle() {
    let mut s = Server::new();
    let code = s.code();
    assert_eq!(mode(&s.path("pair-code")), 0o600);
    assert_eq!(mode(&s.path("")), 0o700);
    status(&s.call("GET", "capabilities", "", ""), 401);
    status(
        &s.call("GET", "capabilities", "Authorization: Bearer abc\r\n", ""),
        401,
    );
    status(&s.pair("{}"), 400);
    status(&s.pair(r#"{"code":"incorrect"}"#), 401);
    let token = s.issue();
    assert_eq!(mode(&s.path("grant.json")), 0o600);
    assert!(
        !fs::read_to_string(s.path("grant.json"))
            .unwrap()
            .contains(&token)
    );
    assert!(!s.path("pair-code").exists());
    status(&s.pair(&json!({"code":code}).to_string()), 401);
    let auth = format!("Authorization: Bearer {token}\r\n");
    let mut s = s.restart();
    let r = s.call("GET", "capabilities", &auth, "");
    status(&r, 200);
    assert!(r.contains(r#"{"capabilities":[]}"#));
    status(&s.raw(&format!("GET /api/v1/capabilities HTTP/1.1\r\nHost: HOST\r\nOrigin: https://other.example\r\n{auth}\r\n")),401);
    status(&s.call("POST", "capabilities", &auth, ""), 405);
    status(&s.call("GET", "missing", &auth, ""), 404);
    s.issue();
    status(&s.call("GET", "capabilities", &auth, ""), 401);
}
#[test]
fn expired_credentials() {
    let mut s = Server::start(tempdir().unwrap(), 0);
    status(&s.pair(&json!({"code":s.code()}).to_string()), 401);
    let mut s = s.restart();
    let token = s.issue();
    let path = s.path("grant.json");
    let mut grant: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    grant["expires"] = json!(0);
    fs::write(path, grant.to_string()).unwrap();
    let mut s = s.restart();
    status(
        &s.call(
            "GET",
            "capabilities",
            &format!("Authorization: Bearer {token}\r\n"),
            "",
        ),
        401,
    );
}
#[test]
fn failures_and_budget() {
    let mut s = Server::new();
    status(&s.call("POST", "pair", "", "{}"), 415);
    fs::create_dir(s.path("grant.json")).unwrap();
    let body = json!({"code":s.code()}).to_string();
    status(&s.pair(&body), 500);
    fs::remove_dir(s.path("grant.json")).unwrap();
    status(&s.pair(&body), 200);
    for _ in 0..2 {
        status(&s.pair("{}"), 400);
    }
    status(&s.pair("{}"), 429);
    for _ in 6..120 {
        status(&s.call("GET", "status", "", ""), 200);
    }
    status(&s.call("GET", "status", "", ""), 429);
}

#[test]
fn invalid_config_fails_before_state_creation() {
    let dir = tempdir().unwrap();
    let cfg = dir.path().join("origins.json");
    for origin in ["*", "null", "https://*.example"] {
        fs::write(&cfg, json!({"origins":[origin]}).to_string()).unwrap();
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_mousetool-agent"))
            .arg(&cfg)
            .env_clear()
            .env("XDG_STATE_HOME", dir.path())
            .env("MOUSETOOL_AGENT_PORT", "0")
            .output()
            .unwrap();
        assert!(!result.status.success());
        assert!(!dir.path().join("mousetool-agent").exists());
    }
}
#[test]
fn state_rejects_contention_and_insecure_directory() {
    let s = Server::new();
    let open = || Session::open(s.dir.path(), Duration::from_secs(300));
    assert!(open().is_err());
    drop(s.api);
    fs::set_permissions(
        s.dir.path().join("mousetool-agent"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    assert!(open().is_err());
}

#[test]
fn rejected_callers_preserve_request_budget() {
    let mut s = Server::new();
    for (host, origin) in [
        ("HOST", "https://elsewhere.example"),
        ("wrong.example", ORIGIN),
    ] {
        for _ in 0..120 {
            let r = s.raw(&format!(
                "GET /api/v1/status HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\n\r\n"
            ));
            status(&r, 403);
            assert!(!r.contains("Access-Control-Allow-Origin"));
        }
    }
    for _ in 0..120 {
        status(&s.call("GET", "status", "", ""), 200);
    }
    let response = s.call("GET", "status", "", "");
    status(&response, 429);
    assert!(response.contains("Access-Control-Allow-Origin: https://ui.example\r\n"));
}

#[test]
fn incomplete_connections_leave_capacity_for_status() {
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Command, Stdio};
    struct FixtureProcess(Child);
    impl Drop for FixtureProcess {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let dir = tempdir().unwrap();
    let cfg = dir.path().join("config.json");
    fs::write(&cfg, json!({"origins":[ORIGIN]}).to_string()).unwrap();
    let mut server = FixtureProcess(
        Command::new(env!("CARGO_BIN_EXE_mousetool-agent"))
            .arg(cfg)
            .env_clear()
            .env("XDG_STATE_HOME", dir.path())
            .env("MOUSETOOL_AGENT_PORT", "0")
            .stdout(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut banner = String::new();
    BufReader::new(server.0.stdout.take().unwrap())
        .read_line(&mut banner)
        .unwrap();
    let addr = banner
        .trim()
        .strip_prefix("Listening on http://127.0.0.1:")
        .unwrap()
        .parse::<u16>()
        .unwrap();
    let connect = || TcpStream::connect(("127.0.0.1", addr)).unwrap();
    let mut pending = Vec::new();
    for i in 0..5 {
        let mut stream = connect();
        if i % 2 == 0 {
            stream.write_all(b"GET ").unwrap();
        }
        pending.push(stream);
    }
    let mut client = connect();
    client
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    write!(
        client,
        "GET /api/v1/status HTTP/1.1\r\nHost: 127.0.0.1:{addr}\r\nOrigin: {ORIGIN}\r\n\r\n"
    )
    .unwrap();
    let mut response = String::new();
    client.read_to_string(&mut response).unwrap();
    status(&response, 200);
}
