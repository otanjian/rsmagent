"""TEST ONLY: supervise one inert HTTP process, never an Agent deployment.

The fixture has no jobs, descendants or traffic proxy. Production adapters must
prove their entire execution group stopped and quarantine all ingress/channels.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

action, config_path = sys.argv[1:3]
cfg = json.loads(Path(config_path).read_text())
control = Path(cfg['control_root'])
active = json.loads((control / 'active.json').read_text())
pidfile = control / 'fixture.pid'
endpoint = control / 'fixture.endpoint'


def living():
    if not pidfile.exists():
        return False
    pid = int(pidfile.read_text())
    rows = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True).splitlines()
    return any(int(row.split()[0]) == pid and not row.split()[1].startswith('Z') for row in rows)


result = {'ok': True}
if action == 'probe':
    result.update(protocol='rongai-source-manager-v1', quarantine=True, independent_worker=True)
elif action == 'launch':
    python, script, operation = sys.argv[3:6]
    child = subprocess.Popen([python, script, config_path, operation], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    result['pid'] = child.pid
elif action == 'stop':
    if living():
        os.killpg(int(pidfile.read_text()), signal.SIGTERM)
    for _ in range(100):
        if not living():
            break
        time.sleep(.02)
    result['ok'] = not living()
elif action == 'stopped':
    result.update(group_empty=not living(), restart_disabled=True)
elif action == 'start':
    endpoint.unlink(missing_ok=True)
    python = Path(active['release']) / '.venv/bin/python'
    if not python.exists():
        python = Path(sys.executable)
    child = subprocess.Popen([str(python), str(Path(active['release']) / 'app.py'),
                              active['data_root'], str(endpoint)], cwd=active['release'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    pidfile.write_text(str(child.pid))
    for _ in range(100):
        if endpoint.exists():
            break
        time.sleep(.02)
    result['ok'] = endpoint.exists() and living()
elif action == 'health':
    with urllib.request.urlopen(endpoint.read_text(), timeout=5) as response:
        result.update(json.load(response))
    result.update(release=active['release'], data_root=active['data_root'], quarantined=True)
elif action == 'promote':
    if (control / 'fail-promote').exists():
        result['ok'] = False
    else:
        (control / 'promoted').write_text(json.dumps(active))
else:
    result['ok'] = False
print(json.dumps(result))
raise SystemExit(0 if result['ok'] else 1)
