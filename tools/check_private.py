"""Refuse to publish private details: checks files (and optionally git history) for the strings in
your own blocklist, plus common secret patterns. The blocklist lives OUTSIDE the repo.

    python tools/check_private.py               # files git would commit (tracked + new, not ignored)
    python tools/check_private.py --staged      # what is staged (the pre-commit hook)
    python tools/check_private.py --history     # every commit's text, author and committer

Blocklist: $FLEET_STATUS_BLOCKLIST, else ~/.config/fleet-status/blocklist.txt. One regular expression
a line (case-insensitive); blank lines and lines starting with # are skipped. Put in your machine
names, host names, tailnet name, user name, e-mail, domain names, project names, home paths.
Exit status 1 when anything is found.
"""
import argparse
import os
import re
import subprocess
import sys

SECRETS = [
    ('GitHub token', r'\bgh[pousr]_[A-Za-z0-9]{30,}'),
    ('AWS access key', r'\bAKIA[0-9A-Z]{16}\b'),
    ('Slack token', r'\bxox[abprs]-[A-Za-z0-9-]{10,}'),
    ('private key', r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ('Anthropic/OpenAI key', r'\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}'),
    ('Google API key', r'\bAIza[0-9A-Za-z_-]{35}\b'),
    ('password assignment', r'(?i)\b(password|passwd|secret|api_key|token)\s*[:=]\s*["\'][^"\'\s]{6,}["\']'),
    ('Tailscale address', r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b'),
    ('Tailscale host name', r'\b[a-z0-9-]+\.tail[0-9a-f]{4,}\.ts\.net\b'),
]


def blocklist():
    path = os.environ.get('FLEET_STATUS_BLOCKLIST') or os.path.join(os.path.expanduser('~'), '.config', 'fleet-status', 'blocklist.txt')
    if not os.path.exists(path):
        sys.exit(f'No blocklist at {path}: create it (one regex a line) before checking.')
    with open(path, encoding='utf-8') as f:
        terms = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith('#')]
    return [(f'blocklist: {t}', t) for t in terms], path


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout


def scan(name, text, rules):
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, rx in rules:
            if re.search(rx, line, re.I if label.startswith('blocklist') else 0):
                hits.append(f'{name}:{i}: {label}: {line.strip()[:120]}')
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--staged', action='store_true')
    ap.add_argument('--history', action='store_true')
    a = ap.parse_args()
    bl, path = blocklist()
    rules = bl + SECRETS
    hits = []
    if a.history:
        text = git('log', '--all', '-p', '--format=commit %H%nauthor %an <%ae>%ncommitter %cn <%ce>%n%B')
        hits += scan('history', text, rules)
    else:
        files = git('diff', '--cached', '--name-only', '--diff-filter=ACMR').split() if a.staged else \
            git('ls-files', '--cached', '--others', '--exclude-standard').split()
        for f in files:
            hits += scan(f, f, rules)  # the file name itself
            try:
                if a.staged:
                    text = git('show', f':{f}')
                else:
                    with open(f, encoding='utf-8', errors='replace') as fh:
                        text = fh.read()
            except OSError:
                continue
            hits += scan(f, text, rules)
    if hits:
        print('\n'.join(hits))
        print(f'\n{len(hits)} private or secret-looking line(s) found (blocklist: {path}).', file=sys.stderr)
        sys.exit(1)
    print(f'clean ({len(bl)} blocklist terms, {len(SECRETS)} secret patterns)')


if __name__ == '__main__':
    main()
