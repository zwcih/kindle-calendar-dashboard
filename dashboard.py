#!/usr/bin/env python3
"""Render a Kindle calendar dashboard from a CalDAV calendar."""
from __future__ import annotations
import argparse, base64, html, json, os, re, sys, tempfile, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

PROJECT_DIR = Path(__file__).resolve().parent


def load_config(path: str | Path | None = None) -> dict:
    """Load ignored local configuration; environment variables override it."""
    config_path = Path(path or os.getenv("KINDLE_DASHBOARD_CONFIG", PROJECT_DIR / "config.local.json"))
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    nextcloud = config.setdefault("nextcloud", {})
    weather = config.setdefault("weather", {})
    display = config.setdefault("display", {})
    fonts = config.setdefault("fonts", {})
    allowed = {
        "root": {"nextcloud", "weather", "display", "fonts"},
        "nextcloud": {"caldav_url", "webdav_url", "username", "password_env"},
        "weather": {"latitude", "longitude", "timezone"},
        "display": {"width", "height"},
        "fonts": {"regular", "bold"},
    }
    unknown = set(config) - allowed["root"]
    if unknown:
        raise ValueError(f"unknown top-level config fields: {', '.join(sorted(unknown))}")
    for name, section in (("nextcloud", nextcloud), ("weather", weather), ("display", display), ("fonts", fonts)):
        unknown = set(section) - allowed[name]
        if unknown:
            raise ValueError(f"unknown {name} config fields: {', '.join(sorted(unknown))}")
    nextcloud["caldav_url"] = os.getenv("KINDLE_CALDAV_URL", nextcloud.get("caldav_url", ""))
    nextcloud["webdav_url"] = os.getenv("KINDLE_WEBDAV_URL", nextcloud.get("webdav_url", ""))
    nextcloud["username"] = os.getenv("KINDLE_NEXTCLOUD_USER", nextcloud.get("username", ""))
    nextcloud["password_env"] = nextcloud.get("password_env", "NEXTCLOUD_PASSWORD")
    weather["latitude"] = os.getenv("KINDLE_WEATHER_LAT", weather.get("latitude", ""))
    weather["longitude"] = os.getenv("KINDLE_WEATHER_LON", weather.get("longitude", ""))
    weather["timezone"] = os.getenv("KINDLE_TIMEZONE", weather.get("timezone", "Asia/Shanghai"))
    display.setdefault("width", 1072)
    display.setdefault("height", 1448)
    fonts.setdefault("regular", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    fonts.setdefault("bold", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
    forbidden = {"password", "secret", "token"} & set(nextcloud)
    if forbidden:
        raise ValueError(f"credentials are not allowed in config: {', '.join(sorted(forbidden))}")
    for key in ("caldav_url", "webdav_url"):
        value = nextcloud[key]
        if value and urllib.parse.urlsplit(value).scheme.lower() != "https":
            raise ValueError(f"nextcloud.{key} must use HTTPS")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(nextcloud["password_env"])):
        raise ValueError("nextcloud.password_env must be an uppercase environment variable name")
    if bool(weather["latitude"]) != bool(weather["longitude"]):
        raise ValueError("weather latitude and longitude must be configured together")
    if weather["latitude"] != "":
        try:
            latitude, longitude = float(weather["latitude"]), float(weather["longitude"])
        except (TypeError, ValueError) as exc:
            raise ValueError("weather latitude and longitude must be numbers") from exc
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("weather latitude/longitude are out of range")
        weather["latitude"], weather["longitude"] = latitude, longitude
    ZoneInfo(str(weather["timezone"]))
    if any(type(display[key]) is not int or display[key] <= 0 for key in ("width", "height")):
        raise ValueError("display width and height must be positive integers")
    if any(not isinstance(fonts[key], str) or not fonts[key] for key in ("regular", "bold")):
        raise ValueError("font paths must be non-empty strings")
    return config


CONFIG = load_config()
NEXTCLOUD = CONFIG["nextcloud"]
WEATHER = CONFIG["weather"]
TZ = ZoneInfo(WEATHER["timezone"])
CALDAV_URL = NEXTCLOUD["caldav_url"]
WEBDAV_URL = NEXTCLOUD["webdav_url"]
USER = NEXTCLOUD["username"]
PASSWORD_ENV = NEXTCLOUD["password_env"]
FONT_REGULAR = CONFIG["fonts"]["regular"]
FONT_BOLD = CONFIG["fonts"]["bold"]
W, H = int(CONFIG["display"]["width"]), int(CONFIG["display"]["height"])
# Bottom strip is exclusively reserved for client-rendered battery/status text.
STATUS_BAR_HEIGHT = 36

@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime | None
    all_day: bool = False
    location: str = ""


@dataclass(frozen=True)
class WeatherDay:
    day: date
    condition: str
    low: int
    high: int
    rain: int


def weather_condition(code: int) -> str:
    if code == 0: return "晴"
    if code in (1, 2): return "晴间多云"
    if code == 3: return "阴"
    if code in (45, 48): return "雾"
    if code in (51, 53, 55, 56, 57): return "毛毛雨"
    if code in (61, 63, 65, 66, 67, 80, 81, 82): return "雨"
    if code in (71, 73, 75, 77, 85, 86): return "雪"
    if code in (95, 96, 99): return "雷雨"
    return "未知"


def parse_weather_payload(payload: dict, start_day: date, count: int = 3) -> list[WeatherDay]:
    daily = payload["daily"]
    result=[]
    for i, raw_day in enumerate(daily["time"]):
        day = date.fromisoformat(raw_day)
        if day < start_day or len(result) >= count: continue
        result.append(WeatherDay(day, weather_condition(int(daily["weather_code"][i])),
            round(float(daily["temperature_2m_min"][i])), round(float(daily["temperature_2m_max"][i])),
            int(daily["precipitation_probability_max"][i] or 0)))
    return result


def fetch_weather(start_day: date, count: int = 3) -> list[WeatherDay]:
    lat = str(WEATHER["latitude"])
    lon = str(WEATHER["longitude"])
    if not lat or not lon:
        raise ValueError("weather.latitude and weather.longitude are required")
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": WEATHER["timezone"], "forecast_days": max(3, count),
    })
    req = urllib.request.Request("https://api.open-meteo.com/v1/forecast?" + query,
                                 headers={"User-Agent": "OpenClaw-Kindle-Dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return parse_weather_payload(json.loads(response.read()), start_day, count)


def unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def ical_unescape(value: str) -> str:
    return (value.replace("\\N", "\n").replace("\\n", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def property_value(lines: list[str], name: str) -> tuple[str, str]:
    for line in lines:
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        if head.split(";", 1)[0].upper() == name.upper():
            return head, ical_unescape(value.strip())
    return "", ""


def parse_ical_datetime(head: str, value: str) -> tuple[datetime, bool]:
    params = {p.split("=", 1)[0].upper(): p.split("=", 1)[1] for p in head.split(";")[1:] if "=" in p}
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and "T" not in value):
        d = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(d, time.min, TZ), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(TZ), False
    fmt = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    naive = datetime.strptime(value[:15] if fmt.endswith("%S") else value[:13], fmt)
    tzid = params.get("TZID")
    try:
        zone = ZoneInfo(tzid) if tzid else TZ
    except Exception:
        zone = TZ
    return naive.replace(tzinfo=zone).astimezone(TZ), False


def parse_calendar_data(text: str) -> list[Event]:
    events: dict[tuple[str, str], tuple[tuple[int, str], Event]] = {}
    for block in re.findall(r"(?ms)^BEGIN:VEVENT\s*$.*?^END:VEVENT\s*$", text.replace("\r\n", "\n")):
        lines = unfold(block)
        _, status = property_value(lines, "STATUS")
        if status.upper() == "CANCELLED":
            continue
        _, uid = property_value(lines, "UID")
        _, rid = property_value(lines, "RECURRENCE-ID")
        hstart, vstart = property_value(lines, "DTSTART")
        if not vstart:
            continue
        try:
            start, all_day = parse_ical_datetime(hstart, vstart)
        except ValueError:
            continue
        hend, vend = property_value(lines, "DTEND")
        end = parse_ical_datetime(hend, vend)[0] if vend else None
        _, title = property_value(lines, "SUMMARY")
        _, location = property_value(lines, "LOCATION")
        _, seq = property_value(lines, "SEQUENCE")
        _, stamp = property_value(lines, "LAST-MODIFIED")
        if not stamp:
            _, stamp = property_value(lines, "DTSTAMP")
        rank = (int(seq) if seq.isdigit() else 0, stamp)
        key = (uid or f"anon-{vstart}-{title}", rid or vstart)
        event = Event(uid=key[0], title=title or "未命名日程", start=start, end=end,
                      all_day=all_day, location=location)
        if key not in events or rank >= events[key][0]:
            events[key] = (rank, event)
    return sorted((x[1] for x in events.values()), key=lambda e: (e.start, e.title))


def basic_auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode()


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward calendar credentials to a different URL origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme.lower(), old.hostname, old.port) != (new.scheme.lower(), new.hostname, new.port):
            raise urllib.error.HTTPError(newurl, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(req: urllib.request.Request, password: str, timeout: int = 45) -> bytes:
    if urllib.parse.urlsplit(req.full_url).scheme.lower() != "https":
        raise ValueError("authenticated requests require HTTPS")
    req.add_header("Authorization", basic_auth(password))
    opener = urllib.request.build_opener(SameOriginRedirectHandler())
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def fetch_events(start_day: date, days: int, password: str) -> list[Event]:
    local_start = datetime.combine(start_day, time.min, TZ)
    local_end = local_start + timedelta(days=days + 1)
    u0 = local_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    u1 = local_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = f'''<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
 <d:prop><c:calendar-data><c:expand start="{u0}" end="{u1}"/></c:calendar-data></d:prop>
 <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{u0}" end="{u1}"/></c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>'''.encode()
    req = urllib.request.Request(CALDAV_URL, data=body, method="REPORT",
        headers={"Depth":"1", "Content-Type":"application/xml; charset=utf-8"})
    xml = request(req, password)
    root = ET.fromstring(xml)
    ns = {"c":"urn:ietf:params:xml:ns:caldav"}
    items: list[Event] = []
    seen: set[tuple[str, datetime, str]] = set()
    for node in root.findall(".//c:calendar-data", ns):
        for ev in parse_calendar_data(node.text or ""):
            key = (ev.uid, ev.start, ev.title)
            if key not in seen:
                seen.add(key); items.append(ev)
    return sorted(items, key=lambda e:(e.start, e.title))


def clean_title(title: str) -> str:
    # Remove unsupported supplementary-plane emoji and a redundant trailing schedule only.
    title = "".join(ch for ch in title if ord(ch) <= 0xFFFF).strip()
    title = re.sub(r"[-· ]*周[一二三四五六日末]*(?:\s*)\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}\s*$", "", title)
    title = re.sub(r"[-· ]*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}\s*$", "", title)
    return title.rstrip("-· ") or "未命名日程"


def format_cn_time(value: datetime) -> str:
    hour=value.hour
    period="上午" if hour < 12 else "下午"
    display=hour if hour <= 12 else hour-12
    if display == 0: display=12
    return f"{period}{display}:{value.minute:02d}"


def highlight_due(event: Event, now: datetime) -> bool:
    """Timed events active now or starting within two hours (inclusive)."""
    if event.all_day:
        return False
    start = event.start.astimezone(TZ)
    now = now.astimezone(TZ)
    active = event.end is not None and start <= now < event.end.astimezone(TZ)
    return active or now <= start <= now + timedelta(hours=2)


def render(events: list[Event], weather: list[WeatherDay], now: datetime, output: Path, days: int) -> None:
    img=Image.new("L",(W,H),255); d=ImageDraw.Draw(img); margin=52
    def font(sz,bold=False): return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR,sz)
    def text(x,y,s,sz,bold=False,fill=0,anchor=None): d.text((x,y),s,font=font(sz,bold),fill=fill,anchor=anchor)
    def line(y,x1=margin,x2=W-margin,w=2,fill=0): d.line((x1,y,x2,y),fill=fill,width=w)
    def box(coords,r=18,fill=255,outline=0,width=2): d.rounded_rectangle(coords,radius=r,fill=fill,outline=outline,width=width)
    def weather_icon(cx,cy,condition,scale=1.0):
        # Bold geometric icons survive grayscale quantization and e-ink refreshes.
        def sun(x,y,r):
            d.ellipse((x-r,y-r,x+r,y+r),outline=0,width=max(2,round(3*scale)))
            for dx,dy in ((0,-1),(1,-1),(1,0),(1,1),(0,1),(-1,1),(-1,0),(-1,-1)):
                a=r+5*scale; b=r+11*scale
                d.line((x+dx*a,y+dy*a,x+dx*b,y+dy*b),fill=0,width=max(2,round(2*scale)))
        def cloud(x,y):
            w=52*scale; h=22*scale
            d.ellipse((x-w*.45,y-h*.70,x-w*.05,y+h*.35),fill=0)
            d.ellipse((x-w*.20,y-h,x+w*.25,y+h*.38),fill=0)
            d.ellipse((x+w*.05,y-h*.60,x+w*.48,y+h*.38),fill=0)
            d.rounded_rectangle((x-w*.48,y-h*.15,x+w*.48,y+h*.55),radius=round(8*scale),fill=0)
        if condition == "晴":
            sun(cx,cy,13*scale)
        elif condition == "晴间多云":
            sun(cx-13*scale,cy-9*scale,10*scale); cloud(cx+5*scale,cy+6*scale)
        elif condition in ("雨","毛毛雨","雷雨"):
            cloud(cx,cy-5*scale)
            for dx in (-15,0,15):
                d.line((cx+dx*scale,cy+15*scale,cx+(dx-4)*scale,cy+27*scale),fill=0,width=max(2,round(3*scale)))
            if condition == "雷雨":
                d.line((cx+4*scale,cy+12*scale,cx-3*scale,cy+25*scale,cx+6*scale,cy+25*scale,cx-2*scale,cy+38*scale),fill=0,width=max(2,round(3*scale)))
        elif condition == "雪":
            cloud(cx,cy-5*scale)
            for dx in (-14,0,14):
                x=cx+dx*scale; yy=cy+23*scale
                d.line((x-5*scale,yy,x+5*scale,yy),fill=0,width=2)
                d.line((x,yy-5*scale,x,yy+5*scale),fill=0,width=2)
        elif condition == "雾":
            for off in (-12,0,12): d.line((cx-27*scale,cy+off*scale,cx+27*scale,cy+off*scale),fill=0,width=max(2,round(3*scale)))
        else:
            cloud(cx,cy)
    def ellipsis(s,fo,maxw):
        if d.textlength(s,font=fo)<=maxw:return s
        while s and d.textlength(s+"…",font=fo)>maxw:s=s[:-1]
        return s+"…"
    def fit_size(s,maxw,start,minimum=28):
        size=start
        while size>minimum and d.textlength(s,font=font(size,True))>maxw:
            size-=2
        return size
    def wrap_text(s,size,maxw,max_lines=3):
        lines=[]; current=""
        for ch in s:
            if current and d.textlength(current+ch,font=font(size,True))>maxw:
                lines.append(current); current=ch
            else:
                current+=ch
        if current: lines.append(current)
        if len(lines)>max_lines:
            lines=lines[:max_lines]
            lines[-1]=ellipsis(lines[-1],font(size,True),maxw)
        return lines
    def split_time_label(value):
        """Put period words above the numeric range to save horizontal space."""
        if value == "全天":
            return "", value
        periods=re.findall(r"上午|下午",value)
        clocks=re.findall(r"\d{1,2}:\d{2}",value)
        if not clocks:
            return "", value
        period="–".join(dict.fromkeys(periods))
        clock="–".join(clocks)
        return period,clock
    today=now.astimezone(TZ).date(); weekdays="一二三四五六日"

    weather_by_day={w.day:w for w in weather}

    def time_range(event):
        if event.all_day:
            return "全天"
        st=event.start.astimezone(TZ)
        if not event.end:
            return format_cn_time(st)
        et=event.end.astimezone(TZ)
        end=format_cn_time(et)
        for prefix in ("上午","下午"):
            if end.startswith(prefix) and format_cn_time(st).startswith(prefix):
                end=end.removeprefix(prefix)
        return f"{format_cn_time(st)}–{end}"

    local_now=now.astimezone(TZ)

    def day_rows(day):
        rows=[]
        for event in events:
            if event.start.astimezone(TZ).date()!=day:
                continue
            # Today only: hide events whose end time has already passed.
            # Keep all-day events and events without an explicit end until their start time.
            if day == today and not event.all_day:
                cutoff=(event.end or event.start).astimezone(TZ)
                if cutoff < local_now:
                    continue
            title_value=clean_title(event.title)
            if title_value.startswith("学校·"):
                rows.append(("校内",title_value.removeprefix("学校·"),highlight_due(event,local_now)))
            else:
                rows.append((time_range(event),title_value,highlight_due(event,local_now)))
        return rows

    today_rows=day_rows(today)
    tomorrow=today+timedelta(days=1)
    tomorrow_rows=day_rows(tomorrow)

    first_row_drawn=False

    def draw_day(day,label,rows,top,bottom,compact=False):
        nonlocal first_row_drawn
        box((24,top,W-24,bottom),26,fill=255,outline=0,width=4)
        header_h=132 if compact else 154
        label_size=58 if compact else 72
        date_size=34 if compact else 40
        center_y=top+header_h//2-4
        text(58,center_y,label,label_size,True,anchor="lm")
        text(218 if compact else 246,center_y,
             f"{day.month}月{day.day}日 星期{weekdays[day.weekday()]}",date_size,fill=45,anchor="lm")

        w=weather_by_day.get(day); weather_x=W-58
        if w:
            # Weather is one compact, vertically aligned cluster: icon on the
            # left, condition/temperature on the first line, rain just below it.
            weather_line=f"{w.condition}  {w.low}–{w.high}℃"
            weather_size=31 if compact else 37
            weather_left=weather_x-d.textlength(weather_line,font=font(weather_size,True))
            # Position from the measured text edge: close for short conditions,
            # while long names retain a small non-overlapping gap.
            icon_x=weather_left-(43 if compact else 52)
            # Optical center of the complete two-line weather block.
            icon_y=top+(74 if compact else 87)
            weather_icon(icon_x,icon_y,w.condition,1.05 if compact else 1.25)
            text(weather_x,top+(22 if compact else 27),
                 weather_line,weather_size,True,anchor="ra")
            text(weather_x,top+(76 if compact else 91),
                 f"降雨概率 {w.rain}%",26 if compact else 30,fill=45,anchor="ra")
        else:
            text(weather_x,center_y,"天气暂缺",34 if compact else 40,True,fill=70,anchor="rm")

        divider=top+header_h
        d.line((52,divider,W-52,divider),fill=100,width=4)
        y=divider+16; available=bottom-y-16
        if not rows:
            text(W//2,(y+bottom)//2,f"{label}没有安排",58 if compact else 86,True,fill=45,anchor="mm")
            return
        min_row=160 if compact else 140
        max_rows=max(1,available//min_row)
        shown=min(len(rows),max_rows)
        overflow=len(rows)-shown
        note_h=48 if overflow else 0
        row_h=(available-note_h)//shown
        inner_left=76; inner_right=W-76; inner_w=inner_right-inner_left
        # One shared time column keeps every row aligned. The title receives all
        # remaining width and grows independently for short labels.
        split_times=[split_time_label(tm) for tm,_,_ in rows[:shown]]
        time_col=194 if compact else 214
        title_x=inner_left+time_col+34
        title_w=inner_right-title_x
        # Fit mixed-size period/time labels; center each complete line without leading zeros.
        shared_clock_size=min(66 if compact else 74,max(28,(row_h-38)//3))
        while shared_clock_size>24:
            label_size=round(shared_clock_size*.64)
            required=d.textlength("上",font=font(label_size,True))+4+d.textlength("00:00",font=font(shared_clock_size,True))
            if required<=time_col: break
            shared_clock_size-=1
        shared_period_size=round(shared_clock_size*.64)
        for (tm,title_value,due),(period,clock) in zip(rows[:shown],split_times):
            card_bottom=y+row_h-12
            highlighted=not first_row_drawn and due
            first_row_drawn=True
            row_text=255 if highlighted else 0
            box((52,y,W-52,card_bottom),22,fill=0 if highlighted else 244,outline=0 if highlighted else 145,width=2)
            center_y=(y+card_bottom)//2
            clocks=clock.split("–")
            if period:
                periods=period.split("–")
                if len(periods)==1: periods=periods*len(clocks)
                clock_size=shared_clock_size
                period_size=shared_period_size
                offset=min(round(clock_size*1.12),(row_h-34-clock_size)//2)
                for idx,part in enumerate(clocks):
                    clock_y=center_y if len(clocks)==1 else center_y+(-offset if idx==0 else offset)
                    period_text=periods[min(idx,len(periods)-1)].replace("午", "")
                    period_width=d.textlength(period_text,font=font(period_size,True))
                    number_width=d.textlength(part,font=font(clock_size,True))
                    line_left=inner_left+(time_col-period_width-4-number_width)/2
                    text(line_left,clock_y,period_text,period_size,True,fill=row_text,anchor="lm")
                    text(line_left+period_width+4,clock_y,part,clock_size,True,fill=row_text,anchor="lm")
                if len(clocks)==2:
                    text(inner_left+time_col//2,center_y,"—",max(24,clock_size-8),True,fill=row_text,anchor="mm")
            else:
                clock_size=fit_size(clock,time_col,min(68,max(32,row_h//3)),26)
                text(inner_left+time_col//2,center_y,clock,clock_size,True,fill=row_text,anchor="mm")
            d.line((title_x-18,y+22,title_x-18,card_bottom-22),fill=175,width=2)
            title_start=min(130 if not compact else 98,max(48,round(row_h*.48)))
            # Keep near-fitting titles at their largest single-line size;
            # only switch to two lines when a single line would be too small.
            single_size=fit_size(title_value,title_w,title_start,38)
            if single_size >= min(title_start,84 if compact else 100):
                title_start=single_size
            if d.textlength(title_value,font=font(title_start,True))>title_w:
                title_size=min(title_start,78 if compact else 90)
                title_lines=wrap_text(title_value,title_size,title_w,max(2,len(title_value)))
                while title_size>38 and (len(title_lines)>2 or len(title_lines)*round(title_size*1.16)>row_h-34):
                    title_size-=2
                    title_lines=wrap_text(title_value,title_size,title_w,max(2,len(title_value)))
                title_lines=wrap_text(title_value,title_size,title_w,2)
                line_h=round(title_size*1.16)
                first_y=center_y-(len(title_lines)-1)*line_h//2
                for part in title_lines:
                    text(title_x+title_w//2,first_y,part,title_size,True,fill=row_text,anchor="mm")
                    first_y+=line_h
            else:
                text(title_x+title_w//2,center_y,title_value,title_start,True,fill=row_text,anchor="mm")
            y+=row_h
        if overflow:
            text(W//2,bottom-43,f"明天还有 {overflow} 项",30,True,fill=55,anchor="ma")

    top=24; bottom=H-STATUS_BAR_HEIGHT-12; gap=18
    if not today_rows:
        # Once today is finished, tomorrow takes the whole screen.
        draw_day(tomorrow,"明天",tomorrow_rows,top,bottom)
    elif tomorrow_rows and len(today_rows)<=2:
        # Today remains primary, but one or two items do not waste the lower half.
        today_h=580 if len(today_rows)==1 else 760
        split=top+today_h
        draw_day(today,"今天",today_rows,top,split)
        draw_day(tomorrow,"明天",tomorrow_rows,split+gap,bottom,compact=True)
    else:
        draw_day(today,"今天",today_rows,top,bottom)
    # Three stable gray levels; no dithering artifacts on e-ink.
    img=img.point(lambda p:255 if p>180 else (0 if p<80 else 160))
    output.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=output.name+".",suffix=".tmp",dir=output.parent); os.close(fd)
    try:
        img.save(tmp,format="PNG",optimize=True); os.replace(tmp,output)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def upload(path: Path, password: str) -> bool:
    if not WEBDAV_URL:
        raise ValueError("nextcloud.webdav_url is required unless --no-upload is used")
    data=path.read_bytes()
    try:
        current=request(urllib.request.Request(WEBDAV_URL,method="GET"),password)
        if current == data:
            return False
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    req=urllib.request.Request(WEBDAV_URL,data=data,method="PUT",headers={"Content-Type":"image/png"})
    request(req,password)
    return True


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--no-upload",action="store_true"); ap.add_argument("--output",default="output/dashboard.png"); ap.add_argument("--days",type=int,default=1); ap.add_argument("--weather-file",default="output/weather.json"); args=ap.parse_args()
    if not 1<=args.days<=14: print("days must be 1..14",file=sys.stderr); return 2
    if not CALDAV_URL or not USER:
        print("nextcloud.caldav_url and nextcloud.username are required",file=sys.stderr); return 2
    password=os.getenv(PASSWORD_ENV)
    if not password: print(f"credential environment variable {PASSWORD_ENV} is not available",file=sys.stderr); return 2
    now=datetime.now(TZ)
    try:
        events=fetch_events(now.date(),args.days,password)
        try:
            weather_path=Path(args.weather_file)
            if weather_path.exists():
                weather=parse_weather_payload(json.loads(weather_path.read_text(encoding="utf-8")),now.date(),2)
            else:
                weather=fetch_weather(now.date(),2)
        except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError, IndexError) as e:
            weather=[]
            print(f"weather unavailable: {type(e).__name__}", file=sys.stderr)
        render(events,weather,now,Path(args.output),args.days)
        uploaded=False if args.no_upload else upload(Path(args.output),password)
        suffix="" if args.no_upload else (" and uploaded" if uploaded else "; remote unchanged")
        print(f"ok: rendered {len(events)} events and {len(weather)} weather days to {args.output}" + suffix); return 0
    except (urllib.error.URLError,ET.ParseError,OSError,ValueError) as e:
        print(f"dashboard update failed: {type(e).__name__}: {e}",file=sys.stderr); return 1
if __name__=="__main__": raise SystemExit(main())
