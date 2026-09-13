"""Extracted client functions with synthetic hardware; never access a Kindle."""

from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_kindle import CONTROLLER, DEVICE, SH, WORKER, function


DISPLAY = (DEVICE / "calendar-display.sh").read_text(encoding="utf-8")
OLD_HASH = "a" * 64
NEW_HASH = "b" * 64
OLD_TIME = "2026-09-12 08:30"
NEW_TIME = "2026-09-13 19:45"


@unittest.skipUnless(SH, "Requires an existing POSIX shell")
class KindleDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        (self.work / "work").mkdir()
        (self.work / "dashboard.png").write_bytes(b"synthetic original PNG")
        (self.work / "work" / "download.png").write_bytes(b"synthetic new PNG")
        self.write_status(OLD_HASH, OLD_TIME)

    def write_status(self, digest, when):
        (self.work / "dashboard.status").write_text(f"{digest}\n{when}\n")

    def shell(self, source, timeout=15):
        return subprocess.run(
            [SH], input=source, text=True, encoding="utf-8",
            cwd=self.work, capture_output=True, timeout=timeout,
        )

    def fixture(self):
        return DISPLAY + "\n" + function(WORKER, "display_refresh") + f"""
WORK=./work
DIR=.
IMAGE=./dashboard.png
CALENDAR_STATUS_FILE=./dashboard.status
CALENDAR_DISPLAY_OUTPUT=$WORK/output
CALENDAR_DISPLAY_DEADLINE=130
CALENDAR_STATUS_TIME='{OLD_TIME}'
FBINK=fbink
FBINK_HELP=' -k, --cls [top=NUM,left=NUM,width=NUM,height=NUM]'
old_hash={OLD_HASH}
new_hash={NEW_HASH}
standalone_manual=0
size=123
battery=96
BATTERY=73
CALLS=0
FAIL_AT=0
FAIL_RC=7
WARN_AT=0
log() {{ printf '%s\\n' "$*" >> events; }}
monotime() {{ mono=100; }}
calendar_display_clock() {{ display_now=100; }}
window_gate() {{ return 0; }}
calendar_display_ready() {{ printf 'authorized\\n' >> events; }}
calendar_display_geometry() {{ printf 'virtual_size=unavailable\\nrotate=unavailable\\nbits_per_pixel=unavailable\\n'; }}
date() {{ printf 'sampled\\n' >> clock.calls; printf '%s\\n' '{NEW_TIME}'; }}
calendar_display_run() {{
    printf '%s\\n' "$1" >> limits
    shift
    CALLS=$((CALLS + 1))
    printf '%s\\n' "$*" >> calls
    printf '%s\\n' "$@" > "$WORK/args.$CALLS"
    [ "$CALLS" != "$FAIL_AT" ] || return "$FAIL_RC"
    case "$1" in
        lipc-get-prop) printf '%s\\n' "$BATTERY" > "$CALENDAR_DISPLAY_OUTPUT" ;;
        fbink)
            if [ "$2" = --help ]; then
                printf '%s\\n' "$FBINK_HELP" > "$CALENDAR_DISPLAY_OUTPUT"
            elif [ "$CALLS" = "$WARN_AT" ]; then
                printf '[FBInk] Failed to wait for completion of update 42!\\n' > "$CALENDAR_DISPLAY_OUTPUT"
            else
                : > "$CALENDAR_DISPLAY_OUTPUT"
            fi
            ;;
        *) printf 'UNEXPECTED_HARDWARE_COMMAND\\n' >&2; return 99 ;;
    esac
}}
"""

    def calls(self):
        path = self.work / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def events(self):
        return (self.work / "events").read_text()

    def test_changed_image_commits_original_and_bound_timestamp(self):
        result = self.shell(self.fixture() + "\ndisplay_refresh\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic new PNG")
        self.assertEqual((self.work / "dashboard.status").read_text(), f"{NEW_HASH}\n{NEW_TIME}\n")
        self.assertIn("UPDATED:", self.events())
        calls = self.calls()
        self.assertIn("--help", calls[0])
        self.assertIn("-g file=./work/download.png,w=-1,h=-1", calls[1])
        self.assertIn("battLevel", calls[2])
        self.assertEqual(sum("battLevel" in call for call in calls), 1)
        self.assertFalse(any("isCharging" in call for call in calls))
        self.assertTrue(any("73%" in call for call in calls))
        self.assertTrue(any(f"Updated {NEW_TIME}" in call for call in calls))

    def test_unchanged_and_standalone_manual_keep_content_time(self):
        for standalone in (0, 1):
            with self.subTest(standalone=standalone):
                (self.work / "work" / "download.png").write_bytes(b"synthetic original PNG")
                (self.work / "calls").write_text("")
                before = (self.work / "dashboard.status").stat().st_mtime_ns
                result = self.shell(
                    self.fixture() + f"\nnew_hash=$old_hash\nstandalone_manual={standalone}\n"
                    + "display_refresh\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum("-g " in call for call in self.calls()), standalone)
                self.assertEqual((self.work / "dashboard.status").stat().st_mtime_ns, before)
                self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
                self.assertFalse((self.work / "clock.calls").exists())
                if standalone:
                    self.assertTrue(any(f"Updated {OLD_TIME}" in call for call in self.calls()))
                else:
                    self.assertEqual(self.calls(), [])
                    self.assertTrue((self.work / "work" / "download.png").exists())
                    self.assertIn("UNCHANGED:", self.events())

    def test_unchanged_does_not_even_sample_battery_or_load_timestamp(self):
        (self.work / "dashboard.status").write_text("invalid record")
        result = self.shell(self.fixture() + """
new_hash=$old_hash
BATTERY=unreadable
calendar_status_load() { exit 91; }
calendar_display_ready() { exit 92; }
calendar_display_run() { exit 93; }
display_refresh
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.work / "dashboard.status").read_text(), "invalid record")
        self.assertFalse((self.work / "clock.calls").exists())

    def test_all_battery_values_and_digit_transitions(self):
        for value in (100, 99, 10, 9, 0, *range(101)):
            with self.subTest(value=value):
                (self.work / "calls").write_text("")
                result = self.shell(
                    self.fixture() + f"\nBATTERY={value}\nCALENDAR_STATUS_TIME=--\n"
                    + "calendar_display_battery\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertTrue(any(call.endswith(f" {value}%") for call in calls))
                self.assertFalse(any(" -g " in call or " -c " in call for call in calls))
                rectangles = []
                texts = []
                for index, call in enumerate(calls, 1):
                    args = (self.work / "work" / f"args.{index}").read_text().splitlines()
                    if "-k" in args:
                        rect = dict(part.split("=") for part in args[args.index("-k") + 1].split(","))
                        rectangles.append((args, {key: int(val) for key, val in rect.items()}))
                    elif "-S" in args:
                        texts.append(args)
                self.assertGreaterEqual(len(rectangles), 4)
                clear_args, clear = rectangles[0]
                self.assertEqual(clear, {"top": 1412, "left": 0, "width": 1072, "height": 36})
                self.assertIn("WHITE", clear_args)
                for args, rect in rectangles:
                    self.assertGreaterEqual(rect["top"], 1412)
                    self.assertLessEqual(rect["top"] + rect["height"], 1448)
                    self.assertGreaterEqual(rect["left"], 0)
                    self.assertLessEqual(rect["left"] + rect["width"], 1072)
                    self.assertIn("-b", args)
                    self.assertNotIn("-c", args)
                fill = [rect for _, rect in rectangles if rect["height"] == 10]
                width = 25 * value // 100
                self.assertEqual([rect["width"] for rect in fill], [width] if width else [])
                for args in texts:
                    # Official IBM bitmap font is 8x8, scaled to 24x24.
                    self.assertEqual(args[args.index("-F") + 1], "IBM")
                    self.assertEqual(args[args.index("-S") + 1], "3")
                    left = int(args[args.index("-X") + 1]) + 8
                    top = int(args[args.index("-Y") + 1])
                    self.assertGreaterEqual(top, 1412)
                    self.assertLessEqual(top + 24, 1448)
                    text = args[-1]
                    self.assertLessEqual(left + len(text) * 24, 1072 - 12)
                    if text.endswith("%"):
                        self.assertEqual(len(text), 4)
                        self.assertEqual(left, 964)
                    else:
                        self.assertEqual(left, 12)
                self.assertIn("-s", shlex.split(calls[-1]))
                self.assertNotIn("-f", shlex.split(calls[-1]))
                self.assertNotIn("-b", shlex.split(calls[-1]))
                self.assertIn("-w", shlex.split(calls[-1]))

    def test_invalid_battery_and_timeout_do_not_draw_or_publish(self):
        for invalid in ("", "-1", "101", "01", " 9", "9 ", "9.0", "9\n10", "secret-output", "9%"):
            with self.subTest(invalid=invalid):
                (self.work / "calls").write_text("")
                result = self.shell(
                    self.fixture() + f"\nBATTERY={shlex.quote(invalid)}\n"
                    + "calendar_display_battery\n"
                )
                self.assertEqual(result.returncode, 15, result.stderr)
                self.assertEqual(len(self.calls()), 1)
                self.assertNotIn("secret-output", self.events())
                self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
        for code in (1, 124, 125):
            result = self.shell(self.fixture() + f"\nFAIL_AT=1\nFAIL_RC={code}\ncalendar_display_battery\n")
            self.assertEqual(result.returncode, 15, result.stderr)
            self.assertNotIn("UNCHANGED:", self.events())

    def test_every_display_failure_preserves_cache_and_timestamp(self):
        successful = self.shell(self.fixture() + "\ndisplay_refresh\n")
        self.assertEqual(successful.returncode, 0, successful.stderr)
        steps = len(self.calls())
        for step in range(1, steps + 1):
            with self.subTest(step=step):
                (self.work / "dashboard.png").write_bytes(b"synthetic original PNG")
                (self.work / "work" / "download.png").write_bytes(b"synthetic new PNG")
                self.write_status(OLD_HASH, OLD_TIME)
                (self.work / "events").write_text("")
                (self.work / "calls").write_text("")
                result = self.shell(self.fixture() + f"\nFAIL_AT={step}\ndisplay_refresh\n")
                self.assertEqual(result.returncode, 15, result.stderr)
                self.assertEqual(len(self.calls()), step)
                self.assertNotIn("UPDATED:", self.events())
                self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")
                self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
        result = self.shell(self.fixture() + "\ndisplay_refresh\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic new PNG")

    def test_unsafe_old_fbink_is_rejected_by_nondrawing_help_probe(self):
        for help_text in (
            " -k, --cls",
            " -k, --cls\n -s [top=NUM,left=NUM,width=NUM,height=NUM]",
            "unrecognized option",
            " -k, --cls [top=NUM,left=NUM,width=NUM,height=NUM] unexpected",
        ):
            with self.subTest(help_text=help_text):
                (self.work / "calls").write_text("")
                result = self.shell(
                    self.fixture() + f"\nFBINK_HELP={shlex.quote(help_text)}\ndisplay_refresh\n"
                )
                self.assertEqual(result.returncode, 15, result.stderr)
                self.assertEqual(self.calls(), ["fbink --help"])
                self.assertIn("DISPLAY_DEPENDENCY:", self.events())
                self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")
        result = self.shell(
            self.fixture() + "\nFBINK_HELP=' -k, --cls top=NUM,left=NUM,width=NUM,height=NUM'\ndisplay_refresh\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_quiet_fbink_exit_zero_wait_warning_is_failure(self):
        for step in (2, 11):
            with self.subTest(step=step):
                result = self.shell(self.fixture() + f"\nWARN_AT={step}\ndisplay_refresh\n")
                self.assertEqual(result.returncode, 15, result.stderr)
                self.assertIn("diagnostic output despite exit=0", self.events())
                self.assertNotIn("update 42", self.events())
                self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")
                self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")

    def failing_refresh(self, extra="", output="synthetic ioctl detail"):
        return self.shell(self.fixture() + f"""
calendar_display_run() {{
    printf '%s\\n' "$*" >> calls
    printf '%s' {shlex.quote(output)} > "$CALENDAR_DISPLAY_OUTPUT"
    return 255
}}
{extra}
calendar_display_command refresh 5 fbink -q -w -W GC16 -s top=1412,left=0,width=1072,height=36
""")

    def test_refresh_255_preserves_bounded_private_output_without_retry(self):
        output = "x" * 6000 + "\nsynthetic ioctl detail\n"
        result = self.failing_refresh(output=output)
        self.assertEqual(result.returncode, 15, result.stderr)
        diagnostic = (self.work / "display-refresh-error.log").read_bytes()
        self.assertLessEqual(len(diagnostic), 4096)
        self.assertIn(b"stage=refresh original_exit=255", diagnostic)
        self.assertIn(b"argv=-q -w -W GC16 -s top=1412,left=0,width=1072,height=36", diagnostic)
        self.assertIn(output.encode()[-3500:], diagnostic)
        self.assertIn(b"virtual_size=unavailable", diagnostic)
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn("synthetic ioctl detail", self.events())
        self.assertIn("DISPLAY_DIAGNOSTIC_SAVED:", self.events())
        self.assertFalse(list(self.work.glob(".display-refresh-error.*")))
        self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")
        self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
        result = self.failing_refresh(output="")
        self.assertEqual(result.returncode, 15, result.stderr)
        self.assertIn("original_exit=255", (self.work / "display-refresh-error.log").read_text())

    def test_diagnostic_io_failure_keeps_original_recovery_status(self):
        path = self.work / "display-refresh-error.log"
        path.write_text("prior diagnostic")
        result = self.failing_refresh("""
mv() {
    [ "$3" != "./display-refresh-error.log" ] || return 1
    command mv "$@"
}
""")
        self.assertEqual(result.returncode, 15, result.stderr)
        self.assertEqual(path.read_text(), "prior diagnostic")
        self.assertIn("DISPLAY_FAILED: stage=refresh exit=255", self.events())
        self.assertIn("DISPLAY_DIAGNOSTIC_FAILED:", self.events())
        self.assertFalse(list(self.work.glob(".display-refresh-error.*")))
        path.unlink()
        path.mkdir()
        result = self.failing_refresh()
        self.assertEqual(result.returncode, 15, result.stderr)
        self.assertTrue(path.is_dir())
        self.assertFalse(list(self.work.glob(".display-refresh-error.*")))

    def test_success_unchanged_and_cancellation_do_not_collect_diagnostics(self):
        source = self.fixture() + """
calendar_display_geometry() { exit 91; }
new_hash=$old_hash
display_refresh || exit "$?"
calendar_display_command refresh 5 fbink -q -w -W GC16 -s top=1412,left=0,width=1072,height=36
"""
        result = self.shell(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "display-refresh-error.log").exists())
        self.assertEqual(len(self.calls()), 1)
        for code in (16, 32, 129, 130, 143):
            result = self.shell(self.fixture() + f"""
calendar_display_geometry() {{ exit 91; }}
calendar_display_run() {{ return {code}; }}
calendar_display_command refresh 5 fbink
""")
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertFalse((self.work / "display-refresh-error.log").exists())

    def test_geometry_diagnostic_fixed_paths_bounded_values_and_deadline(self):
        geometry = function(DISPLAY, "calendar_display_geometry")
        for name in ("virtual_size", "rotate", "bits_per_pixel"):
            self.assertIn(f"/sys/class/graphics/fb0/{name}", geometry)
        # Relocate only the three fixed sysfs paths into a synthetic directory.
        geometry = geometry.replace("/sys/class/graphics/fb0/", "./synthetic-fb0/")
        directory = self.work / "synthetic-fb0"
        directory.mkdir()
        (directory / "virtual_size").write_text("1448,1072\nignored second line\n")
        (directory / "rotate").write_text("3\n")
        (directory / "bits_per_pixel").write_text("8\n")
        source = self.fixture() + "\n" + geometry + """
calendar_display_run() {
    printf '%s\\n' "$*" >> calls
    [ "$1" = 1 ] || exit 91
    shift
    "$@" > "$CALENDAR_DISPLAY_OUTPUT"
}
"""
        result = self.shell(source + "\ncalendar_display_geometry\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "virtual_size=1448,1072\nrotate=3\nbits_per_pixel=8\n")
        (directory / "virtual_size").write_text("1" * 33 + ",2\n")
        (directory / "rotate").write_text("not-numeric\n")
        (directory / "bits_per_pixel").unlink()
        result = self.shell(source + "\ncalendar_display_geometry\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        unavailable = "virtual_size=unavailable\nrotate=unavailable\nbits_per_pixel=unavailable\n"
        self.assertEqual(result.stdout, unavailable)
        result = self.shell(source + """
CALENDAR_DISPLAY_DEADLINE=100
calendar_display_run() { exit 92; }
calendar_display_geometry
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, unavailable)
        result = self.shell(source + """
calendar_display_run() { return 124; }
calendar_display_geometry
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, unavailable)

    def test_signal_and_guard_codes_stop_overlay(self):
        for code in (16, 32, 129, 130, 143):
            (self.work / "calls").write_text("")
            result = self.shell(
                self.fixture() + f"\nFAIL_AT=3\nFAIL_RC={code}\ndisplay_refresh\n"
            )
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(len(self.calls()), 3)
            self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")

    def test_display_commands_share_one_deadline_and_cap_remaining_budget(self):
        result = self.shell(self.fixture() + """
CALENDAR_DISPLAY_DEADLINE=102
calendar_display_command fixture 15 fbink
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "limits").read_text(), "2\n")
        (self.work / "calls").write_text("")
        result = self.shell(self.fixture() + """
clock_tick=90
calendar_display_clock() { clock_tick=$((clock_tick + 10)); display_now=$clock_tick; }
display_refresh
""")
        self.assertEqual(result.returncode, 15, result.stderr)
        self.assertEqual(len(self.calls()), 3)
        self.assertIn("shared 30s", self.events())
        self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")
        self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")

    def test_actual_cache_power_gate_also_uses_remaining_display_budget(self):
        result = self.shell(
            self.fixture() + "\n" + function(CONTROLLER, "calendar_display_ready") + """
record() { log "$@"; }
clock_now=129
calendar_display_clock() { display_now=$clock_now; }
calendar_display_run() {
    printf '%s\\n' "$*" >> calls
    clock_now=$((clock_now + $1))
    printf '1\\n' > "$CALENDAR_DISPLAY_OUTPUT"
}
calendar_display_ready
"""
        )
        self.assertEqual(result.returncode, 15, result.stderr)
        self.assertEqual(self.calls(), ["1 lipc-get-prop -i com.lab126.powerd preventScreenSaver"])
        self.assertIn("shared 30s", self.events())

    def test_unknown_missing_malformed_or_mismatched_timestamp(self):
        cases = (None, "", f"{NEW_HASH}\n{OLD_TIME}\n", f"{OLD_HASH}\n2026-02-30 08:30\n",
                 f"{OLD_HASH}\n2026-09-13 24:00\n", "x" * 97)
        for data in cases:
            with self.subTest(data=data):
                (self.work / "work" / "download.png").write_bytes(b"synthetic original PNG")
                if data is None:
                    (self.work / "dashboard.status").unlink(missing_ok=True)
                else:
                    (self.work / "dashboard.status").write_text(data)
                (self.work / "calls").write_text("")
                result = self.shell(self.fixture() + "\nnew_hash=$old_hash\nstandalone_manual=1\ndisplay_refresh\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(any("Updated --" in call for call in self.calls()))
                self.assertIn("STATUS_TIME_UNKNOWN:", self.events())
                self.assertFalse((self.work / "clock.calls").exists())

    def test_clock_failure_and_calendar_validation(self):
        for when in ("2024-02-29 23:59", "2000-02-29 00:00", "2026-12-31 08:09"):
            result = self.shell(DISPLAY + f"\ncalendar_status_valid_time '{when}'\n")
            self.assertEqual(result.returncode, 0, result.stderr)
        for when in ("1900-02-29 00:00", "2026-02-29 12:00", "2026-04-31 09:00",
                     "2026-00-01 00:00", "2026-13-01 00:00", "2026-01-00 00:00",
                     "2026-01-01 24:00", "2026-01-01 08:60", "unavailable"):
            result = self.shell(
                self.fixture() + f"\ndate() {{ printf '%s\\n' '{when}'; }}\ndisplay_refresh\n"
            )
            self.assertEqual(result.returncode, 14, result.stderr)
            self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
            self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")

    def test_png_and_sidecar_commit_failure_window(self):
        for target in ("./dashboard.png", "./dashboard.status"):
            with self.subTest(target=target):
                (self.work / "dashboard.png").write_bytes(b"synthetic original PNG")
                (self.work / "work" / "download.png").write_bytes(b"synthetic new PNG")
                self.write_status(OLD_HASH, OLD_TIME)
                (self.work / "events").write_text("")
                result = self.shell(
                    self.fixture() + f"""
mv() {{
    [ "$3" != "{target}" ] || return 1
    command mv "$@"
}}
display_refresh
"""
                )
                self.assertEqual(result.returncode, 14, result.stderr)
                self.assertNotIn("UPDATED:", self.events())
                self.assertEqual((self.work / "dashboard.status").read_text(), f"{OLD_HASH}\n{OLD_TIME}\n")
                if target.endswith(".status"):
                    self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic new PNG")
                    result = self.shell(
                        self.fixture() + f"\ncalendar_status_load {NEW_HASH}\nprintf '%s\\n' \"$CALENDAR_STATUS_TIME\"\n"
                    )
                    self.assertEqual(result.stdout, "--\n")
                else:
                    self.assertEqual((self.work / "dashboard.png").read_bytes(), b"synthetic original PNG")

    def test_cache_entry_and_refresh_recovery_share_display_and_lock(self):
        cache = function(CONTROLLER, "display_cache")
        refresh = function(CONTROLLER, "refresh_once")
        for label in ("CACHE_DISPLAY", "CACHE_RESTORE"):
            (self.work / "calls").write_text("")
            result = self.shell(
                self.fixture() + "\n" + cache + "\n"
                + f"""
RUN=./work
sha256sum() {{ printf '%s  %s\\n' '{OLD_HASH}' "$1"; }}
record() {{ log "$@"; }}
calendar_lock_resource() {{ printf 'locked\\n' >> events; exec 8>./resource; }}
display_cache {label}
"""
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("locked\n", self.events())
            self.assertIn(f"{label}: exit=0", self.events())
            self.assertTrue(any("file=./dashboard.png" in call for call in self.calls()))
            self.assertTrue(any(f"Updated {OLD_TIME}" in call for call in self.calls()))
            self.assertFalse((self.work / "clock.calls").exists())
        for kind, expected in (("manual", "--dedicated-manual"), ("scheduled", "--dedicated")):
            for code in (0, 15, 12, 13, 30, 31):
                (self.work / "calls").write_text("")
                result = self.shell(
                    self.fixture() + "\n" + refresh + f"""
RUN=./work
slot=123
record() {{ log "$@"; }}
run() {{ printf '%s\\n' "$*" >> calls; return {code}; }}
display_cache() {{ printf '%s\\n' "$*" >> calls; }}
refresh_once {kind}
"""
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, self.calls()[0])
                self.assertEqual("CACHE_RESTORE" in self.calls(), code == 15)

    def test_cache_lock_busy_and_power_gate_fail_before_display(self):
        result = self.shell(
            self.fixture() + "\n" + function(CONTROLLER, "display_cache") + """
calendar_lock_resource() { return 32; }
display_cache CACHE_DISPLAY
"""
        )
        self.assertEqual(result.returncode, 32)
        self.assertFalse(self.calls())
        result = self.shell(
            self.fixture() + "\ncalendar_display_ready() { return 32; }\ndisplay_refresh\n"
        )
        self.assertEqual(result.returncode, 32)
        self.assertTrue(all("--help" in call for call in self.calls()))

    def test_display_library_packaging_and_recovery_independence(self):
        self.assertIn('. "$CONFIG_DIR/calendar-display.sh"', WORKER)
        config_block = CONTROLLER.split('# Recovery and the guardian', 1)[1].split(
            'case "${1-}" in\n    --start)', 1
        )[0]
        self.assertIn('--start|--run)', config_block)
        self.assertIn('. "$CONFIG_DIR/calendar-display.sh"', config_block)
        self.assertNotIn("--recover)", config_block)
        self.assertIn("calendar-display.sh", CONTROLLER.split("# Only this controller", 1)[1])
        self.assertIn("calendar-display.sh", (DEVICE.parent.parent / "tests" / "kindle-lock-bundle.sh").read_text())
        self.assertIn('display_cache CACHE_DISPLAY || exit "$?"', CONTROLLER)
        self.assertIn('15) display_cache CACHE_RESTORE || exit "$?"', CONTROLLER)
        self.assertIn('display_refresh\nexit "$?"', WORKER)


@unittest.skipUnless(
    sys.platform.startswith("linux") and SH and shutil.which("flock"),
    "Real bounded children require Linux /proc and flock; hardware remains stubbed",
)
class KindleDisplayExecutionTests(unittest.TestCase):
    setUp = KindleDisplayTests.setUp
    write_status = KindleDisplayTests.write_status
    shell = KindleDisplayTests.shell

    def test_cache_display_releases_resource_before_actual_restoration(self):
        lock = (DEVICE / "calendar-lock.sh").read_text()
        source = DISPLAY + "\n" + lock + "\n" + "\n".join(
            function(CONTROLLER, name) for name in ("display_cache", "restore")
        )
        for code in (0, 15, 32, 143):
            with self.subTest(code=code):
                result = self.shell(source + f"""
RUN=./work
DIR=.
CALENDAR_STATUS_FILE=./dashboard.status
CALENDAR_LIFECYCLE_LOCK=./lifecycle.flock
CALENDAR_RESOURCE_LOCK=./resource.flock
CALENDAR_LEGACY_LOCK=./legacy.lock
log() {{ printf '%s\\n' "$*" >> events; }}
record() {{ log "$@"; }}
sha256sum() {{ printf '%s  %s\\n' '{OLD_HASH}' "$1"; }}
calendar_display_image() {{
    calendar_lock_inherited || exit 91
    [ "$(readlink -f /proc/$$/fd/8)" = "$(readlink -f "$CALENDAR_RESOURCE_LOCK")" ] || exit 92
    return {code}
}}
run() {{ printf 'synthetic Home request\\n' >> events; }}
calendar_lock_lifecycle || exit "$?"
if display_cache CACHE_RESTORE; then result=0; else result=$?; fi
[ "$result" = {code} ] || exit 93
calendar_lock_inherited || exit 94
if readlink /proc/$$/fd/8 >/dev/null 2>&1; then exit 95; fi
restore || exit 96
[ -f "$RUN/restored" ] || exit 97
calendar_lock_inherited || exit 98
""")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_runners_inherit_locks_and_handle_failure_timeout_and_signal(self):
        lock = (DEVICE / "calendar-lock.sh").read_text()
        (self.work / "fake-command.sh").write_text("""#!/bin/sh
readlink -f "/proc/$$/fd/8" > resource-seen
readlink -f "/proc/$$/fd/9" > lifecycle-seen
printf '%s\\n' "$$" > command.pid
case "$1" in
    success) printf '73\\n' ;;
    failure) exit 7 ;;
    timeout) exec sleep 30 ;;
    signal) kill -TERM "$PPID"; exec sleep 30 ;;
    *) exit 99 ;;
esac
""")
        for owner in ("worker", "controller"):
            if owner == "worker":
                extracted = "\n".join(
                    function(WORKER, name) for name in
                    ("identity", "monotime", "same_child", "child_running", "cancel_child",
                     "on_signal", "honor_signal", "bounded", "calendar_display_run")
                )
                setup = """
child_pid= child_start= launch_guard=0 pending_signal=0 cleaning=0
monotime || exit 90
deadline=$((mono + 15))
trap 'on_signal 143' TERM
"""
            else:
                extracted = "\n".join(
                    function(CONTROLLER, name) for name in
                    ("identity", "alive", "lease", "guard_ok", "signal_exit", "honor_signal",
                     "cancel_child", "run", "calendar_display_run")
                )
                setup = """
child= child_start= launching=0 pending_signal=0 critical=0 ROLE=fixture
CHILD_FILE=$RUN/child
trap 'signal_exit 143' TERM
"""
            for mode, expected in (("success", 0), ("failure", 15), ("timeout", 15), ("signal", 143)):
                with self.subTest(owner=owner, mode=mode):
                    source = DISPLAY + "\n" + lock + "\n" + extracted + """
WORK=./work
RUN=./work
CALENDAR_LIFECYCLE_LOCK=./lifecycle.flock
CALENDAR_RESOURCE_LOCK=./resource.flock
CALENDAR_LEGACY_LOCK=./legacy.lock
log() { printf '%s\\n' "$*" >> events; }
calendar_lock_lifecycle || exit "$?"
calendar_lock_resource || exit "$?"
trap 'cancel_child' 0
""" + setup + f"""
calendar_display_begin || exit "$?"
calendar_display_command fixture 2 /bin/sh ./fake-command.sh {mode}
exit "$?"
"""
                    result = self.shell(source, timeout=35)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(
                        (self.work / "resource-seen").read_text().strip(),
                        str(self.work / "resource.flock"),
                    )
                    self.assertEqual(
                        (self.work / "lifecycle-seen").read_text().strip(),
                        str(self.work / "lifecycle.flock"),
                    )
                    pid = int((self.work / "command.pid").read_text())
                    self.assertFalse(Path(f"/proc/{pid}").exists(), "bounded command survived cleanup")
                    for name in ("resource.flock", "lifecycle.flock"):
                        self.assertTrue((self.work / name).is_file())
                        probe = subprocess.run(
                            ["flock", "-n", str(self.work / name), "true"],
                            capture_output=True, timeout=5,
                        )
                        self.assertEqual(probe.returncode, 0, probe.stderr)


if __name__ == "__main__":
    unittest.main()
