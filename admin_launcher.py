"""Start Desktop Bridge elevated, using ordinary Windows UAC consent."""
import ctypes, json, os, socket, subprocess, sys, time, traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PORT=8765

def is_admin():
    return bool(ctypes.WinDLL('shell32').IsUserAnAdmin())

def request_elevation(script, args):
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    shell.ShellExecuteW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
    shell.ShellExecuteW.restype=ctypes.c_void_p
    executable=str(Path(sys.executable).with_name('pythonw.exe'))
    result=shell.ShellExecuteW(None,'runas',executable,subprocess.list2cmdline([str(script),*args]),str(ROOT),0)
    if not result or result<=32:
        raise RuntimeError(f'UAC launch failed or cancelled: ShellExecute={result}, Windows error={ctypes.get_last_error()}')

def main():
    if not is_admin():
        request_elevation(Path(__file__).resolve(),sys.argv[1:])
        print('Administrator launch requested. Confirm the Windows UAC prompt.')
        return
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',PORT))==0:
            if '--restart' not in sys.argv: raise RuntimeError('Port 8765 in use; use --restart for the project server')
            env=os.environ.copy(); env['BRIDGE_SERVER_PATH']=str(ROOT/'server.py')
            script="$ErrorActionPreference='Stop'; $l=Get-NetTCPConnection -LocalPort 8765 -State Listen; $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$l.OwningProcess); if (!$p -or !$p.CommandLine.Contains($env:BRIDGE_SERVER_PATH)) { throw 'Server script identity mismatch' }; Stop-Process -Id $p.ProcessId -ErrorAction Stop"
            result=subprocess.run(['powershell.exe','-NoProfile','-Command',script],env=env,capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode: raise RuntimeError('Restart check failed: '+result.stderr)
            time.sleep(0.5)
    with (ROOT/'server.log').open('a') as out,(ROOT/'server-error.log').open('a') as err:
        server=subprocess.Popen([sys.executable,str(ROOT/'server.py')],cwd=ROOT,stdout=out,stderr=err)
        report={'elevated':True,'port':PORT,'server_pid':server.pid}
        if '--test' in sys.argv:
            test=subprocess.Popen([sys.executable,str(ROOT/'admin-test-window.py')],cwd=ROOT,stdout=out,stderr=err)
            report['test_pid']=test.pid
    (ROOT/'admin-launch-status.json').write_text(json.dumps(report),encoding='utf-8')
    for _ in range(50):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',PORT))==0: return
        if server.poll() is not None: raise RuntimeError('Server exited; inspect server-error.log')
        time.sleep(0.2)
    raise RuntimeError('Server did not listen in 10 seconds; inspect server-error.log')

if __name__=='__main__':
    try: main()
    except Exception:
        (ROOT/'admin-launch-error.log').write_text(traceback.format_exc(),encoding='utf-8')
        raise
