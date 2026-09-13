#!/usr/bin/env python3
"""Check Git blobs, not ignored workspace files; never print matched values."""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys
import urllib.parse

RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'aws-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'personal-path': re.compile(r'/(?:home|Users)/[A-Za-z0-9_.-]+/|[A-Z]:\\Users\\[^\\\s]+\\'),
    'bearer-token': re.compile(r'(?i)\bBearer\s+[A-Za-z0-9_.~-]{24,}'),
}
KEY = r"[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY)"
CREDENTIAL = re.compile(
    r"(?i)(?:[\"']" + KEY + r"[\"']\s*:|\b" + KEY + r"\s*=)\s*[\"']([^\"'\r\n]+)[\"']"
)
SAFE_VALUES = {'bad', 'not-sent', 'synthetic-not-sent', 'your-app-password',
               'replace-with-an-app-password', 'YOUR_PASSWORD', 'YOUR_TOKEN'}
URL = re.compile(r'https?://[^\s<>"\x27`]+')


def example_host(host):
    return host in {'example.com', 'example.org', 'example.net', 'localhost'} or host.endswith(('.example.com', '.example.org', '.example.net', '.test', '.invalid', '.example'))


def forbidden_path(path):
    p = PurePosixPath(path)
    if any(x in {'.local', 'output', '__pycache__', 'backups', 'cache', 'runtime'} for x in p.parts):
        return True
    n = p.name.lower()
    if n.startswith('.env') and n != '.env.example':
        return True
    if n == 'config.local.json' or '.local.' in n or n in {'wifi-ssid.conf', 'github-pat', 'id_rsa', 'id_ed25519', 'dashboard.status'}:
        return True
    return p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.pdf', '.ics', '.log', '.bak', '.backup', '.pem', '.key', '.p12', '.pyc'} or (n.startswith('config') and n.endswith('.conf') and n != 'config.example.conf')


def findings(path, data):
    if forbidden_path(path):
        yield 0, 'private-artifact-path'
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        yield 0, 'unreviewed-binary'
        return
    if '\0' in text:
        yield 0, 'unreviewed-binary'
    for number, line in enumerate(text.splitlines(), 1):
        for name, pattern in RULES.items():
            if pattern.search(line):
                yield number, name
        for match in CREDENTIAL.finditer(line):
            value = match.group(1)
            if value not in SAFE_VALUES and len(value) >= 8 and not value.startswith(('$', '***')):
                yield number, 'credential-literal'
        for match in URL.finditer(line):
            try:
                url = urllib.parse.urlsplit(match.group())
                host = url.hostname or ''
            except ValueError:
                continue
            if example_host(host):
                continue
            if url.username is not None or url.password is not None:
                yield number, 'url-credentials'
            if '/remote.php/dav/' in url.path or re.search(r'/s/[A-Za-z0-9]{8,}', url.path):
                yield number, 'private-endpoint'


def git(*args):
    return subprocess.check_output(['git', *args])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--staged', action='store_true')
    group.add_argument('--history', action='store_true')
    args = parser.parse_args(argv)
    if args.history and git('rev-parse', '--is-shallow-repository').strip() == b'true':
        print('History scan requires a full checkout.', file=sys.stderr)
        return 2
    refs = git('rev-list', '--all').decode().splitlines() if args.history else [None]
    seen = set()
    count = 0
    failures = 0
    for ref in refs:
        entries = git('ls-tree', '-rz', ref) if ref else git('ls-files', '--stage', '-z')
        for entry in entries.split(b'\0'):
            if not entry:
                continue
            meta, raw_path = entry.split(b'\t', 1)
            parts = meta.decode().split()
            mode, oid = (parts[0], parts[2]) if ref else (parts[0], parts[1])
            path = raw_path.decode('utf-8')
            if (path, oid) in seen:
                continue
            seen.add((path, oid)); count += 1
            if mode not in {'100644', '100755'}:
                print(f'{path}:0: unreviewed-link-or-submodule'); failures += 1
                continue
            for line, rule in findings(path, git('cat-file', 'blob', oid)):
                print(f'{path}:{line}: {rule}'); failures += 1
    print(f'Privacy scan: {count} distinct file versions; {failures} findings.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
