"""Made-up data for `python server.py --demo`: four machines, a few jobs, a queue and experiment
results, moving a little on every refresh. Used for trying the page and for screenshots."""
import math
import random
import time

T0 = time.time()
GB = 2**30


def wave(t, period, lo, hi, phase=0.0):
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * (t / period + phase)))


def history(t, cpu, mem, gpu=None):
    out = []
    for i in range(120):
        ts = t - (119 - i) * 15
        out.append({'t': ts, 'cpu': round(wave(ts, 900, *cpu), 1), 'mem': round(wave(ts, 3600, *mem), 1),
                    'gpu': None if gpu is None else round(wave(ts, 600, *gpu, 0.3), 1)})
    return out


def job(where, cmd, elapsed, cores, rss_gb, steps=(), logs=(), eta=None, loss=None, procs=1, workers=0, script=None, queue=False):
    j = {'where': where, 'cmd': cmd, 'elapsed': int(elapsed), 'cores': cores, 'rss': int(rss_gb * GB), 'procs': procs,
         'workers': workers, 'steps': list(steps), 'queue': queue, 'sleeping': False, 'script': script, 'started_by': None}
    if logs:
        j['logs'] = list(logs)
        j['log_age'] = min(l['age'] for l in logs)
    if eta:
        j['eta'] = eta
    if loss:
        j['loss'] = loss
    return j


def machine(name, role, os_, cpus, mem_gb, gpu, t, cpu, mem, gpu_rng=None, disks=(), containers=(), jobs=(), desktop=None,
            sessions=(), reachable=True, error=None):
    h = history(t, cpu, mem, gpu_rng)
    last = h[-1]
    m = {'name': name, 'role': role, 'os': os_, 'reachable': reachable, 'error': error, 'updated': t - random.uniform(1, 9),
         'cpus': cpus, 'cpu_pct': last['cpu'], 'mem_total': mem_gb * GB, 'mem_used': int(mem_gb * GB * last['mem'] / 100),
         'mem_pct': last['mem'], 'uptime': 86400 * 3 + (t - T0), 'desktop': desktop, 'desktop_check': desktop is not None,
         'disks': [{'id': d, 'size': s * GB, 'free': f * GB} for d, s, f in disks], 'containers': list(containers),
         'jobs': list(jobs), 'sessions': list(sessions), 'history': h, 'probe_seconds': 4.2}
    m['gpus'] = [{'name': gpu, 'util': last['gpu'], 'mem_used': 11800, 'mem_total': 16303, 'temp': 67, 'power': 240}] if gpu else []
    return m


def snapshot():
    t = time.time()
    run = t - T0
    # A training run: 6 h budget, loss falling.
    train_el = 4 * 3600 + 1800 + run
    losses = [round(0.92 * math.exp(-i / 25) + 0.48 + random.uniform(-0.004, 0.004), 4) for i in range(60)]
    step = int(80000 + run * 2)
    # A batch of 2,000,000 items at ~1,200 a second.
    done = min(2_000_000, int(1_094_400 + run * 1200))
    left = (2_000_000 - done) / 1200
    # An evaluation over 3,000 boards, 8 s a board.
    ev = min(3000, int(2550 + run / 8))
    ev_left = (3000 - ev) * 8

    ctr = lambda n, st, cpu, mem, lim, service=False: {'name': n, 'state': st, 'status': 'Up 2 days' if st == 'running' else 'Exited (0) 3 hours ago',
                                                        'image': n, 'cpu': cpu, 'mem': mem * GB, 'mem_limit': lim * GB, 'service': service}
    gpu1 = machine(
        'gpu-box-1', 'dev', 'windows', 128, 512, 'RTX 5080', t, (35, 70), (40, 48), (88, 99),
        disks=[('C:', 200, 24), ('D:', 1700, 640)], desktop='active',
        containers=[ctr('trainer', 'running', 2400, 61, 384), ctr('api', 'running', 12, 6.6, 32, True)],
        jobs=[job('trainer', 'queue.sh', 6 * 3600 + 1380 + run, 24.1, 60.8, procs=29, workers=26, script='queue.sh', queue=True,
                  steps=[{'cmd': 'eval.py --model runs/a --boards boards_d.jsonl --n 3000', 'elapsed': 6 * 3600 + run,
                          'eta': {'pct': round(100 * ev / 3000, 1), 'left': ev_left, 'how': 'the rate its log prints'}},
                         {'cmd': 'eval.py --model runs/b --boards boards_d.jsonl --n 3000', 'elapsed': 6 * 3600 + run,
                          'eta': {'pct': 91.7, 'left': ev_left * 0.55, 'how': 'the rate its log prints'}}],
                  logs=[{'path': '/work/runs/d-a.log', 'age': 12, 'last': f'{ev}/3000  8.2 s a board wall'}],
                  eta={'left': ev_left, 'more': 1, 'how': 'the rate its log prints'}),
              job('Windows', 'python train.py --out D:\\runs\\r1 --n-train 4000000', 1700 + run, 27.5, 11.8, procs=2)],
        sessions=[{'title': 'Tune the evaluation', 'id': 'demo-0001', 'last': t - 300, 'commands': 41, 'containers': ['trainer'], 'active': True}])
    gpu2 = machine(
        'gpu-box-2', 'worker', 'linux', 128, 512, 'RTX 5080', t, (20, 40), (22, 26), (95, 100),
        disks=[('/', 900, 410), ('/data', 3700, 1900)],
        containers=[ctr('train', 'running', 300, 22, 384)],
        jobs=[job('train', 'run_retrain.sh', train_el + 3000, 3.1, 22.0, procs=3, script='run_retrain.sh', queue=False,
                  steps=[{'cmd': 'train.py --data ../data --run ../runs/r2 --hours 6 --d 512 --layers 8',
                          'elapsed': train_el, 'eta': {'left': max(0, 6 * 3600 - train_el), 'how': 'its time limit --hours 6'}}],
                  logs=[{'path': '/work/runs/r2.out', 'age': 20, 'last': f'step {step} loss {losses[-1]} lr 3.1e-05 1,978 games/s'}],
                  eta={'left': max(0, 6 * 3600 - train_el), 'more': 0, 'how': 'its time limit --hours 6'},
                  loss={'key': 'loss', 'values': losses, 'bad': False, 'flat': False})],
        sessions=[{'title': 'Retrain the r2 model', 'id': 'demo-0002', 'last': t - 2400, 'commands': 18, 'containers': ['train'], 'active': False}])
    cpu_a = machine(
        'cpu-node-a', 'worker', 'linux', 32, 192, None, t, (90, 100), (28, 34),
        disks=[('/', 1800, 806)],
        jobs=[job('host', 'run_all.sh', 3200 + run, 31.6, 1.0, procs=2, script='run_all.sh',
                  steps=[{'cmd': 'solve.py --problems in/part_02.npz --out out/part_02.npz --threads 32',
                          'elapsed': 1200 + run, 'eta': {'pct': round(100 * done / 2e6, 1), 'left': left, 'how': 'the log says'}}],
                  logs=[{'path': '/work/out/part_02.log', 'age': 30,
                         'last': f'{done:,} of 2,000,000 solved, {int(910 + run)} s, 1,202 a second, {left / 3600:.2f} h left'}],
                  eta={'left': left + 4 * 1700, 'more': 0, 'how': 'the log says; then 4 more $p as long as this one'})])
    web = machine(
        'web-1', 'prod', 'windows', 24, 64, None, t, (5, 15), (55, 60),
        disks=[('C:', 930, 14)], desktop='disconnected',
        containers=[ctr('site', 'running', 8, 3.1, 16, True), ctr('worker', 'exited', 0, 0, 8)],
        jobs=[job('Windows', 'python -m site_server --port 8530', 240000 + run, 0.1, 0.4)])
    machines = [cpu_a, gpu1, gpu2, web]

    exps = []
    for i in range(14):
        ended = t - (14 - i) * 9000 - 3600
        g = round(random.Random(i).gauss(0.01 + 0.004 * i, 0.03), 3)
        live = round(g * 0.6 + random.Random(i + 99).gauss(0, 0.02), 3) if i % 3 == 0 else None
        v = {'gate': {'v': g, 'ci': 0.035, 'verdict': 'better' if g > 0.035 else 'no difference'}}
        if live is not None:
            v['live'] = {'v': live, 'ci': 0.05, 'verdict': 'better' if live > 0.05 else 'no difference'}
        exps.append({'name': f'{i + 1:03d}-variant', 'title': f'Variant {i + 1}: learning rate x{1 + i % 4}', 'machine': 'gpu-box-2',
                     'started': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended - 3600)),
                     'ended': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended)), 'state': 'done',
                     'verdict': 'better' if g > 0.035 else 'no difference', 'values': v})
    queue = [{'kind': 'Experiment', 'name': f'{15 + k:03d}-variant', 'detail': f'Variant {15 + k}: wider network', 'machine': 'next free',
              'waits_for': '', 'state': 'queued', 'pos': k + 1, 'since': t - 1800 * (3 - k), 'eta_each': 3600,
              'eta_left': (k // 2 + 1) * 3600, 'eta_how': '1.0 h each, 2 at a time'} for k in range(3)]
    queue.append({'kind': 'Experiment', 'name': '018-variant', 'detail': 'Variant 18: on hold', 'machine': 'next free',
                  'waits_for': 'held', 'state': 'held', 'pos': None, 'since': t - 7200, 'eta_each': 3600, 'eta_how': '1.0 h once started'})
    return {
        'now': t, 'title': 'Fleet status (demo)', 'period': 15, 'machines': machines, 'queue': queue,
        'counters': [{'label': 'API requests today', 'value': 12345 + int(run), 'limit': 50000, 'note': ''}],
        'experiments': exps,
        'experiments_meta': {'title': 'Experiments', 'subtitle': 'each candidate against the current best, score with 95% interval',
                             'unit': 'score', 'decimals': 3, 'better': 'better',
                             'series': [{'key': 'gate', 'label': 'Offline gate'}, {'key': 'live', 'label': 'Live check'}],
                             'markers': [{'t': t - 5 * 9000, 'label': 'new baseline'}]},
        'sessions': [{'title': 'Tune the evaluation', 'id': 'demo-0001', 'project': 'demo', 'mtime': t - 300, 'active': True,
                      'machines': {'gpu-box-1': {'last': t - 300, 'commands': 41, 'containers': ['trainer']}}},
                     {'title': 'Retrain the r2 model', 'id': 'demo-0002', 'project': 'demo', 'mtime': t - 2400, 'active': False,
                      'machines': {'gpu-box-2': {'last': t - 2400, 'commands': 18, 'containers': ['train']}}}],
        'public': [{'url': 'https://www.example.com/', 'code': 200, 'ms': 120, 'error': None, 'ever_ok': True},
                   {'url': 'https://status.example.com/', 'code': 502, 'ms': 80, 'error': None, 'ever_ok': True}],
        'public_checked': t - 20,
        'tasks': [{'machine': 'gpu-box-1', 'name': 'NightlyBackup', 'state': 'Ready', 'next': time.strftime('%Y-%m-%dT02:00:00'),
                   'last': time.strftime('%Y-%m-%dT02:00:00'), 'result': 0},
                  {'machine': 'web-1', 'name': 'RenewCertificates', 'state': 'Ready', 'next': None,
                   'last': time.strftime('%Y-%m-%dT04:30:00'), 'result': 1}],
        'show_tasks': True, 'stale_log': 1800, 'low_disk': 20 * GB,
    }
