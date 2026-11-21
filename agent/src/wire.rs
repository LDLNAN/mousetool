//! One bounded HTTP/1.1 request per connection. Connection close is the response delimiter.
use super::{Api, Backend, Reply};
use std::{
    io::{self, Read, Write},
    net::TcpStream,
    sync::Mutex,
    time::{Duration, Instant},
};

pub(super) fn header<'a>(r: &'a httparse::Request, name: &str) -> Option<&'a str> {
    r.headers
        .iter()
        .find(|h| h.name.eq_ignore_ascii_case(name))
        .and_then(|h| std::str::from_utf8(h.value).ok())
}
fn read_chunk(s: &mut TcpStream, end: Instant, buf: &mut [u8]) -> io::Result<usize> {
    let wait = end
        .checked_duration_since(Instant::now())
        .ok_or(io::ErrorKind::TimedOut)?;
    s.set_read_timeout(Some(wait.max(Duration::from_millis(1))))?;
    s.read(buf)
}
fn request<B: Backend>(s: &mut TcpStream, api: &Mutex<Api<B>>) -> io::Result<Reply> {
    let start = Instant::now();
    let end = start + Duration::from_secs(2);
    let mut bytes = [0; 8192];
    let mut used = 0;
    let boundary = loop {
        let mut slots = [httparse::EMPTY_HEADER; 32];
        match httparse::Request::new(&mut slots).parse(&bytes[..used]) {
            Ok(httparse::Status::Complete(n)) => break n,
            Ok(httparse::Status::Partial) if used < bytes.len() => (),
            _ => return Ok(Reply::err(400, "invalid_headers")),
        }
        let deadline = if used == 0 {
            start + Duration::from_millis(500)
        } else {
            end
        };
        let n = read_chunk(s, deadline, &mut bytes[used..])?;
        if n == 0 {
            return Ok(Reply::err(400, "incomplete_request"));
        }
        used += n;
    };
    let mut slots = [httparse::EMPTY_HEADER; 32];
    let mut req = httparse::Request::new(&mut slots);
    req.parse(&bytes[..boundary])
        .expect("already parsed")
        .unwrap();
    if req.version != Some(1)
        || req.headers.iter().enumerate().any(|(i, h)| {
            !h.value.is_ascii()
                || req.headers[..i]
                    .iter()
                    .any(|prev| h.name.eq_ignore_ascii_case(prev.name))
        })
        || header(&req, "transfer-encoding").is_some()
    {
        return Ok(Reply::err(400, "invalid_headers"));
    }
    let length = header(&req, "content-length").unwrap_or("0");
    if length.is_empty() || !length.bytes().all(|b| b.is_ascii_digit()) {
        return Ok(Reply::err(400, "invalid_length"));
    }
    let size = length.parse::<usize>().unwrap_or(usize::MAX);
    if size > 4096 {
        return Ok(Reply::err(413, "body_too_large"));
    }
    let mut body = Vec::from(&bytes[boundary..used]);
    if body.len() > size {
        return Ok(Reply::err(400, "invalid_length"));
    }
    let mut pos = body.len();
    body.resize(size, 0);
    while pos < size {
        let n = read_chunk(s, end, &mut body[pos..])?;
        if n == 0 {
            return Ok(Reply::err(400, "incomplete_request"));
        }
        pos += n;
    }
    Ok(api.lock().expect("API lock poisoned").handle(&req, &body))
}
pub fn serve_one<B: Backend>(mut socket: TcpStream, api: &Mutex<Api<B>>) -> io::Result<()> {
    socket.set_write_timeout(Some(Duration::from_secs(1)))?;
    let r = request(&mut socket, api)?;
    let data = if r.status == 204 {
        Vec::new()
    } else {
        serde_json::to_vec(&r.body)?
    };
    let mut response = format!(
        "HTTP/1.1 {} {}\r\nConnection: close\r\nCache-Control: no-store\r\nVary: Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network\r\n",
        r.status,
        if r.status < 400 { "OK" } else { "Rejected" }
    );
    if r.status != 204 {
        response.push_str(&format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            data.len()
        ));
    }
    for (name, value) in r.headers {
        response.push_str(&format!("{name}: {value}\r\n"));
    }
    response.push_str("\r\n");
    socket.write_all(response.as_bytes())?;
    socket.write_all(&data)?;
    socket.shutdown(std::net::Shutdown::Write)
}
