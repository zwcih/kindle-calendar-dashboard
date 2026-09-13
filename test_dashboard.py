import unittest
import json
import tempfile
from datetime import date
from pathlib import Path
from dashboard import Event, parse_calendar_data, parse_ical_datetime, clean_title, format_cn_time, load_config, parse_weather_payload, request, TZ

class DashboardTests(unittest.TestCase):
    def test_highlight_due_boundaries(self):
        from datetime import datetime, timedelta
        from dashboard import highlight_due
        now=datetime(2030,1,2,10,tzinfo=TZ)
        def event(start,end=None,all_day=False):
            return Event(uid="synthetic",title="Example",start=start,end=end,all_day=all_day,location="")
        self.assertTrue(highlight_due(event(now+timedelta(hours=2)),now))
        self.assertFalse(highlight_due(event(now+timedelta(hours=2,seconds=1)),now))
        self.assertTrue(highlight_due(event(now-timedelta(hours=1),now+timedelta(hours=1)),now))
        self.assertFalse(highlight_due(event(now-timedelta(hours=1),now),now))
        self.assertFalse(highlight_due(event(now-timedelta(minutes=1)),now))
        self.assertFalse(highlight_due(event(now,all_day=True),now))

    def test_client_status_strip_stays_blank(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from PIL import Image
        import dashboard as d
        # Use a portable host font; this test checks pixels/bounds, not CJK glyphs.
        from PIL import ImageFont
        try:
            font_path=ImageFont.truetype("DejaVuSans.ttf",24).path
        except OSError:
            self.skipTest("A host TrueType font is required for rendering")
        now=datetime(2030,1,2,8,tzinfo=TZ)
        for count in (0,1,2,5,12):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                events=[]
                for day_offset,n in ((0,count),(1,8)):
                    for i in range(n):
                        start=now+timedelta(days=day_offset,hours=i+1)
                        events.append(Event(uid=f"fixture-{day_offset}-{i}",title="Example event",
                                            start=start,end=start+timedelta(minutes=45),all_day=False,location=""))
                out=Path(tmp)/"test.png"
                with patch.object(d,"FONT_REGULAR",font_path), patch.object(d,"FONT_BOLD",font_path):
                    d.render(events,[],now,out,1)
                with Image.open(out) as image:
                    self.assertEqual(image.size,(d.W,d.H))
                    self.assertEqual(image.mode,"L")
                    self.assertEqual(image.crop((0,d.H-d.STATUS_BAR_HEIGHT,d.W,d.H)).getextrema(),(255,255))
                    self.assertEqual(image.crop((0,0,d.W,d.H-d.STATUS_BAR_HEIGHT)).getextrema()[0],0)

    def test_utc_timezone(self):
        dt, allday=parse_ical_datetime('DTSTART','20260912T010000Z')
        self.assertFalse(allday); self.assertEqual((dt.hour,dt.minute),(9,0))

    def test_all_day(self):
        dt, allday=parse_ical_datetime('DTSTART;VALUE=DATE','20260912')
        self.assertTrue(allday); self.assertEqual(dt.date(),date(2026,9,12))

    def test_cancelled_and_folded(self):
        ics='''BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a
DTSTART:20260912T010000Z
SUMMARY:主\n 播课
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:b
DTSTART:20260912T020000Z
SUMMARY:取消课
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR'''
        events=parse_calendar_data(ics)
        self.assertEqual(len(events),1); self.assertEqual(events[0].title,'主播课')

    def test_latest_revision(self):
        ics='''BEGIN:VEVENT
UID:a
DTSTART:20260912T010000Z
SUMMARY:旧标题
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:a
DTSTART:20260912T010000Z
SUMMARY:新标题
SEQUENCE:2
END:VEVENT'''
        self.assertEqual(parse_calendar_data(ics)[0].title,'新标题')

    def test_clean_title(self):
        self.assertEqual(clean_title('🤺击剑·示例教练·周六 17:30-18:30'),'击剑·示例教练')
        self.assertEqual(clean_title('第3课：时间 10:00'),'第3课：时间 10:00')

    def test_chinese_time(self):
        from datetime import datetime
        self.assertEqual(format_cn_time(datetime(2026,9,10,9,5)), '上午9:05')
        self.assertEqual(format_cn_time(datetime(2026,9,10,13,0)), '下午1:00')
        self.assertEqual(format_cn_time(datetime(2026,9,10,19,30)), '下午7:30')

    def test_weather_payload(self):
        payload={'daily':{'time':['2026-09-10'],'weather_code':[2],
                 'temperature_2m_min':[14.6],'temperature_2m_max':[30.9],
                 'precipitation_probability_max':[20]}}
        days=parse_weather_payload(payload,date(2026,9,10),3)
        self.assertEqual((days[0].condition,days[0].low,days[0].high,days[0].rain),('晴间多云',15,31,20))

    def test_local_config_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.json'
            path.write_text(json.dumps({
                'nextcloud': {'caldav_url': 'https://example.test/calendar/', 'username': 'reader'},
                'weather': {'latitude': 1, 'longitude': 2},
            }), encoding='utf-8')
            config=load_config(path)
        self.assertEqual(config['nextcloud']['username'],'reader')
        self.assertEqual(config['nextcloud']['password_env'],'NEXTCLOUD_PASSWORD')
        self.assertEqual(config['display'],{'width':1072,'height':1448})
        self.assertEqual(config['weather']['timezone'],'Asia/Shanghai')

    def test_config_rejects_plaintext_password_and_http(self):
        for nextcloud in (
            {'caldav_url':'https://example.test/calendar/','username':'reader','password':'bad'},
            {'caldav_url':'http://example.test/calendar/','username':'reader'},
        ):
            with self.subTest(nextcloud=nextcloud), tempfile.TemporaryDirectory() as td:
                path=Path(td)/'config.json'
                path.write_text(json.dumps({
                    'nextcloud':nextcloud,
                    'weather':{'latitude':1,'longitude':2},
                }),encoding='utf-8')
                with self.assertRaises(ValueError): load_config(path)

    def test_authenticated_request_rejects_http(self):
        import urllib.request
        with self.assertRaises(ValueError):
            request(urllib.request.Request('http://example.test/private'), 'not-sent')

if __name__=='__main__': unittest.main()
