"""X11 desktop backend: EWMH windows, XTest input, same action contract as win32_backend."""
import math
import os
import shutil
import subprocess
import threading
import time

import Xlib.threaded  # noqa: F401  Request/reply handling is shared by HTTP handler threads.
from Xlib import X, XK, display, error as xerror, protocol
from Xlib.ext import xtest

XLOCK = threading.RLock()
_display = None


def dp():
    global _display
    if _display is None:
        if not os.environ.get("DISPLAY"):
            raise PermissionError("Interactive desktop unavailable: DISPLAY is not set (X11 session required)")
        try:
            _display = display.Display()
        except Exception as error:
            raise PermissionError(f"Interactive desktop unavailable: cannot open X display ({error})") from error
        if not _display.has_extension("XTEST"):
            raise PermissionError("X server lacks the XTEST extension required for input")
    return _display


def atom(name):
    return dp().intern_atom(name)


def root():
    return dp().screen().root


class TargetError(RuntimeError):
    code = "target_mismatch"

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


KEYS = {'CTRL': 'Control_L', 'ALT': 'Alt_L', 'SHIFT': 'Shift_L', 'WIN': 'Super_L', 'ENTER': 'Return',
        'ESC': 'Escape', 'TAB': 'Tab', 'BACKSPACE': 'BackSpace', 'DELETE': 'Delete', 'SPACE': 'space',
        'HOME': 'Home', 'END': 'End', 'LEFT': 'Left', 'UP': 'Up', 'RIGHT': 'Right', 'DOWN': 'Down',
        'INSERT': 'Insert', 'PGUP': 'Prior', 'PGDN': 'Next'}
KEYS.update({f'F{i}': f'F{i}' for i in range(1, 13)})


def virtual_screen():
    """[left, top, width, height] of the whole X screen, like Win32 SM_*VIRTUALSCREEN."""
    with XLOCK:
        geometry = root().get_geometry()
    return [0, 0, geometry.width, geometry.height]


def runtime_status():
    name = os.environ.get("DISPLAY")
    available = False
    try:
        with XLOCK:
            dp()
            available = True
    except PermissionError:
        pass
    return {"elevated": os.geteuid() == 0, "input_desktop": name, "interactive_desktop_available": available,
            "platform": "linux", "session_type": os.environ.get("XDG_SESSION_TYPE") or "x11"}


def require_interactive_desktop():
    with XLOCK:
        dp()


# ---- window helpers -------------------------------------------------------

def _prop(win, name, kind=X.AnyPropertyType):
    try:
        value = win.get_full_property(atom(name), kind)
    except xerror.XError:
        return None
    return value.value if value else None


def _window(wid):
    return dp().create_resource_object("window", wid)


def _title(win):
    raw = _prop(win, "_NET_WM_NAME", atom("UTF8_STRING"))
    if raw is None:
        try:
            raw = win.get_wm_name()
        except xerror.XError:
            raw = None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw or ""


def _pid(win):
    value = _prop(win, "_NET_WM_PID", X.AnyPropertyType)
    return int(value[0]) if value is not None and len(value) else None


def _client_bounds(win):
    geometry = win.get_geometry()
    origin = win.translate_coords(root(), 0, 0)
    left, top = -origin.x, -origin.y
    return [left, top, left + geometry.width, top + geometry.height]


def _frame_bounds(win):
    left, top, right, bottom = _client_bounds(win)
    extents = _prop(win, "_NET_FRAME_EXTENTS")
    if extents is not None and len(extents) == 4:
        el, er, et, eb = (int(v) for v in extents)
        return [left - el, top - et, right + er, bottom + eb]
    return [left, top, right, bottom]


def _states(win):
    value = _prop(win, "_NET_WM_STATE") or []
    return {dp().get_atom_name(a) for a in value}


def _viewable(win):
    try:
        return win.get_attributes().map_state == X.IsViewable
    except xerror.XError:
        return False


def _clients():
    """Managed top-level clients, topmost first (like EnumWindows)."""
    stacking = _prop(root(), "_NET_CLIENT_LIST_STACKING") or _prop(root(), "_NET_CLIENT_LIST") or []
    return [int(w) for w in reversed(list(stacking))]


def _active_id():
    value = _prop(root(), "_NET_ACTIVE_WINDOW")
    return int(value[0]) if value is not None and len(value) and value[0] else None


def _wm_class(win):
    try:
        cls = win.get_wm_class()
    except xerror.XError:
        cls = None
    return cls[1] if cls else ""


def _transient_for(win):
    value = _prop(win, "WM_TRANSIENT_FOR")
    return int(value[0]) if value is not None and len(value) and value[0] else None


def _override_popups(active):
    """Mapped override-redirect windows (menus, combobox popups, tooltips), topmost first."""
    rows = []
    for child in reversed(root().query_tree().children):
        try:
            attrs = child.get_attributes()
            if not attrs.override_redirect or attrs.map_state != X.IsViewable:
                continue
            bounds = _client_bounds(child)
        except xerror.XError:
            continue
        if bounds[2] - bounds[0] <= 1 or bounds[3] - bounds[1] <= 1:
            continue
        rows.append((child, bounds))
    return rows


def windows(include_popups=False, owner=None):
    if owner is not None and (not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0):
        raise ValueError("owner must be a positive window id integer")
    include_popups = include_popups or owner is not None
    with XLOCK:
        active = _active_id()
        result = []
        for wid in _clients():
            win = _window(wid)
            try:
                if not _viewable(win) or "_NET_WM_STATE_HIDDEN" in _states(win):
                    continue
                title = _title(win)
                if not title and not include_popups:
                    continue
                row = {"id": wid, "title": title, "bounds": _frame_bounds(win), "active": wid == active}
                if include_popups:
                    parent = _transient_for(win)
                    types = {dp().get_atom_name(a) for a in (_prop(win, "_NET_WM_WINDOW_TYPE") or [])}
                    popup = bool(parent) or bool(types & {"_NET_WM_WINDOW_TYPE_DIALOG", "_NET_WM_WINDOW_TYPE_POPUP_MENU",
                                                          "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_MENU"})
                    row.update(class_name=_wm_class(win), process_id=_pid(win), popup=popup, owner=parent)
                    if owner is not None and row["owner"] != owner:
                        continue
            except xerror.XError:
                continue  # The window vanished while being described.
            result.append(row)
        if include_popups:
            for win, bounds in _override_popups(active):
                popup_owner = _popup_owner(win, bounds, active)
                if owner is not None and popup_owner != owner:
                    continue
                result.insert(0, {"id": win.id, "title": _title(win), "bounds": bounds, "active": False,
                                  "class_name": _wm_class(win), "process_id": _pid(win), "popup": True,
                                  "owner": popup_owner})
        return result


def _popup_owner(win, bounds, active):
    """Override-redirect popups carry no owner; attribute them to the same-process active client."""
    transient = _transient_for(win)
    if transient:
        return transient
    pid = _pid(win)
    if active and pid and _pid(_window(active)) == pid:
        return active
    return active if pid is None else None


def active_window():
    with XLOCK:
        wid = _active_id()
        if not wid:
            return None
        win = _window(wid)
        try:
            return {"id": wid, "title": _title(win), "bounds": _frame_bounds(win), "active": True}
        except xerror.XError:
            return None


def _client_of(win):
    """Map any window (frame or descendant) to its managed client or override-redirect top."""
    clients = set(_prop(root(), "_NET_CLIENT_LIST") or [])
    if win.id in clients:
        return win.id
    # Reparenting WMs: the client is a descendant of the frame.
    stack = [win]
    while stack:
        current = stack.pop()
        if current.id in clients:
            return current.id
        try:
            stack.extend(current.query_tree().children)
        except xerror.XError:
            pass
    return win.id


def target_at(x, y):
    with XLOCK:
        r = root()
        reply = r.translate_coords(r, int(x), int(y))
        top = reply.child
        if not top:
            return {"x": int(x), "y": int(y), "window_id": None, "root_window_id": None}
        deepest = top
        for _ in range(32):  # Bounded descent: window trees are shallow.
            # translate_coords is called on the destination: the reply's child
            # is the child of `deepest` that contains the root point.
            nested = deepest.translate_coords(r, int(x), int(y)).child
            if not nested:
                break
            deepest = nested
        return {"x": int(x), "y": int(y), "window_id": deepest.id, "root_window_id": _client_of(top)}


def cursor_state():
    with XLOCK:
        pointer = root().query_pointer()
    return target_at(pointer.root_x, pointer.root_y)


def require_window(hwnd):
    if not isinstance(hwnd, int) or isinstance(hwnd, bool):
        raise ValueError("window_id must identify an existing window")
    with XLOCK:
        win = _window(hwnd)
        try:
            win.get_geometry()
        except xerror.XError:
            raise ValueError("window_id must identify an existing window")
        if not _viewable(win):
            raise TargetError("Target window is not visible")


def require_foreground(hwnd):
    with XLOCK:
        if _active_id() != hwnd:
            raise TargetError("Foreground changed; action stopped before sending further input")


def _send_root_message(win, name, data):
    event = protocol.event.ClientMessage(window=win, client_type=atom(name), data=(32, (data + [0] * 5)[:5]))
    root().send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    dp().flush()


def focus_window(hwnd, timeout=1.5):
    require_window(hwnd)
    with XLOCK:
        if _active_id() != hwnd:
            # Source indication 2 (pager) asks the WM to honour the request without focus-stealing prevention.
            _send_root_message(_window(hwnd), "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
    deadline = time.monotonic() + timeout
    while True:
        with XLOCK:
            if _active_id() == hwnd:
                break
        if time.monotonic() >= deadline:
            raise TargetError("Window manager denied foreground activation")
        time.sleep(0.025)
    return window_info(hwnd)


def window_info(hwnd):
    require_window(hwnd)
    return next((w for w in windows() if w["id"] == hwnd), {"id": hwnd})


# ---- input ----------------------------------------------------------------

def _keycode_for(keysym):
    """Return (keycode, needs_shift) or None when the keysym is not on the current map levels 0/1."""
    for keycode, index in dp().keysym_to_keycodes(keysym):
        if index in (0, 1):
            return keycode, index == 1
    return None


_spare = None


def _spare_keycode():
    """A keycode with no keysyms, used to type characters absent from the layout (like xdotool)."""
    global _spare
    if _spare is None:
        d = dp()
        low, high = d.display.info.min_keycode, d.display.info.max_keycode
        mapping = d.get_keyboard_mapping(low, high - low + 1)
        for offset, syms in enumerate(mapping):
            if not any(syms):
                _spare = low + offset
        if _spare is None:
            raise RuntimeError("No spare keycode available for Unicode input")
    return _spare


def _char_keysym(char):
    if char == "\n":
        return XK.string_to_keysym("Return")
    if char == "\t":
        return XK.string_to_keysym("Tab")
    code = ord(char)
    # Latin-1 keysyms equal their code points; everything else uses the Unicode keysym range.
    return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 + code


def _tap(keycode, shift=False):
    d = dp()
    shift_code = d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
    if shift:
        xtest.fake_input(d, X.KeyPress, shift_code)
    xtest.fake_input(d, X.KeyPress, keycode)
    xtest.fake_input(d, X.KeyRelease, keycode)
    if shift:
        xtest.fake_input(d, X.KeyRelease, shift_code)
    d.sync()


def _type_keysym(keysym):
    found = _keycode_for(keysym)
    if found:
        _tap(*found)
        return
    d = dp()
    spare = _spare_keycode()
    d.change_keyboard_mapping(spare, [(keysym, keysym)])
    d.sync()
    time.sleep(0.02)  # Clients must receive MappingNotify before the key event.
    try:
        _tap(spare)
        time.sleep(0.01)
    finally:
        d.change_keyboard_mapping(spare, [(0, 0)])
        d.sync()


def resolve_keys(keys, hwnd=None):
    """Validate key names/characters; returns keysyms in press order."""
    result = []
    for name in keys:
        upper = name.upper()
        if upper in KEYS:
            keysym = XK.string_to_keysym(KEYS[upper])
        elif len(name) == 1 and 32 <= ord(name) <= 0xFFFF and not 0xD800 <= ord(name) <= 0xDFFF:
            keysym = _char_keysym(name.lower() if name.isascii() else name)
        else:
            raise ValueError("Unknown key: " + name)
        if not keysym:
            raise ValueError("Character has no X keysym: " + name)
        if keysym not in result:
            result.append(keysym)
    return result


def press(keys, hwnd=None):
    with XLOCK:
        d = dp()
        codes = []
        for keysym in resolve_keys(keys, hwnd):
            found = _keycode_for(keysym)
            if not found:
                raise ValueError("Key is not mapped in the current keyboard layout; use type_text for characters")
            codes.append(found[0])
        try:
            for code in codes:
                xtest.fake_input(d, X.KeyPress, code)
            d.sync()
        finally:
            for code in reversed(codes):
                xtest.fake_input(d, X.KeyRelease, code)
            d.sync()


def type_text(text, guard=None):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for char in text:
        if guard:
            guard()
        with XLOCK:
            _type_keysym(_char_keysym(char))
        time.sleep(0.008)


def _desktop_contains(x, y):
    left, top, width, height = virtual_screen()
    return left <= x < left + width and top <= y < top + height


def _move(x, y):
    d = dp()
    xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
    d.sync()


def _button(number, down):
    d = dp()
    xtest.fake_input(d, X.ButtonPress if down else X.ButtonRelease, number)
    d.sync()


def click(x, y, hwnd=None, button="left"):
    if not _desktop_contains(x, y):
        raise ValueError("Point outside desktop")
    hit = target_at(x, y)
    if hwnd is not None and hit["root_window_id"] != hwnd:
        raise TargetError(f"Click blocked: point belongs to window {hit['root_window_id']}, target is {hwnd}")
    with XLOCK:
        _move(x, y)
    if hwnd is not None and target_at(x, y)["root_window_id"] != hwnd:
        raise TargetError("Window under cursor changed before click")
    number = {"left": 1, "right": 3}[button]
    with XLOCK:
        _button(number, True)
        _button(number, False)
    return hit


def drag(x, y, to_x, to_y, hwnd, target_hwnd, duration_ms=700):
    require_window(hwnd)
    require_window(target_hwnd)
    for px, py in ((x, y), (to_x, to_y)):
        if not _desktop_contains(px, py):
            raise ValueError("Drag point outside desktop")
    if target_at(x, y)["root_window_id"] != hwnd:
        raise TargetError("Drag source does not belong to window_id")
    if target_at(to_x, to_y)["root_window_id"] != target_hwnd:
        raise TargetError("Drop point does not belong to target_window_id")
    with XLOCK:
        _move(x, y)
    if target_at(x, y)["root_window_id"] != hwnd:
        raise TargetError("Drag source changed before button down")
    hit = None
    try:
        with XLOCK:
            _button(1, True)
        time.sleep(.2)
        steps = max(8, duration_ms // 16)
        for step in range(1, steps + 1):
            with XLOCK:
                _move(round(x + (to_x - x) * step / steps), round(y + (to_y - y) * step / steps))
            time.sleep(duration_ms / steps / 1000)
        hit = target_at(to_x, to_y)
        # Window moves change the stack under the pointer; only foreign targets cancel.
        if hit["root_window_id"] not in (target_hwnd, hwnd):
            press(["ESC"])
            raise TargetError("Drop target changed; drag cancelled")
        time.sleep(.1)
    finally:
        with XLOCK:
            _button(1, False)
    return {"input_method": "physical_drag", "from": [x, y], "to": [to_x, to_y], "hit": hit}


# ---- launch ---------------------------------------------------------------

ALIASES = {"notepad": ["gedit", "gnome-text-editor", "mousepad", "kate", "xed", "pluma"]}


def normalize_launch(spec):
    if 'path' in spec and 'app' in spec:
        raise ValueError('Provide path or legacy app, not both')
    path = spec.get('path', spec.get('app'))
    if not isinstance(path, str) or not path.strip() or '\0' in path or len(path) > 4096:
        raise ValueError('launch_app requires a nonempty path or app alias')
    if path == "regedit":
        raise ValueError("regedit is Windows-only")
    args = spec.get('args', [])
    if not isinstance(args, list) or len(args) > 256 or any(not isinstance(x, str) or '\0' in x for x in args):
        raise ValueError('args must be a list of at most 256 strings without NUL')
    cwd = spec.get('cwd')
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd)):
        raise ValueError('cwd must be null or an existing absolute directory')
    elevated = spec.get('elevated', False)
    if not isinstance(elevated, bool):
        raise ValueError('elevated must be boolean; default is false')
    if elevated:
        raise ValueError('elevated launch is not supported on Linux; run the program with ordinary user rights')
    timeout = spec.get('timeout', 8)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not .2 <= timeout <= 30:
        raise ValueError('timeout must be finite seconds from 0.2 to 30')
    return dict(path=path, args=list(args), cwd=cwd, elevated=elevated, timeout=timeout)


def _resolve(path):
    if path in ALIASES:
        for candidate in ALIASES[path]:
            found = shutil.which(candidate)
            if found:
                return found
        raise ValueError("No text editor found for alias notepad")
    if os.path.isabs(path):
        if not os.access(path, os.X_OK):
            raise ValueError("path is not an executable file")
        return path
    found = shutil.which(path)
    if not found:
        raise ValueError(f"Program not found in PATH: {path}")
    return found


def _descendants(pid):
    """pid and all its descendants, read from /proc."""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as stat:
                parent = int(stat.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(entry))
    found, stack = set(), [pid]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(children.get(current, []))
    return found


def launch_app(spec):
    spec = normalize_launch(spec)
    resolved = _resolve(spec["path"])
    before = {w["id"] for w in windows()}
    active_before = _active_id_safe()
    process = subprocess.Popen([resolved, *spec["args"]], cwd=spec["cwd"], start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    root_info = {"process_id": process.pid, "elevated": False, "resolved_path": resolved,
                 "launch_method": "exec", "worker_elevated": False}
    started = time.monotonic()
    deadline = started + spec["timeout"]
    while True:
        known = _descendants(process.pid)
        rows = windows()
        fresh = [w for w in rows if w["id"] not in before]
        ours = [w for w in fresh if _pid_safe(w["id"]) in known]
        # Single-instance apps (gedit, Chrome) hand the request to an existing process:
        # accept a new window that became active after our launch.
        delegated = time.monotonic() - started >= .2 and process.poll() is not None
        candidates = ours or ([w for w in fresh if w["active"]] if delegated or not ours else [])
        if not candidates and delegated:
            active = _active_id_safe()
            if active and active != active_before:
                candidates = [w for w in rows if w["id"] == active]
        if candidates:
            row = candidates[0]
            window = focus_window(row["id"])
            return {**root_info, "window_id": row["id"], "window": window, "window_process_id": _pid_safe(row["id"]),
                    "requested_elevated": False, "reused_window": row["id"] in before,
                    "foreground_verified": True, "launch_status": "window_ready"}
        if time.monotonic() >= deadline:
            return {**root_info, "window_id": None, "window_process_id": None, "requested_elevated": False,
                    "reused_window": False, "foreground_verified": False, "launch_status": "started_no_window"}
        time.sleep(.05)


def _pid_safe(wid):
    with XLOCK:
        try:
            return _pid(_window(wid))
        except xerror.XError:
            return None


def _active_id_safe():
    with XLOCK:
        return _active_id()


# ---- actions --------------------------------------------------------------

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
        import atspi_backend
        atspi_backend.validate_selector(a["selector"])
        if "window_id" not in a:
            raise ValueError("selector requires window_id")
    if kind in ("set_value", "click_element") and "selector" not in a:
        raise ValueError("selector is required")
    if kind == "click_text" and (not isinstance(a.get("text"), str) or not a["text"]):
        raise ValueError("click_text requires nonempty text")
    if kind == "click_text":
        import atspi_backend
        atspi_backend.validate_selector({"name": a["text"], **({"role": a["role"]} if "role" in a else {})})
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
        resolve_keys(a["keys"], a.get("window_id"))
    if kind == "launch_app":
        normalize_launch(a)
    if "settle_ms" in a and (not isinstance(a["settle_ms"], int) or isinstance(a["settle_ms"], bool) or not 0 <= a["settle_ms"] <= 1000):
        raise ValueError("settle_ms must be an integer in range 0–1000 milliseconds; use repeated inspection for longer waits")
    if kind in ("type_text", "set_value") and a.get("method", "uia") not in ("uia", "keyboard"):
        raise ValueError("Input method must be uia or keyboard")
    if kind == "set_value" and a.get("method", "uia") != "uia":
        raise ValueError("set_value uses the accessibility tree; use type_text for keyboard input")
    if kind in ("click_text", "click_element") and a.get("method", "click") not in ("click", "invoke"):
        raise ValueError("Click method must be click or invoke")


def action(a):
    import atspi_backend as ui
    validate_action(a)
    require_interactive_desktop()
    kind = a["type"]
    hwnd = a.get("window_id")
    result = {"type": kind}
    if hwnd is not None:
        if kind in ("click", "click_text", "click_element", "move", "scroll", "drag"):
            require_window(hwnd)
        else:
            focus_window(hwnd)
        result["window_id"] = hwnd
    if kind == "drag":
        result.update(drag(a["x"], a["y"], a["to_x"], a["to_y"], hwnd, a["target_window_id"], a.get("duration_ms", 700)))
    elif kind == "launch_app":
        result.update(launch_app(a))
    elif kind == "focus_window":
        result.update(window=window_info(hwnd), foreground_verified=True)
    elif kind in ("click_text", "click_element"):
        selector = a.get("selector", {"name": a.get("text"), **({"role": a["role"]} if a.get("role") else {})})
        ui.validate_selector(selector)
        if a.get("method", "click") == "invoke":
            result.update(ui.invoke(hwnd, selector))
        else:
            point = ui.click_point(hwnd, selector)
            result.update(input_method="physical_click", element=point["element"],
                          hit=click(point["x"], point["y"], hwnd))
    elif kind == "set_value" or (kind == "type_text" and "selector" in a and a.get("method", "uia") == "uia"):
        require_foreground(hwnd)
        append = kind == "type_text" and not a.get("replace", False)
        try:
            result.update(ui.set_value(hwnd, a["selector"], a["text"], append=append))
        except ui.NoTextInterface:
            # Chromium exposes Text but not EditableText: replace via focused keyboard input and verify.
            ui.focus_element(hwnd, a["selector"])
            wanted = ((ui.keyboard_value(hwnd, a["selector"]) or "") if append else "") + a["text"]
            if not append:
                press(["CTRL", "A"])
                press(["DELETE"])
            else:
                press(["END"])
            type_text(a["text"], guard=lambda: require_foreground(hwnd))
            result.update(ui.verify_keyboard(hwnd, a["selector"], wanted))
            result["input_method"] = "keyboard_fallback"
    elif kind == "type_text":
        initial_value = None
        if "selector" in a:
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
        guarded = hwnd if hwnd is not None else _active_id_safe()
        type_text(a["text"], guard=lambda: require_foreground(guarded))
        if "selector" in a:
            if not ui.selected(hwnd, a["selector"])["focused"]:
                raise TargetError("Requested field lost focus during input; inspect before retrying")
            if initial_value is not None and a.get("replace", False):
                result.update(ui.verify_keyboard(hwnd, a["selector"], a["text"]))
            else:
                result.update(verified=False, verification="not_available" if initial_value is None else "selection_unknown")
        result["input_method"] = "keyboard"
    elif kind == "press_keys":
        if hwnd is not None:
            require_foreground(hwnd)
        press(a["keys"], hwnd=hwnd)
    elif kind in ("click", "move"):
        x, y = a["x"], a["y"]
        if kind == "click":
            result["hit"] = click(x, y, hwnd, a.get("button", "left"))
        else:
            if not _desktop_contains(x, y):
                raise ValueError("Point outside desktop")
            with XLOCK:
                _move(x, y)
            result["hit"] = target_at(x, y)
    elif kind == "scroll":
        if hwnd is not None:
            point = cursor_state()
            if not point or point["root_window_id"] != hwnd:
                raise TargetError("Move cursor over the target window before scrolling")
        delta = max(-12000, min(12000, a.get("delta", -120)))
        # X11 wheel: button 4 scrolls up (positive Windows delta), 5 down; one notch per 120.
        number = 4 if delta > 0 else 5
        for _ in range(max(1, abs(delta) // 120)):
            with XLOCK:
                _button(number, True)
                _button(number, False)
    time.sleep(a.get("settle_ms", 30) / 1000)
    return result
