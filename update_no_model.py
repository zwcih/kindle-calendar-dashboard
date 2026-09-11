#!/usr/bin/env python3
"""Standalone two-day weather refresh and CalDAV dashboard update (no model)."""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_WEATHER_BYTES = 128 * 1024
WEATHER_TIMEOUT = 20
DAILY_FIELDS = (
    "weather_code", "temperature_2m_min", "temperature_2m_max",
    "precipitation_probability_max",
)
WMO_CODES = frozenset((0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57,
                       61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81,
                       82, 85, 86, 95, 96, 99))


def validate_weather(payload: object, today: date) -> dict:
    """Accept exactly today/tomorrow, finite JSON numbers and known WMO codes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        raise ValueError("weather daily object is required")
    daily = payload["daily"]
    for key in ("time", *DAILY_FIELDS):
        if not isinstance(daily.get(key), list) or len(daily[key]) != 2:
            raise ValueError("weather arrays must contain exactly two days")
    expected = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    if daily["time"] != expected:
        raise ValueError("weather dates must be today and tomorrow in order")
    for key in DAILY_FIELDS:
        for value in daily[key]:
            # bool is a subclass of int; strings/null/non-finite numbers are invalid.
            if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                raise ValueError("weather values must be finite numbers")
    if any(code not in WMO_CODES for code in daily["weather_code"]):
        raise ValueError("unsupported weather code")
    for low, high in zip(daily["temperature_2m_min"], daily["temperature_2m_max"]):
        if not -100 <= low <= high <= 70:
            raise ValueError("weather temperatures must satisfy -100 <= min <= max <= 70 Celsius")
    if any(not 0 <= rain <= 100 for rain in daily["precipitation_probability_max"]):
        raise ValueError("rain probability must be between 0 and 100")
    # Older caches may omit units. When supplied, they must match our request.
    if "daily_units" in payload:
        units = payload["daily_units"]
        expected_units = {"time": "iso8601", "weather_code": "wmo code",
                          "temperature_2m_min": "°C", "temperature_2m_max": "°C",
                          "precipitation_probability_max": "%"}
        if not isinstance(units, dict) or any(units.get(k) != v for k, v in expected_units.items()):
            raise ValueError("unexpected weather units")
    return payload


def decode_weather(data: bytes, today: date) -> dict:
    if len(data) > MAX_WEATHER_BYTES:
        raise ValueError("weather response is too large")
    # Reject non-standard constants even in otherwise unused metadata.
    def invalid_constant(_value):
        raise ValueError("non-finite JSON constant")
    return validate_weather(json.loads(data, parse_constant=invalid_constant), today)


def weather_coordinates(settings: dict) -> tuple[float, float]:
    values = []
    for key, bound in (("latitude", 90), ("longitude", 180)):
        raw = settings.get(key)
        if isinstance(raw, bool) or raw is None or raw == "":
            raise ValueError("weather coordinates are required")
        value = float(raw)
        if not math.isfinite(value) or not -bound <= value <= bound:
            raise ValueError("weather coordinates are out of range")
        values.append(value)
    return values[0], values[1]


def fetch_weather(settings: dict) -> bytes:
    latitude, longitude = weather_coordinates(settings)
    query = urllib.parse.urlencode({
        "latitude": latitude, "longitude": longitude,
        "daily": ",".join(DAILY_FIELDS), "timezone": settings["timezone"],
        "forecast_days": 2, "temperature_unit": "celsius",
    })
    request = urllib.request.Request(
        FORECAST_URL + "?" + query,
        headers={"User-Agent": "Kindle-Calendar-Dashboard/1.0"},
    )
    with urllib.request.urlopen(request, timeout=WEATHER_TIMEOUT) as response:
        data = response.read(MAX_WEATHER_BYTES + 1)
    if len(data) > MAX_WEATHER_BYTES:
        raise ValueError("weather response is too large")
    return data


def atomic_write(path: Path, data: bytes) -> None:
    """Replace only after a complete flushed write; no failing work after commit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = None  # The stream now owns the descriptor, including on failure.
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            os.unlink(temporary)


def read_cache(path: Path, today: date) -> dict:
    with path.open("rb") as stream:
        return decode_weather(stream.read(MAX_WEATHER_BYTES + 1), today)


def cached_or_missing(path: Path, today: date) -> tuple[dict | None, str]:
    try:
        return read_cache(path, today), "cache"
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None, "missing"


def update_weather(settings: dict, cache: Path, today: date) -> tuple[dict | None, str]:
    """Weather failures are non-fatal; never rewrite or delete the old cache."""
    try:
        data = fetch_weather(settings)
        payload = decode_weather(data, today)
        atomic_write(cache, data)
        return payload, "fresh"
    except Exception as exc:
        # This bounded best-effort stage also covers malformed HTTP/JSON responses.
        # Do not log exception text: network errors may contain private URLs.
        print(f"weather refresh failed: {type(exc).__name__}", file=sys.stderr)
        return cached_or_missing(cache, today)


def validate_setup(dashboard, output: Path, cache: Path, no_upload: bool) -> str:
    """Validate credentials/endpoints before fetching or writing anything."""
    config = dashboard.CONFIG
    nextcloud = config["nextcloud"]
    for key in ("caldav_url",) if no_upload else ("caldav_url", "webdav_url"):
        value = nextcloud[key]
        url = urllib.parse.urlsplit(value)
        if (url.scheme.lower() != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.fragment or any(c.isspace() for c in value)):
            raise ValueError("calendar/upload endpoints must be HTTPS URLs without embedded credentials")
        _ = url.port  # Reject malformed ports before any request.
    if not nextcloud["username"]:
        raise ValueError("calendar username is required")
    password = os.environ.get(nextcloud["password_env"])
    if not password:
        raise ValueError("configured credential environment variable is unavailable")
    # Prevent accidental image/cache collisions or overwriting the selected config/source.
    protected = {Path(__file__).resolve(), Path(dashboard.__file__).resolve(),
                 Path(os.environ.get("KINDLE_DASHBOARD_CONFIG",
                                     Path(__file__).resolve().parent / "config.local.json")).resolve()}
    if output.resolve() == cache.resolve() or output.resolve() in protected or cache.resolve() in protected:
        raise ValueError("output, weather cache, source and configuration paths must be distinct")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-upload", action="store_true",
                        help="skip WebDAV upload; weather and CalDAV still use the network")
    parser.add_argument("--output", type=Path, default=Path("output/dashboard.png"))
    parser.add_argument("--weather-file", type=Path, default=Path("output/weather.json"),
                        help="atomically refreshed weather cache (default: output/weather.json)")
    args = parser.parse_args(argv)
    try:
        # Lazy import keeps --help independent of local config and optional dependencies.
        dashboard = importlib.import_module("dashboard")
        password = validate_setup(dashboard, args.output, args.weather_file, args.no_upload)
    except Exception as exc:
        print(f"configuration/credential/dependency error: {type(exc).__name__}", file=sys.stderr)
        return 2

    now = datetime.now(dashboard.TZ)
    payload, weather_status = update_weather(dashboard.WEATHER, args.weather_file, now.date())
    try:
        # Weather may take us across local midnight: never render yesterday's pair.
        now = datetime.now(dashboard.TZ)
        if payload is not None:
            try:
                validate_weather(payload, now.date())
            except ValueError:
                payload, weather_status = cached_or_missing(args.weather_file, now.date())
        weather = [] if payload is None else dashboard.parse_weather_payload(payload, now.date(), 2)
        events = dashboard.fetch_events(now.date(), 1, password)
        dashboard.render(events, weather, now, args.output, 1)
        uploaded = False if args.no_upload else dashboard.upload(args.output, password)
    except Exception as exc:
        print(f"calendar/render/upload failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    upload_status = "disabled" if args.no_upload else ("uploaded" if uploaded else "unchanged")
    print(f"ok: rendered; weather={weather_status}; upload={upload_status}")
    return 0 if weather_status == "fresh" else 3


if __name__ == "__main__":
    raise SystemExit(main())
