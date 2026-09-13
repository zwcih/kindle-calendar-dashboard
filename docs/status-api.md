# Kindle battery image API (server only)

`status_api.py` is an independent, loopback-only HTTP service. It reads the latest
static schedule PNG and returns a transient battery overlay. It does **not** import
`dashboard.py`, read its private config, fetch calendars/URLs, publish files, change
shares, modify the base PNG, or change the Kindle client.

The existing renderer already reserves the bottom 36 pixels and publishes by
atomic replacement. Keep that static generation/upload/share flow unchanged. A
future client can fall back to that static share on **any** API/network/image error.

## Contract

Public transport: **HTTPS**, terminated by a separately managed reverse proxy.
Deployment contract: backend **`http://127.0.0.1:8766/image`**; public
**`https://calendar.example.com/kindle-status/image`**. The separately managed Caddy
`handle_path /kindle-status/*` route strips `/kindle-status` before forwarding,
so the backend receives `POST /image`, not `/kindle-status/image`.
Backend transport is loopback HTTP only (literal loopback IPv4 or IPv6 address).
This implementation does not install/configure the proxy, TLS, firewall or a service.

```http
POST /image HTTP/1.1
Authorization: Bearer <runtime-token>
Content-Type: application/json
Content-Length: 31

{"battery":73,"charging":false}
```

Status is optional and best-effort. Battery is accepted only as an integer 0..100;
charging only as a JSON boolean. Missing/invalid battery displays `电量未知`;
missing/invalid charging omits the charging suffix. Unknown fields are ignored
and never interpreted as paths or URLs. Malformed JSON (including duplicate keys,
non-UTF-8, NaN/Infinity or trailing data) yields unknown status instead of rejecting
the image. Empty/bodyless POST is supported. Unsupported/missing Content-Type is
consumed within the body bound and treated as unknown status, not parsed.

The route must be exactly `/image`, without a query, alternate spelling,
absolute URL, traversal or extra slash. Header names and media type are
case-insensitive; authentication requires the literal `Bearer ` prefix.

Success: `200`, `Content-Type: image/png`, `Content-Length`, `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `Connection: close`.

The response is a **1072 × 1448, single-frame `L` grayscale PNG**:

- Rows **0..1411** preserve the base image's pixels byte-for-byte.
- Rows **1412..1447** are replaced with a white strip, a gray separator and
  right-aligned `电量 73%` or `电量 73% · 充电中`.
- Font size is **24 px**; the overlay uses only **0 / 160 / 255**, with text
  antialiasing disabled. Existing upper-image grays are preserved, not quantized.
- Font bounding boxes are checked and all status drawing occurs on a separate
  36-pixel-high image. Nothing can draw above the strip.
- Input metadata is removed, and the response is never saved or uploaded.
- Identical source pixels and status produce identical PNG bytes; every request
  reopens the source, so an atomic replacement appears on the next request.

## Startup

Python **3.11+**, POSIX/Linux and the existing `Pillow>=10.0,<13` dependency are
required. HTTP and process management use the standard library; no web framework
or new package is required. Use an existing CJK font, preferably the same Noto CJK
font as the static renderer. Glyph coverage is an operator responsibility; font
loading alone cannot prove that an arbitrary font contains Chinese glyphs.

Supply deployment-specific paths yourself; these are **placeholders**, not real
installation paths:

```sh
export KINDLE_STATUS_SOURCE=/absolute/path/to/latest-static-dashboard.png
export KINDLE_STATUS_FONT=/absolute/path/to/NotoSansCJK-Regular.ttc
python3 status_api.py --host 127.0.0.1 --port 8766 \
  --token-file /absolute/path/to/protected-status-token \
  --max-concurrency 2 --max-body 256 \
  --request-timeout 10 --render-timeout 3
```

The token file must already exist, be a regular file owned by the service user,
with mode **exactly `0600`** (including no special permission bits); `0400` is
also rejected by this deployment contract. Symlinks and
FIFOs are rejected. One terminal LF or CRLF is allowed. Store it outside the
repository, in a private parent directory, through a trusted credential facility.
Use a cryptographically random token (e.g. 32 random bytes encoded as base64url),
not a password or a token literal in a command/script. The accepted syntax is
32–256 bearer characters (`A-Z a-z 0-9 . _ ~ + / -`) plus up to two padding `=`.
Syntax checking does not establish entropy.

**Only `--token-file PATH` is supported.** The argument is a non-secret filesystem
path, never a token value. There is no token-value CLI argument, and abbreviated
CLI options are disabled. Both `KINDLE_STATUS_TOKEN` and `KINDLE_STATUS_TOKEN_FILE`
environment variables cause startup to fail; unset legacy values rather than
passing token contents through the environment. Never put credentials into a URL,
shell argument, repo file or access log. Tokens are loaded at startup; rotation
requires an operator-managed restart. The parent/operator provisions the file;
this module neither creates nor changes its permissions.

Other configuration supports environment defaults plus CLI overrides:

| CLI | Environment | Default / constraint |
| --- | --- | --- |
| `--token-file` | Not supported | Required path to owner-owned, exactly `0600` token file |
| `--source` | `KINDLE_STATUS_SOURCE` | Required fixed local PNG path |
| `--font` | `KINDLE_STATUS_FONT` | Required local CJK font path |
| `--host` | `KINDLE_STATUS_HOST` | `127.0.0.1`; literal loopback only, `::1` supported |
| `--port` | `KINDLE_STATUS_PORT` | `8766`; 0..65535 (0 for tests) |
| `--max-body` | `KINDLE_STATUS_MAX_BODY` | 256 bytes; allowed 32..4096 |
| `--max-concurrency` | `KINDLE_STATUS_MAX_CONCURRENCY` | 2; allowed 1..16 |
| `--request-timeout` | `KINDLE_STATUS_REQUEST_TIMEOUT` | 10 seconds; finite 0.1..120 |
| `--render-timeout` | `KINDLE_STATUS_RENDER_TIMEOUT` | 3 seconds; finite 0.1..120 |

No private URL, source path, token, calendar data or device value is compiled into
this module. Generic protocol constants, dimensions and bounded defaults are fixed.
Do not pass token values to `--help`, diagnostics or test commands. Startup errors
are intentionally generic and do not print credential values or source paths.
Font/overlay errors return the validated base pixels without a status overlay.
A missing source does not prevent startup; requests return 503 until it is available.
An unavailable font does not prevent serving the validated base image.

## Failures and resource limits

| Status | Meaning |
| --- | --- |
| 400 | Conflicting/invalid length, incomplete body, unsupported transfer/content encoding |
| 401 | Missing, invalid or duplicate authorization (with `WWW-Authenticate: Bearer`) |
| 404 | Not the exact endpoint |
| 405 | Common methods other than POST (with `Allow: POST`) |
| 413 | Declared body exceeds configured limit |
| 417 | Any Expect header; no 100-continue handshake |
| 431 | Request line/headers exceed 16 KiB aggregate (or standard-library header limits) |
| 503 | Source missing/corrupt/unsupported, image encode/worker failure, render deadline, or saturated admission slots |

Unknown HTTP methods get the standard-library 501 with a generic body. Grossly
malformed HTTP can get other parser errors; no error reflects user input. A full
request deadline closes the socket, including for slow headers, slow bodies or a
slow response reader; a PNG or error response is **not guaranteed after timeout**.
The client must treat connection close/timeouts as failure and retain/fetch static.
Responses, including parser errors and saturation, carry `no-store` where a response
can be sent. No access/body/token logging is enabled.

- SHA-256 digests of the configured/supplied bearer token are compared with
  `hmac.compare_digest`, including when supplied credentials are invalid. Never
  compare credentials with ordinary string equality.
- Authentication precedes body reads and rendering. Saturation and HTTP parser/
  Expect rejection can happen before authentication; no schedule is disclosed.
- Transfer-Encoding (including chunked) and Content-Encoding (including gzip) are
  rejected. At most one numeric Content-Length is allowed; absent means zero bytes. Parse
  `application/json`, optionally with `charset=utf-8` (quoted or unquoted). Other
  media types yield unknown status. Bodies are bounded before reading.
- A semaphore limits admitted connections **before** worker-thread creation.
  Accepted but busy connections receive 503 with `Retry-After: 1`; there is no
  unbounded application queue. Kernel listen backlog is 16. Idle connections count
  against capacity and expire. Slots remain occupied through response cleanup.
- Each authenticated valid request spawns one disposable render process. Its
  deadline is the smaller of the configured render timeout and the remaining
  request budget (less 0.1 seconds for delivery). A timer kills stalled work and
  the process is reaped. It cannot keep a rendering thread alive indefinitely.
  A separate socket watchdog enforces elapsed-time deadlines, not just per-read
  inactivity; trickling bytes does not extend them. These are application limits,
  not guarantees against OS-level failures or uninterruptible kernel I/O.
- PNG reads use one descriptor, are limited to **8 MiB**, require a regular file,
  and reject final-component symlinks/FIFOs. Validate PNG format, single frame,
  `L` mode, exact size and integrity before rendering. No stale image is cached
  when the configured source is absent/invalid. Error details are deliberately
  collapsed to `image_unavailable` (503).
- The producer must continue writing via temporary file + atomic rename, **not**
  in-place writes. An open old inode remains a consistent snapshot while the
  next request opens the new inode. Parent directories and configured font are
  trusted operator inputs and must not be writable by untrusted users. This is
  not a general file server, URL fetcher or path sandbox for hostile local admins.
- The base PNG is only opened read-only. Client status and response images exist
  only transiently in request/worker memory and IPC, never in app files or logs.
  OS swap/core dumps and reverse-proxy logging are separate deployment concerns.

## Proxy handoff (not deployed here)

The parent/operator must separately configure the agreed `/kindle-status/image`
public route via Caddy `handle_path /kindle-status/*`, forwarding to
`127.0.0.1:8766` with the prefix stripped. Terminate HTTPS with a certificate the
Kindle can validate, forward Authorization
and Content-Type unchanged, disable response caching, avoid header/body logs,
and align proxy body/read/upstream limits with the values above. Apply suitable
rate/access controls at the edge; loopback binding is not public TLS or rate
limiting. Do not expose this standard-library HTTP server directly to the Internet.
No Caddy, gateway, systemd/user-service, scheduler or share configuration is changed
by this implementation. Actual HTTPS routing and device compatibility remain
**unverified until that separate deployment/client work**.

## Verification (offline synthetic data)

```sh
python3 -m unittest -v test_status_api
python3 -m unittest discover -v
python3 -m py_compile status_api.py test_status_api.py
```

Tests start real HTTP servers on ephemeral loopback ports and use disposable
synthetic PNGs and randomly generated test-only tokens. They never read a real
schedule, live share, token file or calendar configuration. API tests use Noto CJK
if present, otherwise a portable font for geometry; set `KINDLE_STATUS_TEST_FONT`
to an available CJK font to require that font. The rest of the repository's tests
retain their own existing configuration conventions.

Coverage includes all battery boundaries/charging variants, PNG size/mode and
strip palette, preservation of upper pixels, unchanged source hash, metadata
removal, deterministic/latest image output, open-inode atomic rename semantics,
strict framing/authentication and malformed/optional status fallback, source errors/FIFO/symlink rejection, overloaded
concurrency and recovery, slow trickling requests, killed render workers, token
file ownership/modes, loopback-only configuration and absence of application logs.
Tests do not claim TLS, proxy, real-schedule or on-device integration coverage.
