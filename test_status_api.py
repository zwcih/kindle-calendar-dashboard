"""Offline synthetic fixtures; only ephemeral loopback HTTP, no private configuration."""
import contextlib
from dataclasses import replace
import hashlib
import http.client
import io
import multiprocessing
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
import status_api as api


def stalled_worker(sender, *args):
    # Importable spawn target for proving hard decoder timeout and process cleanup.
    time.sleep(30)


class StatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("KINDLE_STATUS_TEST_FONT")
        candidates = [configured] if configured else [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "DejaVuSans.ttf"]
        for candidate in candidates:
            try:
                cls.font = Path(ImageFont.truetype(candidate, 24).path)
                break
            except OSError:
                continue
        else:
            raise unittest.SkipTest("set KINDLE_STATUS_TEST_FONT to a local TrueType/CJK font")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "base.png"
        self.make_source()
        self.secret = secrets.token_urlsafe(32)
        self.cfg = api.Config(self.source, self.font, self.secret, port=0)

    def make_source(self, color=255):
        image = Image.new("L", (1072, 1448), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 1000, 300), fill=160)
        draw.text((32, 40), "SYNTHETIC SCHEDULE", fill=0)
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("fixture", "must-not-be-copied")
        image.save(self.source, pnginfo=metadata)

    @contextlib.contextmanager
    def serving(self, cfg=None):
        server = api.StatusServer(cfg or self.cfg)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(server.threads)

    def request(self, server, body=b'{"battery":73,"charging":false}', *, path=api.ENDPOINT,
                method="POST", headers=None):
        conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
        hdr = {"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"}
        if headers:
            hdr.update(headers)
        try:
            conn.request(method, path, body=body, headers=hdr)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def raw(self, server, headers, body=b"", *, path=api.ENDPOINT, shutdown=False):
        conn = socket.create_connection(server.server_address[:2], timeout=5)
        try:
            raw = f"POST {path} HTTP/1.1\r\nHost: localhost\r\n".encode()
            raw += ("Authorization: Bearer " + self.secret + "\r\n").encode()
            raw += b"\r\n".join(headers) + b"\r\n\r\n" + body
            conn.sendall(raw)
            if shutdown:
                conn.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(conn)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def wait_for_cleanup(self, server):
        with server.threads_lock:
            finishing = list(server.threads)
        for thread in finishing:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def assert_no_store(self, response, code):
        status, headers, _ = response
        self.assertEqual(status, code)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Connection"], "close")

    def test_success_pixels_states_and_immutable_source(self):
        original = self.source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        with self.serving() as server:
            for battery in (0, 1, 73, 100):
                results = []
                for charging in (False, True):
                    payload = ('{"battery":%d,"charging":%s}' %
                               (battery, str(charging).lower())).encode()
                    response = self.request(server, payload)
                    self.assert_no_store(response, 200)
                    _, headers, data = response
                    self.assertEqual(headers["Content-Type"], "image/png")
                    self.assertEqual(int(headers["Content-Length"]), len(data))
                    with Image.open(io.BytesIO(data)) as output, Image.open(io.BytesIO(original)) as base:
                        self.assertEqual(output.mode, "L")
                        self.assertEqual(output.size, (1072, 1448))
                        self.assertEqual(output.format, "PNG")
                        self.assertFalse(output.info)
                        upper = (0, 0, 1072, 1412)
                        self.assertEqual(output.crop(upper).tobytes(), base.crop(upper).tobytes())
                        strip = output.crop((0, 1412, 1072, 1448))
                        self.assertEqual(set(strip.getdata()), {0, 160, 255})
                        results.append(strip.tobytes())
                self.assertNotEqual(*results)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["base.png"])

    def test_latest_atomic_replacement_and_deterministic_output(self):
        with self.serving() as server:
            first = self.request(server)[2]
            self.assertEqual(first, self.request(server)[2])
            temporary = self.root / "new.png"
            Image.new("L", (1072, 1448), 160).save(temporary)
            os.replace(temporary, self.source)
            latest = self.request(server)[2]
            self.assertNotEqual(first, latest)
            with Image.open(io.BytesIO(latest)) as image:
                self.assertEqual(image.crop((0, 0, 1072, 1412)).getextrema(), (160, 160))

    def test_open_descriptor_survives_atomic_replace(self):
        before = self.source.read_bytes()
        new = self.root / "replacement.png"
        Image.new("L", (1072, 1448), 160).save(new)
        fdopen = os.fdopen

        def replace_after_open(fd, *args, **kwargs):
            os.replace(new, self.source)
            return fdopen(fd, *args, **kwargs)

        with patch.object(api.os, "fdopen", side_effect=replace_after_open):
            self.assertEqual(api.read_regular_file(self.source, api.MAX_SOURCE_BYTES), before)
        self.assertNotEqual(self.source.read_bytes(), before)

    def test_auth_fails_before_render_or_body_read(self):
        with self.serving() as server, patch.object(api, "bounded_render") as render:
            for authorization in ("", "Basic x", "Bearer wrong", "Bearer " + self.secret + "x"):
                response = self.request(server, b"invalid", headers={"Authorization": authorization})
                self.assert_no_store(response, 401)
                self.assertEqual(response[1]["WWW-Authenticate"], "Bearer")
            response = self.raw(server, [("Authorization: Bearer " + self.secret).encode(),
                                         b"Content-Length: 999999"])
            self.assert_no_store(response, 401)
            render.assert_not_called()

    def test_status_errors_still_return_image(self):
        bodies = [b"", b"bad", b"[]", b"null", b"1", b"{}",
                  b'{"battery":10}', b'{"battery":10,"charging":false,"url":"https://example.test"}',
                  b'{"battery":-1,"charging":false}', b'{"battery":101,"charging":false}',
                  b'{"battery":true,"charging":false}', b'{"battery":10.0,"charging":false}',
                  b'{"battery":"10","charging":false}', b'{"battery":1e1,"charging":false}',
                  b'{"battery":10,"charging":0}', b'{"battery":10,"charging":"false"}',
                  b'{"battery":10,"charging":null}', b'{"battery":NaN,"charging":false}',
                  b'{"battery":Infinity,"charging":false}',
                  b'{"battery":10,"battery":20,"charging":false}',
                  b'{"battery":10,"charging":false,"charging":true}',
                  b'{"battery":10,"charging":false}{}',
                  b'\xef\xbb\xbf{"battery":10,"charging":false}', b'\xff',
                  b'{"battery":10,"charging":false,}']
        with self.serving() as server:
            for body in bodies:
                with self.subTest(body=body):
                    response = self.request(server, body)
                    self.assert_no_store(response, 200)
                    with Image.open(io.BytesIO(response[2])) as image:
                        self.assertEqual(image.size, (1072, 1448))

    def test_body_size_and_content_types(self):
        with self.serving() as server:
            self.assert_no_store(self.request(server, b"x" * 257), 413)
            # Excessive declared size is rejected without waiting for any body.
            self.assert_no_store(self.raw(server, [b"Content-Length: 99999999"]), 413)
            for content_type in ("text/plain", "", "application/json; charset=latin1",
                                 "application/json; boundary=x", "application/json; charset=utf-8; x=y"):
                self.assert_no_store(self.request(server, headers={"Content-Type": content_type}), 200)
            self.assert_no_store(self.request(server, headers={
                "Content-Type": 'Application/JSON; charset="UTF-8"'}), 200)
            self.assert_no_store(self.raw(server, [b"Content-Length: 2"], b"{}"), 200)

    def test_http_framing(self):
        cases = [([], 200), ([b"Content-Length: -1"], 400),
                 ([b"Content-Length: +1"], 400), ([b"Content-Length: 1,1"], 400),
                 ([b"Content-Length: 2", b"Content-Length: 2"], 400),
                 ([b"Transfer-Encoding: chunked"], 400),
                 ([b"Content-Encoding: gzip"], 400), ([b"Expect: 100-continue"], 417),
                 ([b"Expect: anything"], 417),
                 ([b"Content-Length: 0", b"Content-Type: application/json",
                   b"Content-Type: application/json"], 200)]
        with self.serving() as server:
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    self.assert_no_store(self.raw(server, headers), expected)
            self.assert_no_store(self.raw(server, [b"Content-Length: 10",
                                                  b"Content-Type: application/json"],
                                          b"{}", shutdown=True), 400)

    def test_header_limits(self):
        with self.serving() as server:
            self.assert_no_store(self.raw(server, [b"X-Large: " + b"a" * 17000]), 431)
            self.assert_no_store(self.raw(server, [b"X-Small: " + b"a" * 1000] * 20), 431)
            self.assert_no_store(self.raw(server, [], path="/" + "a" * 17000), 431)

    def test_no_paths_urls_queries_or_alternate_methods(self):
        with self.serving() as server, patch.object(api, "bounded_render") as render:
            for path in ("/", "/status.png", "/kindle-status/image", "/../base.png", "/%2e%2e/base.png", "/image?battery=4",
                         "//image", "/image/", "http://example.test/image"):
                self.assert_no_store(self.request(server, path=path), 404)
            for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
                self.assert_no_store(self.request(server, method=method), 405)
            render.assert_not_called()

    def test_sources_unavailable_invalid_or_unsafe(self):
        with self.serving() as server:
            self.source.unlink()
            self.assert_no_store(self.request(server), 503)
            self.source.write_bytes(b"not a png")
            self.assert_no_store(self.request(server), 503)
            for mode, size, fmt in (("RGB", (1072, 1448), "PNG"), ("L", (10, 10), "PNG"),
                                    ("P", (1072, 1448), "PNG"), ("L", (1072, 1448), "BMP")):
                Image.new(mode, size).save(self.source, format=fmt)
                self.assert_no_store(self.request(server), 503)
            self.make_source()
            self.source.write_bytes(self.source.read_bytes()[:-20])
            self.assert_no_store(self.request(server), 503)
            self.source.write_bytes(b"x" * (api.MAX_SOURCE_BYTES + 1))
            self.assert_no_store(self.request(server), 503)
            self.source.unlink()
            self.source.mkdir()
            self.assert_no_store(self.request(server), 503)
            self.source.rmdir()
            other = self.root / "other.png"
            Image.new("L", (1072, 1448)).save(other)
            self.source.symlink_to(other)
            self.assert_no_store(self.request(server), 503)
            self.source.unlink()
            os.mkfifo(self.source)
            start = time.monotonic()
            self.assert_no_store(self.request(server), 503)
            self.assertLess(time.monotonic() - start, 2)
            self.source.unlink()
            self.make_source()
            self.assert_no_store(self.request(server), 200)

    def test_animated_png_is_rejected(self):
        a = Image.new("L", (1072, 1448), 255)
        b = Image.new("L", (1072, 1448), 0)
        a.save(self.source, save_all=True, append_images=[b], duration=100, loop=0)
        with self.serving() as server:
            self.assert_no_store(self.request(server), 503)

    def test_missing_font_returns_base_image(self):
        with self.serving(replace(self.cfg, font=self.root / "missing.ttf")) as server:
            response = self.request(server)
            self.assert_no_store(response, 200)
            with Image.open(io.BytesIO(response[2])) as result, Image.open(self.source) as base:
                self.assertEqual(result.tobytes(), base.tobytes())

    def test_optional_status_normalization(self):
        for raw in (b"", b"{}", b"bad", b'{"battery":101}', b'{"battery":-1}'):
            self.assertEqual(api.parse_status(raw), (None, None))
        self.assertEqual(api.parse_status(b'{"battery":73}'), (73, None))
        self.assertEqual(api.parse_status(b'{"charging":true}'), (None, True))
        self.assertEqual(api.parse_status(b'{"battery":73,"charging":"bad"}'), (73, None))

    def test_concurrency_saturation_and_recovery(self):
        entered, release = threading.Event(), threading.Event()
        original = api.bounded_render

        def held_render(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return original(*args)

        with self.serving(replace(self.cfg, max_concurrency=1)) as server:
            responses = []
            with patch.object(api, "bounded_render", side_effect=held_render):
                first = threading.Thread(target=lambda: responses.append(self.request(server)))
                first.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assert_no_store(self.request(server), 503)
                    self.assertEqual(len(server.threads), 1)
                finally:
                    release.set()
                    first.join(4)
                self.assert_no_store(responses[0], 200)
            # Reading the response can finish just before the server's finally block.
            self.wait_for_cleanup(server)
            self.assert_no_store(self.request(server), 200)

    def test_slow_header_and_body_total_deadline_and_slot_recovery(self):
        cfg = replace(self.cfg, max_concurrency=1, request_timeout=0.4)
        with self.serving(cfg) as server:
            for prefix in (b"POST /image HTTP/1.1\r\nX-Slow: ",
                           ("POST /image HTTP/1.1\r\nAuthorization: Bearer " + self.secret +
                            "\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n").encode()):
                conn = socket.create_connection(server.server_address[:2], timeout=2)
                start = time.monotonic()
                try:
                    conn.sendall(prefix)
                    for _ in range(12):
                        try:
                            conn.sendall(b"a")
                        except OSError:
                            break
                        time.sleep(0.06)
                    self.assertEqual(conn.recv(4096), b"")
                finally:
                    conn.close()
                self.assertLess(time.monotonic() - start, 1.2)
                # An invalid request proves slot recovery without spending the short deadline on PNG work.
                self.wait_for_cleanup(server)
                self.assert_no_store(self.request(server, b"{}", headers={"Authorization": "Bearer invalid"}), 401)
                self.wait_for_cleanup(server)

    def test_render_timeout_kills_worker_and_recovers(self):
        cfg = replace(self.cfg, render_timeout=0.25, max_concurrency=1)
        children = {p.pid for p in multiprocessing.active_children()}
        with self.serving(cfg) as server:
            with patch.object(api, "_render_worker", stalled_worker):
                start = time.monotonic()
                self.assert_no_store(self.request(server), 503)
                self.assertLess(time.monotonic() - start, 2)
            self.wait_for_cleanup(server)
            self.assertEqual({p.pid for p in multiprocessing.active_children()}, children)
            self.assert_no_store(self.request(server, b"{}", headers={"Authorization": "Bearer invalid"}), 401)

    def test_no_logs_or_response_disclosure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.serving() as server:
                self.source.unlink()
                for response in (self.request(server), self.request(server, b"invalid"),
                                 self.request(server, path="/secret-path")):
                    for forbidden in (self.secret.encode(), str(self.source).encode(), b"secret-path"):
                        self.assertNotIn(forbidden, response[2])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(self.secret, repr(self.cfg))

    def test_loopback_and_configuration_validation(self):
        for host in ("0.0.0.0", "::", "192.0.2.1", "localhost", "example.test"):
            with self.assertRaises(ValueError):
                replace(self.cfg, host=host)
        for field, values in (("max_concurrency", [0, 17, True]), ("max_body", [0, 5000]),
                              ("port", [-1, 65536]), ("render_timeout", [0, float("nan"), float("inf")]),
                              ("request_timeout", [-1, 121]), ("token", ["bad", "x" * 300, "x " * 32]),
                              ("source", ["", "https://example.test/base.png"])):
            for value in values:
                with self.assertRaises(ValueError):
                    replace(self.cfg, **{field: value})
        self.assertEqual(replace(self.cfg, host="::1").host, "::1")

    def test_cli_environment_overrides_and_redacted_startup_failure(self):
        file = self.root / "token"
        file.write_text(self.secret, encoding="ascii")
        file.chmod(0o600)
        cli = ["--token-file", str(file)]
        env = {"KINDLE_STATUS_SOURCE": str(self.source), "KINDLE_STATUS_FONT": str(self.font),
               "KINDLE_STATUS_PORT": "9001",
               "KINDLE_STATUS_MAX_CONCURRENCY": "3", "KINDLE_STATUS_RENDER_TIMEOUT": "2.5"}
        with patch.dict(os.environ, env, clear=True), patch.object(api, "StatusServer") as constructor:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(api.main(cli + ["--port", "0"]), 0)
            config = constructor.call_args.args[0]
            self.assertEqual(config.port, 0)
            self.assertEqual(config.max_concurrency, 3)
            self.assertEqual(config.render_timeout, 2.5)
            self.assertEqual(config.source, self.source)
            self.assertNotIn(self.secret, stdout.getvalue())
        with patch.dict(os.environ, {**env, "KINDLE_STATUS_HOST": "not-an-ip"}, clear=True):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(api.main(cli), 2)
            self.assertNotIn(self.secret, stderr.getvalue())
            self.assertNotIn(str(self.root), stderr.getvalue())
        for variable in ("KINDLE_STATUS_TOKEN", "KINDLE_STATUS_TOKEN_FILE"):
            with patch.dict(os.environ, {**env, variable: self.secret}, clear=True):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(api.main(cli), 2)
        for extra in (["--token", "synthetic-not-sent"], ["--token-f", str(file)]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                api.main(cli + extra)
            self.assertEqual(result.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            api.main([])
        self.assertEqual(result.exception.code, 2)

    def test_idle_connections_use_admission_slots_and_expire(self):
        cfg = replace(self.cfg, max_concurrency=1, request_timeout=0.3)
        entered = threading.Event()
        with self.serving(cfg) as server:
            original = server.finish_request

            def observe(*args):
                entered.set()
                return original(*args)

            with patch.object(server, "finish_request", side_effect=observe):
                conn = socket.create_connection(server.server_address[:2], timeout=2)
                try:
                    self.assertTrue(entered.wait(1))
                    self.assert_no_store(self.request(server), 503)
                    self.assertEqual(conn.recv(1024), b"")
                finally:
                    conn.close()
            self.wait_for_cleanup(server)
            self.assert_no_store(self.request(server, b"{}", headers={"Authorization": "Bearer invalid"}), 401)

    def test_no_keepalive_or_pipelined_second_request(self):
        with self.serving() as server:
            conn = socket.create_connection(server.server_address[:2], timeout=2)
            try:
                request = ("POST /image HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " +
                           self.secret + "\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                           "Connection: keep-alive\r\n\r\n{}").encode()
                conn.sendall(request + request)
                data = b""
                while True:
                    part = conn.recv(4096)
                    if not part:
                        break
                    data += part
                self.assertEqual(data.count(b"HTTP/1.1"), 1)
                self.assertIn(b"Connection: close", data)
            finally:
                conn.close()

    def test_token_protected_file_only(self):
        file = self.root / "token"
        file.write_text(self.secret + "\n", encoding="ascii")
        file.chmod(0o600)
        self.assertEqual(api.load_token(file), self.secret)
        with self.assertRaises(ValueError):
            api.load_token("")
        for mode in (0o644, 0o400, 0o640, 0o700, 0o1600):
            file.chmod(mode)
            with self.assertRaises(ValueError):
                api.load_token(file)
        file.chmod(0o600)
        link = self.root / "token-link"
        link.symlink_to(file)
        with self.assertRaises(OSError):
            api.load_token(link)
        info = os.stat(file)
        fake = type("Info", (), {"st_mode": info.st_mode, "st_size": info.st_size,
                                 "st_uid": os.geteuid() + 1})()
        with patch.object(api.os, "fstat", return_value=fake), self.assertRaises(ValueError):
            api.load_token(file)
        for invalid in ("short", self.secret + "\n\n"):
            file.write_text(invalid)
            with self.assertRaises(ValueError):
                api.load_token(file)


if __name__ == "__main__":
    unittest.main()
