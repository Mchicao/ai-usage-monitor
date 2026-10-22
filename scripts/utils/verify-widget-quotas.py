import copy
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
FOLDER = ROOT / '.cache/widget-quota-verification'
FOLDER.mkdir(parents=True, exist_ok=True)
for name, filename in [('AI_AGY_SNAPSHOT_FILE', 'agy.json'), ('AI_QUOTA_SNAPSHOT_FILE', 'quota.json'), ('AI_QUOTA_IDENTITIES_FILE', 'identities.local.json')]:
    os.environ[name] = str(FOLDER / filename)

import ai_quota_widget as monitor
import win_tray_widget as tray


class HiddenPopup(tray.FluentPopup):
    def _fit_to_content(self):
        self.withdraw()
        super()._fit_to_content()

    def focus_force(self):
        pass


def labels(widget):
    result = []
    for child in widget.winfo_children():
        if isinstance(child, tray.ctk.CTkLabel):
            result.append(child.cget('text'))
        result.extend(labels(child))
    return result


def check():
    data = monitor.collect_all_quotas()
    assert data['antigravity']['status'] == 'ok'
    assert any(g['label'] == 'Gemini 4 Argon' for g in data['antigravity']['data']['groups'])
    accounts = data['codex']['data']['accounts']
    assert any(a['label'] == 'OpenCode' and a.get('status') != 'error' for a in accounts)
    before = copy.deepcopy(data)
    before['antigravity']['data']['groups'] = [g for g in before['antigravity']['data']['groups'] if g['label'] != 'Gemini 4 Argon']
    before['codex']['data']['accounts'] = [a for a in accounts if a['label'] != 'OpenCode']
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    foreground = user32.GetForegroundWindow()
    root = tray.ctk.CTk()
    root.withdraw()
    popup = None
    try:
        popup = HiddenPopup(before)
        initial = labels(popup)
        assert 'AGY · Gemini 4 Argon' not in initial and 'Codex · OpenCode' not in initial
        popup.update_data(data)
        root.update_idletasks()
        actual = labels(popup)
        assert 'AGY · Gemini 4 Argon' in actual, actual
        assert 'Codex · OpenCode' in actual, actual
        assert user32.GetForegroundWindow() == foreground, 'La verificacion cambio el foco.'
        groups = [{k: g.get(k) for k in ('label', 'remaining_5h_percent', 'weekly_remaining_percent')} for g in data['antigravity']['data']['groups']]
        quotas = [{'label': a['label'], 'windows': {name: w['remaining_percent'] for name, w in monitor.codex_windows_with_data(a)}} for a in accounts]
        return {'status': 'passed', 'finished': datetime.now(timezone.utc).isoformat(), 'groups': groups, 'accounts': quotas,
                'popup_labels': [t for t in actual if t.startswith(('AGY ·', 'Codex ·'))], 'focus_preserved': True}
    finally:
        if popup:
            popup.destroy()
        root.destroy()


if __name__ == '__main__':
    try:
        result = check()
    except Exception as error:
        (FOLDER / 'result.json').write_text(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), encoding='utf-8')
        raise
    (FOLDER / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
