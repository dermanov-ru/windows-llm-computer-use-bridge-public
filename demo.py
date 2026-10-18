"""Run against an already open, foreground, empty Notepad window."""
import json, time
from pathlib import Path
from client import Client, save_image

c=Client(); root=Path(__file__).parent
initial=c.call('windows')['windows']
active=next(w for w in initial if w['active'])
assert 'Notepad' in active['title'] or 'Блокнот' in active['title'], 'Foreground must be Notepad'
lease=c.call('lease',{'ttl':120})['lease_id']
def act(action):
    action['window_id'] = active['id']
    result=c.call('actions',{'lease_id':lease,'action':action})
    assert result['status']=='completed'
try:
    try:
        c.call('lease',{})
        raise AssertionError('Concurrent lease unexpectedly allowed')
    except Exception as e:
        assert getattr(e,'code',None)==409
    act({'type':'type_text','text':'Desktop Bridge работает!\r\nВвод через HTTP API: русский текст и Unicode — ✓\r\n'})
    obs=c.call('observe',{'window_id':active['id']})
    save_image(obs,root/'demo-screen.jpg')
    act({'type':'press_keys','keys':['CTRL','SHIFT','S']})
    time.sleep(1)
    obs=c.call('observe',{})
    save_image(obs,root/'demo-save-dialog.jpg')
    print(json.dumps({'status':'typed_and_opened_save_dialog','size':[obs['width'],obs['height']]},ensure_ascii=False))
finally: c.call('release',{'lease_id':lease})
