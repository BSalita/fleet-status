"""Turns one probe snapshot of a machine into what the page shows: CPU, memory, GPU, disks,
containers, and "jobs" (one per process session) with their logs, loss curve and ETA.

No I/O here: server.py runs the probes and calls summarize(); tests call the parts directly.
"""
import datetime as dt
import json
import math
import re
from collections import deque

DEFUNCT = re.compile(r'^\[.*\] <defunct>$')
MP_HELPER = re.compile(r'multiprocessing\.(spawn|resource_tracker|forkserver)|--multiprocessing-fork')
# The probe's own processes on a Linux host (python3 reading its script from stdin, logscan).
PROBE_SELF = re.compile(r'^python3? -$|import base64;exec\(base64\.b64decode|^ps -[eu]')

# Set by configure() from the config file.
JOB_EXE = SKIP_CMD = SERVICE_NOISE = HOST_JOB = None
prev_cpu = {}  # (machine, scope, pid) -> (cpu seconds, wall time)


def configure(cfg):
    global JOB_EXE, SKIP_CMD, SERVICE_NOISE, HOST_JOB
    JOB_EXE = re.compile(cfg['windows_job_exe'], re.I)
    SKIP_CMD = re.compile(cfg['skip_cmd'], re.I)
    SERVICE_NOISE = re.compile(cfg['service_noise'], re.I)
    HOST_JOB = re.compile(cfg['linux_job_cmd'])


def cpu_rate(key, seconds, t, lifetime=None):
    """Cores in use since the last poll (lifetime average on first sight)."""
    old = prev_cpu.get(key)
    prev_cpu[key] = (seconds, t)
    if old and t > old[1] and seconds >= old[0]:
        return (seconds - old[0]) / (t - old[1])
    return lifetime


def parse_size(s):
    m = re.match(r'([\d.]+)\s*([KMGT]?i?B)', s or '')
    if not m:
        return None
    mult = {'B': 1, 'KB': 1e3, 'MB': 1e6, 'GB': 1e9, 'TB': 1e12, 'KIB': 2**10, 'MIB': 2**20,
            'GIB': 2**30, 'TIB': 2**40}[m.group(2).upper()]
    return float(m.group(1)) * mult


def script_name(args):
    """The script a command runs (queue.sh, train.py), for matching it to a Claude session."""
    for tok in args.split():
        b = tok.rsplit('/', 1)[-1]
        if re.search(r'\.(sh|py)$', b):
            return b
    return None


def short_cmd(args):
    args = re.sub(r'^(/usr/bin/|/usr/local/bin/)?(python3?(\.\d+)?|bash|sh)\s+(-u\s+)?', '', args)
    return args[:160]


def parse_logs(text):
    """logscan.py output -> (container clock, {path: info}, {pid: script text})."""
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return None, {}, {}
    return d.get('now'), d.get('logs') or {}, d.get('scripts') or {}


# ---- ETA: from progress lines in the logs, the step's time limit, and the steps left in its script ----
prog_hist = {}  # (machine, container, log path) -> deque of (log mtime, done)
TIME_LIMIT = re.compile(r'--(hours|minutes|max-hours|max-minutes|time-limit-hours)[ =]([\d.]+)')
NOT_STEP = re.compile(r'^(echo|sleep|cd|export|done|fi|wait|while|until|if|then|else|elif|for|do|touch|mkdir|rm|cp|mv|ls|'
                      r'cat|date|true|false|set|trap|test|\[|\[\[|:|pgrep|tail|head|nvidia-smi|printf|exit|source|\.|\]|return|'
                      r'continue|break|local|shift|\{|\}|\w+\(\)|)$')


def sampled_rate(key, mtime, done):
    """Items a second from the progress seen in the log over the last 30 minutes of polls."""
    h = prog_hist.setdefault(key, deque(maxlen=200))
    if not h or h[-1] != (mtime, done):
        if h and done < h[-1][1]:
            h.clear()  # the step started again
        h.append((mtime, done))
    while len(h) > 2 and mtime - h[0][0] > 1800:
        h.popleft()
    t0, d0 = h[0]
    if mtime - t0 >= 60 and done > d0:
        return (done - d0) / (mtime - t0)
    return None


def log_eta(key, info, clock, elapsed):
    """Seconds left for the step writing this log (from its latest progress line), or None."""
    p = info.get('prog')
    if not p:
        return None
    age = max(0, clock - info['mtime']) if clock else 0
    out = {'line': p.get('line')}
    if p.get('total'):
        out['pct'] = round(100 * p['done'] / p['total'], 1)
    if p.get('left') is not None:
        out.update(left=max(0, p['left'] - age), how='the log says', due=p['left'] - age <= 0)
        return out
    if not p.get('total'):
        return None
    rest = p['total'] - p['done']
    if rest <= 0:
        out.update(left=0, how='at 100 %')
        return out
    rate = sampled_rate(key, info['mtime'], p['done'])
    how = 'its rate over the last polls'
    if not rate and p.get('rate'):
        rate, how = p['rate'], 'the rate its log prints'
    if rate:
        out.update(left=rest / rate - age, how=how)
    elif p['done'] > 0 and elapsed:
        out.update(left=elapsed * rest / p['done'], how='its pace since the step started')
    else:
        return None
    if out['left'] <= 0:  # should have moved on by now: the log is behind or the step slowed
        out.update(left=0, due=True)
    return out


def limit_eta(args, elapsed):
    """A step given a time budget (--hours 6) ends when the budget is spent."""
    m = TIME_LIMIT.search(args or '')
    if not m:
        return None
    total = float(m.group(2)) * (60 if 'minutes' in m.group(1) else 3600)
    return {'left': max(0, total - (elapsed or 0)), 'how': f'its time limit --{m.group(1)} {m.group(2)}'}


def script_lines(text):
    text = re.sub(r'\\\n\s*', ' ', text or '')
    out = []
    for ln in text.split('\n'):
        for part in re.split(r'\s*(?:;|&&|\|\|)\s*', ln.strip()):
            part = re.sub(r'^(do|then|else)\s+', '', part.rstrip('&').strip())
            if part and not part.startswith('#'):
                out.append(part)
    return out


def first_word(line):
    if re.match(r'^[A-Za-z_]\w*=["\'(]', line):
        return ''  # a variable set to a quoted command: not run here
    words = [w for w in line.split() if not re.match(r'^[A-Za-z_]\w*=', w)]
    while words and words[0] in ('nohup', 'nice', 'setsid', 'time', 'exec', '-n') or (words and re.fullmatch(r'-?\d+', words[0])):
        words = words[1:]
    return words[0] if words else ''


def steps_after(text, steps):
    """What the job's script still has to run after the steps running now: items left in a
    "for x in a b c" loop around them, and later commands (seconds known for those with a time limit)."""
    if not text or not steps:
        return None
    if all(st['cmd'].startswith('sleep') for st in steps):  # waiting before its first step
        m = re.search(r'\bfor\s+(\w+)\s+in\s+([^;\n]+?)\s*(?:;|\n)\s*do\b', text)
        n = len(m.group(2).split()) if m else len([ln for ln in script_lines(text) if not NOT_STEP.match(first_word(ln))])
        return {'loop_left': 0, 'later_unknown': n, 'later_known': 0, 'waiting': True}
    toks = [set(t for t in re.split(r'[\s=/]+', st.get('full') or st['cmd']) if len(t) > 3 and not t.startswith('-')) for st in steps]
    m = re.search(r'\bfor\s+(\w+)\s+in\s+([^;\n]+?)\s*(?:;|\n)\s*do\b', text)
    if m:
        items = m.group(2).split()
        cur = [i for i, it in enumerate(items) for st in steps
               if re.search(r'(?<![\w-])' + re.escape(it) + r'(?![\w-])', st.get('full') or st['cmd'])]
        if cur:
            return {'loop_left': len(items) - 1 - max(cur), 'loop_var': m.group(1), 'later_unknown': 0, 'later_known': 0}
    lines = [ln for ln in script_lines(text) if not NOT_STEP.match(first_word(ln))]
    best = -1
    for i, ln in enumerate(lines):
        lt = set(t for t in re.split(r'[\s=/]+', ln) if len(t) > 3)
        if any(tk and (len(tk & lt) >= 3 or tk <= lt) for tk in toks):
            best = i
    if best < 0:
        return None
    known = unknown = 0
    for ln in lines[best + 1:]:
        lim = limit_eta(ln, 0)
        if lim:
            known += lim['left']
        else:
            unknown += 1
    return {'loop_left': 0, 'later_unknown': unknown, 'later_known': known}


def job_eta(steps, step_etas, after):
    """The job's ETA: its slowest running step, plus what its script still has to run."""
    lefts = [e['left'] for e in step_etas if e and e.get('left') is not None]
    if not lefts:
        if after and (after['loop_left'] or after['later_unknown'] or after['later_known']):
            return {'left': None, 'more': after['loop_left'] + after['later_unknown'], 'waiting': after.get('waiting', False),
                    'how': 'waiting for its start condition' if after.get('waiting') else 'no progress in the logs'}
        return None
    left = max(lefts)
    first = next(e for e in step_etas if e and e.get('left') == left)
    eta = {'left': left, 'more': 0, 'how': first['how'], 'due': bool(first.get('due')) and left == 0}
    if any(not e and not st['cmd'].startswith('sleep') for st, e in zip(steps, step_etas)):
        eta['partial'] = True  # a step running beside it shows no progress: it may take longer
    if after:
        if after['loop_left']:
            per = max(st['elapsed'] + (e['left'] if e and e.get('left') is not None else 0)
                      for st, e in zip(steps, step_etas))
            eta['left'] += after['loop_left'] * per
            eta['how'] += f"; then {after['loop_left']} more ${after['loop_var']} as long as this one"
        if after['later_known']:
            eta['left'] += after['later_known']
            eta['how'] += '; plus the time limits of later steps'
        eta['more'] = after['later_unknown']
    return eta


def loss_values(raw):
    vals = []
    for v in raw or []:
        try:
            x = float(v)
        except ValueError:
            continue
        vals.append(x if math.isfinite(x) else None)
    return vals


def attach_logs(job, members, focus, logs, clock):
    """Logs of one job: those any member writes; shown ones are the leader's and its steps'."""
    mine = [(p, v) for p, v in logs.items() if members & set(v.get('pids') or [])]
    if not mine or clock is None:
        return
    for _, v in mine:
        v['age'] = max(0, clock - v['mtime'])
    job['log_age'] = round(min(v['age'] for _, v in mine))
    shown = [(p, v) for p, v in mine if focus & set(v.get('pids') or [])] or [(p, v) for p, v in mine if v.get('std')]
    shown.sort(key=lambda pv: (not pv[1].get('std'), pv[1]['age']))
    job['logs'] = [{'path': p, 'age': round(v['age']), 'last': v.get('last') or '', 'size': v.get('size')}
                   for p, v in shown[:2]]
    with_loss = [v for _, v in mine if v.get('loss')]
    if with_loss:
        best = max(with_loss, key=lambda v: (len(v['loss']), -v['age']))
        vals = loss_values(best['loss'])
        job['loss'] = {'key': best.get('loss_key'), 'values': vals,
                       'bad': bool(vals) and vals[-1] is None,
                       'flat': len(vals) >= 5 and None not in vals[-5:] and len(set(vals[-5:])) == 1}


def container_jobs(machine, cname, ctr_cmd, ps_text, t, logs_text=None, host=False):
    """Jobs inside one container (or, with host=True, a Linux host's own processes): one per
    process session, as `nohup setsid` starts them. ps_text: `ps -eo sid=,pid=,ppid=,etimes=,cputimes=,rss=,args=`."""
    clock, logs, scripts = parse_logs(logs_text)
    rows = []
    for line in (ps_text or '').splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            sid, pid, ppid, et, cpu, rss = (int(x) for x in parts[:6])
        except ValueError:
            continue
        args = parts[6]
        if args.startswith('ps -eo') or DEFUNCT.match(args) or SERVICE_NOISE.search(args):
            continue
        rows.append(dict(sid=sid, pid=pid, ppid=ppid, et=et, cpu=cpu, rss=rss * 1024, args=args))
    by_sid = {}
    for r in rows:
        r['rate'] = cpu_rate((machine, cname, r['pid']), r['cpu'], t, r['cpu'] / r['et'] if r['et'] else 0)
        by_sid.setdefault(r['sid'], []).append(r)
    jobs = []
    service = 'sleep infinity' not in (ctr_cmd or '')
    for sid, members in by_sid.items():
        if sid == 0:
            continue
        if host:
            # On a host, only sessions that run real work, and never the probe itself.
            if any(PROBE_SELF.search(m['args']) for m in members) or not any(HOST_JOB.search(m['args']) for m in members):
                continue
        elif sid == 1 and (service or len(members) == 1):
            continue
        pids = {m['pid'] for m in members}
        leader = next((m for m in members if m['pid'] == sid), None) or min(members, key=lambda m: -m['et'])
        if len(members) == 1 and re.fullmatch(r'(-?bash|sh|sleep \S+)', leader['args'].strip()):
            continue
        children = [m for m in members if m['ppid'] == leader['pid'] and not MP_HELPER.search(m['args'])]
        workers = [m for m in members if MP_HELPER.search(m['args'])]
        kids = {}
        for m_ in members:
            kids.setdefault(m_['ppid'], []).append(m_['pid'])

        def subtree(pid):
            out, todo = set(), [pid]
            while todo:
                x = todo.pop()
                if x not in out:
                    out.add(x)
                    todo += kids.get(x, [])
            return out
        shown = sorted(children, key=lambda m: m['et'], reverse=True)[:4]
        steps, step_etas = [], []
        for c in shown or [leader]:
            st = {'cmd': short_cmd(c['args']), 'elapsed': c['et'], 'full': c['args']}
            tree = subtree(c['pid'])
            cands = [e for e in (log_eta((machine, cname, path), info, clock, c['et'])
                                 for path, info in logs.items() if tree & set(info.get('pids') or [])) if e]
            e = max(cands, key=lambda x: x.get('left', -1)) if cands else limit_eta(c['args'], c['et'])
            if e:
                st['eta'] = e
            step_etas.append(e)
            steps.append(st)
        eta_steps = steps
        if not shown:
            steps = []  # the leader is the work itself: no step lines
        cores = sum(m['rate'] or 0 for m in members)
        job = {
            'where': cname, 'cmd': short_cmd(leader['args']), 'elapsed': leader['et'],
            'procs': len(members), 'workers': len(workers), 'cores': round(cores, 2),
            'rss': sum(m['rss'] for m in members), 'steps': steps,
            'queue': bool(re.search(r'queue', leader['args'], re.I)),
            'sleeping': any(st['cmd'].startswith('sleep') for st in steps),
            'script': script_name(leader['args']),
        }
        attach_logs(job, pids, {leader['pid']} | {c['pid'] for c in children}, logs, clock)
        after = steps_after(scripts.get(str(leader['pid'])), steps) if steps else None
        eta = job_eta(eta_steps, step_etas, after)
        if eta:
            job['eta'] = eta
        for st in steps:
            st.pop('full', None)
        jobs.append(job)
    return jobs


def windows_jobs(machine, procs, t, probe_time):
    by_pid = {p['pid']: p for p in procs}
    shown = {}
    for p in procs:
        if not JOB_EXE.match(p.get('name') or ''):
            continue
        cmd = p.get('cmd') or p['name']
        if SKIP_CMD.search(cmd):
            continue
        shown[p['pid']] = p
    jobs = {}
    for pid, p in shown.items():
        root = p
        seen = set()
        while root['ppid'] in shown and root['ppid'] not in seen:
            seen.add(root['ppid'])
            root = shown[root['ppid']]
        secs = p['t'] / 1e7
        created = p.get('created')
        age = None
        if created:
            try:
                age = max(0, (dt.datetime.fromisoformat(probe_time[:26]) -
                              dt.datetime.fromisoformat(created[:26])).total_seconds())
            except ValueError:
                age = None
        rate = cpu_rate((machine, 'win', pid), secs, t, secs / age if age else 0) or 0
        j = jobs.setdefault(root['pid'], {
            'where': 'Windows', 'cmd': (root.get('cmd') or root['name'])[:200], 'elapsed': None,
            'procs': 0, 'workers': 0, 'cores': 0.0, 'rss': 0, 'steps': [], 'queue': False})
        j['procs'] += 1
        j['cores'] = round(j['cores'] + rate, 2)
        j['rss'] += p.get('ws') or 0
        if p is root:
            j['elapsed'] = int(age) if age is not None else None
    # Drop idle helpers: a lone python with no CPU that has run under a minute is likely a tool call.
    out = [j for j in jobs.values() if not (j['cores'] < 0.01 and (j['elapsed'] or 0) < 60)]
    for j in out:
        j['cmd'] = re.sub(r'^"?[A-Za-z]:\\[^"]*\\(python[w]?\.exe)"?\s*', 'python ', j['cmd'])
    return out


def summarize(name, d, t):
    s = {'cpus': d.get('logical_cpus'), 'cpu_pct': d.get('cpu_pct')}
    tot, free = d.get('mem_total_kb') or 0, d.get('mem_free_kb') or 0
    s['mem_total'] = tot * 1024
    s['mem_used'] = (tot - free) * 1024
    s['mem_pct'] = round(100 * (tot - free) / tot, 1) if tot else None
    gpus = []
    for line in d.get('gpu') or []:
        f = [x.strip() for x in line.split(',')]
        if len(f) >= 4:
            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            gpus.append({'name': f[0].replace('NVIDIA GeForce ', ''), 'util': num(f[1]),
                         'mem_used': num(f[2]), 'mem_total': num(f[3]),
                         'temp': num(f[4]) if len(f) > 4 else None,
                         'power': num(f[5]) if len(f) > 5 else None})
    s['gpus'] = gpus
    s['disks'] = d.get('disks') or []
    try:
        boot = dt.datetime.fromisoformat(d['boot'][:26])
        s['uptime'] = (dt.datetime.fromisoformat(d['time'][:26]) - boot).total_seconds()
    except (KeyError, ValueError):
        s['uptime'] = None
    s['os'] = d.get('os') or 'windows'
    desk = None
    if s['os'] == 'windows':  # is the probe's user signed in on the desktop? (quser)
        user = (d.get('user') or '').lower()
        desk = 'none'
        for line in (d.get('sessions') or '').splitlines()[1:]:
            if user and user in line.lower():
                desk = 'active' if ' Active ' in line else 'disconnected'
    s['desktop'] = desk
    stats = {}
    for line in d.get('stats') or []:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        used = (o.get('MemUsage') or '').split('/')
        stats[o.get('Name') or o.get('name')] = {'cpu': float((o.get('CPUPerc') or '0').rstrip('%') or 0),
                            'mem': parse_size(used[0].strip()) if used else None,
                            'mem_limit': parse_size(used[1].strip()) if len(used) > 1 else None}
    containers, jobs = [], []
    for line in d.get('containers') or []:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        cname = o.get('Names')
        if isinstance(cname, list):  # podman
            cname = cname[0] if cname else None
        st = stats.get(cname, {})
        containers.append({'name': cname, 'state': str(o.get('State') or '').lower(), 'status': o.get('Status'),
                           'image': o.get('Image'), 'cpu': st.get('cpu'), 'mem': st.get('mem'),
                           'mem_limit': st.get('mem_limit'),
                           'service': 'sleep infinity' not in (o.get('Command') or '')})
        if str(o.get('State')).lower() == 'running':
            jobs += container_jobs(name, cname, o.get('Command'), (d.get('ps') or {}).get(cname), t,
                                   (d.get('logs') or {}).get(cname))
    containers.sort(key=lambda c: (c['state'] != 'running', c['name']))
    jobs += windows_jobs(name, d.get('procs') or [], t, d.get('time') or '')
    if d.get('hostps'):  # a Linux host's own jobs, outside containers
        jobs += container_jobs(name, 'host', None, d['hostps'], t, d.get('hostlogs'), host=True)
    jobs.sort(key=lambda j: -j['cores'])
    s['containers'] = containers
    s['jobs'] = jobs
    return s
