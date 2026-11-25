"""Runs inside a container (python3, standard library): the log files running processes write.

Prints one JSON line: {now, logs: {path: {pids, mtime, size, last, loss_key, loss, prog}}, scripts}.
prog is the latest progress line of the log ("350/400", "1,094 of 2,000", "[25/40]", "0.2 h left",
"7.3 s a sample", "1,202 a second"); scripts holds the text of each running shell script, so the
server can count the steps still to come. A log is a regular
file on fd 1 or 2, or any file open for writing whose name ends in .log/.txt/.out/.err/.csv/.jsonl.
The server sends this file base64-encoded inside probe.ps1; it reads only.
"""
import json
import os
import re
import time

EXT = re.compile(r'\.(log|txt|out|err|csv|jsonl)$')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
NUM = r'([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|nan|inf|-inf)'
LOSS = re.compile(r'\b([A-Za-z_]*loss)\s*[=:]?\s*' + NUM + r'(?![\w.])', re.I)
MAX_FILES = 60
INT = r'(\d{1,3}(?:,\d{3})+|\d+)'
FRAC = re.compile(r'(?<![\w.:/-])' + INT + r'\s*(?:/|\bof\b)\s*' + INT + r'(?![\w.:/-]|\s*[-/]\d)')
LEFT = re.compile(r'([\d.]+)\s*(h|hours?|min|minutes?|m|s|sec|seconds?)\s+(?:left|remaining|to go)\b', re.I)
CLOCK = r'(?:(\d+):)?(\d+):(\d\d)'
ETA = re.compile(r'\bETA[:=\s]+' + CLOCK + r'\b|<' + CLOCK + r'[,\]]', re.I)
PER_S = re.compile(r'([\d,]*\.?\d+)\s*(?:[A-Za-z]+\s+)?(?:a|per)\s+second\b|([\d,]*\.?\d+)\s*(?:it|items?|deals?|boards?|games?|rows?|samples?)/s\b', re.I)
PER_H = re.compile(r'([\d,]*\.?\d+)\s*(?:[A-Za-z]+\s+)?(?:an|per)\s+hour\b', re.I)
S_EACH = re.compile(r'([\d.]+)\s*s\s+(?:a|per)\s+(?!second\b)[A-Za-z]+', re.I)
SCRIPT_MAX = 16384
TODO = re.compile(r'\bto\s+(?:play|do|run|solve|bid|process|go|check|score)\b', re.I)


def num(x):
    return float(x.replace(',', ''))


def progress(lines):
    """The latest line with done/total or time left -> {done, total, left, rate, line}."""
    for ln in reversed(lines[-400:]):
        if ln.startswith('{') and '"step"' not in ln:
            continue
        p = {}
        m = LEFT.search(ln)
        if m:
            u = m.group(2).lower()
            p['left'] = num(m.group(1)) * (3600 if u.startswith('h') else 60 if u.startswith('m') else 1)
        m = ETA.search(ln)
        if m and 'left' not in p:
            h, mi, se = (m.group(1), m.group(2), m.group(3)) if m.group(2) else (m.group(4), m.group(5), m.group(6))
            p['left'] = int(h or 0) * 3600 + int(mi) * 60 + int(se)
        best = None
        header = TODO.search(ln)  # "300 of 300 files to process": the size of the job, not progress
        for m in ([] if header else FRAC.finditer(ln)):
            d, t = num(m.group(1)), num(m.group(2))
            if 0 <= d <= t and t >= 10:
                best = (d, t)
        if best:
            p['done'], p['total'] = best
        if not p:
            continue
        m = PER_S.search(ln)
        if m:
            p['rate'] = num(m.group(1) or m.group(2))
        else:
            m = PER_H.search(ln)
            if m:
                p['rate'] = num(m.group(1)) / 3600
            else:
                m = S_EACH.search(ln)
                if m and num(m.group(1)) > 0:
                    p['rate'] = 1 / num(m.group(1))
        p['line'] = ln[:200]
        return p
    return None


def scripts():
    """Text of every running "bash x.sh" / "bash -c ..." (the queue scripts), by pid."""
    out = {}
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % pid, 'rb') as f:
                argv = [a.decode('utf-8', 'replace') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) not in ('bash', 'sh', 'dash'):
            continue
        rest = argv[1:]
        if len(rest) >= 2 and rest[0] == '-c':
            out[pid] = rest[1][:SCRIPT_MAX]
            continue
        sh = next((a for a in rest if a.endswith('.sh')), None)
        if not sh:
            continue
        try:
            path = sh if sh.startswith('/') else os.path.join(os.readlink('/proc/%s/cwd' % pid), sh)
            with open(path, encoding='utf-8', errors='replace') as f:
                out[pid] = f.read(SCRIPT_MAX)
        except OSError:
            continue
    return out
TAIL = 65536

found = {}
std = set()  # paths some process has as stdout or stderr
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    base = '/proc/' + pid
    try:
        fds = os.listdir(base + '/fd')
    except OSError:
        continue
    for fd in fds:
        try:
            path = os.readlink(base + '/fd/' + fd)
        except OSError:
            continue
        if not path.startswith('/') or path.startswith(('/dev/', '/proc/', '/sys/')) or path.endswith(' (deleted)'):
            continue
        if fd not in ('1', '2'):
            if not EXT.search(path):
                continue
            try:
                with open(base + '/fdinfo/' + fd) as f:
                    flags = int(f.read().split('flags:')[1].split()[0], 8)
            except (OSError, IndexError, ValueError):
                continue
            if flags & 3 == 0:  # read-only
                continue
        found.setdefault(path, set()).add(int(pid))
        if fd in ('1', '2'):
            std.add(path)

out = {}
for path, pids in sorted(found.items(), key=lambda kv: (kv[0] not in std, -len(kv[1]))):
    if len(out) >= MAX_FILES:
        break
    try:
        st = os.stat(path)
    except OSError:
        continue
    if not os.path.isfile(path):
        continue
    last, key, losses, prog = '', None, [], None
    try:
        with open(path, 'rb') as f:
            if st.st_size > TAIL:
                f.seek(st.st_size - TAIL)
            text = f.read().decode('utf-8', 'replace')
        text = ANSI.sub('', text)
        lines = [ln.strip() for chunk in text.split('\n') for ln in chunk.split('\r')]
        lines = [ln for ln in lines if ln]
        if lines:
            last = lines[-1][:200]
            prog = progress(lines)
        seen = []
        for ln in lines:
            for m in LOSS.finditer(ln):
                seen.append((m.group(1).lower(), m.group(2).lower()))
        if seen:
            keys = [k for k, _ in seen]
            # The main loss: plain "loss" or "train_loss" when present, else the most frequent key.
            key = next((k for k in ('loss', 'train_loss') if k in keys), max(set(keys), key=keys.count))
            losses = [v for k, v in seen if k == key][-60:]
    except OSError:
        pass
    out[path] = {'pids': sorted(pids)[:400], 'std': path in std, 'mtime': st.st_mtime, 'size': st.st_size,
                 'last': last, 'loss_key': key, 'loss': losses, 'prog': prog}
print(json.dumps({'now': time.time(), 'logs': out, 'scripts': scripts()}))
