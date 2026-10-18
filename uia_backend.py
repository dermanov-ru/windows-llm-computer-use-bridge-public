"""UI Automation selectors scoped to a single top-level HWND; no application APIs."""
import time
import sys
from contextlib import contextmanager
sys.coinit_flags = 0
import comtypes
import comtypes.client

UIA = comtypes.client.GetModule("UIAutomationCore.dll")
ROLES = {
    "button": 50000, "checkbox": 50002, "combobox": 50003, "edit": 50004,
    "hyperlink": 50005, "listitem": 50007, "list": 50008, "menuitem": 50011,
    "tab": 50018, "tabitem": 50019, "text": 50020, "tree": 50023,
    "treeitem": 50024, "group": 50026, "document": 50030, "window": 50032,
    "pane": 50033,
}
PROPERTIES = {"name": 30005, "automation_id": 30011, "class_name": 30012, "role": 30003}


ProviderError = comtypes.COMError


class UIError(RuntimeError):
    code = "ui_element_error"


class ElementNotFound(UIError):
    """A missing match is retryable during wait_for; ambiguity/provider errors are not."""


class AmbiguousSelector(UIError):
    code = "ambiguous_selector"
    def __init__(self,matches):
        super().__init__(f"Ambiguous selector: {matches} matches; add role or automation_id")
        self.details={"matches":matches}


# Only read-only wait probes retry these provider/redraw/disconnection errors.
TRANSIENT_COM_HRESULTS={0x80040201,0x80010108,0x8001010A,0x80010001,
                       0x800401FD,0x800706BA,0x80131505,0x80004005}


def transient_com_error(error):
    return isinstance(error,comtypes.COMError) and (error.hresult & 0xFFFFFFFF) in TRANSIENT_COM_HRESULTS


@contextmanager
def session():
    initialized = False
    try:
        comtypes.CoInitializeEx(0)
        initialized = True
    except OSError as error:
        if getattr(error, "winerror", None) != -2147417850:
            raise
        # A caller may already own an STA apartment. Never uninitialize its COM.
    try:
        automation = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        yield automation
    finally:
        if initialized:
            comtypes.CoUninitialize()


def validate_selector(selector, depth=0):
    if not isinstance(selector, dict) or not selector:
        raise ValueError("selector must contain name, automation_id, role or class_name")
    if depth > 3:
        raise ValueError("Selector nesting must be at most 3")
    if set(selector) - (set(PROPERTIES) | {"within"}):
        raise ValueError("Unknown selector key")
    if not set(selector) & set(PROPERTIES):
        raise ValueError("Selector must identify an element, not only its container")
    for key, value in selector.items():
        if key == "within":
            validate_selector(value, depth+1)
            continue
        if not isinstance(value, str) or not value:
            raise ValueError("Selector values must be nonempty strings")
        if key == "role" and value.lower() not in ROLES:
            raise ValueError("Unknown UI Automation role")


def find(automation, window_id, selector, allow_disabled=False):
    validate_selector(selector)
    root = find(automation, window_id, selector["within"],allow_disabled=True) if "within" in selector else automation.ElementFromHandle(window_id)
    condition = None
    for key, value in selector.items():
        if key == "within":
            continue
        if key == "role":
            value = ROLES[value.lower()]
        part = automation.CreatePropertyCondition(PROPERTIES[key], value)
        condition = part if condition is None else automation.CreateAndCondition(condition, part)
    matches = root.FindAll(4, condition)
    if matches.Length == 0:
        raise ElementNotFound("No matching element in target window; inspect the window again")
    if matches.Length != 1:
        raise AmbiguousSelector(matches.Length)
    element = matches.GetElement(0)
    if element.CurrentIsPassword:
        raise UIError("Password controls are not supported by this automation endpoint")
    if not allow_disabled and not element.CurrentIsEnabled:
        raise UIError("Element is disabled")
    return element


def value_pattern(element):
    try:
        pattern = element.GetCurrentPattern(10002)
        return pattern.QueryInterface(UIA.IUIAutomationValuePattern) if pattern else None
    except comtypes.COMError:
        return None


def describe(element, depth=0):
    rect = element.CurrentBoundingRectangle
    role_id = element.CurrentControlType
    row = {
        "name": (element.CurrentName or "")[:300], "automation_id": element.CurrentAutomationId or "",
        "role": next((k for k, v in ROLES.items() if v == role_id), str(role_id)),
        "class_name": element.CurrentClassName or "", "depth": depth,
        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
        "enabled": bool(element.CurrentIsEnabled), "offscreen": bool(element.CurrentIsOffscreen),
        "focused": bool(element.CurrentHasKeyboardFocus),
    }
    if not element.CurrentIsPassword:
        value = value_pattern(element)
        if value:
            raw=value.CurrentValue or ""
            row["value"] = raw[:500]
            row["value_truncated"] = len(raw)>500
            row["read_only"] = bool(value.CurrentIsReadOnly)
    else:
        row["password"] = True
    return row


def inspect(window_id, max_depth=20, max_nodes=300):
    with session() as a:
        root = a.ElementFromHandle(window_id)
        walker = a.ControlViewWalker
        rows, stack = [], [(root, 0)]
        truncated = False
        while stack and len(rows) < max_nodes:
            element, depth = stack.pop()
            try:
                rows.append(describe(element, depth))
                if depth >= max_depth:
                    # Never walk an unbounded subtree just to report truncation.
                    truncated = truncated or bool(walker.GetFirstChildElement(element))
                    continue
                children = []
                child = walker.GetFirstChildElement(element)
                while child and len(children) + len(rows) < max_nodes:
                    children.append((child, depth + 1))
                    child = walker.GetNextSiblingElement(child)
                truncated = truncated or bool(child)
                stack.extend(reversed(children))
            except comtypes.COMError:
                truncated = True  # The application removed an element during traversal.
            except (TypeError, AttributeError) as error:
                # Provider/traversal failures are not malformed HTTP requests.
                raise UIError("UI Automation tree inspection failed; inspect again or narrow the selector") from error
        return {"window_id": window_id, "elements": rows, "truncated": truncated or bool(stack)}


def selected(window_id, selector, allow_disabled=False):
    with session() as a:
        return describe(find(a, window_id, selector, allow_disabled=allow_disabled))


def set_value(window_id, selector, text, append=False):
    with session() as a:
        element = find(a, window_id, selector)
        pattern = value_pattern(element)
        if not pattern or pattern.CurrentIsReadOnly:
            raise UIError("Element has no writable Value Pattern; use explicit keyboard input if appropriate")
        wanted = ((pattern.CurrentValue or "") if append else "") + text
        element.SetFocus()
        pattern.SetValue(wanted)
        deadline = time.monotonic() + 0.6
        stable_since = None
        while True:
            if pattern.CurrentValue == wanted:
                if stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= 0.08:
                    return {"input_method": "uia_value", "verified": True, "characters": len(wanted),
                            "element": describe(element)}
            else:
                stable_since = None
            if time.monotonic() >= deadline:
                raise UIError("Value Pattern did not retain the requested text; inspect the field before explicit method=keyboard,replace=true recovery; do not blindly retry")
            time.sleep(0.03)


def ready_keyboard(window_id, selector, timeout=0.8):
    """Re-resolve during transitions; require stable visible focused bounds before input."""
    deadline = time.monotonic()+timeout
    previous, stable_since = None, None
    while True:
        row = selected(window_id, selector)
        bounds = row["bounds"]
        ready = (row["focused"] and row["enabled"] and not row["offscreen"]
                 and bounds[2] > bounds[0] and bounds[3] > bounds[1])
        now = time.monotonic()
        if ready and bounds == previous:
            if stable_since is not None and now-stable_since >= 0.1:
                return row
        else:
            stable_since = now if ready else None
        previous = bounds if ready else None
        if now >= deadline:
            raise UIError("Field did not retain stable visible keyboard focus; inspect before retrying")
        time.sleep(0.025)


def focus_element(window_id, selector):
    with session() as a:
        element = find(a, window_id, selector)
        if element.CurrentIsOffscreen:
            raise UIError("Element is offscreen; scroll it into view before typing")
        element.SetFocus()
    return ready_keyboard(window_id, selector)


def keyboard_value(window_id, selector):
    with session() as a:
        element = find(a, window_id, selector)
        pattern = value_pattern(element)
        return (pattern.CurrentValue or "") if pattern else None


def verify_keyboard(window_id, selector, expected, timeout=0.6):
    deadline, stable_since = time.monotonic()+timeout, None
    while True:
        row = selected(window_id, selector)
        if not row["focused"]:
            raise UIError("Requested field lost focus during input; inspect before retrying")
        now = time.monotonic()
        if keyboard_value(window_id, selector) == expected:
            if stable_since is None:
                stable_since = now
            if now-stable_since >= 0.08:
                return {"verified": True, "verification": "value", "element": row}
        else:
            stable_since = None
        if now >= deadline:
            error = UIError("Keyboard input value mismatch; input was sent once; inspect before retrying")
            error.details = {"input_sent": True, "verified": False, "verification": "value_mismatch"}
            raise error
        time.sleep(0.025)


def click_point(window_id, selector):
    with session() as a:
        element = find(a, window_id, selector)
        if element.CurrentIsOffscreen:
            raise UIError("Element is offscreen; scroll it into view before clicking")
        rect = element.CurrentBoundingRectangle
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise UIError("Element has no clickable rectangle")
        return {"x": (rect.left + rect.right) // 2, "y": (rect.top + rect.bottom) // 2,
                "element": describe(element)}


def invoke(window_id, selector):
    with session() as a:
        element = find(a, window_id, selector)
        if element.CurrentIsOffscreen:
            raise UIError("Element is offscreen; scroll it into view before invoking")
        pattern = element.GetCurrentPattern(10000)
        if not pattern:
            raise UIError("Element has no Invoke Pattern; use method=click")
        metadata = describe(element)
        pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
        return {"input_method": "uia_invoke", "element": metadata, "invoked": True,
                "verified": False, "verification": "not_requested",
                "effect_status": "unconfirmed",
                "guidance": "Invoke dispatched once; verify the application effect with wait_for. WebView/Chromium may ignore Invoke; inspect before choosing method=click."}
