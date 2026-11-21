//! Single writer, per-user pairing state.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, DirBuilder, File},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;

pub const TOKEN_TTL: u64 = 86400;
fn random<const N: usize>() -> io::Result<String> {
    let mut bytes = [0; N];
    getrandom::fill(&mut bytes).map_err(io::Error::other)?;
    Ok(bytes.iter().map(|x| format!("{x:02x}")).collect())
}
fn digest(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}
fn now() -> io::Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs())
}
fn save(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut f = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    f.write_all(bytes)?;
    f.as_file().sync_all()?;
    f.persist(path)?;
    Ok(())
}
#[derive(Serialize, Deserialize)]
struct Grant {
    hash: String,
    origin: String,
    expires: u64,
}
pub struct Session {
    dir: PathBuf,
    _lock: File,
    code: Option<String>,
    deadline: Instant,
    grant: Option<Grant>,
}
impl Session {
    pub fn open(base: &Path, lifetime: Duration) -> io::Result<Self> {
        if !base.is_absolute() {
            return Err(io::Error::other("state home must be absolute"));
        }
        let dir = base.join("mousetool-agent");
        DirBuilder::new().recursive(true).mode(0o700).create(&dir)?;
        let meta = dir.symlink_metadata()?;
        if !meta.is_dir()
            || meta.uid() != rustix::process::getuid().as_raw()
            || meta.mode() & 0o077 != 0
        {
            return Err(io::Error::other(
                "state directory must be private and user-owned",
            ));
        }
        let lock = File::open(&dir)?;
        lock.try_lock().map_err(io::Error::other)?;
        let grant = match fs::read(dir.join("grant.json")) {
            Ok(bytes) => Some(serde_json::from_slice(&bytes)?),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => return Err(e),
        };
        let code = random::<8>()?;
        save(&dir.join("pair-code"), code.as_bytes())?;
        Ok(Self {
            dir,
            _lock: lock,
            code: Some(code),
            deadline: Instant::now() + lifetime,
            grant,
        })
    }
    pub fn exchange(&mut self, code: &str, origin: &str) -> io::Result<Option<(String, u64)>> {
        if Instant::now() >= self.deadline
            || !self
                .code
                .as_ref()
                .is_some_and(|expected| bool::from(expected.as_bytes().ct_eq(code.as_bytes())))
        {
            return Ok(None);
        }
        let token = random::<32>()?;
        let expires = now()? + TOKEN_TTL;
        let grant = Grant {
            hash: digest(&token),
            origin: origin.into(),
            expires,
        };
        save(&self.dir.join("grant.json"), &serde_json::to_vec(&grant)?)?;
        self.grant = Some(grant);
        self.code = None;
        let _ = fs::remove_file(self.dir.join("pair-code"));
        Ok(Some((token, expires)))
    }
    pub fn authorized(&self, token: &str, origin: &str) -> bool {
        self.grant.as_ref().is_some_and(|g| {
            g.origin == origin && now().is_ok_and(|t| t < g.expires) && g.hash == digest(token)
        })
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.dir.join("pair-code"));
    }
}
