# Security and privacy policy

## Never commit private deployment data

This is a public, reusable project. Source, examples, tests, documentation, Git history and release artifacts must not contain:

- Passwords, API keys, access/refresh tokens, cookies, private keys or recovery codes.
- Real CalDAV/WebDAV endpoints, private share URLs, household locations or precise personal coordinates.
- Personal schedules, names, contact details, school/class data, Wi-Fi identifiers or device identifiers.
- Local configuration, credential files, generated calendar images, caches, logs or backups.
- User-specific absolute filesystem paths.

Keep deployment configuration outside Git (for example ignored `config.local.json`); provide credentials through environment variables or a trusted host credential facility. Never put credentials in URLs or command arguments. Published examples must use reserved example domains and synthetic data. Public project attribution and upstream service URLs are not private deployment information.

## Release checks

Run from the repository root with Python 3.11 or newer:

```sh
python3 scripts/check_privacy.py --staged
python3 scripts/check_privacy.py --history
```

The scanner reads Git blobs, including force-added ignored files. Findings print only file paths, line numbers and rule identifiers, never matched values. The GitHub privacy workflow checks every reachable commit in its checkout on pushes and pull requests, with read-only permissions and no repository secrets. A shallow history is rejected for history scans.

An optional pre-commit hook checks the exact index before a local commit:

```sh
git config --local core.hooksPath .githooks
```

Inspect existing hooks before enabling it; do not overwrite an existing hook setup. It uses `python3` (or the `PYTHON` executable environment variable). Hook failures must be fixed, not bypassed. GitHub workflow failures must be reviewed before merging or publishing. Repository administrators should make the `privacy / scan` check required in branch protection/rulesets; committing this workflow alone does not configure or enforce that server-side requirement.

Automated checks detect selected token formats, credential literals, private endpoint patterns and prohibited paths. **They cannot prove the absence of all secrets or personal information.** Manually review every staged diff, especially names, free text, location data, screenshots and new examples. Do not add exceptions for real data. Test fixtures must be synthetic; split token-shaped fixtures so they cannot be mistaken for live credentials.

Publish only reviewed Git-tracked files or a clean checkout/archive. `.gitignore` does not protect arbitrary directory copies, manual zip/tar packages or existing history. Do not bundle local output or backups.

The Kindle dynamic image mode uses a separate ignored `image-auth.local.conf` data file. Never put its Bearer in shell arguments, environment variables, examples, logs or PRs. USB/FAT storage cannot enforce POSIX private-file permissions: protect physical access and backups, use a narrowly scoped credential, and rotate it when exposed. Runtime snapshots live in a private `/tmp` directory; per-request curl configuration is created under `umask 077` on `/tmp`, not FAT. Normal cancellation removes it after terminating the exact curl child; SIGKILL or power loss can leave private artifacts. Do not collect runtime directories for public diagnostics. Dynamic requests retain TLS verification and never follow redirects with authentication.

## Reporting a vulnerability

Do not post credentials, private schedules or exploitable details in a public issue. Use the repository's Security tab → Report a vulnerability if private reporting is enabled. If unavailable, open a public issue containing only a request for a private reporting channel; wait for a maintainer-provided private channel before sending details. This file does not enable GitHub private reporting automatically.

Reports should identify the affected revision, impact and safe reproduction using synthetic data. Never include real credentials or private deployments. The current default branch receives security fixes; there is no guaranteed response SLA or support for historical snapshots.

## If data was exposed

1. Revoke or rotate the credential/share immediately; deleting the file is not sufficient.
2. Restrict further access where possible, review access logs, and notify affected people privately.
3. Remove the material from source and artifacts. Coordinate any Git history rewrite with maintainers and collaborators; never force-push unilaterally.
4. Scan reachable history and release artifacts again. Contact the hosting provider about cached views where appropriate; forks and previous clones may retain copies.
5. Add a synthetic regression test without reproducing the exposed value.
