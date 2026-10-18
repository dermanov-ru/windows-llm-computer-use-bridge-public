"""Scripted tour of the bridge for screen recordings.

Runs the same scenario against a Windows or Linux bridge: text editor,
a web app driven through accessibility selectors with server-side waits,
a terminal and a window drag. It prints every request the "agent" sends and
what the desktop answered, so a recording shows both sides.

    DESKTOP_BRIDGE_TOKEN=... python3 examples/demo_tour.py --url http://127.0.0.1:18765 \
        --demo-url file:///C:/bridge-demo/index.html --chrome-profile C:/bridge-demo/chrome-profile
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import Client  # noqa: E402

DIM, BOLD, CYAN, GREEN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"


def say(text):
    print(f"\n{BOLD}{YELLOW}# {text}{RESET}", flush=True)
    time.sleep(0.6)


def brief(action):
    kind = action["type"]
    detail = {k: v for k, v in action.items() if k not in ("type", "window_id", "settle_ms")}
    if "text" in detail and len(detail["text"]) > 38:
        detail["text"] = detail["text"][:35] + "..."
    return f"{kind} {json.dumps(detail, ensure_ascii=False)}" if detail else kind


def title_drag(t, window, to_left, to_top):
    """Drag a window by an empty spot of its title bar so its left/top land at to_left/to_top.

    The bridge hit-tests both ends before pressing the button, so the drop
    target is whatever window currently owns the drop point."""
    left, top, right, bottom = window["bounds"]
    x, y = (left + right) // 2, top + 16
    to_x, to_y = x + to_left - left, y + to_top - top
    target = window["id"]
    if not (left <= to_x < right and top <= to_y < bottom):
        # Only when the drop point leaves the window: ask which window owns it now.
        hit = t.call("observe", {"scope": "desktop", "max_width": 128, "point": {"x": to_x, "y": to_y}})[0]["point_hit"]
        target = hit["root_window_id"] or target
    return {"type": "drag", "window_id": window["id"], "target_window_id": target,
            "x": x, "y": y, "to_x": to_x, "to_y": to_y, "duration_ms": 700}


def place(t, window_id, windows_side, to_left):
    """Bring a window into the recorded area before typing into it."""
    if windows_side:
        return [{"type": "focus_window", "window_id": window_id},
                {"type": "press_keys", "window_id": window_id, "keys": ["WIN", "RIGHT"], "settle_ms": 500},
                {"type": "press_keys", "window_id": window_id, "keys": ["ESC"]}]  # dismiss Snap Assist
    t.call("actions", {"lease_id": t.lease, "actions": [{"type": "focus_window", "window_id": window_id}]})
    bounds = next(w for w in t.call("windows")[0]["windows"] if w["id"] == window_id)
    return [title_drag(t, bounds, to_left, max(bounds["bounds"][1], 60))]


class Tour:
    def __init__(self, url, pace):
        self.c = Client(url)
        self.pace = pace
        self.lease = None

    def call(self, name, payload=None):
        started = time.monotonic()
        result = self.c.call(name, payload)  # None means GET (status, windows)
        return result, round((time.monotonic() - started) * 1000)

    def run(self, actions, label=None):
        print(f"{CYAN}→ POST /v1/actions{RESET}  {len(actions)} step(s)" + (f"  {DIM}{label}{RESET}" if label else ""), flush=True)
        for action in actions:
            print(f"   {DIM}{brief(action)}{RESET}", flush=True)
        result, ms = self.call("actions", {"lease_id": self.lease, "actions": actions})
        waits = [r for r in result["results"] if r["type"] == "wait_for"]
        extra = f", {len(waits)} wait(s) matched" if waits else ""
        print(f"{GREEN}← completed {result['completed_count']}/{len(actions)} in {ms} ms{extra}{RESET}", flush=True)
        time.sleep(self.pace)
        return result

    def wait(self, window_id, selector, state="visible", timeout=10, **kw):
        return {"type": "wait_for", "window_id": window_id, "selector": selector, "state": state, "timeout": timeout, **kw}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18765")
    p.add_argument("--demo-url", required=True, help="URL of examples/demo-app/index.html as seen by the desktop")
    p.add_argument("--chrome-profile", required=True, help="Dedicated Chrome profile directory on the desktop")
    p.add_argument("--pace", type=float, default=1.2, help="Pause after each request, seconds")
    p.add_argument("--left", type=int, default=420, help="Keep demo windows right of this x (recording crop)")
    args = p.parse_args()
    t = Tour(args.url, args.pace)

    say("Agent connects to the desktop bridge")
    status, ms = t.call("status")
    print(f"{CYAN}→ GET /v1/status{RESET}\n{GREEN}← {status['version']} on {status.get('platform', 'windows')}, "
          f"elevated={status['elevated']}, {status['windows']} windows ({ms} ms){RESET}", flush=True)
    windows_side = status.get("platform", "windows") == "windows"
    lease, _ = t.call("lease", {"ttl": 300})
    t.lease = lease["lease_id"]
    print(f"{CYAN}→ POST /v1/lease{RESET}\n{GREEN}← lease for 300 s: exclusive desktop control{RESET}", flush=True)
    time.sleep(t.pace)
    try:
        say("Open a text editor and type, Unicode included")
        res = t.run([{"type": "launch_app", "app": "notepad", "timeout": 25, **({} if windows_side else {"args": ["--new-window"]})}])
        editor = res["results"][0]["window_id"]
        lines = ["Hello from an LLM agent on Linux!", "Привет: ввод идёт через мост, а не через API приложения ✓",
                 "Selectors, waits and batches over HTTP."]
        steps = place(t, editor, windows_side, args.left) + [t.wait(editor, {"role": "document"}, "enabled", 5)]
        for i, line in enumerate(lines):
            if i:
                steps.append({"type": "press_keys", "window_id": editor, "keys": ["ENTER"]})
            steps.append({"type": "type_text", "window_id": editor, "text": line, "method": "keyboard"})
        t.run(steps, "one batch, one HTTP call")
        el, ms = t.call("inspect", {"window_id": editor, "selector": {"role": "document"}})
        print(f"{CYAN}→ POST /v1/inspect{RESET} role=document\n{GREEN}← text read back from the accessibility tree "
              f"({len(el['element'].get('value', ''))} chars, {ms} ms){RESET}", flush=True)
        time.sleep(t.pace)

        say("Drive a web app through accessibility selectors")
        chrome_args = [f"--user-data-dir={args.chrome_profile}", "--no-first-run", "--no-default-browser-check",
                       "--force-renderer-accessibility", "--disable-features=Translate,TranslateUI", "--window-size=1150,820",
                       f"--window-position={args.left + 40},60", args.demo_url]
        if not windows_side:
            # Xvfb has no GPU; the keyring prompt and "Chrome is outdated" tab would stall a test desktop.
            chrome_args[3:3] = ["--disable-gpu", "--password-store=basic", "--simulate-outdated-no-au=Tue, 31 Dec 2099 23:59:59 GMT"]
        res = t.run([{"type": "launch_app", "path": "chrome" if windows_side else "google-chrome", "args": chrome_args, "timeout": 30}])
        web = res["results"][0]["window_id"]
        X = lambda d: {"name": "✕", "role": "button", "within": {"role": "listitem", "name": f"{d}✕"}}
        t.run([t.wait(web, {"automation_id": "domInput"}, "enabled", 15),
               {"type": "set_value", "window_id": web, "selector": {"automation_id": "domInput"}, "text": "agent-demo.example"},
               {"type": "click_element", "window_id": web, "selector": {"automation_id": "domGroup"}},
               {"type": "press_keys", "window_id": web, "keys": ["HOME"]}, {"type": "press_keys", "window_id": web, "keys": ["DOWN"]},
               {"type": "press_keys", "window_id": web, "keys": ["ENTER"]},
               t.wait(web, {"automation_id": "btnDom"}, "enabled", 5),
               {"type": "click_element", "window_id": web, "selector": {"automation_id": "btnDom"}},
               t.wait(web, {"role": "listitem", "name": "agent-demo.example✕"}, "visible", 10)],
              "fill, pick group, click Add, wait until the row appears")
        t.run([{"type": "click_element", "window_id": web, "selector": {"automation_id": "tabLog"}},
               t.wait(web, {"role": "listitem", "name": "added domain agent-demo.example → CORP"}, "visible", 5),
               {"type": "click_element", "window_id": web, "selector": {"automation_id": "tabDomains"}},
               t.wait(web, X("agent-demo.example"), "visible", 5),
               {"type": "click_element", "window_id": web, "selector": X("agent-demo.example")},
               t.wait(web, {"role": "listitem", "name": "agent-demo.example✕"}, "absent", 10)],
              "check the log, delete the row, wait until it is gone")

        say("Run commands in a terminal")
        if windows_side:
            res = t.run([{"type": "launch_app", "path": "cmd", "args": ["/k", "prompt $G$S & title Bridge demo terminal"], "timeout": 2}])
            commands = ["echo Hello from the agent && ver", "for %i in (1 2 3) do @echo line-%i"]
        else:
            res = t.run([{"type": "launch_app", "path": "gnome-terminal", "args": ["--", "bash", "--norc", "--noprofile"], "timeout": 15}])
            commands = ['echo "Hello from the agent" && uname -sr', "for i in 1 2 3; do echo line-$i; done"]
        term = res["results"][0]["window_id"]
        deadline = time.monotonic() + 15
        while not term and time.monotonic() < deadline:
            # Windows 11 hands console programs to Windows Terminal, a separate
            # process, so launch_app cannot correlate the window: find it by title.
            term = next((w["id"] for w in t.call("windows")[0]["windows"] if "Bridge demo terminal" in w["title"]), None)
            time.sleep(0.3)
        steps = place(t, term, windows_side, args.left + 120)
        for command in commands:
            steps += [{"type": "type_text", "window_id": term, "text": command, "method": "keyboard"},
                      {"type": "press_keys", "window_id": term, "keys": ["ENTER"], "settle_ms": 400}]
        t.run(steps)
        time.sleep(t.pace)

        t.run([{"type": "focus_window", "window_id": term}, {"type": "type_text", "window_id": term, "text": "exit", "method": "keyboard"},
               {"type": "press_keys", "window_id": term, "keys": ["ENTER"], "settle_ms": 600}], "exit the terminal")

        say("Move a window with a real mouse drag")
        t.run([{"type": "focus_window", "window_id": editor}])
        window = next(w for w in t.call("windows")[0]["windows"] if w["id"] == editor)
        move = title_drag(t, window, window["bounds"][0] + 220, window["bounds"][1] + 160)
        t.run([dict(move, duration_ms=1200)], "physical drag, hit-tested at both ends")

        say("Clean up")
        t.run([{"type": "focus_window", "window_id": web}, {"type": "press_keys", "window_id": web, "keys": ["ALT", "F4"], "settle_ms": 600}],
              "close the browser")
        print(f"\n{BOLD}{GREEN}Done: every step went through the real desktop UI.{RESET}", flush=True)
    finally:
        t.call("release", {"lease_id": t.lease})


if __name__ == "__main__":
    main()
