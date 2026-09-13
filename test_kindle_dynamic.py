"""Extract real client code; synthetic loopback TLS only, never device scripts."""

import contextlib
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib

from test_kindle import CONTROLLER, DEVICE, HELPER, ROOT, SH, WORKER, function


def png(level=73, depth=8, color=0, width=1072, interlace=0):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    pixels = (b"\0" + bytes([level]) * width) * 1448
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, 1448, depth, color, 0, 0, interlace))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


SYNTHETIC_BEARER = "fixture-" + "aB9_~+/.-" * 5 + "=="


@unittest.skipUnless(SH, "Requires an existing POSIX shell")
class DynamicConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.config = self.work / "config.local.conf"
        self.auth = self.work / "image-auth.local.conf"
        self.config.write_text("IMAGE_MODE=dynamic\nIMAGE_URL=https://example.invalid/image\n"
                               "WIFI_SSID=fixture\n", encoding="utf-8")
        self.auth.write_text("BEARER_TOKEN" + "=" + SYNTHETIC_BEARER + "\n", encoding="utf-8")

    def shell(self, source):
        return subprocess.run([SH], input="set -f\numask 077\n" + HELPER + "\n" + source,
                              cwd=self.work, text=True, capture_output=True, timeout=15)

    def test_modes_and_invalid_configuration(self):
        for mode, code in (("", 10), ("post", 10), ("dynamic", 0), ("static", 0)):
            self.config.write_text(f"IMAGE_MODE={mode}\nIMAGE_URL=https://example.invalid/image\n"
                                   "WIFI_SSID=fixture\n", encoding="utf-8")
            result = self.shell("calendar_load_config config.local.conf\n")
            self.assertEqual(result.returncode, code, result.stderr)
        for value in ('https://example.invalid/"', "https://example.invalid/#fragment",
                      "https://example.invalid/\\", "https://example.invalid/\r",
                      "https://example.invalid/\nheader=x"):
            self.config.write_text(f"IMAGE_MODE=dynamic\nIMAGE_URL={value}\nWIFI_SSID=fixture\n")
            result = self.shell("calendar_load_config config.local.conf\n")
            self.assertEqual(result.returncode, 10)
            self.assertNotIn(value, result.stderr)

    def test_missing_invalid_symlink_and_oversized_credentials(self):
        invalid = ["", "BEARER_TOKEN=\n", "BEARER_TOKEN=a\r\n", "BEARER_TOKEN=a\n\n",
                   "BEARER_TOKEN=a\nheader=x\n", "BEARER_TOKEN=a\x00b\n",
                   "BEARER_TOKEN=a b\n", 'BEARER_TOKEN=a"b\n',
                   "BEARER_TOKEN=a\\b\n", "BEARER_TOKEN=$(touch sentinel)\n",
                   "BEARER_TOKEN=a=b\n", "BEARER_TOKEN=" + "x" * 4096 + "\n",
                   "# comment\nBEARER_TOKEN=a\n"]
        for data in invalid:
            self.auth.write_bytes(data.encode())
            result = self.shell("IMAGE_MODE=dynamic\ncalendar_validate_auth image-auth.local.conf\n")
            self.assertEqual(result.returncode, 10, repr(data))
            self.assertEqual(result.stdout, "")
            self.assertIn("AUTH_CONFIG_ERROR:", result.stderr)
            self.assertFalse((self.work / "sentinel").exists())
        self.auth.unlink()
        result = self.shell("IMAGE_MODE=dynamic\ncalendar_validate_auth image-auth.local.conf\n")
        self.assertEqual(result.returncode, 10)
        if sys.platform.startswith("linux"):
            self.auth.symlink_to(self.config)
            self.assertEqual(self.shell("IMAGE_MODE=dynamic\n"
                                       "calendar_validate_auth image-auth.local.conf\n").returncode, 10)
        self.assertEqual(self.shell("IMAGE_MODE=static\n"
                                   "calendar_validate_auth image-auth.local.conf\n").returncode, 0)

    def test_safe_request_config_and_numeric_validation(self):
        for battery, charging, expected in (("0", "0", 0), ("73", "1", 0), ("100", "0", 0),
                                            ("101", "0", 31), ("01", "0", 31),
                                            ("-1", "1", 31), ("73", "true", 31)):
            result = self.shell(
                "calendar_write_request config.local.conf image-auth.local.conf request.conf "
                + shlex.quote(battery) + " " + shlex.quote(charging) + "\n")
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertNotIn(SYNTHETIC_BEARER, result.stderr)
            if expected == 0:
                data = (self.work / "request.conf").read_text()
                self.assertIn('header = "Authorization: Bearer ' + SYNTHETIC_BEARER + '"', data)
                self.assertIn(r'\"battery\":' + battery, data)
                self.assertIn(r'\"charging\":' + ("true" if charging == "1" else "false"), data)
                if sys.platform.startswith("linux"):
                    self.assertEqual((self.work / "request.conf").stat().st_mode & 0o777, 0o600)

    def test_battery_gate_still_precedes_network(self):
        block = WORKER.split("if bounded 3 lipc-get-prop -i com.lab126.powerd battLevel; then", 1)[1]
        block = "if bounded 3 lipc-get-prop -i com.lab126.powerd battLevel; then" + block
        block = block.split("check_sleep\n\nprepare_dynamic_request", 1)[0]
        for battery, charging, code in (("20", "0", 30), ("0", "0", 30),
                                        ("20", "1", 0), ("100", "0", 0),
                                        ("101", "1", 31), ("73", "2", 31)):
            source = ('WORK=.\nlog() { :; }\nbounded() { case "$*" in *battLevel*) '
                      f"printf '%s\\n' {battery} > output ;; *) "
                      f"printf '%s\\n' {charging} > output ;; esac; }}\n" + block)
            self.assertEqual(self.shell(source).returncode, code)
        self.assertLess(WORKER.index("\nprepare_dynamic_request ||"),
                        WORKER.index("\nif bounded 3 lipc-get-prop -i com.lab126.cmd wirelessEnable"))

    def test_dynamic_runtime_snapshot_and_static_cleanup(self):
        installed = self.work / "installed"
        installed.mkdir()
        for name in ("calendar-auto-refresh.sh", "calendar-config.sh", "calendar-lock.sh"):
            shutil.copyfile(DEVICE / name, installed / name)
        shutil.copyfile(self.config, installed / self.config.name)
        shutil.copyfile(self.auth, installed / self.auth.name)
        (self.work / "self.sh").write_text("#!/bin/sh\n")
        start = CONTROLLER.split('case "${1-}" in\n    --start)\n', 1)[1]
        start = start.split("    --stop|--recover)", 1)[0]
        source = (
            "DIR=./installed\nRUN=./runtime\n"
            "readlink() { printf '%s\\n' ./self.sh; }\nnohup() { :; }\nalive() { return 1; }\n"
            "calendar_lock_legacy() { :; }\ncalendar_lock_lifecycle() { :; }\n"
            "case --start in\n--start)\n" + start + "esac\n"
        )
        result = self.shell(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = self.work / "runtime"
        self.assertEqual((runtime / self.auth.name).read_bytes(), self.auth.read_bytes())
        if sys.platform.startswith("linux"):
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual((runtime / self.auth.name).stat().st_mode & 0o777, 0o600)
        (runtime / "restored").touch()
        (installed / self.config.name).write_text("IMAGE_URL=https://example.invalid/image\nWIFI_SSID=fixture\n")
        (installed / self.auth.name).unlink()
        self.assertEqual(self.shell(source).returncode, 0)
        self.assertFalse((runtime / self.auth.name).exists())
        recovery = CONTROLLER.split("    --stop|--recover)\n", 1)[1].split("    --touch)", 1)[0]
        self.assertNotIn("calendar_load_config", recovery)
        self.assertNotIn("calendar_validate_auth", recovery)


@unittest.skipUnless(sys.platform.startswith("linux") and SH and shutil.which("curl")
                     and shutil.which("openssl") and shutil.which("flock"),
                     "Requires Linux, curl, openssl and real flock; host smoke is not Linux validation")
class DynamicProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.cert = cls.root / "fixture-cert.pem"
        key = cls.root / "fixture-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True, timeout=20)
        cls.requests = []
        cls.lock = threading.Lock()
        cls.arrived = threading.Event()
        cls.release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with cls.lock:
                    cls.requests.append((self.command, self.path, dict(self.headers), body))
                if self.path == "/slow":
                    cls.arrived.set()
                    cls.release.wait(10)
                status, content, data = 200, "image/png", png()
                if self.path in ("/400", "/401", "/403"):
                    status, content, data = int(self.path[1:]), "application/json", b'{"error":"synthetic"}'
                elif self.path == "/json":
                    content, data = "application/json", b'{"error":"synthetic"}'
                elif self.path == "/html":
                    content, data = "text/html", b"<html>synthetic</html>"
                elif self.path == "/fake":
                    data = b'{"error":"not a PNG"}' * 4
                elif self.path == "/wrong":
                    data = png(width=1071)
                elif self.path == "/depth":
                    data = png(depth=16)
                elif self.path == "/color":
                    data = png(color=2)
                elif self.path == "/interlace":
                    data = png(interlace=2)
                elif self.path == "/tiny":
                    data = png()[:44]
                elif self.path == "/large":
                    data = b"x" * (5242880 + 1)
                elif self.path == "/changed":
                    data = png(level=74)
                elif self.path == "/redirect":
                    status = 302
                self.send_response(status)
                self.send_header("Content-Type", content)
                if status == 302:
                    self.send_header("Location", cls.other_url + "/image")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    self.wfile.write(data)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.servers = []
        for _ in range(2):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers.append((server, thread))
        cls.url = f"https://localhost:{cls.servers[0][0].server_port}"
        cls.other_url = f"https://localhost:{cls.servers[1][0].server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.release.set()
        for server, thread in cls.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.temporary_case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temporary_case.cleanup)
        self.work = Path(self.temporary_case.name)
        (self.work / "work").mkdir(mode=0o700)
        (self.work / "config").mkdir(mode=0o700)
        (self.work / "bin").mkdir()
        shutil.copyfile(DEVICE / "calendar-config.sh", self.work / "config" / "calendar-config.sh")
        (self.work / "config" / "image-auth.local.conf").write_text(
            "BEARER_TOKEN" + "=" + SYNTHETIC_BEARER + "\n")
        (self.work / "image.png").write_bytes(png())
        self.original = (self.work / "image.png").read_bytes()
        with self.lock:
            self.requests.clear()
        self.arrived.clear()
        self.release.clear()
        self.env = os.environ.copy()
        for name in list(self.env):
            if name.lower().endswith("_proxy"):
                del self.env[name]
        self.env.update(FIXTURE=str(self.work), FIXTURE_CERT=str(self.cert),
                        FIXTURE_CURL=shutil.which("curl"), NO_PROXY="*")
        self.env["PATH"] = str(self.work / "bin") + ":" + self.env["PATH"]
        wrapper = self.work / "bin" / "curl"
        wrapper.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$$" > "$FIXTURE/curl.pid"\n'
            'printf "%s\\n" "$@" >> "$FIXTURE/argv"\n'
            'exec "$FIXTURE_CURL" "$@" --cacert "$FIXTURE_CERT"\n')
        wrapper.chmod(0o700)
        # A hostile default curlrc must not affect either production mode.
        (self.work / ".curlrc").write_text('header = "X-Unwanted: inherited"\nlocation\n')
        self.env["CURL_HOME"] = str(self.work)

    def script(self, path="/image", mode="dynamic", manual=0, decode=0, budget=12, charge="1", battery="73"):
        (self.work / "config" / "config.local.conf").write_text(
            f"IMAGE_MODE={mode}\nIMAGE_URL={self.url}{path}\nWIFI_SSID=fixture\n")
        functions = "\n".join(function(WORKER, name) for name in (
            "report", "monotime", "identity", "same_child", "child_running", "cancel_child",
            "on_signal", "honor_signal", "bounded", "log", "cleanup",
            "window_gate", "network_gate", "prepare_dynamic_request", "download_image"))
        locks = (DEVICE / "calendar-lock.sh").read_text()
        tail = "attempt=0\ndownloaded=0\n" + WORKER.split("attempt=0\ndownloaded=0\n", 1)[1]
        return (
            "set -f\numask 077\n" + HELPER + "\n" + locks + "\n" + functions + "\n"
            "DIR=.\nWORK=./work\nIMAGE=./image.png\nLOG=./refresh.log\nCONFIG_DIR=./config\n"
            "AUTH_WORK=\nchild_pid=\nchild_start=\nlaunch_guard=0\npending_signal=0\n"
            "cleaning=0\nlog_failed=0\nradio_owned=0\nwifi_owned=0\n"
            f"battery={battery}\ncharging={charge}\nstandalone_manual={manual}\nmanual_refresh=1\n"
            "network_phase=network\n"
            "CALENDAR_LIFECYCLE_LOCK=./lifecycle.flock\nCALENDAR_RESOURCE_LOCK=./resource.flock\n"
            "CALENDAR_LEGACY_LOCK=./legacy.lock\n"
            "calendar_load_config ./config/config.local.conf || exit 10\nURL=$IMAGE_URL\n"
            "calendar_validate_auth ./config/image-auth.local.conf || exit 10\n"
            "calendar_lock_lifecycle || exit $?\ncalendar_lock_resource || exit $?\n"
            "trap cleanup 0\ntrap 'on_signal 143' TERM\ntrap 'on_signal 130' INT\n"
            # Only powerd/hardware are substituted. Real curl, gates, bounded and commit code run.
            "check_sleep() { :; }\nconnection_ready() { return 0; }\n"
            f'fbink_fixture() {{ printf "%s\\n" "$*" >> display; return {decode}; }}\n'
            "FBINK=fbink_fixture\n"
            f"monotime || exit 125\ndeadline=$((mono + {budget}))\n"
            'prepare_dynamic_request || exit $?\nprintf "%s\\n" "$AUTH_WORK" > auth-path\n'
            + tail
        )

    def run_script(self, **kwargs):
        result = subprocess.run([SH], input=self.script(**kwargs), cwd=self.work, env=self.env,
                                text=True, capture_output=True, timeout=25)
        self.assertNotIn(SYNTHETIC_BEARER, result.stdout + result.stderr)
        self.assertNotIn(SYNTHETIC_BEARER, (self.work / "refresh.log").read_text())
        auth_path = (self.work / "auth-path").read_text().strip()
        if auth_path:
            self.assertFalse(Path(auth_path).exists(), "private request workspace was not removed")
        self.assertFalse((self.work / "work").exists())
        return result

    def test_real_post_boolean_auth_expect_and_unchanged(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.requests), 1)
        method, path, headers, body = self.requests[0]
        self.assertEqual((method, path), ("POST", "/image"))
        self.assertEqual(json.loads(body), {"battery": 73, "charging": True})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer " + SYNTHETIC_BEARER)
        self.assertNotIn("Expect", headers)
        self.assertNotIn("X-Unwanted", headers)
        self.assertNotIn("Cookie", headers)
        args = (self.work / "argv").read_text()
        self.assertNotIn(SYNTHETIC_BEARER, args)
        self.assertNotIn(self.url, args)
        self.assertEqual(args.splitlines()[0], "-q")
        self.assertNotIn("--location", args)
        self.assertIn("=https", args)
        self.assertNotIn("--insecure", args)
        self.assertFalse((self.work / "display").exists())
        self.assertEqual((self.work / "image.png").read_bytes(), self.original)

    def test_changed_response_atomic_cache_and_manual_redraw(self):
        result = self.run_script(path="/changed", charge="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(self.requests[0][3])["charging"])
        self.assertEqual((self.work / "image.png").read_bytes(), png(level=74))
        self.assertIn("-q -c -f -w -V -g file=./work/download.png,w=-1,h=-1",
                      (self.work / "display").read_text())
        (self.work / "work").mkdir(mode=0o700)
        result = self.run_script(path="/changed", manual=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.work / "display").read_text().splitlines()), 2)

    def test_request_boundary_values_reach_real_curl_as_json_numbers(self):
        # Exercise serialization after the separately tested pre-network battery gate.
        for battery, charging in (("0", "0"), ("100", "1")):
            (self.work / "work").mkdir(mode=0o700, exist_ok=True)
            result = self.run_script(battery=battery, charge=charging)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.requests[-1][3]),
                             {"battery": int(battery), "charging": charging == "1"})

    def test_failed_auth_payloads_headers_sizes_and_decode_preserve_cache(self):
        cases = [("/400", 12), ("/401", 12), ("/403", 12), ("/json", 13),
                 ("/html", 13), ("/fake", 13), ("/wrong", 13), ("/depth", 13),
                 ("/color", 13), ("/interlace", 13), ("/tiny", 13), ("/large", 12),
                 ("/changed", 15)]
        for path, code in cases:
            with self.subTest(path=path):
                (self.work / "work").mkdir(mode=0o700, exist_ok=True)
                with self.lock:
                    self.requests.clear()
                result = self.run_script(path=path, decode=1 if path == "/changed" else 0)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(len(self.requests), 1, "HTTP failure must not retry")
                self.assertEqual((self.work / "image.png").read_bytes(), self.original)
                if path != "/changed":
                    self.assertFalse((self.work / "display").exists())

    def test_dynamic_redirect_fails_without_second_request(self):
        result = self.run_script(path="/redirect")
        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertEqual([item[1] for item in self.requests], ["/redirect"])
        self.assertEqual((self.work / "image.png").read_bytes(), self.original)

    def test_static_get_redirect_remains_compatible_without_credentials(self):
        (self.work / "config" / "image-auth.local.conf").unlink()
        result = self.run_script(path="/redirect", mode="static")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item[1] for item in self.requests], ["/redirect", "/image"])
        for method, _, headers, body in self.requests:
            self.assertEqual(method, "GET")
            self.assertEqual(body, b"")
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("X-Unwanted", headers)

    def test_live_argv_permissions_locks_and_signal_cancellation(self):
        process = subprocess.Popen([SH], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=self.work, env=self.env, text=True)
        try:
            process.stdin.write(self.script(path="/slow"))
            process.stdin.close()
            process.stdin = None
            self.assertTrue(self.arrived.wait(8), "loopback request did not arrive")
            auth = Path((self.work / "auth-path").read_text().strip())
            self.assertEqual(auth.stat().st_mode & 0o777, 0o700)
            self.assertEqual((auth / "request.conf").stat().st_mode & 0o777, 0o600)
            pid = int((self.work / "curl.pid").read_text())
            proc = Path("/proc") / str(pid)
            self.assertNotIn(SYNTHETIC_BEARER.encode(), (proc / "cmdline").read_bytes())
            self.assertNotIn(SYNTHETIC_BEARER.encode(), (proc / "environ").read_bytes())
            for fd, name in ((8, "resource.flock"), (9, "lifecycle.flock")):
                self.assertEqual((proc / "fd" / str(fd)).resolve(), self.work / name)
                probe = subprocess.run(["flock", "-n", str(self.work / name), "true"])
                self.assertEqual(probe.returncode, 1)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            self.assertFalse(auth.exists())
            self.assertFalse(proc.exists(), "bounded curl child survived cancellation")
            self.assertEqual((self.work / "image.png").read_bytes(), self.original)
            self.assertFalse((self.work / "display").exists())
            for name in ("resource.flock", "lifecycle.flock"):
                self.assertTrue((self.work / name).is_file())
                self.assertEqual(subprocess.run(["flock", "-n", str(self.work / name), "true"]).returncode, 0)
        finally:
            self.release.set()
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

    def test_timeout_cancels_request_without_cache_commit(self):
        try:
            result = self.run_script(path="/slow", budget=4)
            self.assertEqual(result.returncode, 12, result.stderr)
            self.assertFalse((self.work / "display").exists())
            self.assertEqual((self.work / "image.png").read_bytes(), self.original)
        finally:
            self.release.set()


if __name__ == "__main__":
    unittest.main()
