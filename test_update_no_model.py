"""Offline tests: all files are temporary, all network access is denied or mocked."""
from datetime import date, datetime, timedelta, timezone
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

import update_no_model as updater

TODAY = date(2030, 1, 2)


def forecast(today=TODAY):
    return {"daily": {
        "time": [today.isoformat(), (today + timedelta(days=1)).isoformat()],
        "weather_code": [0, 61],
        "temperature_2m_min": [-1.5, 3.2],
        "temperature_2m_max": [10.5, 12],
        "precipitation_probability_max": [0, 75.5],
    }}


def encoded(today=TODAY):
    return (json.dumps(forecast(today), indent=2) + "\n").encode()


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "weather.json"
        self.output = self.root / "dashboard.png"
        self.network = self.enterContext(mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("real network forbidden")))
        self.urlopen = self.enterContext(mock.patch.object(
            updater.urllib.request, "urlopen", side_effect=AssertionError("unexpected weather network")))
        self.stderr = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))
        self.stdout = self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))

    def assert_cache(self, data):
        self.assertEqual(self.cache.read_bytes(), data)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["weather.json"])


class ValidationTests(OfflineTests):
    def test_valid_payload_and_original_bytes(self):
        payload = forecast()
        self.assertIs(updater.validate_weather(payload, TODAY), payload)
        self.assertEqual(updater.decode_weather(encoded(), TODAY), payload)

    def test_exact_local_dates_required(self):
        for times in (
            ["2030-01-01", "2030-01-02"], ["2030-01-03", "2030-01-04"],
            ["2030-01-02", "2030-01-02"], ["2030-01-03", "2030-01-02"],
            ["2030-01-02", "2030-01-04"], ["20300102", "2030-01-03"],
            [None, "2030-01-03"], ["2030-01-02T00:00:00", "2030-01-03"],
        ):
            with self.subTest(times=times):
                payload = forecast()
                payload["daily"]["time"] = times
                with self.assertRaises(ValueError):
                    updater.validate_weather(payload, TODAY)

    def test_month_year_and_leap_boundaries(self):
        for today in (date(2028, 2, 28), date(2028, 2, 29), date(2029, 12, 31)):
            with self.subTest(today=today):
                self.assertEqual(updater.decode_weather(encoded(today), today), forecast(today))

    def test_root_and_daily_shapes(self):
        for payload in (None, [], "bad", 1, {}, {"daily": []}, {"daily": None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                updater.validate_weather(payload, TODAY)

    def test_missing_misaligned_and_wrong_array_types(self):
        for key in ("time", *updater.DAILY_FIELDS):
            for value in (None, {}, "xx", [], [0], [0, 1, 2], (0, 1)):
                with self.subTest(key=key, value=value):
                    payload = forecast()
                    payload["daily"][key] = value
                    with self.assertRaises(ValueError):
                        updater.validate_weather(payload, TODAY)
            payload = forecast()
            del payload["daily"][key]
            with self.assertRaises(ValueError):
                updater.validate_weather(payload, TODAY)

    def test_strict_finite_numbers(self):
        for key in updater.DAILY_FIELDS:
            for value in (None, True, False, "1", [], {}, float("nan"), float("inf"), -float("inf")):
                with self.subTest(key=key, value=value):
                    payload = forecast()
                    payload["daily"][key][0] = value
                    with self.assertRaises(ValueError):
                        updater.validate_weather(payload, TODAY)

    def test_numeric_ranges(self):
        bad_values = {
            "weather_code": [-1, 4, 100, 2.5, 10 ** 400],
            "temperature_2m_min": [-100.1, 71, -(10 ** 400)],
            "temperature_2m_max": [-101, 70.1, 10 ** 400],
            "precipitation_probability_max": [-0.1, 100.1, 10 ** 400],
        }
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    payload = forecast()
                    payload["daily"][key][0] = value
                    with self.assertRaises(ValueError):
                        updater.validate_weather(payload, TODAY)

    def test_inverted_temperatures(self):
        payload = forecast()
        payload["daily"]["temperature_2m_min"][1] = 13
        with self.assertRaises(ValueError):
            updater.validate_weather(payload, TODAY)

    def test_boundaries_and_integral_float_codes(self):
        payload = forecast()
        payload["daily"].update(weather_code=[0.0, 99], temperature_2m_min=[-100, 70],
                                temperature_2m_max=[-100, 70], precipitation_probability_max=[0, 100])
        updater.validate_weather(payload, TODAY)

    def test_units_when_present(self):
        payload = forecast()
        units = {"time": "iso8601", "weather_code": "wmo code", "temperature_2m_min": "°C",
                 "temperature_2m_max": "°C", "precipitation_probability_max": "%"}
        payload["daily_units"] = units
        updater.validate_weather(payload, TODAY)
        for bad in (None, {}, {**units, "temperature_2m_max": "°F"}):
            payload["daily_units"] = bad
            with self.assertRaises(ValueError):
                updater.validate_weather(payload, TODAY)

    def test_invalid_json_and_response_size(self):
        for data in (b"", b"not json", b"\xff", b"null", b"[]", b" " * (updater.MAX_WEATHER_BYTES + 1)):
            with self.subTest(data_length=len(data)), self.assertRaises(ValueError):
                updater.decode_weather(data, TODAY)
        for token in ("NaN", "Infinity", "-Infinity"):
            data = ('{"extra":' + token + ',"daily":' + json.dumps(forecast()["daily"]) + '}').encode()
            with self.assertRaises(ValueError):
                updater.decode_weather(data, TODAY)

    def test_coordinate_validation_and_zero(self):
        for latitude, longitude in ((0, 0), (0, 1), (-90, 180), ("90", "-180")):
            self.assertEqual(updater.weather_coordinates({"latitude": latitude, "longitude": longitude}),
                             (float(latitude), float(longitude)))
        for latitude, longitude in ((True, 0), (0, False), (None, 0), ("", 0), ("nan", 0),
                                    (91, 0), (0, -181), (0, "inf"), ("bad", 0)):
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(ValueError):
                updater.weather_coordinates({"latitude": latitude, "longitude": longitude})


class WeatherTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.settings = {"latitude": 0, "longitude": 0, "timezone": "UTC"}

    def mock_response(self, data):
        self.urlopen.side_effect = None
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = data
        self.urlopen.return_value = response
        return response

    def test_request_contract_and_successful_atomic_refresh(self):
        response = self.mock_response(encoded())
        self.cache.write_bytes(b"old bytes\r\n")
        payload, status = updater.update_weather(self.settings, self.cache, TODAY)
        self.assertEqual((payload, status), (forecast(), "fresh"))
        self.assert_cache(encoded())
        request = self.urlopen.call_args.args[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme + "://" + url.netloc + url.path, updater.FORECAST_URL)
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["forecast_days"], ["2"])
        self.assertEqual(query["timezone"], ["UTC"])
        self.assertEqual(query["temperature_unit"], ["celsius"])
        self.assertEqual(set(query["daily"][0].split(",")), set(updater.DAILY_FIELDS))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": updater.WEATHER_TIMEOUT})
        response.__enter__.return_value.read.assert_called_once_with(updater.MAX_WEATHER_BYTES + 1)
        response.__exit__.assert_called_once()

    def test_success_creates_missing_parent(self):
        self.mock_response(encoded())
        self.cache = self.root / "new" / "weather.json"
        self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY)[1], "fresh")
        self.assertEqual(self.cache.read_bytes(), encoded())
        self.assertEqual(len(list(self.cache.parent.iterdir())), 1)

    def test_network_failures_preserve_exact_fresh_cache(self):
        old = b" \n" + encoded() + b"\t"
        self.cache.write_bytes(old)
        errors = (TimeoutError("hidden"), urllib.error.URLError("hidden"),
                  urllib.error.HTTPError("https://example.test/hidden", 503, "hidden", {}, None),
                  http.client.IncompleteRead(b"hidden"), OSError("hidden"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY), (forecast(), "cache"))
                self.assert_cache(old)
        self.assertNotIn("hidden", self.stderr.getvalue())

    def test_bad_responses_preserve_cache(self):
        old = encoded()
        self.cache.write_bytes(old)
        invalid = forecast()
        invalid["daily"]["weather_code"][0] = 5
        for data in (b"broken", encoded(TODAY - timedelta(days=1)), encoded(TODAY + timedelta(days=1)),
                     json.dumps(invalid).encode(), b"x" * (updater.MAX_WEATHER_BYTES + 1),
                     b"[" * 2000 + b"]" * 2000):
            with self.subTest(size=len(data)):
                self.mock_response(data)
                self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY)[1], "cache")
                self.assert_cache(old)

    def test_stale_malformed_oversized_caches_not_used_or_modified(self):
        self.urlopen.side_effect = OSError("network unavailable")
        for old in (encoded(TODAY - timedelta(days=1)), encoded(TODAY + timedelta(days=1)),
                    b"bad", b"x" * (updater.MAX_WEATHER_BYTES + 1), b"[" * 2000 + b"]" * 2000):
            with self.subTest(size=len(old)):
                self.cache.write_bytes(old)
                self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY), (None, "missing"))
                self.assert_cache(old)

    def test_missing_cache_and_network_failure_leave_no_files(self):
        self.urlopen.side_effect = OSError("unavailable")
        self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY), (None, "missing"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cache_read_permission_failure(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("hidden")):
            self.assertEqual(updater.cached_or_missing(self.cache, TODAY), (None, "missing"))

    def test_missing_coordinates_fall_back_without_request(self):
        self.cache.write_bytes(encoded())
        self.assertEqual(updater.update_weather({}, self.cache, TODAY)[1], "cache")
        self.urlopen.assert_not_called()
        self.assert_cache(encoded())

    def test_write_failures_preserve_exact_cache_and_remove_temporary(self):
        old = b" \n" + encoded()
        self.cache.write_bytes(old)
        self.mock_response(encoded())
        for target in ("tempfile.mkstemp", "os.fdopen", "os.fsync", "os.replace"):
            with self.subTest(target=target), mock.patch("update_no_model." + target, side_effect=OSError("hidden")):
                self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY)[1], "cache")
                self.assert_cache(old)

    def test_write_or_flush_failure_does_not_truncate_cache(self):
        old = encoded()
        self.cache.write_bytes(old)
        self.mock_response(encoded())
        real_fdopen = os.fdopen
        for failing_method in ("write", "flush"):
            def broken_stream(fd, mode):
                real = real_fdopen(fd, mode)
                wrapper = mock.MagicMock(wraps=real)
                wrapper.__enter__.return_value = wrapper
                wrapper.__exit__.side_effect = lambda *_: real.close()
                getattr(wrapper, failing_method).side_effect = OSError("hidden")
                return wrapper
            with self.subTest(method=failing_method), mock.patch.object(updater.os, "fdopen", side_effect=broken_stream):
                self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY)[1], "cache")
                self.assert_cache(old)

    def test_write_failure_without_old_cache_leaves_none(self):
        self.mock_response(encoded())
        with mock.patch.object(updater.os, "replace", side_effect=PermissionError("hidden")):
            self.assertEqual(updater.update_weather(self.settings, self.cache, TODAY), (None, "missing"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cache_parent_is_file(self):
        blocker = self.root / "blocker"
        blocker.write_bytes(b"unchanged")
        self.mock_response(encoded())
        self.assertEqual(updater.update_weather(self.settings, blocker / "weather.json", TODAY), (None, "missing"))
        self.assertEqual(blocker.read_bytes(), b"unchanged")

    def test_atomic_replace_observes_complete_temp_and_unmodified_old(self):
        old = b"old\x00bytes"
        self.cache.write_bytes(old)
        real_replace = os.replace
        def inspect(source, target):
            self.assertEqual(Path(source).parent, self.cache.parent)
            self.assertEqual(Path(source).read_bytes(), encoded())
            self.assertEqual(self.cache.read_bytes(), old)
            real_replace(source, target)
        with mock.patch.object(updater.os, "replace", side_effect=inspect) as replace:
            updater.atomic_write(self.cache, encoded())
        replace.assert_called_once()
        self.assert_cache(encoded())


class MainTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"TEST_DASHBOARD_PASSWORD": "synthetic-not-sent"}, clear=True))
        self.dashboard = SimpleNamespace(
            __file__=str(Path(updater.__file__).with_name("dashboard.py")),
            CONFIG={"nextcloud": {"caldav_url": "https://example.test/calendar/",
                                   "webdav_url": "https://example.test/dashboard.png",
                                   "username": "example-user", "password_env": "TEST_DASHBOARD_PASSWORD"}},
            WEATHER={"latitude": 0, "longitude": 0, "timezone": "UTC"}, TZ=timezone.utc,
            parse_weather_payload=mock.Mock(return_value=["day1", "day2"]),
            fetch_events=mock.Mock(return_value=["event"]), render=mock.Mock(),
            upload=mock.Mock(return_value=True),
        )
        self.importer = self.enterContext(mock.patch.object(updater.importlib, "import_module", return_value=self.dashboard))
        self.clock = self.enterContext(mock.patch.object(updater, "datetime"))
        self.now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
        self.clock.now.return_value = self.now
        self.refresh = self.enterContext(mock.patch.object(updater, "update_weather", return_value=(forecast(), "fresh")))

    def run_main(self, *extra):
        return updater.main(["--weather-file", str(self.cache), "--output", str(self.output), *extra])

    def test_success_calls_existing_interfaces_and_upload(self):
        self.assertEqual(self.run_main(), 0)
        self.refresh.assert_called_once_with(self.dashboard.WEATHER, self.cache, TODAY)
        self.dashboard.parse_weather_payload.assert_called_once_with(forecast(), TODAY, 2)
        self.dashboard.fetch_events.assert_called_once_with(TODAY, 1, "synthetic-not-sent")
        self.dashboard.render.assert_called_once_with(["event"], ["day1", "day2"], self.now, self.output, 1)
        self.dashboard.upload.assert_called_once_with(self.output, "synthetic-not-sent")
        self.assertIn("weather=fresh; upload=uploaded", self.stdout.getvalue())
        self.assertNotIn("synthetic-not-sent", self.stdout.getvalue() + self.stderr.getvalue())

    def test_remote_unchanged_is_success(self):
        self.dashboard.upload.return_value = False
        self.assertEqual(self.run_main(), 0)
        self.assertIn("upload=unchanged", self.stdout.getvalue())

    def test_no_upload_still_refreshes_weather_and_fetches_calendar(self):
        self.dashboard.CONFIG["nextcloud"]["webdav_url"] = ""
        self.assertEqual(self.run_main("--no-upload"), 0)
        self.refresh.assert_called_once()
        self.dashboard.fetch_events.assert_called_once()
        self.dashboard.upload.assert_not_called()
        self.assertIn("upload=disabled", self.stdout.getvalue())

    def test_fresh_cache_fallback_is_degraded_success(self):
        self.refresh.return_value = forecast(), "cache"
        self.assertEqual(self.run_main(), 3)
        self.dashboard.upload.assert_called_once()
        self.assertIn("weather=cache", self.stdout.getvalue())

    def test_missing_weather_renders_empty_and_returns_degraded(self):
        self.refresh.return_value = None, "missing"
        self.assertEqual(self.run_main("--no-upload"), 3)
        self.dashboard.parse_weather_payload.assert_not_called()
        self.assertEqual(self.dashboard.render.call_args.args[1], [])

    def test_calendar_render_upload_failures_override_degraded_status(self):
        self.refresh.return_value = None, "missing"
        for stage in ("fetch_events", "render", "upload"):
            with self.subTest(stage=stage):
                for name in ("fetch_events", "render", "upload"):
                    getattr(self.dashboard, name).reset_mock(side_effect=True)
                getattr(self.dashboard, stage).side_effect = RuntimeError("secret endpoint must not appear")
                self.assertEqual(self.run_main(), 1)
                if stage == "fetch_events":
                    self.dashboard.render.assert_not_called()
                if stage != "upload":
                    self.dashboard.upload.assert_not_called()
        self.assertNotIn("secret endpoint", self.stderr.getvalue())

    def test_missing_credentials_are_configuration_error_without_work(self):
        del os.environ["TEST_DASHBOARD_PASSWORD"]
        self.assertEqual(self.run_main("--no-upload"), 2)
        self.refresh.assert_not_called()
        self.dashboard.fetch_events.assert_not_called()

    def test_config_or_dependency_import_failure(self):
        for error in (ValueError("hidden config"), ModuleNotFoundError("hidden dependency")):
            with self.subTest(error=type(error).__name__):
                self.importer.side_effect = error
                self.assertEqual(self.run_main(), 2)
                self.refresh.assert_not_called()
        self.assertNotIn("hidden", self.stderr.getvalue())

    def test_invalid_endpoints(self):
        for key in ("caldav_url", "webdav_url"):
            original = self.dashboard.CONFIG["nextcloud"][key]
            for value in ("", "http://example.test/path", "https:///path", "https://user:pass@example.test/",
                          "https://user@example.test/", "https://example.test:bad/", "https://example.test/#fragment",
                          "https://example.test/with space"):
                with self.subTest(key=key, value=value):
                    self.dashboard.CONFIG["nextcloud"][key] = value
                    self.assertEqual(self.run_main(), 2)
            self.dashboard.CONFIG["nextcloud"][key] = original
        self.refresh.assert_not_called()

    def test_missing_username(self):
        self.dashboard.CONFIG["nextcloud"]["username"] = ""
        self.assertEqual(self.run_main(), 2)
        self.refresh.assert_not_called()

    def test_output_cache_or_source_collisions(self):
        self.output = self.cache
        self.assertEqual(self.run_main(), 2)
        self.output = Path(updater.__file__)
        self.assertEqual(self.run_main(), 2)
        self.output = self.root / "dashboard.png"
        self.cache = Path(self.dashboard.__file__)
        self.assertEqual(self.run_main(), 2)
        self.refresh.assert_not_called()

    def test_config_collision(self):
        os.environ["KINDLE_DASHBOARD_CONFIG"] = str(self.cache)
        self.assertEqual(self.run_main(), 2)
        self.refresh.assert_not_called()

    def test_cli_help_and_bad_arguments_do_not_load_config(self):
        for args, status in ((["--help"], 0), (["--unknown"], 2)):
            with self.subTest(args=args), self.assertRaises(SystemExit) as result:
                updater.main(args)
            self.assertEqual(result.exception.code, status)
        self.importer.assert_not_called()
        self.refresh.assert_not_called()

    def test_local_date_not_utc_date(self):
        zone = ZoneInfo("Pacific/Kiritimati")
        self.dashboard.TZ = zone
        self.dashboard.WEATHER["timezone"] = zone.key
        self.clock.now.return_value = datetime(2030, 1, 2, 0, 30, tzinfo=zone)
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(all(call.args == (zone,) for call in self.clock.now.call_args_list))
        self.assertEqual(self.refresh.call_args.args[2], TODAY)

    def test_midnight_does_not_render_or_rewrite_stale_cache(self):
        self.cache.write_bytes(encoded())
        self.clock.now.side_effect = [self.now, self.now + timedelta(days=1)]
        self.assertEqual(self.run_main(), 3)
        self.dashboard.parse_weather_payload.assert_not_called()
        self.assertEqual(self.dashboard.render.call_args.args[1], [])
        self.assertEqual(self.dashboard.fetch_events.call_args.args[0], TODAY + timedelta(days=1))
        self.assertEqual(self.cache.read_bytes(), encoded())

    def test_midnight_can_reuse_new_day_cache_from_another_update(self):
        self.cache.write_bytes(encoded(TODAY + timedelta(days=1)))
        self.clock.now.side_effect = [self.now, self.now + timedelta(days=1)]
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.dashboard.parse_weather_payload.call_args.args[0], forecast(TODAY + timedelta(days=1)))

    def test_end_to_end_orchestration_uses_real_refresh_and_parser_offline(self):
        # Import only with a public example; never read config.local.json or resolve credentials.
        config_path = str(Path(updater.__file__).with_name("config.example.json"))
        with mock.patch.dict(os.environ, {"KINDLE_DASHBOARD_CONFIG": config_path}):
            import dashboard as real_dashboard
        with mock.patch.object(updater, "update_weather", wraps=REAL_UPDATE_WEATHER):
            self.dashboard.parse_weather_payload = real_dashboard.parse_weather_payload
            self.urlopen.side_effect = None
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = encoded()
            self.urlopen.return_value = response
            self.assertEqual(self.run_main("--no-upload"), 0)
        self.assertEqual(self.cache.read_bytes(), encoded())
        weather = self.dashboard.render.call_args.args[1]
        self.assertEqual([item.day for item in weather], [TODAY, TODAY + timedelta(days=1)])
        self.assertEqual((weather[1].condition, weather[1].rain), ("雨", 75))
        self.dashboard.upload.assert_not_called()


REAL_UPDATE_WEATHER = updater.update_weather


class CLITests(OfflineTests):
    def test_help_in_subprocess_ignores_bad_config_and_does_not_create_bytecode(self):
        config = self.root / "invalid.json"
        config.write_text("not json", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
               "KINDLE_DASHBOARD_CONFIG": str(config)}
        result = subprocess.run([sys.executable, str(Path(updater.__file__).resolve()), "--help"],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("weather and CalDAV still use the network", " ".join(result.stdout.split()))
        self.assertEqual([p.name for p in self.root.iterdir()], ["invalid.json"])

    def test_real_invalid_config_exit_is_two_without_network(self):
        config = self.root / "invalid.json"
        config.write_text("not json", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
               "KINDLE_DASHBOARD_CONFIG": str(config)}
        result = subprocess.run([sys.executable, str(Path(updater.__file__).resolve())],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn(str(config), result.stderr)
        self.assertEqual([p.name for p in self.root.iterdir()], ["invalid.json"])


if __name__ == "__main__":
    unittest.main()
