"""Isolated shell fixtures: never execute a complete device script or access USB."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
DEVICE = ROOT / "kindle" / "kindle-dashboard"
CONTROLLER = (DEVICE / "calendar-dedicated.sh").read_text(encoding="utf-8")
WORKER = (DEVICE / "calendar-auto-refresh.sh").read_text(encoding="utf-8")
HELPER = (DEVICE / "calendar-config.sh").read_text(encoding="utf-8")
SH = os.environ.get("KINDLE_TEST_SH") or shutil.which("sh")
URL = "https:" + "//calendar.invalid/image.png?a=1&b=%20&literal=$(touch%20sentinel)"


def function(source, name):
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.M | re.S)
    if not match:
        raise AssertionError(f"Missing shell function: {name}")
    return match.group()


@unittest.skipUnless(SH, "Set KINDLE_TEST_SH to an existing POSIX shell")
class KindleShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def shell(self, source):
        result = subprocess.run(
            [SH], input="PATH=/usr/bin:/bin:$PATH\nexport PATH\n" + source,
            text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.work,
            timeout=15,
        )
        return result

    def config(self, text):
        (self.work / "config.local.conf").write_bytes(text.encode("utf-8"))
        return self.shell(
            HELPER
            + '\ncalendar_load_config config.local.conf || exit $?\n'
            + 'printf "%s\\n%s\\n" "$IMAGE_URL" "$WIFI_SSID"\n'
        )

    def test_shell_syntax_and_encoding(self):
        scripts = [*(ROOT / "kindle").rglob("*.sh"), *(ROOT / "tests").glob("kindle-*.sh")]
        for path in scripts:
            with self.subTest(path=path.name):
                data = path.read_bytes()
                self.assertTrue(data.startswith(b"#!/bin/sh\n"))
                self.assertNotIn(b"\r", data)
                result = subprocess.run(
                    [SH, "-n"], input=data, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        example = (DEVICE / "config.example.conf").read_bytes()
        self.assertFalse(example.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", example)

    def test_config_literal_values_no_execution(self):
        ssid = " lab $(touch sentinel) #=& "
        result = self.config(f"# data only\nIMAGE_URL={URL}\nWIFI_SSID={ssid}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{URL}\n{ssid}\n")
        self.assertFalse((self.work / "sentinel").exists())
        for suffix in ("?a=[1]&b={x,y}", "?a='quoted'&b=\"data\""):
            value = "https:" + "//calendar.invalid/" + suffix
            result = self.config(f"IMAGE_URL={value}\nWIFI_SSID=network\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], value)

    def test_invalid_configs_fail_without_values(self):
        bad_urls = (
            "", "http:" + "//calendar.invalid/", "https:", "https:" + "///",
            "https:" + "//user:secret@calendar.invalid/",
            "https:" + "//calendar.invalid:0/",
            "https:" + "//calendar.invalid:65536/",
            "https:" + "//calendar.invalid/a b",
            "https:" + "//calendar.invalid\\file",
        )
        invalid = [f"IMAGE_URL={value}\nWIFI_SSID=network\n" for value in bad_urls]
        invalid += [
            "", "WIFI_SSID=network\n", f"IMAGE_URL={URL}\n",
            f"IMAGE_URL={URL}\nWIFI_SSID=\n",
            f"IMAGE_URL={URL}\nWIFI_SSID={'a' * 33}\n",
            f"IMAGE_URL={URL}\nWIFI_SSID=a\tb\n",
            f"IMAGE_URL={URL}\r\nWIFI_SSID=network\r\n",
            f"IMAGE_URL={URL}\nWIFI_SSID=a\nWIFI_SSID=b\n",
            f"IMAGE_URL={URL}\nWIFI_SSID=network\nUNKNOWN=value\n",
            f"IMAGE_URL={URL}\nWIFI_SSID=network\ntouch sentinel\n",
            f'\ufeffIMAGE_URL={URL}\nWIFI_SSID=network\n',
            f"IMAGE_URL={URL}\nWIFI_SSID=network\n#{'x' * 8192}\n",
        ]
        for text in invalid:
            with self.subTest(case=invalid.index(text)):
                result = self.config(text)
                self.assertEqual(result.returncode, 10)
                self.assertIn("CONFIG_ERROR:", result.stderr)
                self.assertNotIn(URL, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.work / "sentinel").exists())

    def test_missing_and_example_config_fail(self):
        result = self.shell(HELPER + "\ncalendar_load_config missing.conf\n")
        self.assertEqual(result.returncode, 10)
        result = self.config((DEVICE / "config.example.conf").read_text())
        self.assertEqual(result.returncode, 10)

    def test_ssid_byte_limits_and_order(self):
        for ssid, expected in (("a" * 32, 0), ("界" * 10, 0), ("界" * 11, 10)):
            result = self.config(f"WIFI_SSID={ssid}\nIMAGE_URL={URL}\n")
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_schedule(self):
        # Inputs/outputs are local seconds on an arbitrary day, translated to UTC.
        cases = ((0, 23400), (23399, 23400), (23400, 25200),
                 (28800, 30600), (77400, 79200), (79199, 79200),
                 (79200, 109800), (86399, 109800))
        source = function(CONTROLLER, "next_slot")
        for current, expected in cases:
            source += f'\nnext_slot {86400 + current - 28800}; printf "%s\\n" "$next"\n'
        result = self.shell(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            list(map(int, result.stdout.splitlines())),
            [86400 + expected - 28800 for _, expected in cases],
        )
        self.assertIn('[ "$((schedule_now - slot))" -gt 120 ]', CONTROLLER)

    def test_quiet_hours(self):
        gate = function(WORKER, "window_gate")
        for time, allowed in (("062959", False), ("063000", True),
                              ("080900", True), ("220159", True),
                              ("220200", False), ("230000", False)):
            result = self.shell(
                gate + f"\ndate() {{ printf '%s\\n' {time}; }}\n"
                + "manual_refresh=0\nwindow_gate\n"
            )
            self.assertEqual(result.returncode == 0, allowed)
        result = self.shell(
            gate + "\ndate() { return 1; }\nmanual_refresh=1\nwindow_gate\n"
        )
        self.assertEqual(result.returncode, 0)

    def test_dedicated_manual_keeps_authorization(self):
        check = function(WORKER, "check_sleep")
        base = (
            check + "\nWORK=.\nstandalone_manual=0\ndedicated_trial=1\nmanual_refresh=1\n"
            + "log() { :; }\n"
            + "bounded() { case \"$*\" in *preventScreenSaver*) echo 1 > output ;; "
            + "*) echo active > output ;; esac; }\n"
        )
        for authorized in (False, True):
            result = self.shell(
                base + f"dedicated_owner_alive() {{ return {0 if authorized else 1}; }}\n"
                + "check_sleep\n"
            )
            self.assertEqual(result.returncode, 0 if authorized else 32)

    def test_owner_requires_direct_parent_starttime_and_flags(self):
        run = self.work / "session"
        run.mkdir()
        for name in ("ui-owned", "sleep-owned"):
            (run / name).touch()
        owner = function(WORKER, "dedicated_owner_alive")
        source = (
            owner + "\nTRIAL=./session\n"
            + "identity() { proc_start=123; proc_state=S; }\n"
            + 'printf "%s 123\\n" "$PPID" > "$TRIAL/owner"\n'
        )
        for edit in ("", 'echo "999999 123" > "$TRIAL/owner"\n',
                     'printf "%s 124\\n" "$PPID" > "$TRIAL/owner"\n',
                     'rm "$TRIAL/ui-owned"\n'):
            result = self.shell(source + edit + "dedicated_owner_alive\n")
            self.assertEqual(result.returncode == 0, not edit)

    def test_stop_request_preserves_marker_and_deferred_signal(self):
        source = "\n".join(
            function(CONTROLLER, name)
            for name in ("signal_exit", "honor_signal", "request_stop")
        )
        for critical, launching, pending, expected in (
            (0, 1, 0, 143), (1, 0, 0, 0), (1, 1, 143, 143),
        ):
            with self.subTest(critical=critical, launching=launching, pending=pending):
                result = self.shell(
                    source + "\nRUN=.\nROLE=--run\n"
                    + f"critical={critical}\nlaunching={launching}\npending_signal={pending}\n"
                    + "request_stop\n"
                    + '[ -f "$RUN/stop-requested" ] || exit 91\n'
                    + f'[ "$pending_signal" = {expected} ] || exit 92\n'
                    + "printf 'retained\\n'\ncritical=0\n"
                    + ("honor_signal\n" if expected else "signal_exit 143\n")
                    + "exit 93\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "retained\n")
                (self.work / "stop-requested").unlink()

    @staticmethod
    def event(seconds, microseconds, kind, code, value):
        value &= 0xFFFFFFFF
        fields = (seconds & 65535, seconds >> 16,
                  microseconds & 65535, microseconds >> 16,
                  kind, code, value & 65535, value >> 16)
        return "gesture_event " + " ".join(map(str, fields)) + "\n"

    def gesture(self, events, extra=""):
        source = "\n".join(function(CONTROLLER, name)
                           for name in ("gesture_finish", "gesture_event", "gesture_window"))
        return self.shell(
            source + "\nRUN=.\npressed=0\ncurrent_slot=0\ntracked_slot=0\n"
            + "mt_seen=0\nsource=\nmultiple=0\ncontacts=\nlast_sec=\n"
            + "record() { :; }\nalive() { return 1; }\nrun() { :; }\n"
            + "touch_owner=999999\ntouch_owner_start=1\n"
            + "".join(self.event(*event) for event in events) + extra
        )

    def test_multicontact_epochs_and_boundaries(self):
        functions = "\n".join(
            function(CONTROLLER, name)
            for name in ("gesture_finish", "gesture_event", "gesture_window")
        )
        fixture = (ROOT / "tests" / "kindle-gestures.sh").read_text()
        result = self.shell(functions + "\n" + fixture)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_touch_short_release_coalesces_and_deduplicates(self):
        sec = 1760000000
        result = self.gesture([
            (sec, 0, 1, 330, 1), (sec, 0, 3, 57, 7),
            (sec, 100000, 3, 57, -1), (sec, 100000, 1, 330, 0),
            (sec, 200000, 3, 57, 8), (sec, 300000, 3, 57, -1),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "manual-refresh").exists())
        self.assertFalse((self.work / "gesture-held").exists())
        self.assertFalse((self.work / "stop-requested").exists())

    def test_touch_hold_only_exits_on_release(self):
        result = self.gesture([(100, 900000, 3, 57, 1)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "gesture-held").exists())
        self.assertFalse((self.work / "stop-requested").exists())
        result = self.gesture([(100, 900000, 3, 57, 1), (102, 900000, 3, 57, -1)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "stop-requested").exists())
        self.assertFalse((self.work / "manual-refresh").exists())

    def test_touch_multifinger_and_dropped_or_stuck_input(self):
        result = self.gesture([
            (100, 0, 3, 57, 1), (100, 0, 3, 47, 1),
            (100, 0, 3, 57, 2), (100, 0, 3, 47, 0),
            (100, 500000, 3, 57, -1),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "manual-refresh").exists())
        self.assertEqual(self.gesture([(100, 0, 0, 3, 0)]).returncode, 16)
        result = self.gesture([(100, 0, 3, 57, 1)], "gesture_window\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "stop-requested").exists())

    def test_start_packages_config_and_helper_without_launching_device_code(self):
        directory = self.work / "installed"
        directory.mkdir()
        for name in ("calendar-auto-refresh.sh", "calendar-config.sh", "calendar-lock.sh",
                     "calendar-display.sh"):
            shutil.copyfile(DEVICE / name, directory / name)
        (directory / "config.local.conf").write_text(
            f"IMAGE_URL={URL}\nWIFI_SSID=fixture network\n", encoding="utf-8"
        )
        (self.work / "self.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        start = CONTROLLER.split("case \"${1-}\" in\n    --start)\n", 1)[1]
        start = start.split("    --stop|--recover)", 1)[0]
        source = (
            HELPER + "\nDIR=./installed\nRUN=./runtime\n"
            + "readlink() { printf '%s\\n' ./self.sh; }\n"
            + "nohup() { printf '%s\\n' \"$*\" > launch.txt; }\n"
            + "alive() { return 1; }\n"
            # Packaging only: kernel acquisition is exercised in test_kindle_locks.
            + "calendar_lock_legacy() { return 0; }\n"
            + "calendar_lock_lifecycle() { return 0; }\n"
            + "case --start in\n--start)\n" + start + "esac\n"
        )
        result = self.shell(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("controller.sh", "refresh.sh", "calendar-config.sh",
                     "calendar-lock.sh", "calendar-display.sh", "config.local.conf"):
            self.assertTrue((self.work / "runtime" / name).is_file(), name)
        runtime = shlex.quote((self.work / "runtime").as_posix())
        result = self.shell(
            f"cd {runtime}\n. ./calendar-config.sh\n"
            + 'calendar_load_config ./config.local.conf || exit $?\n'
            + 'printf "%s\\n" "$WIFI_SSID"\n'
        )
        self.assertEqual(result.stdout, "fixture network\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('run 120 /bin/sh "$RUN/refresh.sh" "$refresh_arg"', CONTROLLER)
        self.assertIn('"$@" > "$RUN/output.$$" 2>&1 &', CONTROLLER)
        self.assertIn("/tmp/calendar-dedicated/refresh.sh) CONFIG_DIR=/tmp/calendar-dedicated", WORKER)

    def test_config_precedes_state_changes_and_recovery_is_independent(self):
        self.assertLess(CONTROLLER.index("calendar_load_config"), CONTROLLER.index("cp \"$self\""))
        self.assertLess(CONTROLLER.index("calendar_load_config"), CONTROLLER.index("must GUI_STOP"))
        self.assertIn('--start|--run)', CONTROLLER)
        self.assertLess(WORKER.index("calendar_load_config"), WORKER.index("trap cleanup"))
        self.assertLess(WORKER.index('calendar_display_image "$WORK/download.png"'),
                        WORKER.index('! mv -f "$WORK/download.png"'))
        self.assertIn("curl -q --globoff", WORKER)
        self.assertIn("--proto '=https' --proto-redir '=https'", WORKER)
        self.assertIn('[ "$battery" -le 20 ] && [ "$charging" = 0 ]', WORKER)
        self.assertIn('deadline=$((mono + 60))', WORKER)
        self.assertIn('link_deadline=$((mono + 20))', WORKER)
        self.assertIn('dd bs=4096 count=1', CONTROLLER)
        self.assertIn('od -An -v -tu2', CONTROLLER)


if __name__ == "__main__":
    unittest.main()
