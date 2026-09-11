import unittest
import json
import tempfile
from datetime import date
from pathlib import Path
from dashboard import Event, parse_calendar_data, parse_ical_datetime, clean_title, format_cn_time, load_config, parse_weather_payload, request, TZ

class DashboardTests(unittest.TestCase):
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
