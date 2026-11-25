"""The optional panels, each driven by the config file: queued work, experiment results, counters,
public URLs, Windows scheduled tasks, and which Claude Code sessions reach which machine."""
import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'LogLevel=ERROR']


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def get_path(o, path):
    """o['a']['b'] for path 'a.b'; None when any part is missing."""
    if not path:
        return None
    for k in str(path).split('.'):
        o = o.get(k) if isinstance(o, dict) else None
    return o


def parse_time(s):
    """Seconds since the epoch from an ISO time or 'YYYY-MM-DD HH:MM:SS' (local), else None."""
    if isinstance(s, (int, float)):
        return float(s)
    try:
        d = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d.timestamp()


def median(xs):
    xs = sorted(x for x in xs if x and x > 0)
    return xs[len(xs) // 2] if xs else None


def expand(path):
    return os.path.expandvars(os.path.expanduser(path))


# ---- Experiment results: one JSON file per finished (or running) experiment ----
def experiments(cfg):
    """Rows for the experiments chart and table; see "experiments" in config.example.json."""
    if not cfg:
        return []
    f = cfg.get('fields') or {}
    out = []
    for path in glob.glob(expand(cfg['glob'])):
        r = read_json(path)
        if not isinstance(r, dict):
            continue
        row = {k: get_path(r, f.get(k, k)) for k in ('name', 'title', 'machine', 'started', 'ended', 'state', 'verdict')}
        row['name'] = row['name'] or os.path.basename(os.path.dirname(path))
        row['verdict'] = str(row['verdict'] or '')[:200]
        row['values'] = {}
        for s in cfg.get('series') or []:
            v = get_path(r, s['value'])
            if isinstance(v, (int, float)) and math.isfinite(v):
                ci = get_path(r, s.get('ci'))
                row['values'][s['key']] = {'v': v, 'ci': ci if isinstance(ci, (int, float)) else None,
                                           'verdict': get_path(r, s.get('verdict'))}
        out.append(row)
    out.sort(key=lambda e: str(e.get('ended') or e.get('started') or ''))
    return out


def experiments_meta(cfg):
    if not cfg:
        return None
    return {'title': cfg.get('title', 'Experiments'), 'subtitle': cfg.get('subtitle', ''),
            'unit': cfg.get('unit', ''), 'decimals': cfg.get('decimals', 3),
            'series': [{'key': s['key'], 'label': s.get('label', s['key'])} for s in cfg.get('series') or []],
            'markers': [{'t': parse_time(m['time']), 'label': m.get('label', '')} for m in cfg.get('markers') or []
                        if parse_time(m.get('time'))],
            'better': cfg.get('better_verdict', 'better')}


# ---- Queued work: a folder of JSON files per queue ----
def queue_items(queues, exps):
    """Items waiting in each configured queue, with an ETA from the median length of past experiments
    (when the queue says "duration_from": "experiments") or a fixed "duration_minutes"."""
    exp_each = median((parse_time(e.get('ended')) or 0) - (parse_time(e.get('started')) or 0)
                      for e in exps if e.get('ended') and e.get('started'))
    items = []
    for q in queues or []:
        f = q.get('fields') or {}
        each = q['duration_minutes'] * 60 if q.get('duration_minutes') else exp_each if q.get('duration_from') == 'experiments' else None
        parallel = max(1, int(q.get('parallel', 1)))
        for state, key in (('queued', 'glob'), ('held', 'held_glob')):
            if not q.get(key):
                continue
            for i, path in enumerate(sorted(glob.glob(expand(q[key])))):
                o = read_json(path, {}) if path.endswith('.json') else {}
                o = o if isinstance(o, dict) else {}
                after = get_path(o, f.get('after', 'after'))
                it = {'kind': q.get('label', 'Queued'), 'name': str(get_path(o, f.get('name', 'name')) or os.path.splitext(os.path.basename(path))[0]),
                      'detail': str(get_path(o, f.get('detail', 'title')) or ''),
                      'machine': str(get_path(o, f.get('machine', 'machine')) or q.get('machine') or 'next free'),
                      'waits_for': ', '.join(after) if isinstance(after, list) else str(after or ('held' if state == 'held' else '')),
                      'state': str(get_path(o, f.get('state', 'state')) or state), 'pos': i + 1 if state == 'queued' else None,
                      'since': os.path.getmtime(path)}
                if each:
                    it['eta_each'] = each
                    if state == 'queued':
                        it['eta_left'] = math.ceil((i + 1) / parallel) * each
                        it['eta_how'] = f'{each / 3600:.1f} h each, {parallel} at a time'
                    else:
                        it['eta_how'] = f'{each / 3600:.1f} h once started'
                items.append(it)
    return items


# ---- Counters: a number (and optional limit) read from a JSON file, e.g. an API budget ----
def counters(cfg):
    out = []
    for c in cfg or []:
        o = read_json(expand(c['file']), {}) or {}
        v = get_path(o, c.get('value'))
        lim = c.get('limit') if isinstance(c.get('limit'), (int, float)) else get_path(o, c.get('limit'))
        out.append({'label': c.get('label', c.get('value')), 'value': v, 'limit': lim,
                    'note': str(get_path(o, c.get('note')) or '') if c.get('note') else ''})
    return out


# ---- Public URLs ----
def cloudflared_hosts(cfg):
    """Host names of a cloudflared tunnel, read from its metrics server's /config (optionally over SSH)."""
    url = cfg.get('url', 'http://127.0.0.1:20241/config')
    cmd = SSH + [cfg['ssh'], f'curl -s -m 5 {url}'] if cfg.get('ssh') else ['curl', '-s', '-m', '5', url]
    p = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    c = json.loads(p.stdout.decode('utf-8', 'replace'))
    hosts = []
    for rule in (c.get('config') or {}).get('ingress') or []:
        h = rule.get('hostname')
        if h and '*' not in h and h not in hosts:
            hosts.append(h)
    return [f'https://{h}/' for h in hosts]


def check_url(url):
    t0 = time.time()
    req = urllib.request.Request(url, headers={'User-Agent': 'fleet-status'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001 - shown on the page
        return {'url': url, 'code': None, 'ms': None, 'error': str(e)[:120]}
    return {'url': url, 'code': code, 'ms': int((time.time() - t0) * 1000), 'error': None}


# ---- Windows scheduled tasks ----
TASK_PS = r'''
$ErrorActionPreference='SilentlyContinue'
Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\' -and $_.State -ne 'Disabled' -and $_.Author -notmatch 'Microsoft' -and $_.TaskName -notmatch '__EXCLUDE__' } | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  [pscustomobject]@{ name=$_.TaskName; state=[string]$_.State; next=$(if($i.NextRunTime){$i.NextRunTime.ToString('o')}else{$null}); last=$(if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000){$i.LastRunTime.ToString('o')}else{$null}); result=$i.LastTaskResult }
} | ConvertTo-Json -Compress

'''


def scheduled_tasks(ssh, exclude):
    """Root-folder scheduled tasks not by Microsoft and not matching exclude (a regex)."""
    cmd = ['powershell', '-NoProfile', '-Command', '-'] if ssh is None else SSH + [ssh, 'powershell -NoProfile -Command -']
    script = TASK_PS.replace('__EXCLUDE__', exclude.replace("'", "''"))
    p = subprocess.run(cmd, input=script.encode(), capture_output=True, timeout=60, creationflags=NO_WINDOW)
    text = p.stdout.decode('utf-8', 'replace').strip()
    if not text:
        return []
    v = json.loads(text[text.find('['):] if text.lstrip().startswith('[') else text[text.find('{'):])
    return v if isinstance(v, list) else [v]


# ---- Claude Code sessions: which machines each session's shell commands reached ----
EXEC_RE = re.compile(r'\bexec\s+(?:-\w+\s+)*([A-Za-z][\w.-]*)')
LOCAL_WORK = re.compile(r'wslc|docker|podman|nvidia-smi|python|\.bat\b|Start-Process', re.I)
TAIL_BYTES = 3 * 2**20


class ClaudeSessions:
    """Reads Claude Code transcripts (~/.claude/projects/*/*.jsonl) changed in the last day. A command
    counts for a machine when it names the machine's name or SSH host; a command naming no machine counts
    for the local one when it looks like work (python, a container runtime, nvidia-smi...)."""

    def __init__(self, machines, projects, window_hours=24):
        self.projects = expand(projects)
        self.window = window_hours * 3600
        self.local = next((m['name'] for m in machines if not m.get('ssh')), None)
        self.host_res = []
        for m in machines:
            names = {m['name']} | ({m['ssh']} if m.get('ssh') else set())
            rx = '|'.join(r'(?<![\w-])' + re.escape(n) + r'(?![\w-])' for n in sorted(names, key=len, reverse=True))
            self.host_res.append((m['name'], re.compile(rx, re.I)))
        self.cache = {}

    def cmd_machines(self, cmd):
        hit = [name for name, rx in self.host_res if rx.search(cmd)]
        if hit:
            return hit
        return [self.local] if self.local and LOCAL_WORK.search(cmd) else []

    def parse(self, path):
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            lines = f.read().decode('utf-8', 'replace').splitlines()
        title = agent = prompt = None
        cmds = []
        for line in lines:
            if '"custom-title"' in line or '"agent-name"' in line or '"last-prompt"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                title = o.get('customTitle') or title
                agent = o.get('agentName') or agent
                prompt = o.get('lastPrompt') or prompt
                continue
            if '"tool_use"' not in line or '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = parse_time(o.get('timestamp'))
            for c in (o.get('message') or {}).get('content') or []:
                if isinstance(c, dict) and c.get('type') == 'tool_use' and c.get('name') in ('Bash', 'PowerShell'):
                    cmd = (c.get('input') or {}).get('command') or ''
                    ms = self.cmd_machines(cmd)
                    if ms and ts:
                        ctrs = EXEC_RE.findall(cmd) if re.search(r'wslc|docker|podman', cmd, re.I) else []
                        cmds.append((ts, cmd[:4000], ms, ctrs))
        short = (prompt or '')[:70] + ('…' if prompt and len(prompt) > 70 else '')
        return {'id': os.path.basename(path)[:-6], 'project': os.path.basename(os.path.dirname(path)),
                'title': title or agent or short or '(untitled)', 'cmds': cmds[-600:]}

    def read(self):
        cutoff = time.time() - self.window
        out, alive = [], set()
        for path in glob.glob(os.path.join(self.projects, '*', '*.jsonl')):
            alive.add(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            cached = self.cache.get(path)
            if not cached or cached[0] != mtime:
                try:
                    cached = (mtime, self.parse(path))
                except OSError:
                    continue
                self.cache[path] = cached
            s = dict(cached[1])
            s['mtime'] = mtime
            machines = {}
            for ts, _cmd, ms, ctrs in s['cmds']:
                for m in ms:
                    e = machines.setdefault(m, {'last': 0, 'containers': set(), 'commands': 0})
                    e['last'] = max(e['last'], ts)
                    e['commands'] += 1
                    e['containers'].update(ctrs)
            s['machines'] = {m: {'last': e['last'], 'commands': e['commands'], 'containers': sorted(e['containers'])}
                             for m, e in machines.items()}
            out.append(s)
        for path in list(self.cache):
            if path not in alive:
                self.cache.pop(path, None)
        out.sort(key=lambda s: -s['mtime'])
        return out


def started_by(machine, job, sessions):
    """The session whose latest command on this machine names the job's script (and container)."""
    script = job.get('script')
    if not script:
        return None
    ctr = job['where'] if job['where'] not in ('Windows', 'host') else None
    best = None
    for s in sessions:
        for ts, cmd, ms, _ in reversed(s['cmds']):
            if machine in ms and script in cmd and (ctr is None or ctr in cmd):
                if not best or ts > best[0]:
                    best = (ts, s)
                break
    return {'title': best[1]['title'], 'id': best[1]['id'], 'at': best[0]} if best else None
