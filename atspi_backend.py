"""AT-SPI2 selectors scoped to a single top-level X11 window; same contract as uia_backend."""
import time

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

import linux_backend as lx

R = Atspi.Role
ROLE_MAP = {
    "button": {R.PUSH_BUTTON, R.TOGGLE_BUTTON},
    "checkbox": {R.CHECK_BOX},
    "combobox": {R.COMBO_BOX},
    "edit": {R.ENTRY, R.PASSWORD_TEXT, R.SPIN_BUTTON, R.TEXT},
    "hyperlink": {R.LINK},
    "listitem": {R.LIST_ITEM},
    "list": {R.LIST, R.LIST_BOX},
    "menuitem": {R.MENU_ITEM, R.CHECK_MENU_ITEM, R.RADIO_MENU_ITEM, R.MENU},
    "tab": {R.PAGE_TAB_LIST},
    "tabitem": {R.PAGE_TAB},
    "text": {R.LABEL, R.STATIC, R.HEADING},
    "tree": {R.TREE, R.TREE_TABLE},
    "treeitem": {R.TREE_ITEM},
    "group": {R.GROUPING, R.FORM, R.SECTION},
    "document": {R.DOCUMENT_FRAME, R.DOCUMENT_WEB, R.DOCUMENT_TEXT},
    "window": {R.FRAME, R.WINDOW, R.DIALOG, R.ALERT, R.FILE_CHOOSER},
    "pane": {R.PANEL, R.FILLER, R.SCROLL_PANE, R.SPLIT_PANE, R.VIEWPORT},
    "terminal": {R.TERMINAL},  # Linux-only: VTE terminals expose their screen text.
}
ROLES = {name: index for index, name in enumerate(ROLE_MAP)}
PROPERTIES = ("name", "automation_id", "class_name", "role")
MAX_VISIT = 20000
S = Atspi.StateType

ProviderError = GLib.Error


class UIError(RuntimeError):
    code = "ui_element_error"


class ElementNotFound(UIError):
    """A missing match is retryable during wait_for; ambiguity/provider errors are not."""


class NoTextInterface(UIError):
    """The element is editable but exposes no EditableText (Chromium inputs)."""


class AmbiguousSelector(UIError):
    code = "ambiguous_selector"

    def __init__(self, matches):
        super().__init__(f"Ambiguous selector: {matches} matches; add role or automation_id")
        self.details = {"matches": matches}


def transient_com_error(error):
    # Applications rebuild accessible objects while redrawing; D-Bus then reports
    # unknown objects or timeouts. Only read-only wait probes retry these.
    return isinstance(error, GLib.Error)


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
            validate_selector(value, depth + 1)
            continue
        if not isinstance(value, str) or not value:
            raise ValueError("Selector values must be nonempty strings")
        if key == "role" and value.lower() not in ROLES:
            raise ValueError("Unknown accessibility role")


# ---- element properties ---------------------------------------------------

def _states(acc):
    return acc.get_state_set()


def role_of(acc):
    role = acc.get_role()
    if role == R.TEXT and _states(acc).contains(S.MULTI_LINE):
        return "document"  # Multi-line editors behave like a Windows document control.
    for name, members in ROLE_MAP.items():
        if role in members:
            return name
    return acc.get_role_name() or str(int(role))


def _attributes(acc):
    try:
        return acc.get_attributes() or {}
    except GLib.Error:
        return {}


def automation_id(acc):
    attributes = _attributes(acc)
    if attributes.get("id"):
        return attributes["id"]  # Chromium exposes the DOM id here.
    try:
        return acc.get_accessible_id() or ""
    except (GLib.Error, AttributeError):
        return ""


def class_name(acc):
    attributes = _attributes(acc)
    return attributes.get("class") or attributes.get("tag") or ""


def _extents(acc):
    try:
        rect = acc.get_extents(Atspi.CoordType.SCREEN)
        if rect.x <= -(2 ** 30) or rect.y <= -(2 ** 30) or rect.width <= 0 or rect.height <= 0:
            return [0, 0, 0, 0]  # GTK reports INT_MIN for unrealized/hidden widgets.
        return [rect.x, rect.y, rect.x + rect.width, rect.y + rect.height]
    except (GLib.Error, AttributeError):
        return [0, 0, 0, 0]


def _text(acc):
    if "Text" not in acc.get_interfaces():
        return None
    try:
        return Atspi.Text.get_text(acc, 0, -1) or ""
    except GLib.Error:
        return None


def _selected_name(acc, budget=200):
    """Name of the selected option inside a combobox (Chromium <select>, GTK combo)."""
    stack = _children(acc)
    while stack and budget > 0:
        budget -= 1
        child = stack.pop()
        try:
            if _states(child).contains(S.SELECTED) and child.get_name():
                return child.get_name()
            stack.extend(_children(child))
        except GLib.Error:
            continue
    return None


def _value(acc, role):
    if role in ("edit", "document", "terminal"):
        return _text(acc)
    if role == "combobox":
        text = (_text(acc) or "").replace(OBJECT_CHAR, "").strip()
        return text or _selected_name(acc) or ""
    return None


OBJECT_CHAR = "￼"
NAME_FROM_CONTENT = {"listitem", "tabitem", "menuitem", "hyperlink", "button", "treeitem", "text"}


def _content_name(acc, depth=0):
    """UIA-style name from contents: join descendant names/text in order (e.g. 'example.org✕')."""
    if depth > 3:
        return ""
    parts = []
    for child in _children(acc):
        try:
            name = child.get_name() or ""
            parts.append(name if name else _content_name(child, depth + 1))
        except GLib.Error:
            continue
    joined = "".join(parts)
    if not joined:
        joined = (_text(acc) or "").replace(OBJECT_CHAR, "")
    return joined


def name_of(acc, role=None):
    name = acc.get_name() or ""
    if name:
        return name
    role = role or role_of(acc)
    return _content_name(acc) if role in NAME_FROM_CONTENT else ""


def describe(acc, depth=0):
    states = _states(acc)
    role = role_of(acc)
    bounds = _extents(acc)
    row = {
        "name": name_of(acc, role)[:300], "automation_id": automation_id(acc), "role": role,
        "class_name": class_name(acc), "depth": depth, "bounds": bounds,
        "enabled": states.contains(S.SENSITIVE) or states.contains(S.ENABLED),
        "offscreen": not states.contains(S.SHOWING) or bounds[2] <= bounds[0] or bounds[3] <= bounds[1],
        "focused": states.contains(S.FOCUSED),
    }
    if acc.get_role() == R.PASSWORD_TEXT:
        row["password"] = True
        return row
    value = _value(acc, role)
    if value is not None:
        row["value"] = value[:500]
        row["value_truncated"] = len(value) > 500
        row["read_only"] = not states.contains(S.EDITABLE)
    return row


# ---- window to accessible --------------------------------------------------

def _top_level(window_id):
    """Find the AT-SPI frame/window matching an X11 client or popup window."""
    lx.require_window(window_id)
    with lx.XLOCK:
        win = lx._window(window_id)
        pid = lx._pid(win)
        title = lx._title(win)
        client = lx._client_bounds(win)
    desktop = Atspi.get_desktop(0)
    candidates = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            if pid and app.get_process_id() != pid:
                continue
            for j in range(app.get_child_count()):
                top = app.get_child_at_index(j)
                if top is not None:
                    candidates.append(top)
        except GLib.Error:
            continue
    if not candidates:
        raise UIError("No accessible application for this window; the toolkit may not expose AT-SPI")
    exact = [c for c in candidates if _extents(c) == client]
    if len(exact) == 1:
        return exact[0]

    def inside(c):
        # GTK client-side decorations add an invisible shadow margin to the X
        # window, so the accessible frame lies inside the X bounds.
        left, top, right, bottom = _extents(c)
        return right > left and client[0] <= left and client[1] <= top and right <= client[2] and bottom <= client[3]

    pool = exact or [c for c in candidates if inside(c)] or candidates
    if len(pool) == 1:
        return pool[0]
    if title:
        named = [c for c in pool if (c.get_name() or "") == title]
        if len(named) == 1:
            return named[0]
    with lx.XLOCK:
        active = lx._active_id() == window_id
    if active:
        focused = [c for c in pool if _states(c).contains(S.ACTIVE)]
        if len(focused) == 1:
            return focused[0]
    showing = [c for c in pool if _states(c).contains(S.SHOWING)]
    if len(showing) == 1:
        return showing[0]
    raise UIError("Cannot match the X11 window to a unique accessible top-level; inspect windows again")


def _children(acc):
    try:
        count = acc.get_child_count()
    except GLib.Error:
        return []
    result = []
    for i in range(count):
        try:
            child = acc.get_child_at_index(i)
        except GLib.Error:
            continue
        if child is not None:
            result.append(child)
    return result


def _matches(acc, selector):
    for key, value in selector.items():
        if key == "within":
            continue
        if key == "name" and name_of(acc) != value:
            return False
        if key == "role" and role_of(acc) != value.lower():
            return False
        if key == "automation_id" and automation_id(acc) != value:
            return False
        if key == "class_name" and class_name(acc) != value:
            return False
    return True


def find(window_id, selector, allow_disabled=False, root=None):
    validate_selector(selector)
    if root is None:
        root = find(window_id, selector["within"], allow_disabled=True) if "within" in selector else _top_level(window_id)
    found, stack, visited = [], _children(root), 0
    while stack and visited < MAX_VISIT:
        acc = stack.pop()
        visited += 1
        try:
            if _matches(acc, selector):
                found.append(acc)
                if len(found) > 50:
                    break
            stack.extend(reversed(_children(acc)))
        except GLib.Error:
            continue  # The element disappeared during traversal.
    if not found:
        raise ElementNotFound("No matching element in target window; inspect the window again")
    if len(found) != 1:
        # GTK keeps hidden duplicates (popover menus, fallback header bars);
        # a single showing match is the one a user can see and operate.
        showing = [acc for acc in found if _states(acc).contains(S.SHOWING)]
        if len(showing) != 1:
            raise AmbiguousSelector(len(found))
        found = showing
    element = found[0]
    if element.get_role() == R.PASSWORD_TEXT:
        raise UIError("Password controls are not supported by this automation endpoint")
    states = _states(element)
    if not allow_disabled and not (states.contains(S.SENSITIVE) or states.contains(S.ENABLED)):
        raise UIError("Element is disabled")
    return element


# ---- public API (mirrors uia_backend) -------------------------------------

def inspect(window_id, max_depth=20, max_nodes=300):
    top = _top_level(window_id)
    rows, stack, truncated = [], [(top, 0)], False
    while stack and len(rows) < max_nodes:
        acc, depth = stack.pop()
        try:
            rows.append(describe(acc, depth))
            children = _children(acc)
            if depth >= max_depth:
                truncated = truncated or bool(children)
                continue
            room = max_nodes - len(rows)
            truncated = truncated or len(children) > room
            stack.extend((child, depth + 1) for child in reversed(children[:room]))
        except GLib.Error:
            truncated = True
    return {"window_id": window_id, "elements": rows, "truncated": truncated or bool(stack)}


def selected(window_id, selector, allow_disabled=False):
    return describe(find(window_id, selector, allow_disabled=allow_disabled))


def set_value(window_id, selector, text, append=False):
    element = find(window_id, selector)
    if not _states(element).contains(S.EDITABLE):
        raise UIError("Element is not editable")
    if "EditableText" not in element.get_interfaces():
        raise NoTextInterface("Element has no EditableText interface")
    wanted = ((_text(element) or "") if append else "") + text
    try:
        Atspi.Component.grab_focus(element)
    except GLib.Error:
        pass
    if not Atspi.EditableText.set_text_contents(element, wanted):
        raise UIError("Application refused set_text_contents; use method=keyboard")
    deadline, stable_since = time.monotonic() + 0.6, None
    while True:
        if _text(element) == wanted:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 0.08:
                return {"input_method": "atspi_text", "verified": True, "characters": len(wanted),
                        "element": describe(element)}
        else:
            stable_since = None
        if time.monotonic() >= deadline:
            raise UIError("Text interface did not retain the requested text; inspect the field before explicit method=keyboard,replace=true recovery; do not blindly retry")
        time.sleep(0.03)


def ready_keyboard(window_id, selector, timeout=0.8):
    deadline = time.monotonic() + timeout
    previous, stable_since = None, None
    while True:
        row = selected(window_id, selector)
        bounds = row["bounds"]
        ready = row["focused"] and row["enabled"] and not row["offscreen"]
        now = time.monotonic()
        if ready and bounds == previous:
            if stable_since is not None and now - stable_since >= 0.1:
                return row
        else:
            stable_since = now if ready else None
        previous = bounds if ready else None
        if now >= deadline:
            raise UIError("Field did not retain stable visible keyboard focus; inspect before retrying")
        time.sleep(0.025)


def focus_element(window_id, selector):
    element = find(window_id, selector)
    if describe(element)["offscreen"]:
        raise UIError("Element is offscreen; scroll it into view before typing")
    try:
        if not Atspi.Component.grab_focus(element):
            raise UIError("Element refused keyboard focus")
    except GLib.Error as error:
        raise UIError("Element refused keyboard focus") from error
    return ready_keyboard(window_id, selector)


def keyboard_value(window_id, selector):
    return _text(find(window_id, selector))


def verify_keyboard(window_id, selector, expected, timeout=0.6):
    deadline, stable_since = time.monotonic() + timeout, None
    while True:
        row = selected(window_id, selector)
        if not row["focused"]:
            raise UIError("Requested field lost focus during input; inspect before retrying")
        now = time.monotonic()
        if keyboard_value(window_id, selector) == expected:
            stable_since = stable_since or now
            if now - stable_since >= 0.08:
                return {"verified": True, "verification": "value", "element": row}
        else:
            stable_since = None
        if now >= deadline:
            error = UIError("Keyboard input value mismatch; input was sent once; inspect before retrying")
            error.details = {"input_sent": True, "verified": False, "verification": "value_mismatch"}
            raise error
        time.sleep(0.025)


def click_point(window_id, selector):
    element = find(window_id, selector)
    row = describe(element)
    if row["offscreen"]:
        raise UIError("Element is offscreen; scroll it into view before clicking")
    left, top, right, bottom = row["bounds"]
    return {"x": (left + right) // 2, "y": (top + bottom) // 2, "element": row}


def invoke(window_id, selector):
    element = find(window_id, selector)
    row = describe(element)
    if row["offscreen"]:
        raise UIError("Element is offscreen; scroll it into view before invoking")
    if "Action" not in element.get_interfaces():
        raise UIError("Element has no Action interface; use method=click")
    names = [Atspi.Action.get_action_name(element, i) for i in range(Atspi.Action.get_n_actions(element))]
    preferred = next((i for i, n in enumerate(names) if n in ("click", "press", "activate", "jump")), 0 if names else None)
    if preferred is None:
        raise UIError("Element exposes no actions; use method=click")
    Atspi.Action.do_action(element, preferred)
    return {"input_method": "atspi_action", "action": names[preferred], "element": row, "invoked": True,
            "verified": False, "verification": "not_requested", "effect_status": "unconfirmed",
            "guidance": "Action dispatched once; verify the application effect with wait_for."}
