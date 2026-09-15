#!/usr/bin/env python3
"""Loopback-only, stateless battery overlay API. TLS belongs to a reverse proxy.

No imports from dashboard.py: its configuration and publishing flow stay isolated.
See docs/status-api.md for configuration, contract, limits and deployment boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
import stat
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, STRIP_HEIGHT, FONT_SIZE = 1072, 1448, 36, 24
ENDPOINT = "/image"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]{32,256}={0,2}", re.ASCII)


@dataclass(frozen=True)
class Config:
    source: Path
    font: Path
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8766
    max_body: int = 256
    max_concurrency: int = 2
    request_timeout: float = 10.0
    render_timeout: float = 3.0

    def __post_init__(self):
        address = ipaddress.ip_address(self.host)
        if not address.is_loopback:
            raise ValueError("bind address must be a literal loopback IP")
        if not TOKEN_PATTERN.fullmatch(self.token):
            raise ValueError("token must be 32-256 bearer characters, with optional padding")
        for name, low, high in (("port", 0, 65535), ("max_body", 32, 4096),
                                ("max_concurrency", 1, 16)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid numeric configuration")
        for value in (self.request_timeout, self.render_timeout):
            if not math.isfinite(value) or not 0.1 <= value <= 120:
                raise ValueError("timeouts must be finite, between 0.1 and 120 seconds")
        for name in ("source", "font"):
            value = os.fspath(getattr(self, name))
            if not value or "://" in value:
                raise ValueError("source and font must be local filesystem paths")
            object.__setattr__(self, name, Path(value).absolute())


def read_regular_file(path: Path, limit: int, *, protected: bool = False) -> bytes:
    """One open descriptor survives an atomic rename; never follow final symlinks/FIFOs."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("expected a bounded regular file")
        if protected and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("token file must be owned by this user with mode exactly 0600")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file too large")
        return data


def load_token(filename: str | Path) -> str:
    if not filename:
        raise ValueError("a protected token file is required")
    value = read_regular_file(Path(filename), 512, protected=True).decode("ascii")
    # Permit one final LF or CRLF, not arbitrary surrounding whitespace.
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not TOKEN_PATTERN.fullmatch(value):
        raise ValueError("invalid bearer token format")
    return value


def parse_status(data: bytes) -> tuple[int | None, bool | None]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("non-finite JSON number")

    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None, None
    if type(obj) is not dict:
        return None, None
    battery = obj.get("battery")
    charging = obj.get("charging")
    if type(battery) is not int or not 0 <= battery <= 100:
        battery = None
    if type(charging) is not bool:
        charging = None
    return battery, charging


def render_png(source: Path, font_path: Path, battery: int | None, charging: bool | None) -> bytes:
    raw = read_regular_file(source, MAX_SOURCE_BYTES)
    with Image.open(io.BytesIO(raw)) as image:
        if (image.format != "PNG" or image.mode != "L" or
                image.size != (WIDTH, HEIGHT) or getattr(image, "n_frames", 1) != 1):
            raise ValueError("source must be a single-frame 1072x1448 L PNG")
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        # Copy only pixels, never source text, EXIF, transparency or other metadata.
        output = Image.frombytes("L", image.size, image.tobytes())
    try:
        strip = Image.new("L", (WIDTH, STRIP_HEIGHT), 255)
        draw = ImageDraw.Draw(strip)
        # Disable antialiasing so the entire overlay uses only 0/160/255.
        draw.fontmode = "1"
        font = ImageFont.truetype(str(font_path), FONT_SIZE)
        text = (f"电量 {battery}%" if battery is not None else "电量未知") + (" · 充电中" if charging is True else "")
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tw, th = right - left, bottom - top
        if tw > WIDTH - 32 or th > STRIP_HEIGHT - 4:
            raise ValueError("font does not fit status strip")
        draw.line((16, 0, WIDTH - 17, 0), fill=160)
        draw.text((WIDTH - 16 - tw - left, (STRIP_HEIGHT - th) // 2 - top),
                  text, font=font, fill=0)
        output.paste(strip, (0, HEIGHT - STRIP_HEIGHT))
    except Exception:
        # Overlay failure must not prevent delivery of the validated base pixels.
        pass
    result = io.BytesIO()
    output.save(result, format="PNG")
    return result.getvalue()


def _render_worker(sender, source, font, battery, charging):
    """One disposable worker; no tokens, request bodies or errors in output/logs."""
    try:
        with warnings.catch_warnings():
            # Reject suspicious decoder warnings rather than log source details.
            warnings.simplefilter("error")
            sender.send_bytes(b"\x01" + render_png(source, font, battery, charging))
    except Exception:
        try:
            sender.send_bytes(b"\x00")
        except (OSError, EOFError):
            pass
    finally:
        sender.close()


def bounded_render(config: Config, battery: int, charging: bool, timeout: float) -> bytes:
    """Kill a stuck decoder/filesystem operation, rather than leak timed-out threads."""
    if timeout <= 0:
        raise TimeoutError
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_render_worker,
                         args=(sender, config.source, config.font, battery, charging), daemon=True)
    timer = None
    started = False
    try:
        worker.start()
        started = True
        sender.close()
        # Also interrupts recv_bytes if the worker stalls halfway through a pipe write.
        timer = threading.Timer(timeout, worker.kill)
        timer.daemon = True
        timer.start()
        if not receiver.poll(timeout):
            raise TimeoutError
        data = receiver.recv_bytes(MAX_RESPONSE_BYTES)
        if not data or data[:1] != b"\x01":
            raise ValueError("render unavailable")
        return data[1:]
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        sender.close()
        receiver.close()
        if started:
            worker.join(0.05)
            if worker.is_alive():
                worker.kill()
            worker.join()
            worker.close()


class _HeaderReader:
    """Bound aggregate request-line/header bytes, including many short headers."""
    def __init__(self, stream):
        self.stream = stream
        self.used = 0

    def readline(self, size=-1):
        data = self.stream.readline(min(size, MAX_HEADER_BYTES + 1) if size >= 0
                                    else MAX_HEADER_BYTES + 1)
        self.used += len(data)
        if self.used > MAX_HEADER_BYTES:
            raise http.client.LineTooLong("request headers")
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


class StatusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KindleStatus"
    sys_version = ""

    def setup(self):
        super().setup()
        self.rfile = _HeaderReader(self.rfile)

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        # Base class errors must not echo paths, method text, headers or tracebacks.
        self.reply(code, b'{"error":"invalid_request"}\n')

    def handle(self):
        # Exactly one request per connection: no pipelining or keep-alive state.
        self.close_connection = True
        self.command, self.requestline, self.request_version = "", "", "HTTP/1.1"
        self.deadline = time.monotonic() + self.server.config.request_timeout
        try:
            self.handle_one_request()
        except http.client.LineTooLong:
            self.reply(431, b'{"error":"headers_too_large"}\n')
        except (OSError, ValueError):
            pass

    def handle_expect_100(self):
        self.reply(417, b'{"error":"expect_not_supported"}\n')
        return False

    def reply(self, code, body, *, png=False, extra=()):
        self.close_connection = True
        # Even malformed/HTTP-0.9 requests receive explicit no-store error headers.
        self.request_version = "HTTP/1.1"
        self.send_response(code)
        self.send_header("Content-Type", "image/png" if png else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self):
        cfg = self.server.config
        auth = self.headers.get_all("Authorization", [])
        supplied = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
        # Fixed-size hashes make comparison independent of supplied token length.
        valid = hmac.compare_digest(hashlib.sha256(supplied.encode("utf-8")).digest(),
                                    self.server.token_digest)
        if not valid:
            self.reply(401, b'{"error":"unauthorized"}\n',
                       extra=(("WWW-Authenticate", "Bearer"),))
            return
        if self.path != ENDPOINT or self.requestline.split()[1] != ENDPOINT:
            self.reply(404, b'{"error":"not_found"}\n')
            return
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Encoding"):
            self.reply(400, b'{"error":"unsupported_encoding"}\n')
            return
        if self.headers.get_all("Expect"):
            self.reply(417, b'{"error":"expect_not_supported"}\n')
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            lengths = ["0"]  # A bodyless POST is a valid image-only request.
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0], re.ASCII):
            self.reply(400, b'{"error":"invalid_length"}\n')
            return
        length = int(lengths[0])
        if length > cfg.max_body:
            self.reply(413, b'{"error":"body_too_large"}\n')
            return
        types = self.headers.get_all("Content-Type", [])
        json_type = len(types) == 1 and bool(re.fullmatch(
            r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?',
            types[0], re.IGNORECASE | re.ASCII))
        data = self.rfile.read(length)
        if len(data) != length:
            self.reply(400, b'{"error":"incomplete_body"}\n')
            return
        battery, charging = parse_status(data) if json_type else (None, None)
        del data
        try:
            budget = min(cfg.render_timeout, self.deadline - time.monotonic() - 0.1)
            png = bounded_render(cfg, battery, charging, budget)
        except Exception:
            self.reply(503, b'{"error":"image_unavailable"}\n', extra=(("Retry-After", "1"),))
            return
        self.reply(200, png, png=True)

    def _method_not_allowed(self):
        self.reply(405, b'{"error":"method_not_allowed"}\n', extra=(("Allow", "POST"),))

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed


class StatusServer(HTTPServer):
    """Bound admission before thread creation; saturated requests are never queued in workers."""
    request_queue_size = 16
    allow_reuse_address = True

    def __init__(self, config: Config):
        self.config = config
        self.token_digest = hashlib.sha256(config.token.encode("ascii")).digest()
        self.slots = threading.BoundedSemaphore(config.max_concurrency)
        self.threads = set()
        self.threads_lock = threading.Lock()
        self.address_family = socket.AF_INET6 if ipaddress.ip_address(config.host).version == 6 else socket.AF_INET
        super().__init__((config.host, config.port), StatusHandler)

    @staticmethod
    def _expire(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.05)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nCache-Control: no-store\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        thread = threading.Thread(target=self._serve_one, args=(request, client_address), daemon=True)
        with self.threads_lock:
            self.threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self.threads_lock:
                self.threads.discard(thread)
            self.slots.release()
            self.shutdown_request(request)

    def _serve_one(self, request, client_address):
        timer = threading.Timer(self.config.request_timeout, self._expire, args=(request,))
        timer.daemon = True
        try:
            request.settimeout(self.config.request_timeout)
            timer.start()
            self.finish_request(request, client_address)
        except Exception:
            # No tracebacks: they can expose local paths or request data.
            pass
        finally:
            timer.cancel()
            timer.join()
            self.shutdown_request(request)
            with self.threads_lock:
                self.threads.discard(threading.current_thread())
            self.slots.release()

    def server_close(self):
        super().server_close()
        with self.threads_lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join()

    def handle_error(self, request, client_address):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--token-file", required=True,
                        help="path to an existing owner-only 0600 token file (never the token value)")
    options = (("source", str, None), ("font", str, None), ("host", str, "127.0.0.1"),
               ("port", int, 8766), ("max-body", int, 256), ("max-concurrency", int, 2),
               ("request-timeout", float, 10), ("render-timeout", float, 3))
    for name, kind, default in options:
        parser.add_argument("--" + name, type=kind,
                            default=os.environ.get("KINDLE_STATUS_" + name.upper().replace("-", "_"), default))
    args = parser.parse_args(argv)
    try:
        if not args.source or not args.font:
            raise ValueError("source and CJK font paths are required")
        if "KINDLE_STATUS_TOKEN" in os.environ or "KINDLE_STATUS_TOKEN_FILE" in os.environ:
            raise ValueError("token environment variables are unsupported; use --token-file")
        filename = vars(args).pop("token_file")
        config = Config(**vars(args), token=load_token(filename))
        # Font failure degrades to the validated base image at request time.
        with StatusServer(config) as server:
            print("Kindle status API listening on loopback (HTTP; TLS proxy required)", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError):
        print("Status API startup failed: check non-secret configuration, font and token permissions.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
