"""Portable Linux/Windows HTTP client. Only this module is needed for raw HTTP."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class Client:
    def __init__(self, url="http://127.0.0.1:8765", token=None, timeout=60):
        self.url = url.rstrip("/")
        token_file = Path(os.environ.get("DESKTOP_BRIDGE_TOKEN_FILE", str(Path(__file__).with_name("access.token")))).expanduser()
        self.token = token or os.environ.get("DESKTOP_BRIDGE_TOKEN") or token_file.read_text().strip()
        self.timeout = timeout

    def call(self, path, data=None):
        req = urllib.request.Request(
            self.url+"/v1/"+path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer "+self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return json.load(response)


def save_image(result, output=None):
    """Extract the top-level screenshot OR action's nested observation, once."""
    observation = result if "image_base64" in result else result.get("observation")
    if not observation or "image_base64" not in observation:
        return None
    suffix = ".jpg" if observation.get("mime_type") == "image/jpeg" else ".png"
    path = Path(output or "screen"+suffix)
    if path.suffix.lower() in (".png", ".jpg", ".jpeg") and path.suffix.lower() != suffix:
        path = path.with_suffix(suffix)
    path.write_bytes(base64.b64decode(observation["image_base64"], validate=True))
    observation.pop("image_base64")
    observation["saved_image"] = str(path)
    return path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("command", choices=["status", "windows", "observe", "inspect", "wait_for", "lease", "release", "actions"])
    source = p.add_mutually_exclusive_group()
    source.add_argument("--json", default="{}")
    source.add_argument("--json-file", help="UTF-8 JSON file; avoids nested SSH/PowerShell quoting")
    p.add_argument("--scope", choices=["active_window", "desktop"])
    p.add_argument("--format", choices=["jpeg", "png"])
    p.add_argument("--max-width", type=int, help="0 = native pixels, default server maximum = 1280")
    p.add_argument("--output")
    p.add_argument("--include-popups", action="store_true",help="Include untitled popup HWNDs in windows")
    p.add_argument("--owner",type=int,help="Only popup/owned windows of this HWND (implies --include-popups)")
    args = p.parse_args()
    try:
        data = json.loads(Path(args.json_file).read_text(encoding="utf-8-sig") if args.json_file else args.json)
        if not isinstance(data, dict):
            raise ValueError("JSON must be an object")
        for name in ("scope", "format", "max_width"):
            value = getattr(args, name)
            if value is not None:
                data[name] = value
        path="windows?include_popups=true" if args.command=="windows" and args.include_popups else args.command
        if args.command=="windows" and args.owner is not None:path=f"windows?include_popups=true&owner={args.owner}"
        result = Client(args.url, timeout=args.timeout).call(path, None if args.command in ("status", "windows") else data)
        save_image(result, args.output)
        # ASCII-safe JSON also survives legacy Windows console code pages over SSH.
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {"error": raw, "http_status": error.code}
        print(json.dumps(detail, ensure_ascii=True), file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
