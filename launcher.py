"""Explicit-token launch worker and window correlation. Never invokes a command shell."""
import ctypes as C
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import winreg

k=C.WinDLL('kernel32',use_last_error=True)
a=C.WinDLL('advapi32',use_last_error=True)
u=C.WinDLL('user32',use_last_error=True)
shell=C.WinDLL('shell32',use_last_error=True)
ue=C.WinDLL('userenv',use_last_error=True)
k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
k.CloseHandle.argtypes=[W.HANDLE]
k.GetCurrentProcess.restype=W.HANDLE
k.GetProcessId.argtypes=[W.HANDLE];k.GetProcessId.restype=W.DWORD
k.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
k.GetExitCodeProcess.argtypes=[W.HANDLE,C.POINTER(W.DWORD)]
a.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)]
a.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
a.DuplicateTokenEx.argtypes=[W.HANDLE,W.DWORD,C.c_void_p,C.c_int,C.c_int,C.POINTER(W.HANDLE)]
a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(W.LPWSTR)]
k.LocalFree.argtypes=[C.c_void_p];k.LocalFree.restype=C.c_void_p
u.GetShellWindow.restype=W.HWND
u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]


class STARTUPINFO(C.Structure):
    _fields_=[('cb',W.DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),
              ('dwX',W.DWORD),('dwY',W.DWORD),('dwXSize',W.DWORD),('dwYSize',W.DWORD),
              ('dwXCountChars',W.DWORD),('dwYCountChars',W.DWORD),('dwFillAttribute',W.DWORD),
              ('dwFlags',W.DWORD),('wShowWindow',W.WORD),('cbReserved2',W.WORD),('lpReserved2',C.c_void_p),
              ('hStdInput',W.HANDLE),('hStdOutput',W.HANDLE),('hStdError',W.HANDLE)]


class PROCESS_INFORMATION(C.Structure):
    _fields_=[('hProcess',W.HANDLE),('hThread',W.HANDLE),('dwProcessId',W.DWORD),('dwThreadId',W.DWORD)]


class SHELLEXECUTEINFO(C.Structure):
    _fields_=[('cbSize',W.DWORD),('fMask',W.ULONG),('hwnd',W.HWND),('lpVerb',W.LPCWSTR),
              ('lpFile',W.LPCWSTR),('lpParameters',W.LPCWSTR),('lpDirectory',W.LPCWSTR),
              ('nShow',C.c_int),('hInstApp',W.HINSTANCE),('lpIDList',C.c_void_p),('lpClass',W.LPCWSTR),
              ('hkeyClass',W.HKEY),('dwHotKey',W.DWORD),('hIcon',W.HANDLE),('hProcess',W.HANDLE)]


class PROCESSENTRY32(C.Structure):
    _fields_=[('dwSize',W.DWORD),('cntUsage',W.DWORD),('th32ProcessID',W.DWORD),('th32DefaultHeapID',C.c_size_t),
              ('th32ModuleID',W.DWORD),('cntThreads',W.DWORD),('th32ParentProcessID',W.DWORD),
              ('pcPriClassBase',W.LONG),('dwFlags',W.DWORD),('szExeFile',W.WCHAR*260)]


a.CreateProcessWithTokenW.argtypes=[W.HANDLE,W.DWORD,W.LPCWSTR,W.LPWSTR,W.DWORD,C.c_void_p,W.LPCWSTR,
                                    C.POINTER(STARTUPINFO),C.POINTER(PROCESS_INFORMATION)]
ue.CreateEnvironmentBlock.argtypes=[C.POINTER(C.c_void_p),W.HANDLE,W.BOOL]
ue.DestroyEnvironmentBlock.argtypes=[C.c_void_p]
shell.ShellExecuteExW.argtypes=[C.POINTER(SHELLEXECUTEINFO)]
k.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];k.CreateToolhelp32Snapshot.restype=W.HANDLE
k.Process32FirstW.argtypes=[W.HANDLE,C.POINTER(PROCESSENTRY32)]
k.Process32NextW.argtypes=[W.HANDLE,C.POINTER(PROCESSENTRY32)]


def checked(ok,operation='Native launch operation'):
    if not ok:
        code=C.get_last_error()
        error=RuntimeError(f'{operation}: {C.WinError(code)}')
        error.winerror=code;error.details={'winerror':code}
        raise error


def token_elevated(token):
    value=W.DWORD();size=W.DWORD()
    checked(a.GetTokenInformation(token,20,C.byref(value),C.sizeof(value),C.byref(size)))
    return bool(value.value)


def elevated_handle(handle):
    token=W.HANDLE()
    checked(a.OpenProcessToken(handle,8,C.byref(token)))
    try:return token_elevated(token)
    finally:k.CloseHandle(token)


def process_elevated(pid):
    handle=k.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        try:return elevated_handle(handle)
        except (OSError,RuntimeError):return None
    finally:k.CloseHandle(handle)


def normalize(spec):
    if 'path' in spec and 'app' in spec:raise ValueError('Provide path or legacy app, not both')
    path=spec.get('path',spec.get('app'))
    if not isinstance(path,str) or not path.strip() or '\0' in path or len(path)>32760:
        raise ValueError('launch_app requires a nonempty path or app alias')
    args=spec.get('args',[])
    if not isinstance(args,list) or len(args)>256 or any(not isinstance(x,str) or '\0' in x for x in args):
        raise ValueError('args must be a list of at most 256 strings without NUL')
    if len(subprocess.list2cmdline([path,*args]))>30000:raise ValueError('Launch command line is too long')
    cwd=spec.get('cwd')
    if cwd is not None and (not isinstance(cwd,str) or '\0' in cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd)):
        raise ValueError('cwd must be null or an existing absolute directory')
    elevated=spec.get('elevated',False)
    if not isinstance(elevated,bool):raise ValueError('elevated must be boolean; default is false')
    timeout=spec.get('timeout',8)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not .2<=timeout<=30:
        raise ValueError('timeout must be finite seconds from 0.2 to 30')
    return dict(path=path,args=list(args),cwd=cwd,elevated=elevated,timeout=timeout)


def resolve(path):
    aliases={'notepad':os.path.join(os.environ['SystemRoot'],'System32','notepad.exe'),
             'regedit':os.path.join(os.environ['SystemRoot'],'regedit.exe')}
    if path.lower() in aliases:return aliases[path.lower()]
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',path) and not os.path.splitdrive(path)[0]:return path
    if os.path.isabs(path):
        if not os.path.isfile(path):raise FileNotFoundError('Launch path does not exist: '+path)
        return path
    if os.path.dirname(path):raise ValueError('Use an absolute path or a registered/PATH application name')
    name=path if os.path.splitext(path)[1] else path+'.exe'
    for root in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root,'Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\'+name,0,winreg.KEY_READ|view) as key:
                    registered=os.path.expandvars(winreg.QueryValue(key,None)).strip('"')
                    if os.path.isfile(registered):return registered
            except OSError:pass
    found=shutil.which(path)
    if found:return found
    raise FileNotFoundError('Application is not registered in App Paths or PATH: '+path)


def shell_target(path):
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',path) and not os.path.splitdrive(path)[0]) or Path(path).suffix.lower() not in ('.exe','.com')


def association_executable(path):
    """Best-effort correlation hint for delegated shell/URL launches."""
    if path.lower().endswith('.lnk'):
        try:
            import comtypes.client
            shortcut=comtypes.client.CreateObject('WScript.Shell',dynamic=True).CreateShortcut(path)
            return str(shortcut.TargetPath) or None
        except Exception:return None
    association=path.split(':',1)[0] if not os.path.splitdrive(path)[0] else Path(path).suffix
    if association.lower()=='ms-settings':return 'SystemSettings.exe'
    lib=C.WinDLL('shlwapi');query=lib.AssocQueryStringW
    query.argtypes=[W.DWORD,C.c_int,W.LPCWSTR,W.LPCWSTR,W.LPWSTR,C.POINTER(W.DWORD)]
    length=W.DWORD(32768);buffer=C.create_unicode_buffer(length.value)
    return buffer.value if query(0,2,association,'open',buffer,C.byref(length))==0 else None


def worker(spec):
    if spec.get('_module_path'):sys.path.append(spec['_module_path'])
    actual=elevated_handle(k.GetCurrentProcess())
    if actual!=spec['elevated']:raise RuntimeError('Worker token does not match explicitly requested elevation')
    path=resolve(spec['path'])
    if Path(path).suffix.lower() in ('.cmd','.bat'):
        raise ValueError('Batch files require an explicit cmd.exe path and args; no automatic command shell')
    if not shell_target(path):
        process=subprocess.Popen([path,*spec['args']],cwd=spec['cwd'],shell=False,close_fds=True)
        try:root_elevated=elevated_handle(int(process._handle))
        except (OSError,RuntimeError):root_elevated=None
        return dict(process_id=process.pid,elevated=root_elevated,resolved_path=path,expected_executable=path,
                    launch_method='create_process',worker_elevated=actual)
    info=SHELLEXECUTEINFO();info.cbSize=C.sizeof(info);info.fMask=0x40|0x100|0x400
    info.lpVerb='open';info.lpFile=path;info.lpParameters=subprocess.list2cmdline(spec['args']) or None
    info.lpDirectory=spec['cwd'];info.nShow=1
    checked(shell.ShellExecuteExW(C.byref(info)))
    try:
        pid=int(k.GetProcessId(info.hProcess)) if info.hProcess else None
        try:elevated=elevated_handle(info.hProcess) if info.hProcess else None
        except (OSError,RuntimeError):elevated=None
        return dict(process_id=pid,elevated=elevated,resolved_path=path,expected_executable=association_executable(path),
                    launch_method='shell_execute',worker_elevated=actual)
    finally:
        if info.hProcess:k.CloseHandle(info.hProcess)


def grant_exchange_directory(folder,token):
    """Only the generated exchange directory is shared with the shell user."""
    size=W.DWORD()
    a.GetTokenInformation(token,1,None,0,C.byref(size))
    buffer=C.create_string_buffer(size.value)
    checked(a.GetTokenInformation(token,1,buffer,size,C.byref(size)),'Read shell user SID')
    sid=C.cast(buffer,C.POINTER(C.c_void_p))[0]
    text=W.LPWSTR()
    checked(a.ConvertSidToStringSidW(sid,C.byref(text)),'Format shell user SID')
    try:user_sid=text.value
    finally:k.LocalFree(C.cast(text,C.c_void_p))
    tool=os.path.join(os.environ['SystemRoot'],'System32','icacls.exe')
    for options in (['/grant','*'+user_sid+':(OI)(CI)F','/T'],['/setintegritylevel','(OI)(CI)M','/T']):
        result=subprocess.run([tool,str(folder),*options],capture_output=True,creationflags=0x08000000)
        if result.returncode:raise RuntimeError('Cannot grant ordinary user access to the launch exchange directory')


def start_medium_worker(command,exchange_dir):
    shell_hwnd=u.GetShellWindow()
    if not shell_hwnd:raise RuntimeError('No interactive desktop shell; ordinary-token launch unavailable')
    pid=W.DWORD();u.GetWindowThreadProcessId(shell_hwnd,C.byref(pid))
    process=k.OpenProcess(0x1000,False,pid.value)
    if not process:raise C.WinError(C.get_last_error())
    token=W.HANDLE();primary=W.HANDLE();environment=C.c_void_p()
    try:
        checked(a.OpenProcessToken(process,0xA,C.byref(token)),'Open desktop shell token')
        if token_elevated(token):raise RuntimeError('Desktop shell token is elevated; refusing ordinary-token launch')
        grant_exchange_directory(exchange_dir,token)
        checked(a.DuplicateTokenEx(token,0x2000000,None,2,1,C.byref(primary)),'Duplicate desktop shell token')
        checked(ue.CreateEnvironmentBlock(C.byref(environment),primary,False),'Create user environment')
        startup=STARTUPINFO();startup.cb=C.sizeof(startup);startup.lpDesktop='winsta0\\default'
        result=PROCESS_INFORMATION();line=C.create_unicode_buffer(subprocess.list2cmdline(command))
        checked(a.CreateProcessWithTokenW(primary,1,command[0],line,0x08000400,environment,
                                          os.path.expanduser('~'),C.byref(startup),C.byref(result)),'Create ordinary-token worker')
        k.CloseHandle(result.hThread)
        return result.hProcess
    finally:
        if environment:ue.DestroyEnvironmentBlock(environment)
        if primary:k.CloseHandle(primary)
        if token:k.CloseHandle(token)
        k.CloseHandle(process)


def dispatch(spec):
    # Local exchange files contain launch arguments, never the bridge bearer token.
    with tempfile.TemporaryDirectory(prefix='desktop-bridge-launch-') as folder:
        request=Path(folder)/'request.json';response=Path(folder)/'response.json'
        request.write_text(json.dumps({**spec,'_module_path':sysconfig.get_path('purelib')}),encoding='utf-8')
        # The venv redirector can fail with a freshly built user environment.
        # Worker uses stdlib plus the explicit venv module path for shell links.
        interpreter=getattr(sys,'_base_executable',None) or sys.executable
        command=[interpreter,'-I',str(Path(__file__).resolve()),'--worker',str(request),str(response)]
        exit_code=None
        if spec['elevated']:
            if not elevated_handle(k.GetCurrentProcess()):raise RuntimeError('elevated=true requires the elevated bridge')
            process=subprocess.Popen(command,creationflags=0x08000000,close_fds=True)
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:raise RuntimeError('Launch worker timed out; outcome unknown; do not blindly retry')
            exit_code=process.returncode
        else:
            handle=start_medium_worker(command,folder)
            try:
                if k.WaitForSingleObject(handle,20000)!=0:raise RuntimeError('Launch worker did not finish; do not blindly retry')
                code=W.DWORD();checked(k.GetExitCodeProcess(handle,C.byref(code)))
                exit_code=code.value
            finally:k.CloseHandle(handle)
        if not response.is_file():raise RuntimeError(f'Launch worker returned no result (exit code {exit_code}); do not blindly retry')
        result=json.loads(response.read_text(encoding='utf-8'))
        if 'error' in result:
            error=RuntimeError(result['error']);error.details={'winerror':result.get('winerror'),'requested_elevated':spec['elevated']}
            raise error
        return result


def processes():
    handle=k.CreateToolhelp32Snapshot(2,0)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        row=PROCESSENTRY32();row.dwSize=C.sizeof(row);result={}
        ok=k.Process32FirstW(handle,C.byref(row))
        while ok:
            result[row.th32ProcessID]={'parent':row.th32ParentProcessID,'name':row.szExeFile.lower()}
            ok=k.Process32NextW(handle,C.byref(row))
        return result
    finally:k.CloseHandle(handle)


def descendants(snapshot,known):
    known=set(known)
    while True:
        expanded=known|{pid for pid,p in snapshot.items() if p['parent'] in known}
        if expanded==known:return known
        known=expanded


def choose_window(rows,before,known,expected,desktop,allow_unchanged_active=False):
    before_active={w['id'] for w in before if w['active']};before_ids={w['id'] for w in before}
    eligible=[]
    for row in rows:
        pid=W.DWORD();desktop.u.GetWindowThreadProcessId(row['id'],C.byref(pid))
        correlated=pid.value in known
        new=row['id'] not in before_ids
        activated=row['active'] and row['id'] not in before_active
        unchanged=allow_unchanged_active and row['active']
        if not correlated and not (new or activated or unchanged):continue
        delegated=bool(expected and window_matches_executable(row['id'],expected,desktop))
        if (correlated or delegated) and (new or row['active'] and (correlated or activated or unchanged)):
            eligible.append((not correlated,not row['active'],not new,row,pid.value))
    if not eligible:return None
    _,_,_,row,pid=min(eligible,key=lambda item:item[:3])
    return row,pid


def window_matches_executable(hwnd,expected,desktop):
    if desktop.process_name(hwnd)==expected:return True
    # UWP windows may be hosted by ApplicationFrameHost; verify a child process
    # belonging to the expected app rather than guessing from a new foreground.
    if desktop.process_name(hwnd)!='applicationframehost.exe':return False
    found=False
    @C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
    def visit(child,param):
        nonlocal found
        found=desktop.process_name(child)==expected
        return not found
    desktop.u.EnumChildWindows(hwnd,visit,0)
    return found


def launch(spec,desktop):
    spec=normalize(spec)
    print(json.dumps({'event':'launch_app','path':spec['path'],'args':spec['args'],
                      'cwd':spec['cwd'],'requested_elevated':spec['elevated']},ensure_ascii=True),flush=True)
    before=desktop.windows();root=dispatch(spec)
    pid=root['process_id'];known={pid} if pid else set()
    expected=os.path.basename(root.get('expected_executable') or '').lower() or None
    started=time.monotonic();deadline=started+spec['timeout']
    while True:
        snapshot=processes();known=descendants(snapshot,known)
        now=time.monotonic()
        delegated=now-started>=.2 and (not pid or pid not in snapshot)
        match=choose_window(desktop.windows(),before,known,expected,desktop,allow_unchanged_active=delegated)
        if match:
            row,window_pid=match
            try:window=desktop.focus_window(row['id'])
            except Exception as error:
                details=getattr(error,'details',None)
                error.details={**(details if isinstance(details,dict) else {}),'launch':root,
                               'launch_window_id':row['id'],'requested_elevated':spec['elevated']}
                raise
            actual=process_elevated(window_pid)
            return {**root,'window_id':row['id'],'window':window,'window_process_id':window_pid,
                    'launch_process_elevated':root['elevated'],'elevated':actual,
                    'requested_elevated':spec['elevated'],'reused_window':row['id'] in {w['id'] for w in before},
                    'foreground_verified':True,'launch_status':'window_ready'}
        if now>=deadline:
            return {**root,'window_id':None,'window_process_id':None,'requested_elevated':spec['elevated'],
                    'reused_window':False,'foreground_verified':False,'launch_status':'started_no_window'}
        time.sleep(.05)


if __name__=='__main__' and len(sys.argv)==4 and sys.argv[1]=='--worker':
    ole=C.WinDLL('ole32')
    initialized=ole.CoInitializeEx(None,2)>=0
    try:result=worker(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')))
    except Exception as error:result={'error':str(error),'winerror':getattr(error,'winerror',None)}
    finally:
        if initialized:ole.CoUninitialize()
    Path(sys.argv[3]).write_text(json.dumps(result),encoding='utf-8')
