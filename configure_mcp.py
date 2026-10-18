"""Generate a ready-to-copy MCP config with absolute paths; never embed the token."""
import argparse
import json
import os
from pathlib import Path
import sys


def configuration(token_file, url="http://127.0.0.1:18765"):
    template = json.loads(Path(__file__).with_name("mcp.json").read_text(encoding="utf-8"))
    server = template["mcpServers"]["windows_desktop"]
    server.update(command=sys.executable, args=[str(Path(__file__).with_name("mcp_adapter.py").resolve())])
    server["env"] = {"DESKTOP_BRIDGE_URL": url,
                     "DESKTOP_BRIDGE_TOKEN_FILE": str(Path(token_file).expanduser().resolve())}
    return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--output", type=Path, default=Path("mcp.local.json"))
    args = parser.parse_args()
    if not Path(args.token_file).expanduser().is_file():
        parser.error("token file does not exist; transfer it securely first")
    config = configuration(args.token_file, args.url)
    # The config stores the token's path, never its content.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    print("MCP config written: " + str(args.output.resolve()))


if __name__ == "__main__":
    main()
