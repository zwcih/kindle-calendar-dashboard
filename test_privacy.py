"""Synthetic privacy fixtures only; no network or credential access."""
import unittest
from scripts.check_privacy import findings


class PrivacyTests(unittest.TestCase):
    def check(self, text, path='sample.txt'):
        return list(findings(path, text.encode()))

    def test_private_paths(self):
        for path in ('.local/task.py', 'output/image.png', 'config.local.json', '.env',
                     'calendar.ics', 'key.pem', 'backups/a.txt', 'kindle/config.conf',
                     'kindle/kindle-dashboard/dashboard.status',
                     'kindle/kindle-dashboard/display-refresh-error.log',
                     'kindle/kindle-dashboard/.display-refresh-error.synthetic'):
            with self.subTest(path=path):
                self.assertTrue(self.check('', path))

    def test_example_paths(self):
        for path in ('config.example.json', '.env.example', 'kindle/config.example.conf'):
            self.assertFalse(self.check('', path))

    def test_token_signatures(self):
        for value in ('gh' + 'p_' + 'A' * 36, 'github_' + 'pat_' + 'B' * 40,
                      'AK' + 'IA' + 'Z' * 16, '-----BEGIN ' + 'PRIVATE KEY-----'):
            self.assertTrue(self.check(value))

    def test_credentials(self):
        self.assertTrue(self.check('PASSWORD=' + '"' + 'random-sensitive-value' + '"'))
        self.assertFalse(self.check('PASSWORD=' + '"' + 'synthetic-not-sent' + '"'))

    def test_private_urls_and_paths(self):
        host = 'https://' + 'private-host' + '.com'
        self.assertTrue(self.check(host + '/remote.php/dav/files/account/file'))
        self.assertTrue(self.check(host + '/s/' + 'A' * 15))
        self.assertTrue(self.check('/home/' + 'person' + '/project'))
        self.assertFalse(self.check('https://cloud.example.com/remote.php/dav/files/user/file'))
        self.assertFalse(self.check('https://api.open-meteo.com/v1/forecast'))

    def test_redacted_results(self):
        secret = 'gh' + 'p_' + 'Q' * 36
        result = self.check(secret)
        self.assertNotIn(secret, str(result))

    def test_binary_blocked(self):
        self.assertTrue(list(findings('payload.bin', b'\xff\x00')))


if __name__ == '__main__':
    unittest.main()
