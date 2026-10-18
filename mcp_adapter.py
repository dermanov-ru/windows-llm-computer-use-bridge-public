"""Optional stdio MCP adapter, including Linux through an SSH tunnel."""
import os
import base64
import urllib.error
from mcp.server.fastmcp import FastMCP, Image
from client import Client

mcp = FastMCP("Windows Desktop Bridge")
client = Client(os.environ.get("DESKTOP_BRIDGE_URL", "http://127.0.0.1:8765"))


def call(path, data=None):
    try:
        return client.call(path, data)
    except urllib.error.HTTPError as error:
        # Preserve completed_count/failed_index for the LLM, not only "HTTP 409".
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bridge HTTP {error.code}: {detail}") from error


def content(result):
    observation = result if "image_base64" in result else result.get("observation")
    if observation and "image_base64" in observation:
        image = Image(data=base64.b64decode(observation.pop("image_base64")),
                      format="jpeg" if observation["mime_type"] == "image/jpeg" else "png")
        return [result, image]
    return [result]


@mcp.tool()
def desktop_status() -> dict:
    return call("status")


@mcp.tool()
def desktop_windows(include_popups: bool = False, owner: int | None = None) -> dict:
    """Include visible untitled popups with class_name, process_id and owner HWND."""
    if owner is not None:return call(f"windows?include_popups=true&owner={owner}")
    return call("windows?include_popups=true" if include_popups else "windows")


@mcp.tool()
def desktop_observe(scope: str = "active_window", format: str = "jpeg", max_width: int = 1280,
                    window_id: int | None = None, draw_cursor: bool = True) -> list:
    """Explicit screenshot. Desktop clicks = origin + image pixel * coordinate_scale.
    Default active-window JPEG, width <=1280. Use format=png,max_width=0 for native pixels.
    """
    options = {"scope": scope, "format": format, "max_width": max_width, "draw_cursor": draw_cursor}
    if window_id is not None:
        options["window_id"] = window_id
    return content(call("observe", options))


@mcp.tool()
def desktop_inspect(window_id: int, selector: dict | None = None, max_nodes: int = 300, max_depth: int = 20) -> dict:
    """Read UI Automation roles, names, automation_ids, bounds and editable values.
    Supply selector to read a single element, otherwise returns a bounded tree.
    """
    data = {"window_id": window_id, "max_nodes": max_nodes, "max_depth": max_depth}
    if selector is not None:
        data["selector"] = selector
    return call("inspect", data)


@mcp.tool()
def desktop_wait_for(window_id: int, selector: dict, timeout: float = 10, state: str = "visible",value: str | None = None) -> dict:
    """Wait for a unique UIA selector, without images, focus changes or a lease.
    state=visible (default), exists, enabled (visible + enabled), absent; timeout=0..30 seconds.
    Check matched/status: a normal timeout is a result, not a tool failure.
    Ambiguous selectors/provider errors fail immediately; never replay mutations.
    """
    data={"window_id":window_id,"selector":selector,"timeout":timeout,"state":state}
    if value is not None:data['value']=value
    return call("wait_for",data)


@mcp.tool()
def desktop_lease(ttl: int = 60) -> dict:
    return call("lease", {"ttl": ttl})


@mcp.tool()
def desktop_action(lease_id: str, action: dict, observe_after: bool = False) -> list:
    """Invoke dispatch is unconfirmed (verified=false); verify effects with wait_for. Offscreen Invoke fails.
    Keyboard selector+replace verifies Value Pattern; mismatch means input_sent, never replay blindly.
    Drag/keyboard wait for exit from move/size and stable bounds; no-selector keyboard stays unverified.
    Console launch may report window_correlation=new_terminal_window_heuristic for a new Terminal host.
    Execute one action. No screenshot unless observe_after=true.
    Use window_id for physical input. set_value uses a UIA selector inside that window.
    click_text uses exact text plus optional role; ambiguous selectors fail.
    launch_app uses path/args/cwd; elevated defaults false, true must be explicit.
    Inspect launch_status and measured elevated; no window does not mean no launch.
    """
    return content(call("actions", {"lease_id": lease_id, "action": action, "observe_after": observe_after}))


@mcp.tool()
def desktop_actions(lease_id: str, actions: list[dict], observe_after: bool = False) -> list:
    """Serial batch (max 64); optional ONE screenshot at the end, default none.
    Stops at first failure, reports completed_count/failed_index; no rollback or blind replay.
    """
    return content(call("actions", {"lease_id": lease_id, "actions": actions, "observe_after": observe_after}))


@mcp.tool()
def desktop_release(lease_id: str) -> dict:
    return call("release", {"lease_id": lease_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
