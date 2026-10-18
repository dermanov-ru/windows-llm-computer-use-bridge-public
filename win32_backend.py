import ctypes as C
from ctypes import wintypes as W
import time
import os
import subprocess

u = C.WinDLL('user32', use_last_error=True)
try:
    u.SetProcessDpiAwarenessContext(C.c_void_p(-4))
except Exception:
    pass
ULONG_PTR = C.c_size_t
class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD), ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', ULONG_PTR)]
class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', ULONG_PTR)]
class UNION(C.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]
class INPUT(C.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', W.DWORD), ('data', UNION)]
u.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
u.GetForegroundWindow.restype = W.HWND
u.SetForegroundWindow.argtypes = [W.HWND]
u.ShowWindow.argtypes = [W.HWND, C.c_int]
u.GetWindowTextLengthW.argtypes = [W.HWND]
u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
u.IsWindowVisible.argtypes = [W.HWND]
u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]

def send(items):
    arr = (INPUT * len(items))(*items)
    if u.SendInput(len(arr), arr, C.sizeof(INPUT)) != len(arr):
        raise RuntimeError('SendInput failed; check desktop availability and process privilege levels')

def key(vk, up=False):
    flags = (2 if up else 0) | (1 if vk in EXTENDED_KEYS else 0)
    return INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=flags))

# Dedicated navigation cluster, not the numeric keypad aliases. Both key-down
# and key-up need E0; otherwise NumLock + Shift can cancel selection.
EXTENDED_KEYS = {0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x2D,0x2E,0x5B}

KEYS = {'CTRL':17, 'ALT':18, 'SHIFT':16, 'WIN':91, 'ENTER':13, 'ESC':27, 'TAB':9, 'BACKSPACE':8, 'DELETE':46, 'SPACE':32, 'HOME':36, 'END':35, 'LEFT':37, 'UP':38, 'RIGHT':39, 'DOWN':40, 'INSERT':45, 'PGUP':33, 'PGDN':34}
KEYS.update({f'F{i}':111+i for i in range(1,13)})

def virtual_screen():
    return [u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)]

def runtime_status():
    u.OpenInputDesktop.argtypes = [W.DWORD,W.BOOL,W.DWORD]
    u.OpenInputDesktop.restype = W.HANDLE
    u.CloseDesktop.argtypes = [W.HANDLE]
    u.GetUserObjectInformationW.argtypes = [W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
    handle = u.OpenInputDesktop(0,False,1)
    name = None
    if handle:
        try:
            buf = C.create_unicode_buffer(256); needed = W.DWORD()
            if u.GetUserObjectInformationW(handle,2,buf,C.sizeof(buf),C.byref(needed)):
                name = buf.value
        finally: u.CloseDesktop(handle)
    return {'elevated':bool(C.WinDLL('shell32').IsUserAnAdmin()),
            'input_desktop':name,'interactive_desktop_available':name == 'Default'}

def require_interactive_desktop():
    if not runtime_status()['interactive_desktop_available']:
        raise PermissionError('Interactive desktop unavailable: unlock Windows or handle the secure-desktop prompt locally')

def resolve_keys(keys, hwnd=None):
    codes = []
    for name in keys:
        upper = name.upper()
        if upper in KEYS: requested = [KEYS[upper]]
        elif len(upper) == 1 and upper.isascii() and upper.isalnum(): requested = [ord(upper)]
        elif len(name) == 1 and 32 <= ord(name) <= 0xFFFF and not 0xD800 <= ord(name) <= 0xDFFF:
            thread = u.GetWindowThreadProcessId(hwnd,None) if hwnd else 0
            layout = u.GetKeyboardLayout(thread)
            mapped = u.VkKeyScanExW(name,layout)
            if mapped == -1 or mapped & 0xFF00 & ~0x700:
                raise ValueError('Character is not mapped in the target keyboard layout: ' + name)
            modifiers = (mapped >> 8) & 7
            requested = [vk for bit,vk in ((1,16),(2,17),(4,18)) if modifiers & bit]+[mapped & 255]
        else: raise ValueError('Unknown key: ' + name)
        for vk in requested:
            if vk not in codes: codes.append(vk)
    return codes


def press(keys, hwnd=None):
    codes = resolve_keys(keys,hwnd)
    try: send([key(k) for k in codes])
    finally: send([key(k, True) for k in reversed(codes)])

def type_text(text, guard=None):
    raw = text.replace('\r\n','\n').replace('\r','\n').encode('utf-16-le')
    for i in range(0, len(raw), 2):
        if guard: guard()
        code = int.from_bytes(raw[i:i+2], 'little')
        if code == 10:
            press(['ENTER'])
        else:
            send([INPUT(type=1, ki=KEYBDINPUT(wScan=code, dwFlags=4)), INPUT(type=1, ki=KEYBDINPUT(wScan=code, dwFlags=6))])
        time.sleep(0.015)

def windows(include_popups=False,owner=None):
    if owner is not None and (not isinstance(owner,int) or isinstance(owner,bool) or owner<=0):
        raise ValueError("owner must be a positive HWND integer")
    include_popups=include_popups or owner is not None
    result = []
    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if u.IsWindowVisible(hwnd) and (n or include_popups):
            title = C.create_unicode_buffer(n+1)
            u.GetWindowTextW(hwnd, title, n+1)
            r = W.RECT(); u.GetWindowRect(hwnd, C.byref(r))
            row={'id': int(hwnd), 'title': title.value, 'bounds': [r.left,r.top,r.right,r.bottom], 'active': hwnd == u.GetForegroundWindow()}
            if include_popups:
                cls=C.create_unicode_buffer(256);u.GetClassNameW(hwnd,cls,256)
                if r.right-r.left<=1 or r.bottom-r.top<=1 or cls.value in {
                    'DummyDWMListenerWindow','WorkerW','ThumbnailDeviceHelperWnd','PseudoConsoleWindow'}:
                    return True
                pid=W.DWORD();tid=u.GetWindowThreadProcessId(hwnd,C.byref(pid))
                window_owner=u.GetWindow(hwnd,4)
                popup=not n or cls.value=='#32768' or bool(window_owner)
                if popup and not window_owner:
                    info=gui_thread_info(tid)
                    window_owner=(info.hwndMenuOwner or info.hwndActive) if info else None
                    if window_owner==hwnd:window_owner=None
                row.update(class_name=cls.value,process_id=pid.value,popup=popup,owner=int(window_owner) if window_owner else None)
                if owner is not None and row['owner']!=owner:return True
            result.append(row)
        return True
    u.EnumWindows(visit, 0)
    return result

def active_window():
    hwnd = u.GetForegroundWindow()
    if not hwnd: return None
    n = u.GetWindowTextLengthW(hwnd)
    title = C.create_unicode_buffer(n+1)
    u.GetWindowTextW(hwnd,title,n+1)
    r = W.RECT()
    if not u.GetWindowRect(hwnd,C.byref(r)): return None
    # DWM bounds exclude invisible resize borders on modern Windows.
    try:
        dwm = C.WinDLL('dwmapi')
        dwm.DwmGetWindowAttribute.argtypes = [W.HWND,W.DWORD,C.c_void_p,W.DWORD]
        visible = W.RECT()
        if dwm.DwmGetWindowAttribute(hwnd,9,C.byref(visible),C.sizeof(visible)) == 0:
            r = visible
    except OSError: pass
    return {'id':int(hwnd),'title':title.value,'bounds':[r.left,r.top,r.right,r.bottom],'active':True}

class TargetError(RuntimeError):
    code = "target_mismatch"

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


u.IsWindow.argtypes = [W.HWND]
u.IsIconic.argtypes = [W.HWND]
u.WindowFromPoint.argtypes = [W.POINT]
u.WindowFromPoint.restype = W.HWND
u.GetAncestor.argtypes = [W.HWND, W.UINT]
u.GetAncestor.restype = W.HWND
u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
u.GetWindowThreadProcessId.restype = W.DWORD
u.AttachThreadInput.argtypes = [W.DWORD,W.DWORD,W.BOOL]
u.AttachThreadInput.restype = W.BOOL
u.PeekMessageW.argtypes = [C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT,W.UINT]
kernel32 = C.WinDLL('kernel32',use_last_error=True)
kernel32.GetCurrentThreadId.restype = W.DWORD
u.GetWindow.argtypes=[W.HWND,W.UINT];u.GetWindow.restype=W.HWND
u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
u.GetKeyboardLayout.argtypes=[W.DWORD];u.GetKeyboardLayout.restype=W.HANDLE
u.VkKeyScanExW.argtypes=[W.WCHAR,W.HANDLE];u.VkKeyScanExW.restype=C.c_short


class GUITHREADINFO(C.Structure):
    _fields_=[('cbSize',W.DWORD),('flags',W.DWORD),('hwndActive',W.HWND),('hwndFocus',W.HWND),
              ('hwndCapture',W.HWND),('hwndMenuOwner',W.HWND),('hwndMoveSize',W.HWND),('hwndCaret',W.HWND),('rcCaret',W.RECT)]


u.GetGUIThreadInfo.argtypes=[W.DWORD,C.POINTER(GUITHREADINFO)]


def gui_thread_info(thread=0):
    info=GUITHREADINFO();info.cbSize=C.sizeof(info)
    return info if u.GetGUIThreadInfo(thread,C.byref(info)) else None


def target_at(x, y):
    hwnd = u.WindowFromPoint(W.POINT(int(x), int(y)))
    root = u.GetAncestor(hwnd, 2) if hwnd else None
    return {"x": int(x), "y": int(y), "window_id": int(hwnd) if hwnd else None,
            "root_window_id": int(root) if root else None}


def cursor_state():
    point = W.POINT()
    if not u.GetCursorPos(C.byref(point)):
        return None
    return target_at(point.x, point.y)


def require_window(hwnd):
    if not isinstance(hwnd, int) or isinstance(hwnd, bool) or not u.IsWindow(hwnd):
        raise ValueError("window_id must identify an existing window")
    if not u.IsWindowVisible(hwnd):
        raise TargetError("Target window is not visible")


def require_foreground(hwnd):
    if u.GetForegroundWindow() != hwnd:
        raise TargetError("Foreground changed; action stopped before sending further input")


def focus_window(hwnd, timeout=1.5):
    require_window(hwnd)
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
    # Do not SW_RESTORE an already maximized window.
    if u.GetForegroundWindow() != hwnd:
        u.SetForegroundWindow(hwnd)
        if u.GetForegroundWindow() != hwnd:
            # Never synthesize Alt/Escape: those can activate or dismiss menus.
            # Temporarily share the foreground thread's queue, always detaching.
            foreground = u.GetForegroundWindow()
            owner = u.GetWindowThreadProcessId(foreground,None) if foreground else 0
            current = kernel32.GetCurrentThreadId()
            if owner and owner != current:
                message = W.MSG()
                u.PeekMessageW(C.byref(message),None,0,0,0)  # Ensure our queue exists.
                if u.AttachThreadInput(current,owner,True):
                    try:
                        u.SetForegroundWindow(hwnd)
                    finally:
                        u.AttachThreadInput(current,owner,False)
            if u.GetForegroundWindow() != hwnd:
                # A zero-distance input packet refreshes input without clicking,
                # touching menus, or changing the cursor position.
                send([INPUT(type=0,mi=MOUSEINPUT(dwFlags=1))])
                u.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + timeout
    while u.GetForegroundWindow() != hwnd:
        if time.monotonic() >= deadline:
            raise TargetError("Windows denied foreground activation")
        time.sleep(0.025)
    return window_info(hwnd)


def window_info(hwnd):
    require_window(hwnd)
    return next((w for w in windows() if w["id"] == hwnd), {"id": hwnd})


def process_name(hwnd):
    pid = W.DWORD()
    u.GetWindowThreadProcessId(hwnd, C.byref(pid))
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        buf = C.create_unicode_buffer(32768)
        size = W.DWORD(len(buf))
        if kernel.QueryFullProcessImageNameW(handle, 0, buf, C.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel.CloseHandle(handle)
    return None


def launch_app(spec):
    import launcher
    import sys
    try:return launcher.launch(spec,sys.modules[__name__])
    except OSError as error:
        wrapped=RuntimeError('Launch failed: '+str(error))
        wrapped.details={'winerror':getattr(error,'winerror',None)}
        raise wrapped from error


def validate_action(a):
    if not isinstance(a, dict):
        raise ValueError("Each action must be an object")
    kind = a.get("type")
    allowed = ("launch_app", "focus_window", "click", "move", "scroll", "press_keys",
               "type_text", "set_value", "click_text", "click_element", "drag")
    if kind not in allowed:
        raise ValueError("Unknown action type")
    if kind == "drag":
        for name in ("window_id", "target_window_id", "x", "y", "to_x", "to_y"):
            if not isinstance(a.get(name), int) or isinstance(a.get(name), bool):
                raise ValueError("drag requires integer " + name)
        duration = a.get("duration_ms", 700)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 100 <= duration <= 5000:
            raise ValueError("duration_ms must be 100 to 5000")
    if "window_id" in a and (not isinstance(a["window_id"], int) or isinstance(a["window_id"], bool)):
        raise ValueError("window_id must be an integer")
    if kind in ("set_value", "click_text", "click_element", "focus_window", "click", "type_text", "press_keys", "scroll") and "window_id" not in a:
        raise ValueError("This action requires window_id")
    if kind in ("type_text", "set_value"):
        if not isinstance(a.get("text"), str) or len(a["text"]) > 20000:
            raise ValueError("text must be a string of at most 20000 characters")
    if "selector" in a:
        import uia_backend
        uia_backend.validate_selector(a["selector"])
        if "window_id" not in a:
            raise ValueError("selector requires window_id")
    if kind in ("set_value", "click_element") and "selector" not in a:
        raise ValueError("selector is required")
    if kind == "click_text" and (not isinstance(a.get("text"), str) or not a["text"]):
        raise ValueError("click_text requires nonempty text")
    if kind == "click_text":
        import uia_backend
        uia_backend.validate_selector({"name": a["text"], **({"role": a["role"]} if "role" in a else {})})
    if "replace" in a and not isinstance(a["replace"], bool):
        raise ValueError("replace must be boolean")
    if kind in ("click", "move"):
        for coordinate in ("x", "y"):
            if not isinstance(a.get(coordinate), int) or isinstance(a[coordinate], bool):
                raise ValueError("x and y must be physical desktop pixel integers")
        if a.get("button", "left") not in ("left", "right"):
            raise ValueError("Unsupported mouse button")
    if kind == "scroll" and (not isinstance(a.get("delta", -120), int) or isinstance(a.get("delta"), bool)):
        raise ValueError("delta must be an integer")
    if kind == "press_keys":
        if not isinstance(a.get("keys"), list) or not 1 <= len(a["keys"]) <= 8:
            raise ValueError("keys must be a list of 1 to 8 key names")
        for name in a["keys"]:
            if not isinstance(name, str):
                raise ValueError("Key names must be strings")
            upper = name.upper()
            if upper not in KEYS and not (len(name) == 1 and 32 <= ord(name) <= 0xFFFF and not 0xD800 <= ord(name) <= 0xDFFF):
                raise ValueError("Unknown key: " + name)
        resolve_keys(a['keys'],a.get('window_id'))
    if kind == "launch_app":
        import launcher
        launcher.normalize(a)
    if "settle_ms" in a and (not isinstance(a["settle_ms"], int) or isinstance(a["settle_ms"], bool) or not 0 <= a["settle_ms"] <= 1000):
        raise ValueError("settle_ms must be an integer in range 0–1000 milliseconds; use repeated inspection for longer waits")
    if kind in ("type_text", "set_value") and a.get("method", "uia") not in ("uia", "keyboard"):
        raise ValueError("Input method must be uia or keyboard")
    if kind == "set_value" and a.get("method", "uia") != "uia":
        raise ValueError("set_value uses UI Automation; use type_text for keyboard input")
    if kind in ("click_text", "click_element") and a.get("method", "click") not in ("click", "invoke"):
        raise ValueError("Click method must be click or invoke")


def click(x, y, hwnd=None, button="left"):
    left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    width, height = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    if not (left <= x < left + width and top <= y < top + height):
        raise ValueError("Point outside desktop")
    hit = target_at(x, y)
    if hwnd is not None:
        if hit["root_window_id"] != hwnd:
            raise TargetError(f"Click blocked: point belongs to window {hit['root_window_id']}, target is {hwnd}")
    if not u.SetCursorPos(x, y):
        raise RuntimeError("Cannot move pointer")
    if hwnd is not None:
        if target_at(x, y)["root_window_id"] != hwnd:
            raise TargetError("Window under cursor changed before click")
    flags = {"left": (2, 4), "right": (8, 16)}[button]
    send([INPUT(type=0, mi=MOUSEINPUT(dwFlags=f)) for f in flags])
    return hit


def drag(x, y, to_x, to_y, hwnd, target_hwnd, duration_ms=700):
    require_window(hwnd)
    require_window(target_hwnd)
    left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    right, bottom = left + u.GetSystemMetrics(78), top + u.GetSystemMetrics(79)
    for px, py in ((x, y), (to_x, to_y)):
        if not (left <= px < right and top <= py < bottom):
            raise ValueError("Drag point outside desktop")
    if target_at(x, y)["root_window_id"] != hwnd:
        raise TargetError("Drag source does not belong to window_id")
    if target_at(to_x, to_y)["root_window_id"] != target_hwnd:
        raise TargetError("Drop point does not belong to target_window_id")
    if not u.SetCursorPos(x, y):
        raise RuntimeError("Cannot move pointer")
    if target_at(x, y)["root_window_id"] != hwnd:
        raise TargetError("Drag source changed before button down")
    # Always attempt release, including failed motion, desktop loss or drop guard.
    try:
        send([INPUT(type=0, mi=MOUSEINPUT(dwFlags=2))])
        time.sleep(.2)
        steps = max(8, duration_ms // 16)
        for step in range(1, steps + 1):
            require_interactive_desktop()
            px = round(x + (to_x - x) * step / steps)
            py = round(y + (to_y - y) * step / steps)
            # Shell drag/drop also needs mouse input packets, not just warping
            # the cursor with SetCursorPos.
            dx = round((px-left)*65535/max(1,right-left-1))
            dy = round((py-top)*65535/max(1,bottom-top-1))
            send([INPUT(type=0, mi=MOUSEINPUT(dx=dx,dy=dy,dwFlags=0xC001))])
            time.sleep(duration_ms / steps / 1000)
        hit = target_at(to_x, to_y)
        if hit["root_window_id"] != target_hwnd:
            # Escape cancels shell drags before releasing at an unexpected target.
            press(["ESC"])
            raise TargetError("Drop target changed; drag cancelled")
        time.sleep(.1)
    finally:
        send([INPUT(type=0, mi=MOUSEINPUT(dwFlags=4))])
    return {"input_method":"physical_drag", "from":[x,y], "to":[to_x,to_y], "hit":hit}


def action(a):
    validate_action(a)
    require_interactive_desktop()
    kind = a["type"]
    hwnd = a.get("window_id")
    result = {"type": kind}
    if hwnd is not None:
        # Pointer targets are verified by hit-testing. Activating a shell flyout
        # before clicking can dismiss it; taskbar windows may reject activation.
        if kind in ("click", "click_text", "click_element", "move", "scroll", "drag"):
            require_window(hwnd)
        else:
            focus_window(hwnd)
        result["window_id"] = hwnd
    if kind == "drag":
        result.update(drag(a["x"], a["y"], a["to_x"], a["to_y"], hwnd,
                           a["target_window_id"], a.get("duration_ms",700)))
    elif kind == "launch_app":
        result.update(launch_app(a))
    elif kind == "focus_window":
        result.update(window=window_info(hwnd), foreground_verified=True)
    elif kind in ("click_text", "click_element"):
        import uia_backend as ui
        selector = a.get("selector", {"name": a.get("text"), **({"role": a["role"]} if a.get("role") else {})})
        ui.validate_selector(selector)
        if a.get("method", "click") == "invoke":
            result.update(ui.invoke(hwnd, selector))
        else:
            point = ui.click_point(hwnd, selector)
            result.update(input_method="physical_click", element=point["element"],
                          hit=click(point["x"], point["y"], hwnd))
    elif kind == "set_value" or (kind == "type_text" and "selector" in a and a.get("method", "uia") == "uia"):
        import uia_backend as ui
        require_foreground(hwnd)
        result.update(ui.set_value(hwnd, a["selector"], a["text"],
                                   append=kind == "type_text" and not a.get("replace", False)))
    elif kind == "type_text":
        if "selector" in a:
            import uia_backend as ui
            try:
                ui.focus_element(hwnd, a["selector"])
            except ui.UIError:
                point = ui.click_point(hwnd, a["selector"])
                click(point["x"], point["y"], hwnd)
                time.sleep(0.04)
                if not ui.selected(hwnd, a["selector"])["focused"]:
                    raise TargetError("Field did not receive focus after a verified physical click")
                ui.ready_keyboard(hwnd, a["selector"])
            initial_value = ui.keyboard_value(hwnd, a["selector"])
            if a.get("replace", False):
                require_foreground(hwnd)
                press(["CTRL", "A"])
        # Bind even legacy input to the current foreground instead of silently following focus.
        guarded_hwnd = hwnd if hwnd is not None else u.GetForegroundWindow()
        type_text(a["text"], guard=lambda: require_foreground(guarded_hwnd))
        if "selector" in a:
            focused = ui.selected(hwnd, a["selector"])
            if not focused["focused"]:
                raise TargetError("Requested field lost focus during input; inspect before retrying")
            if initial_value is not None and a.get("replace", False):
                result.update(ui.verify_keyboard(hwnd, a["selector"], a["text"]))
            else:
                result.update(verified=False, verification="not_available" if initial_value is None else "selection_unknown")
        result["input_method"] = "keyboard"
    elif kind == "press_keys":
        if hwnd is not None:
            require_foreground(hwnd)
        press(a["keys"],hwnd=hwnd)
    elif kind in ("click", "move"):
        x, y = a["x"], a["y"]
        if kind == "click":
            result["hit"] = click(x, y, hwnd, a.get("button", "left"))
        else:
            left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            if not (left <= x < left + u.GetSystemMetrics(78) and top <= y < top + u.GetSystemMetrics(79)):
                raise ValueError("Point outside desktop")
            if not u.SetCursorPos(x, y):
                raise RuntimeError("Cannot move pointer")
            result["hit"] = target_at(x, y)
    elif kind == "scroll":
        if hwnd is not None:
            point = cursor_state()
            if not point or point["root_window_id"] != hwnd:
                raise TargetError("Move cursor over the target window before scrolling")
        delta = max(-12000, min(12000, a.get("delta", -120)))
        send([INPUT(type=0, mi=MOUSEINPUT(mouseData=delta & 0xffffffff, dwFlags=0x800))])
    time.sleep(a.get("settle_ms", 30) / 1000)
    return result
