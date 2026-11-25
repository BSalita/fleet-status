"""Tests of the parts that need no machine: progress lines, script steps, jobs and their ETA.

    python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import jobs  # noqa: E402
import server  # noqa: E402

jobs.configure(server.DEFAULTS)

ns = {}
with open(os.path.join(os.path.dirname(HERE), 'logscan.py'), encoding='utf-8') as f:
    exec(f.read().split('found = {}')[0], ns)  # the helpers only, not the /proc scan
progress = ns['progress']


class Progress(unittest.TestCase):
    def test_of_with_time_left(self):
        p = progress(['1,094,400 of 2,000,000 solved, 910 s, 1,202 a second, 0.21 h left'])
        self.assertEqual((p['done'], p['total'], p['rate']), (1094400, 2000000, 1202))
        self.assertAlmostEqual(p['left'], 756)

    def test_brackets_and_rate_an_hour(self):
        p = progress(["[253600/400000]   1827 s  {'deals': 253600}  499663 deals an hour"])
        self.assertEqual((p['done'], p['total']), (253600, 400000))
        self.assertAlmostEqual(p['rate'], 499663 / 3600)

    def test_seconds_each(self):
        p = progress(['350/400  7.3 s a board wall'])
        self.assertAlmostEqual(p['rate'], 1 / 7.3)

    def test_tqdm(self):
        p = progress([' 45%|#####     | 450/1000 [01:23<02:30,  3.00it/s]'])
        self.assertEqual((p['done'], p['total'], p['left'], p['rate']), (450, 1000, 150, 3.0))

    def test_not_progress(self):
        self.assertIsNone(progress(['2026/11/25 12:00 started']))
        self.assertIsNone(progress(['300 of 300 boards to play, 7 workers']))
        self.assertIsNone(progress(['step 80400 loss 0.5338 lr 3.09e-05 1,978 games/s']))

    def test_latest_line_wins(self):
        p = progress(['10/100', 'some noise', '20/100'])
        self.assertEqual(p['done'], 20)


LOOP = '''#!/bin/bash
for p in part_00 part_01 part_02 part_03; do
  [ -f out/$p.npz ] && continue
  nice -n 19 python3 solve.py --problems in/$p.npz --out out/$p.npz > out/$p.log 2>&1
done
touch out/ALL.done
'''
SEQ = '''#!/bin/bash
cd /work
export PYTHONPATH=/work/lib
python train.py --run runs/a --hours 2 > runs/a.log 2>&1
python eval.py --model runs/a --boards b.jsonl > runs/a-eval.log 2>&1
python train.py --run runs/b --hours 3 > runs/b.log 2>&1
echo done > runs/all.done
'''


class Steps(unittest.TestCase):
    def test_loop_items_left(self):
        a = jobs.steps_after(LOOP, [{'cmd': 'solve.py', 'full': 'python3 solve.py --problems in/part_01.npz --out out/part_01.npz'}])
        self.assertEqual((a['loop_left'], a['loop_var']), (2, 'p'))

    def test_sequential(self):
        a = jobs.steps_after(SEQ, [{'cmd': 'eval.py', 'full': 'python eval.py --model runs/a --boards b.jsonl'}])
        self.assertEqual(a['later_known'], 3 * 3600)  # train b has --hours 3
        self.assertEqual(a['later_unknown'], 0)

    def test_waiting(self):
        a = jobs.steps_after('while [ ! -f x ]; do sleep 60; done\nfor r in a b c; do python m.py $r; done\n',
                             [{'cmd': 'sleep 60'}])
        self.assertTrue(a['waiting'])
        self.assertEqual(a['later_unknown'], 3)

    def test_time_limit(self):
        e = jobs.limit_eta('train.py --hours 6 --d 512', 5 * 3600)
        self.assertEqual(e['left'], 3600)


class ContainerJobs(unittest.TestCase):
    def test_job_with_log_eta_and_loop(self):
        ps = '\n'.join([
            '1 1 0 9000 1 1000 sleep infinity',
            '50 50 1 3000 2 3000 bash run_all.sh',
            '50 60 50 1200 30000 900000 python3 solve.py --problems in/part_01.npz --out out/part_01.npz',
        ])
        logs = {'now': 1000.0, 'scripts': {'50': LOOP}, 'logs': {
            '/w/out/part_01.log': {'pids': [60], 'std': True, 'mtime': 990.0, 'size': 10,
                                   'last': '500 of 1,000 solved, 0.1 h left', 'loss_key': None, 'loss': [],
                                   'prog': {'done': 500, 'total': 1000, 'left': 360, 'line': '500 of 1,000 solved, 0.1 h left'}}}}
        js = jobs.container_jobs('m', 'c', 'sleep infinity', ps, 1000.0, json.dumps(logs))
        self.assertEqual(len(js), 1)
        j = js[0]
        self.assertEqual(j['cmd'], 'run_all.sh')
        self.assertEqual(j['steps'][0]['eta']['pct'], 50.0)
        # 350 s left on this part, then 2 more parts as long as this one (1200 s run + 350 s left).
        self.assertAlmostEqual(j['eta']['left'], 350 + 2 * (1200 + 350))
        self.assertIn('2 more $p', j['eta']['how'])

    def test_host_skips_probe_and_idle_shells(self):
        ps = '\n'.join([
            '10 10 1 500 0 100 -bash',
            '20 20 1 30 0 100 python3 -',
            '20 21 20 1 0 100 ps -u 1000 -o sid=,pid=',
            '30 30 1 700 600 5000 python train.py --hours 1',
        ])
        js = jobs.container_jobs('m', 'host', None, ps, 1000.0, None, host=True)
        self.assertEqual([j['cmd'] for j in js], ['train.py --hours 1'])
        self.assertEqual(js[0]['eta']['left'], 3600 - 700)


if __name__ == '__main__':
    unittest.main()
