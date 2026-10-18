import argparse
import hashlib
import json
import secrets
import threading
import time
import math
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from PIL import ImageGrab
import sys
import captures
if sys.platform == "win32":
    import win32_backend as desktop
    import uia_backend
    BACKEND_FILES = ("win32_backend.py", "uia_backend.py", "launcher.py")
else:
    import linux_backend as desktop
    import atspi_backend as uia_backend
    BACKEND_FILES = ("linux_backend.py", "atspi_backend.py")

ROOT = Path(__file__).resolve().parent
LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()
LEASE = {"id": None, "expires": 0}
VERSION = "0.3.0"
_build = hashlib.sha256()
for _name in ("server.py", "captures.py", *BACKEND_FILES):
    _build.update(_name.encode("ascii")+b"\0"+(ROOT/_name).read_bytes())
BUILD_ID = _build.hexdigest()[:16]  # Snapshot at process startup, not after later file edits.
OBSERVATION_KEYS = ("scope", "format", "max_width", "quality", "draw_cursor", "point", "window_id")


class ContextError(RuntimeError):
    def __init__(self,cause,details):
        super().__init__(str(cause));self.cause=cause;self.details=details


class WaitTimeout(RuntimeError):
    code="wait_timeout"


def error_details(error):
    # comtypes.COMError.details is a tuple, not our structured error context.
    details=getattr(error,"details",None)
    result=dict(details) if isinstance(details,dict) else {}
    if isinstance(error,uia_backend.ProviderError) and isinstance(getattr(error,"hresult",None),int):
        result.update(hresult=error.hresult,hresult_hex=f"0x{error.hresult & 0xFFFFFFFF:08X}")
    return result


def validate_wait(data):
    hwnd, selector = data.get("window_id"), data.get("selector")
    if not isinstance(hwnd,int) or isinstance(hwnd,bool):
        raise ValueError("window_id must be an integer")
    uia_backend.validate_selector(selector)
    timeout = data.get("timeout",10)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
        raise ValueError("timeout must be finite seconds from 0 to 30")
    state = data.get("state","visible")
    if state not in ("exists","visible","absent","enabled"):
        raise ValueError("state must be exists, visible, enabled or absent")
    if "value" in data and (not isinstance(data["value"],str) or len(data["value"])>500 or state=="absent"):
        raise ValueError("value must be a string of at most 500 characters; cannot combine with absent")
    return hwnd,selector,timeout,state


def wait_for(data,lease_data=None):
    started = time.monotonic()
    attempts = 0
    transient_errors = 0
    element=None
    try:
        hwnd,selector,timeout,state=validate_wait(data)
        deadline = started + timeout
        while True:
            probe_error=None
            # Serialize one probe, never the entire wait; actions may retain their
            # batch lock but status/inspect/release remain available.
            with LOCK:
                if lease_data is not None:require_lease(lease_data)
                desktop.require_interactive_desktop()
                desktop.require_window(hwnd)
                attempts += 1
                try:
                    element = uia_backend.selected(hwnd,selector,allow_disabled=True)
                except uia_backend.ElementNotFound:
                    element = None
                except uia_backend.ProviderError as error:
                    if not uia_backend.transient_com_error(error):raise
                    transient_errors += 1
                    element=None
                    probe_error=error
            matched = (element is None) if state=="absent" else (element is not None and (state=="exists" or not element["offscreen"]))
            if probe_error is not None:matched=False  # An unreadable tree is not evidence of absence.
            if state=="enabled":matched=bool(matched and element.get("enabled"))
            if "value" in data:matched=bool(matched and not element.get("value_truncated",False) and element.get("value")==data["value"])
            now = time.monotonic()
            result = {"status":"matched" if matched else "timeout", "matched":matched,
                      "window_id":hwnd,"state":state,"element":element,
                      "elapsed_ms":round((now-started)*1000),"attempts":attempts,"transient_errors":transient_errors}
            if now >= deadline and probe_error is not None:raise probe_error
            if matched or now >= deadline:return result
            time.sleep(min(.1,deadline-now))
    except Exception as error:
        details={"status":"error","matched":False,"window_id":data.get("window_id"),
                 "state":data.get("state","visible"),"element":element,
                 "elapsed_ms":round((time.monotonic()-started)*1000),"attempts":attempts,"transient_errors":transient_errors}
        details.update(error_details(error))
        raise ContextError(error,details) from error


def execute_actions(data):
    completed=None
    with ACTION_LOCK:
        try:
            with LOCK:
                require_lease(data)
                if "action" in data and "actions" in data:raise ValueError("Provide action or actions, not both")
                actions=data.get("actions",[data["action"]] if "action" in data else None)
                if not isinstance(actions,list) or not 1<=len(actions)<=64:raise ValueError("actions must contain 1 to 64 commands")
                options={k:data[k] for k in OBSERVATION_KEYS if k in data}
                captures.validate_options(options)
                if "observe_after" in data and not isinstance(data["observe_after"],bool):raise ValueError("observe_after must be boolean")
                for action in actions:
                    if isinstance(action,dict) and action.get("type")=="wait_for":validate_wait(action)
                    else:desktop.validate_action(action)
            completed=[]
            for action in actions:
                with LOCK:
                    require_lease(data)
                    before=desktop.active_window()
                if action["type"]=="wait_for":
                    step=wait_for(action,lease_data=data)
                    if not step["matched"]:
                        raise ContextError(WaitTimeout("wait_for timed out; remaining actions were not executed"),
                                           {**step,"status":"error","wait_status":"timeout"})
                    step["type"]="wait_for"
                else:
                    with LOCK:
                        require_lease(data)
                        step=dict(desktop.action(action))
                with LOCK:foreground=desktop.active_window()
                step.update(foreground=foreground,foreground_changed=(before or {}).get("id")!=(foreground or {}).get("id"))
                completed.append(step)
            result={"status":"completed","request_id":data.get("request_id"),"results":completed,
                    "completed_count":len(completed),"foreground":foreground}
            if data.get("observe_after",False):
                try:
                    with LOCK:result["observation"]=observe(options)
                except Exception as error:
                    status,code=error_status(error)
                    result["observation_error"]={"error":str(error),"code":code,"status":status,**error_details(error)}
            return result
        except Exception as error:
            if completed is None:raise
            details={**error_details(error),"results":completed,"completed_count":len(completed),
                     "failed_index":len(completed),"partial":bool(completed),
                     "outcome":"failed_action_may_have_side_effects","request_id":data.get("request_id"),
                     "foreground":desktop.active_window()}
            raise ContextError(error,details) from error


def observe(options=None):
    options = options or {}
    captures.validate_options(options)
    desktop.require_interactive_desktop()
    scope = options.get("scope", "active_window")
    origin = desktop.virtual_screen()[:2]
    active = desktop.active_window()
    requested = options.get("window_id")
    if requested is not None and (not active or active["id"] != requested):
        current = f"{active['id']} ({active['title']})" if active else "none"
        raise desktop.TargetError(f"Requested observation window {requested} is not foreground; current foreground: {current}",
                                  requested_window_id=requested, foreground=active)
    image = ImageGrab.grab(all_screens=True)
    if scope == "active_window":
        if active is None:
            raise desktop.TargetError("No active window; explicitly request scope=desktop if needed")
        left, top, right, bottom = active["bounds"]
        left, top = max(left, origin[0]), max(top, origin[1])
        right, bottom = min(right, origin[0] + image.width), min(bottom, origin[1] + image.height)
        if right <= left or bottom <= top:
            raise desktop.TargetError("Active window is outside the visible desktop")
        image = image.crop((left-origin[0], top-origin[1], right-origin[0], bottom-origin[1]))
        origin = [left, top]
        after = desktop.active_window()
        if not after or after["id"] != active["id"] or after["bounds"] != active["bounds"]:
            raise desktop.TargetError("Active window changed during capture; observe again",
                                      requested_window_id=requested, foreground=after)
    result = captures.encode(image, origin, options, desktop.cursor_state())
    result.update(scope=scope, active_window=active,
                  windows=desktop.windows() if scope == "desktop" else ([active] if active else []))
    if "point" in options:
        result["point_hit"] = desktop.target_at(options["point"]["x"], options["point"]["y"])
    return result


def require_lease(data):
    if LEASE["expires"] <= time.monotonic() or data.get("lease_id") != LEASE["id"]:
        raise LeaseError("Valid desktop lease required (expired or owned by another client)")


class LeaseError(RuntimeError):
    code = "lease_required"


def error_status(error):
    if isinstance(error,ContextError):return error_status(error.cause)
    if isinstance(error,WaitTimeout):return 408,error.code
    if isinstance(error, LeaseError):
        return 409, error.code
    if isinstance(error, desktop.TargetError):
        return 409, error.code
    if isinstance(error, PermissionError):
        return 423, "interactive_desktop_unavailable"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return 400, "invalid_request"
    if isinstance(error,uia_backend.UIError):
        return 422, error.code
    return 500, "execution_error"


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), fmt % args, flush=True)

    def reply(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        if not secrets.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")):
            return self.reply(401, {"error": "Unauthorized", "code": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1048576:
                return self.reply(413, {"error": "Body too large"})
            data = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            parsed=urlsplit(self.path)
            if (self.command,parsed.path) == ("POST","/v1/wait_for"):
                return self.reply(200,wait_for(data))
            if (self.command,parsed.path) == ("POST","/v1/actions"):
                return self.reply(200,execute_actions(data))
            with LOCK:
                route = (self.command, parsed.path)
                if route == ("GET", "/v1/status"):
                    result = {"version": VERSION, "build_id": BUILD_ID, "windows": len(desktop.windows()),
                              "lease_active": LEASE["expires"] > time.monotonic(), **desktop.runtime_status()}
                elif route == ("GET", "/v1/windows"):
                    query=parse_qs(parsed.query)
                    flag=query.get("include_popups",["false"])[0].lower()
                    if flag not in ("true","false","0","1"):raise ValueError("include_popups must be true or false")
                    owner=None
                    if "owner" in query:
                        try:owner=int(query['owner'][0])
                        except ValueError:raise ValueError("owner must be a positive HWND integer")
                        if owner<=0:raise ValueError("owner must be a positive HWND integer")
                    result = {"windows": desktop.windows(include_popups=flag in ("true","1"),owner=owner)}
                elif route == ("POST", "/v1/observe"):
                    result = observe(data)
                elif route == ("POST", "/v1/inspect"):
                    desktop.require_interactive_desktop()
                    hwnd = data.get("window_id")
                    desktop.require_window(hwnd)
                    depth, nodes = data.get("max_depth", 20), data.get("max_nodes", 300)
                    if not isinstance(depth, int) or not 1 <= depth <= 30:
                        raise ValueError("max_depth must be 1 to 30")
                    if not isinstance(nodes, int) or not 1 <= nodes <= 1000:
                        raise ValueError("max_nodes must be 1 to 1000")
                    if "selector" in data:
                        result = {"window_id": hwnd, "element": uia_backend.selected(hwnd, data["selector"])}
                    else:
                        result = uia_backend.inspect(hwnd, depth, nodes)
                elif route == ("POST", "/v1/lease"):
                    if LEASE["expires"] > time.monotonic():
                        return self.reply(409, {"error": "Desktop busy", "code": "desktop_busy"})
                    ttl = data.get("ttl", 60)
                    if not isinstance(ttl, int) or isinstance(ttl, bool):
                        raise ValueError("ttl must be integer seconds")
                    ttl = max(5, min(300, ttl))
                    LEASE.update(id=secrets.token_urlsafe(24), expires=time.monotonic()+ttl)
                    result = {"lease_id": LEASE["id"], "ttl": ttl}
                elif route == ("POST", "/v1/release"):
                    require_lease(data)
                    LEASE.update(id=None, expires=0)
                    result = {"status": "released"}
                else:
                    return self.reply(404, {"error": "Not found"})
            self.reply(200, result)
        except Exception as error:
            status, code = error_status(error)
            payload = {"error": str(error), "code": code}
            payload.update(error_details(error))
            self.reply(status, payload)


if __name__ == "__main__":
    if sys.platform == "win32":
        from admin_launcher import is_admin, request_elevation
        if not is_admin():
            request_elevation(Path(__file__).resolve(), sys.argv[1:])
            sys.exit(0)
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    token_path = ROOT/"access.token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.token = token_path.read_text().strip()
    print(f"Desktop Bridge {VERSION} listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()
