# MT-011 · Hosted Mouse Tool and its optional Linux helper

**Decision for review, step 1.** Publish the existing static UI over HTTPS. Keep the JavaScript drivers as the single implementation of browser device protocols. The optional helper is a per-user, loopback-only Rust executable. This step implements discovery, pairing and authenticated capability discovery using a fixture. Real backends, distribution packages and UI integration belong to later steps.

## What users can do

| User action | Owner and availability |
| --- | --- |
| Select a mouse and grant access, read or set DPI/stages, polling rate, button mappings, lighting, power settings, read battery while the page is open | Chromium WebHID and the existing driver for that model. Expose only the features supported by the selected device. Use vendor-specific interfaces because standard mouse/keyboard collections are protected. The helper never proxies raw HID reports for the page. |
| Read/apply a kernel acceleration curve, read actual motion and normalized speed, list system mice, inspect/change desktop pointer settings | Native helper with the corresponding backend and OS permissions. |
| Monitor battery and deliver desktop notifications while the page is closed | Native background process. Plan OS power-supply data first. Per-model availability and cache freshness must be visible. |

Future discovery should check the fixed loopback status URL, with a short timeout and an explicit Connect action when browser permission is needed. Offer installation, pairing, or permission instructions according to the outcome. Gate individual controls on authenticated capabilities. A missing helper, denied permission or absent backend leaves device controls available. Show Chromium and HTTPS requirements before asking for WebHID permission.

## Wire contract implemented here

Use HTTP/1.1 at `http://127.0.0.1:8767`. The port can be changed with `MOUSETOOL_AGENT_PORT` (zero selects an ephemeral test port). Every request needs one literal Host matching the bound IP and actual port, and one allowed Origin. Comparison includes scheme and port. No cookies, URL credentials, query tokens or origin guessing. Routes match the exact path. JSON replies use `Cache-Control: no-store`. Clients should accept additional JSON object fields within v1. Incompatible semantics require a new version prefix.

| Method and path | Credential / request | Success |
| --- | --- | --- |
| GET /api/v1/status | Origin and Host only | 200 `{"api_version":1,"service":"mousetool-agent"}`. Safe to probe before disclosing device or account data. |
| POST /api/v1/pair | One-time code in `{"code":"<16 hex characters>"}`, Content-Type exactly `application/json` | 200 `{"token":"<64 hex characters>","expires_at":<Unix seconds>}`. A successful exchange consumes the code and replaces the previous grant. |
| GET /api/v1/capabilities | `Authorization: Bearer <token>` | 200 `{"capabilities":[]}` for the fixture. An empty list disables all host operations. |
| OPTIONS on those paths | Origin, Host, and Access-Control-Request-Method matching the route. For PNA, include Access-Control-Request-Private-Network with value true. Optional requested headers may contain only Authorization and Content-Type, case-insensitive. | 204, empty body. Echo the exact allowed origin and route method, and allow the two named headers. Send Access-Control-Allow-Private-Network only for a valid PNA request. Preflight accesses no application data. |

All application routes beyond status and code exchange require the origin-bound bearer. Ordinary bearer reuse within the grant lifetime is intentional. Replaying a consumed code, expired token, or superseded token fails. Errors are JSON objects with an `error` string. Framing/JSON errors return 400, invalid credentials 401, host/origin/preflight denial 403, unknown authenticated route 404, wrong authenticated method 405, oversized payload 413, wrong pairing media type 415, quota exhaustion 429, persistence failure 500. A transport timeout can close the connection. Clients must handle a CORS/network failure when rejected before a trusted Origin is established.

## Pairing and security

Launch `mousetool-agent ORIGINS.json`. Example configuration for fixtures is `{"origins":["https://ui.example"]}`. The installer must provision the actual first-party origin in a user-controlled file. Canonical HTTPS origins and explicit HTTP localhost or 127.0.0.1 development origins are accepted. Empty lists, wildcard hosts, paths, userinfo and opaque origins fail validation. The legacy file page has an opaque Origin and continues to use Python.

The user reads `pair-code` under `${XDG_STATE_HOME:-$HOME/.local/state}/mousetool-agent/` and enters it in the trusted UI. The code expires five minutes after startup (monotonic time). If it expires, restart the helper and read the newly written pair-code file. Rereading the old file before restarting yields the expired code. The helper returns a random 256-bit bearer once and atomically stores its SHA-256 verifier, origin and 24-hour expiry in `grant.json`. The state directory must be owned by the user and inaccessible to group/others. Generated credential files have mode 0600. An exclusive directory lock serializes instances. Pairing state changes are serialized with requests. Restart preserves the grant. To revoke immediately, stop the helper, remove its grant file, then restart. Keep the browser bearer in page memory, clear it on disconnect, and never put the code or token in logs, URLs, analytics or persistent browser storage.

| Threat | Control and residual risk |
| --- | --- |
| Drive-by site | Exact Origin on all methods, including simple requests. No wildcard, credentialed cookies, or state-changing GET. Approved origins also need the code/token. A compromised script on an approved origin can act as the user. Use a dedicated first-party origin with controlled scripts. |
| DNS rebinding | Bind only IPv4 loopback and check literal Host including port. Ignore forwarding headers. A domain resolving to loopback still supplies the wrong Host. |
| Malicious extension | Origin checks protect ordinary web callers. A privileged extension may read page memory, alter requests or impersonate the UI. Minimize browser persistence and revoke/re-pair after removing it. |
| Another local user | Loopback is shared. Native clients can forge Origin and Host. Private state, OS randomness and token auth protect the running server. A local process can occupy the port while the agent is down, impersonate it or exhaust quotas. Discovery alone does not authenticate the server to the page. Resolve server identity before deploying on hostile multi-user machines. |
| Token theft | A stolen bearer plus its Origin can be replayed by a native caller until rotation or expiry. Expiry assumes a trustworthy system clock. Per-origin binding, private verifier storage, no-store responses and explicit revocation limit exposure. Root and same-user malware are outside this boundary. |
| Request floods and slow connections | Where browser permissions allow loopback access, a visited page can send repeated requests. Host and Origin checks run before quota accounting. Incomplete requests occupy individual worker slots until their deadlines. Sustained saturation of all eight slots can still deny service, and a native caller forging an allowed origin can exhaust the shared quota. |

The listener admits up to eight simultaneous requests and closes excess connections. Workers read and write sockets independently. The API lock serializes quota and grant updates only while handling a parsed request. Limits are 32 headers in 8 KiB, a 4 KiB body, 500 ms to receive the first byte, two seconds total to read a request, and a one-second write timeout. Duplicate headers and transfer encoding are rejected. Fixed one-minute windows admit 120 requests that pass Host and Origin checks and five pairing attempts. An allowed caller receives a CORS-readable 429 when its request exceeds the shared quota. SIGINT/SIGTERM wake the accept loop and wait for admitted workers to finish.

**Browser compatibility.** Ordinary CORS preflights support Chromium's Local Network Access permission flow for pairing and authenticated reads. Older PNA clients can request the additional private-network response header. Both paths enforce the same origin, method and header checks. The wire tests cover both shapes. Test the hosted UI in actual Chrome and Edge before release, including browser permission denial and a blocked origin, which can both appear as network errors. [Chrome Local Network Access announcement](https://developer.chrome.com/blog/local-network-access)

## Python parity and migration

Baseline is local dev at `d911db9`, specifically `mousetool.py`'s Api and serve handlers. Python serves the API on 127.0.0.1:8766. Its launcher opens the local HTML file. The existing app keeps those entry points throughout migration. Below, only v1 status, pair and capabilities are implemented.

| Current Python behavior | Proposed authenticated Rust v1 equivalent / parity gate |
| --- | --- |
| GET /api/status returns version 2, maccel, niri, desktop and system_mice | Public v1 status returns protocol identity only. Assemble the old dashboard from capabilities and private accel, desktop, devices reads. Preserve Niri-specific fields for legacy consumers. |
| GET /api/devices returns system discovery in mice | GET /api/v1/devices. Keep system inventory distinct from the WebHID chooser. |
| GET /api/desktop returns desktop state. POST /api/desktop validates and applies a profile/speed update, then reads it back | GET/POST /api/v1/desktop. Preserve backend, label, writable, accel_profile, accel_speed, message, permission conflicts and partial-write recovery across Niri, GNOME, Plasma, Hyprland and COSMIC. |
| POST /api/niri edits the Niri mouse block under lock, returns niri state. Its read is embedded in status | Planned Niri adapter via v1 desktop plus Niri compatibility view. Test legacy empty/extra-key behavior and comment/format preservation before retiring the old route. |
| POST /api/maccel validates MODE and parameter ranges, converts to fixed point, writes parameters, attempts persistence, refreshes motion DPI and returns maccel state | GET/POST /api/v1/accel. Return backend identity, availability, permissions, mode and values. Carry over MA-002's reviewed backend selection, write validation and persistence rules. /api/accel has no handler in this Python revision. |
| GET /api/motion and /api/motion/stream are aliases for SSE with optional dpi (50–50000), JSON data frames and idle keepalives | GET /api/v1/motion as authenticated streaming fetch (native EventSource lacks custom Authorization). Match speed/raw_speed, peak/hold/age, swipe extrema and ID, DPI scaling, source, active state and device count. Preserve first/last subscriber acquisition/release, idle decay and disconnect behavior. Long-lived streams will need a separate subscriber limit to preserve capacity for control requests. |
| Battery is a CLI operation. Single-device reads cache fresh values and report stale/age on failure. --all combines wireless mice and kernel power supplies. /api/battery has no handler | Proposed GET /api/v1/battery for OS-side observations, with percent, charging, source, freshness and age. Keep foreground WebHID readings in JS. Gate notification settings separately after MT-009, including duplicate suppression and unavailable notification service. |
| OPTIONS handles legacy CORS and POST checks X-Mouse-Tool | Use the v1 preflight and credential rules above. Preserve the Python client/server pair until migration. |

The mouseaccel fork retains maccel curve names, parameter formats and the eight-byte big-endian speed sample. Its module, sysfs parameter directory and character device use the distinct name mouseaccel. GAP_MIN_MS and GAP_MAX_MS are fork-only fixed-point parameters. The kernel fork has per-device state, reports the latest motion frame speed across devices, refuses an existing conflict and enters pass-through if maccel loads later. Surface conflicts explicitly and avoid writing both modules. Keep backend availability, readable motion and writable acceleration as separate capabilities.

1. Review this contract and resolve browser permission handling before changing the page. Wait for overlapping UI work to merge.
2. Add native read backends behind traits. Compare captured/synthetic responses against Python fixtures for missing modules, read-only access, sleeping devices, unsupported desktops and motion timing. Keep writes disabled.
3. Add narrowly scoped writes with validation, readback and recovery tests. Compare against the merged MA-002/MT-009 behavior and retain Python CLI coverage. Never expose arbitrary filesystem paths, shell commands or raw device writes.
4. Add discovery and pairing to the hosted UI and select one helper for each session. Exercise denied browser permission, expired grant, offline helper, absent module and reconnect. Keep the existing local Python launcher available for rollback. Retire it only after feature/permission/error parity and user acceptance.

## Installation and packaging

**Linux target flow.** For WebHID, install the repository's single 60-mousetool.rules under /etc/udev/rules.d, reload rules and reconnect the mouse so logind can grant seat access. The optional helper installer should be one user command installing a verified static binary, allowlist and systemd user unit, then starting it and explaining where to retrieve the pairing code. The installer command and signed distribution channel are still to be defined. Run the helper as the logged-in user. The existing install.sh installs the Python path.

Only acceleration requires a separate one-time administrator DKMS step. With reviewed mouseaccel 0.1.0 sources staged at /usr/src/mouseaccel-0.1.0, DKMS and matching kernel headers installed, the intended install command is `sudo dkms install -m mouseaccel -v 0.1.0`. Package installation must also arrange module loading, conflict handling and narrowly scoped parameter permissions. The helper can display instructions when those prerequisites are missing. Arch/CachyOS, deb and rpm packaging and Secure Boot signing need distribution testing. No installer or module action is part of this prototype.

**Windows later.** Keep the same Chromium device tier and wire contract. Plan a signed per-user installer with a login-started helper and per-user ACL-protected credentials. Evaluate service/session isolation, token storage and a Raw Accel adapter separately. Driver installation/elevation belongs to an explicit installer action. Linux desktop and kernel features stay capability-gated. Windows builds and install behavior remain unverified.

## Dependency and footprint choices

The standard library supplies TCP, directory locking and deadlines. Each direct dependency has one role.

| Crate | Purpose |
| --- | --- |
| httparse | Parse HTTP headers within a fixed buffer. |
| url | Validate canonical serialized browser origins, including IDNA. |
| serde | Define typed configuration, pairing requests and persisted records. |
| serde_json | Encode/decode JSON state and wire formats. |
| getrandom | Obtain secret bytes from the OS. |
| subtle | Compare equal-length pairing codes in constant time. |
| sha2 | Store only a SHA-256 verifier of the random token. |
| tempfile | Create private temporary files, atomically replace grants, and isolate test directories. |
| rustix | Verify the state directory owner against the current UID. |
| signal-hook | Receive Unix termination signals safely. |

The lockfile pins transitive dependencies. No async runtime, device library or desktop bus is needed for this fixture.

**Linux release measurement.** From agent/, build with `RUSTFLAGS='-C target-feature=+crt-static' cargo build --locked --offline --release --target x86_64-unknown-linux-gnu`. Using rustc 1.99.0, opt-level z, LTO, one codegen unit, abort-on-panic and stripping, `stat -c %s` reported **1,825,528 bytes**. `file` reported a stripped x86-64 static PIE. The ELF dynamic table had no NEEDED entries. Release SHA-256 was `05a8a017f921af9fd6141c5f243f55f41dbd182359a90d7c1dec11ff8c9a87f6`.

Three fresh fixture processes each bound an ephemeral loopback port with temporary HOME and XDG state. One second after each listening message, before any client connection, /proc/PID/status VmRSS was **1,652, 1,604 and 1,512 KiB** (median **1,604 KiB**). SIGTERM, SIGINT and SIGTERM respectively returned exit status 0 and removed the pending code. These samples measure the idle skeleton on the build host. Device backends and active streaming will require fresh measurements.

## Decisions for the user

- Which dedicated HTTPS origin should ship, and should a local HTTP development origin ship only in a developer config?
- Is a hostile local user in the deployment threat model, requiring authenticated server discovery?
- Is one active pairing per user, with 24-hour tokens and restart-to-renew codes, sufficient? Should a future CLI issue codes and revoke grants while the service runs?
- Which distribution gets the first signed package, and what is the supported path for systems without systemd user services or with Secure Boot?
- Which Windows acceleration backend and battery sources should count toward parity? Are accounts or synced profiles wanted in a separate project?
