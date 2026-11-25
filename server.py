"""fleet-status: one live page with every machine you run jobs on.

Polls each machine in config.json every 15 s (probe.ps1 on Windows, probe_linux.py on Linux; locally or
over SSH), keeps 30 minutes of history, reads the optional panels the config names, and serves the page
on http://127.0.0.1:8540/. Read-only: it starts, stops and changes nothing on any machine.

    python server.py [--config config.json] [--port 8540] [--bind 127.0.0.1]
    python server.py --demo        # made-up machines and jobs, no config needed
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jobs
import plugins

HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

DEFAULTS = {
    'title': 'Fleet status',
    'bind': '127.0.0.1',
    'port': 8540,
    'period_seconds': 15,
    'history_minutes': 30,
    'stale_log_minutes': 30,
    'low_disk_gb': 20,
    'machines': [],
    'windows_job_exe': r'^(python|pythonw|py|cargo|rustc|cl|link|msbuild|robocopy|java|node)\.exe$',
    'skip_cmd': r'fleet-status|claude|\.vscode|jupyter|language_server|pylance|ms-python|WindowsApps',
    'linux_job_cmd': r'python|torchrun|accelerate|deepspeed|cargo|make|java|node|Rscript|julia|\.sh\b',
    'service_noise': r'chrome|chromium|crashpad|playwright|streamlit|Xvfb|x11vnc|websockify|novnc',
    'queues': [],
    'experiments': None,
    'counters': [],
    'urls': [],
    'cloudflared': None,
    'scheduled_tasks': {'enabled': True,
                        'exclude': r'^(Microsoft|Google|Adobe|MicrosoftEdge|OneDrive|NvTm|NVIDIA|Intel|Lenovo|ASUS|'
                                   r'Armoury|npcap|User_Feed|MiniTool|CCleaner|Opera|Brave|Zoom|Dropbox)'},
    'claude_sessions': {'enabled': True, 'projects': '~/.claude/projects', 'window_hours': 24, 'recent_hours': 6,
                        'active_minutes': 15},
}
MACHINE_DEFAULTS = {'ssh': None, 'os': 'windows', 'role': 'worker', 'runtime': 'none', 'wslc_session': ''}

CFG = {}
lock = threading.Lock()
state = {}
ever_ok = set()
extras = {'queue': [], 'experiments': [], 'counters': [], 'public': [], 'public_checked': None, 'tasks': [],
          'sessions': []}
sessions_reader = None


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    with open(path, encoding='utf-8') as f:
        user = json.load(f)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    machines = []
    for m in cfg['machines']:
        m = {**MACHINE_DEFAULTS, **m}
        if m['os'] not in ('windows', 'linux'):
            raise SystemExit(f"machine {m['name']}: os must be windows or linux")
        if m['runtime'] not in ('none', 'wslc', 'docker', 'podman'):
            raise SystemExit(f"machine {m['name']}: runtime must be none, wslc, docker or podman")
        machines.append(m)
    cfg['machines'] = sorted(machines, key=lambda m: m['name'].lower())
    return cfg


def probe_text(m):
    """The probe script for this machine, with logscan.py and the runtime put in. Read again each
    time, so edits apply at the next poll without a restart."""
    name = 'probe.ps1' if m['os'] == 'windows' else 'probe_linux.py'
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        text = f.read()
    with open(os.path.join(HERE, 'logscan.py'), 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    text = (text.replace('__LOGSCAN_B64__', b64).replace('__RUNTIME__', m['runtime'])
            .replace('__WSLC_SESSION__', m.get('wslc_session') or ''))
    return (text + '\n\n').encode('utf-8')


def run_probe(m):
    if m['os'] == 'windows':
        remote = 'powershell -NoProfile -Command -'
        local = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', '-']
    else:
        remote = 'python3 -'
        local = [sys.executable if os.name != 'nt' else 'python3', '-']
    cmd = local if not m['ssh'] else plugins.SSH + [m['ssh'], remote]
    p = subprocess.run(cmd, input=probe_text(m), capture_output=True, timeout=90, creationflags=NO_WINDOW)
    text = p.stdout.decode('utf-8', 'replace').strip()
    start = text.find('{"host"')
    if start < 0:
        err = p.stderr.decode('utf-8', 'replace').strip() or text or f'exit {p.returncode}'
        raise RuntimeError(err.splitlines()[-1][:300])
    return json.loads(text[start:])


def poll_machine(m):
    period = CFG['period_seconds']
    while True:
        started = time.time()
        try:
            d = run_probe(m)
            s = jobs.summarize(m['name'], d, time.time())
            with lock:
                st = state[m['name']]
                st.update(s)
                st.update(reachable=True, error=None, updated=time.time(), probe_seconds=round(time.time() - started, 1))
                g = s['gpus'][0] if s['gpus'] else None
                st['history'].append({'t': time.time(), 'cpu': s['cpu_pct'], 'mem': s['mem_pct'],
                                      'gpu': g['util'] if g else None})
        except Exception as e:  # noqa: BLE001 - shown on the page
            with lock:
                st = state[m['name']]
                st.update(reachable=False, error=str(e)[:300], failed_at=time.time())
        time.sleep(max(1, period - (time.time() - started)))


def slow_loop():
    """Every minute: experiments, queues, counters, scheduled tasks, public URLs."""
    last_tunnel, tunnel_urls = 0, []
    while True:
        try:
            exps = plugins.experiments(CFG['experiments'])
            items = plugins.queue_items(CFG['queues'], exps)
            cnt = plugins.counters(CFG['counters'])
            with lock:
                extras.update(experiments=exps, queue=items, counters=cnt)
        except Exception as e:  # noqa: BLE001
            with lock:
                extras['plugin_error'] = str(e)[:200]
        tasks = []
        if CFG['scheduled_tasks'].get('enabled'):
            for m in CFG['machines']:
                if m['os'] != 'windows':
                    continue
                try:
                    for tk in plugins.scheduled_tasks(m['ssh'], CFG['scheduled_tasks']['exclude']):
                        tk['machine'] = m['name']
                        tasks.append(tk)
                except Exception:  # noqa: BLE001
                    pass
        if CFG['cloudflared'] and time.time() - last_tunnel > 600:
            try:
                tunnel_urls = plugins.cloudflared_hosts(CFG['cloudflared'])
                last_tunnel = time.time()
            except Exception:  # noqa: BLE001
                pass
        urls = list(dict.fromkeys(list(CFG['urls']) + tunnel_urls))
        results, threads = [], []
        for u in urls:
            th = threading.Thread(target=lambda u=u: results.append(plugins.check_url(u)), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(15)
        for r in results:
            if r['code'] and r['code'] < 500:
                ever_ok.add(r['url'])
            r['ever_ok'] = r['url'] in ever_ok
        with lock:
            extras['tasks'] = sorted(tasks, key=lambda x: (x.get('next') or '9999', x['machine']))
            extras['public'] = sorted(results, key=lambda r: r['url'])
            extras['public_checked'] = time.time() if urls else None
        time.sleep(60)


def sessions_loop():
    while True:
        try:
            ss = sessions_reader.read()
            with lock:
                extras['sessions'] = ss
        except Exception:  # noqa: BLE001
            pass
        time.sleep(CFG['period_seconds'])


def snapshot():
    cs = CFG['claude_sessions']
    with lock:
        ss = extras['sessions']
        t = time.time()
        machines = []
        for m in CFG['machines']:
            st = dict(state[m['name']])
            st['history'] = list(st['history'])
            st['desktop_check'] = m['runtime'] == 'wslc'  # wslc build/run need the signed-in desktop
            st['jobs'] = [{**j, 'started_by': plugins.started_by(m['name'], j, ss)} for j in st.get('jobs') or []]
            st['sessions'] = sorted(
                ({'title': s['title'], 'id': s['id'], 'last': s['machines'][m['name']]['last'],
                  'commands': s['machines'][m['name']]['commands'], 'containers': s['machines'][m['name']]['containers'],
                  'active': t - s['mtime'] < cs['active_minutes'] * 60}
                 for s in ss if m['name'] in s['machines'] and t - s['machines'][m['name']]['last'] < cs['recent_hours'] * 3600),
                key=lambda x: -x['last'])
            machines.append(st)
        sessions = [{'title': s['title'], 'id': s['id'], 'project': s['project'], 'mtime': s['mtime'],
                     'active': t - s['mtime'] < cs['active_minutes'] * 60,
                     'machines': s['machines']} for s in ss]
        return {'now': t, 'title': CFG['title'], 'period': CFG['period_seconds'], 'machines': machines,
                'queue': extras['queue'], 'counters': extras['counters'], 'tasks': extras['tasks'],
                'public': extras['public'], 'public_checked': extras['public_checked'],
                'sessions': sessions if cs.get('enabled') else None,
                'experiments': extras['experiments'], 'experiments_meta': plugins.experiments_meta(CFG['experiments']),
                'stale_log': CFG['stale_log_minutes'] * 60, 'low_disk': CFG['low_disk_gb'] * 2**30,
                'show_tasks': CFG['scheduled_tasks'].get('enabled') and any(m['os'] == 'windows' for m in CFG['machines'])}


class Handler(BaseHTTPRequestHandler):
    snap = staticmethod(snapshot)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/api/status'):
            body = json.dumps(self.snap(), default=str).encode()
            ctype = 'application/json'
        elif self.path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                body = f.read()
            ctype = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global CFG, sessions_reader
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--port', type=int)
    ap.add_argument('--bind')
    ap.add_argument('--demo', action='store_true', help='serve made-up data; no config or machines needed')
    a = ap.parse_args()
    if a.demo:
        import demo
        CFG = json.loads(json.dumps(DEFAULTS))
        Handler.snap = staticmethod(demo.snapshot)
    else:
        if not os.path.exists(a.config):
            raise SystemExit(f'No config at {a.config}. Copy config.example.json to config.json and edit it, '
                             'or run with --demo.')
        CFG = load_config(a.config)
        jobs.configure(CFG)
        hist = max(2, CFG['history_minutes'] * 60 // CFG['period_seconds'])
        for m in CFG['machines']:
            state[m['name']] = {'name': m['name'], 'role': m['role'], 'os': m['os'], 'reachable': None, 'error': None,
                                'updated': None, 'history': deque(maxlen=hist)}
            threading.Thread(target=poll_machine, args=(m,), daemon=True).start()
        threading.Thread(target=slow_loop, daemon=True).start()
        sessions_reader = plugins.ClaudeSessions(CFG['machines'], CFG['claude_sessions']['projects'],
                                                 CFG['claude_sessions']['window_hours'])
        if CFG['claude_sessions'].get('enabled'):
            threading.Thread(target=sessions_loop, daemon=True).start()
    bind, port = a.bind or CFG['bind'], a.port or CFG['port']
    print(f'fleet-status on http://{bind}:{port}/' + (' (demo data)' if a.demo else ''), flush=True)
    # Windows lets a second server bind the same port when SO_REUSEADDR is on; keep it off.
    ThreadingHTTPServer.allow_reuse_address = False
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == '__main__':
    main()
