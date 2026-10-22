import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
os.environ['AI_AGY_SNAPSHOT_FILE'] = str(ROOT / '.cache/live-agy-snapshot.json')

import ai_quota_widget as monitor
import win_tray_widget as tray


def check():
    started = datetime.now(timezone.utc)
    status_file = Path(os.environ['LOCALAPPDATA']) / 'ConsoleWatcher/output/status.json'
    watcher_status = json.loads(status_file.read_text(encoding='utf-8'))
    assert watcher_status['state'] == 'running'
    assert (started - datetime.fromisoformat(watcher_status['heartbeat'])).total_seconds() < 30, 'El watcher no tiene un heartbeat reciente.'
    for shell in (False, True):
        result = monitor._run_quota_command(
            [sys.executable.replace('pythonw.exe', 'python.exe'), '-c',
             'import ctypes,json; k=ctypes.windll.kernel32; k.GetConsoleWindow.restype=ctypes.c_void_p; print(json.dumps({"console":bool(k.GetConsoleWindow())}))'],
            shell=shell,
        )
        assert result.returncode == 0, result.stderr
        assert not json.loads(result.stdout)['console'], 'El hijo creo una consola.'
    quota = monitor.get_antigravity_quota()
    assert quota['status'] == 'ok' and quota['data']['method'] == 'agy' and not quota['data']['stale'], quota.get('status')
    groups = {g['label']: g for g in quota['data']['groups']}
    assert {'Gemini', 'Claude y GPT', 'Gemini 4 Argon'} <= groups.keys(), list(groups)
    assert groups['Gemini 4 Argon']['weekly_remaining_percent'] is not None
    data = {'antigravity': quota}
    cards = tray.FluentPopup._get_ag_groups(type('State', (), {'data': data})())
    argon_card = next(info for label, info in cards if label == 'Gemini 4 Argon')
    assert argon_card['pct'] == groups['Gemini 4 Argon']['weekly_remaining_percent']
    snapshot = monitor.build_quota_snapshot(data)
    assert snapshot['quotas']['agy:gemini_argon']['windows']['7d'] == argon_card['pct']
    time.sleep(0.5)
    log_folder = Path(os.environ['LOCALAPPDATA']) / 'ConsoleWatcher/logs'
    own_windows = []
    for path in log_folder.glob('console-events.jsonl*'):
        for line in path.read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            if not record.get('event', '').startswith('console_window_'):
                continue
            if datetime.fromisoformat(record['time']) < started:
                continue
            if any(p.get('pid') == os.getpid() for p in record.get('process', [])):
                own_windows.append(record)
    assert not own_windows, 'El watcher capturo una ventana originada por esta consulta.'
    return {'status': 'passed', 'pid': os.getpid(), 'started': started.isoformat(),
            'finished': datetime.now(timezone.utc).isoformat(), 'groups': list(groups.values()),
            'cards': cards, 'tooltip': tray.get_compact_tooltip(data),
            'snapshot': snapshot, 'window_events_from_probe': len(own_windows)}


if __name__ == '__main__':
    report = ROOT / '.cache/verify-agy-live.json'
    try:
        result = check()
    except Exception as error:
        report.write_text(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
