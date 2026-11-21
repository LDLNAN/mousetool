use mousetool_agent::{Api, Config, Fixture, Session, serve_one};
use signal_hook::{
    consts::{SIGINT, SIGTERM},
    iterator::Signals,
};
use std::{
    env, fs, io,
    net::TcpListener,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg: Config = serde_json::from_slice(&fs::read(
        env::args_os()
            .nth(1)
            .ok_or("usage: mousetool-agent ORIGINS.json")?,
    )?)?;
    if !cfg.validate() {
        return Err("invalid origin configuration".into());
    }
    let port = env::var("MOUSETOOL_AGENT_PORT")
        .unwrap_or_else(|_| "8767".into())
        .parse::<u16>()?;
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let port = listener.local_addr()?.port();
    let base = env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state")))
        .ok_or("set XDG_STATE_HOME")?;
    let session = Session::open(&base, Duration::from_secs(300))?;
    let api = Mutex::new(Api::new(cfg, port, session, Fixture)?);
    let stop = Arc::new(AtomicBool::new(false));
    let mut signals = Signals::new([SIGTERM, SIGINT])?;
    let wake = listener.local_addr()?;
    let stopping = stop.clone();
    std::thread::spawn(move || {
        if signals.forever().next().is_some() {
            stopping.store(true, Ordering::Release);
            let _ = std::net::TcpStream::connect(wake);
        }
    });
    println!("Listening on http://127.0.0.1:{port}");
    std::thread::scope(|scope| {
        const MAX_CONNECTIONS: usize = 8;
        let mut workers: Vec<std::thread::ScopedJoinHandle<'_, ()>> = Vec::new();
        for conn in listener.incoming() {
            if stop.load(Ordering::Acquire) {
                break;
            }
            let socket = match conn {
                Ok(socket) => socket,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    eprintln!("accept failed: {e}");
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
            };
            workers.retain(|worker| !worker.is_finished());
            if workers.len() == MAX_CONNECTIONS {
                continue;
            }
            match std::thread::Builder::new().spawn_scoped(scope, || {
                let _ = serve_one(socket, &api);
            }) {
                Ok(worker) => workers.push(worker),
                Err(e) => eprintln!("connection worker unavailable: {e}"),
            }
        }
    });
    Ok(())
}
