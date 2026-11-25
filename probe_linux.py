"""One status snapshot of a Linux machine as JSON on stdout (fleet-status).

server.py pipes this file to `python3 -` (locally, or over SSH) after putting in __LOGSCAN_B64__
(logscan.py), __RUNTIME__ (docker, podman or none). Standard library only. Reads only; changes nothing.
Prints the same keys as probe.ps1 where they apply, plus hostps/hostlogs for jobs outside containers.
"""
import base64
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import time

RUNTIME = '__RUNTIME__'
LOGSCAN = base64.b64decode('__LOGSCAN_B64__').decode()
PS_FIELDS = 'sid=,pid=,ppid=,etimes=,cputimes=,rss=,args='
REAL_FS = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs', 'jfs', 'reiserfs', 'ntfs', 'ntfs3', 'vfat', 'exfat',
           'nfs', 'nfs4', 'cifs', 'smb3', '9p', 'fuseblk'}


def run(cmd, timeout=30, stdin=None):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def cpu_times():
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def main():
    out = {'host': socket.gethostname(), 'os': 'linux', 'time': dt.datetime.now().astimezone().isoformat(),
           'user': os.environ.get('USER') or os.environ.get('LOGNAME') or ''}
    out['logical_cpus'] = os.cpu_count()
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            mem[k] = int(v.split()[0])
    out['mem_total_kb'] = mem.get('MemTotal', 0)
    out['mem_free_kb'] = mem.get('MemAvailable', mem.get('MemFree', 0))
    with open('/proc/uptime') as f:
        up = float(f.read().split()[0])
    out['boot'] = (dt.datetime.now().astimezone() - dt.timedelta(seconds=up)).isoformat()
    a_tot, a_idle = cpu_times()
    time.sleep(1)
    b_tot, b_idle = cpu_times()
    if b_tot > a_tot:
        out['cpu_pct'] = round(100 * (1 - (b_idle - a_idle) / (b_tot - a_tot)), 1)

    if shutil.which('nvidia-smi'):
        g = run(['nvidia-smi', '--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw',
                 '--format=csv,noheader,nounits'])
        if g.strip():
            out['gpu'] = [ln for ln in g.splitlines() if ln.strip()]

    disks, seen = [], set()
    with open('/proc/mounts') as f:
        for line in f:
            dev, mnt, fs = line.split()[:3]
            if (fs not in REAL_FS or dev in seen or not os.path.isdir(mnt)
                    or mnt.startswith(('/snap', '/boot/efi', '/var/lib/docker', '/proc', '/sys'))):
                continue
            seen.add(dev)
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            if st.f_blocks:
                disks.append({'id': mnt, 'size': st.f_blocks * st.f_frsize, 'free': st.f_bavail * st.f_frsize})
    out['disks'] = disks
    out['sessions'] = run(['who'])
    out['procs'] = []

    # Jobs outside containers: this user's processes, and the logs they write.
    out['hostps'] = run(['ps', '-u', str(os.getuid()), '-o', PS_FIELDS, '--cols', '400'])
    logs = run([sys.executable, '-c', LOGSCAN], timeout=60)
    out['hostlogs'] = next((ln for ln in reversed(logs.splitlines()) if ln.startswith('{')), None)

    exe = RUNTIME if RUNTIME in ('docker', 'podman') and shutil.which(RUNTIME) else None
    if exe:
        lines = lambda text: [ln for ln in text.splitlines() if ln.startswith('{')]
        out['containers'] = lines(run([exe, 'ps', '-a', '--no-trunc', '--format', '{{json .}}']))
        out['stats'] = lines(run([exe, 'stats', '--no-stream', '--format', '{{json .}}'], timeout=40))
        out['ps'], out['logs'] = {}, {}
        for line in out['containers']:
            o = json.loads(line)
            if str(o.get('State')).lower() != 'running':
                continue
            n = o.get('Names')
            n = n[0] if isinstance(n, list) else n
            out['ps'][n] = run([exe, 'exec', n, 'ps', '-eo', PS_FIELDS, '--cols', '400'])
            lg = run([exe, 'exec', '-i', n, 'python3', '-'], timeout=60, stdin=LOGSCAN)
            out['logs'][n] = next((ln for ln in reversed(lg.splitlines()) if ln.startswith('{')), None)
    out['done'] = dt.datetime.now().astimezone().isoformat()
    print(json.dumps(out))


main()
