# Agent rules for LLM Computer Use Bridge

Rules for an LLM agent driving a Windows or Linux desktop through this bridge. The HTTP/MCP contract is the same on both platforms; Linux differences are listed at the end.

## Connect once

Use the shipped MCP adapter and tools. Do not write a new HTTP client for GUI work or acceptance. client.py is the existing diagnostic CLI when MCP is unavailable; development regression tests may use its Client class.

On Linux create a venv and install requirements-mcp.txt. Transfer access.token securely to a protected file (chmod 600), then run the generator using that same venv:

    .venv/bin/python configure_mcp.py --token-file /secure/path/access.token

Copy the generated mcp.local.json entry into your MCP host configuration and reload its tools. It contains absolute interpreter/adapter paths, tunnel URL and token-file path, never the token itself. mcp.json is the checked-in template. Preserve other MCP servers in the host configuration.

Keep the tunnel running:

    ssh -N -L 18765:127.0.0.1:8765 WINDOWS_SSH_ALIAS

Use the owner's existing SSH alias/host. Do not invent credentials. Smoke test with the same settings:

    DESKTOP_BRIDGE_URL=http://127.0.0.1:18765 DESKTOP_BRIDGE_TOKEN_FILE=/secure/path/access.token .venv/bin/python check_mcp.py

## Text first

1. desktop_status, then desktop_windows. Resolve the real HWND, not a browser title mentioning the app.
2. desktop_inspect(window_id). Prefer inspect(selector) for subsequent reads of one element.
3. desktop_lease, then desktop_actions with window_id on each input action. Release in finally. Group known sequential actions; re-inspect after state transitions.
4. Physical pointer actions do not activate windows. Explicitly focus_window for an ordinary covered app before clicking. Taskbar and shell flyouts should be clicked directly with hit-tested selectors; activating them can dismiss them.
5. Use set_value with an exact selector. Verify resulting UI state through inspect; transport completion alone is not acceptance.
6. On 422, partial failure or timeout, inspect first. Never blindly replay. For a dirty field that rejects Value Pattern, explicitly choose type_text with selector, method=keyboard,replace=true and the complete desired text. It is slower; verify the result. No automatic keyboard fallback or extra writes.
7. Images only when needed: desktop_observe defaults to active-window JPEG crop up to 1280px. Keep observe_after=false. Use full desktop only to locate shell surfaces or when window data is insufficient. Pixel-to-desktop conversion uses origin + coordinate_scale * pixel.
8. Drag requires physical source/destination points and both HWNDs. Inspect the result after dropping. Never assume the application accepted a successful gesture.

## Acceptance rules

Timing: settle_ms is an integer in 0–1000 milliseconds inclusive (default 30). Larger values return 400 before input. Use desktop_wait_for(window_id,selector,timeout=10,state=visible) instead of repeated inspect. timeout=0..30 seconds; state=exists/visible/enabled/absent. enabled means visible and enabled. Optional value filters the exact current value (max 500 chars, not with absent). Check matched/status: standalone timeout is a normal result. Errors retain status=error, matched=false, elapsed_ms and attempts; ambiguity adds code=ambiguous_selector and matches. No lease is needed for standalone waiting; there are no screenshots or activation, and the desktop lock is released between probes. A closed HWND fails. UIA calls can exceed the polling deadline.

Use {type:wait_for,window_id:...,selector:...,state:enabled,timeout:20} as an actions step to build click → wait → click in one call. Keep a valid lease: every probe checks it. Full batch validation occurs before mutations. Batch timeout stops later actions with HTTP 408/code=wait_timeout; inspect completed_count/failed_index and the wait context. Never replay completed steps. While the batch retains its ordering lock, status/inspect/release remain available between probes. Increase HTTP client timeout for long batches.

Menus/dropdowns: desktop_windows(include_popups=true) exposes untitled popup HWNDs, class_name, process_id and owner. Find the popup associated with the target and inspect it; pointer actions use that popup HWND rather than the parent's covered coordinates. owner may be null, so never assume every untitled window belongs to the target. For keyboard mnemonics use the foreground owner HWND. press_keys accepts a single BMP character via VkKeyScanExW in that target thread's current layout, adding required modifiers. Unmapped characters fail; the bridge does not change layouts. Use the actual underlined character: Regedit Созд&ать uses а, not с.

Targeting: click, click_text, click_element, scroll, press_keys, type_text and set_value require window_id. move only positions the pointer: window_id is optional and does not constrain its destination. drag requires source window_id and target_window_id. Coordinates are physical desktop pixels.

Launch: {type:launch_app,path:chrome,args:[...],cwd:null,elevated:false,timeout:15}. path is an absolute executable, App Paths/PATH name, shortcut or URI/URL. Use an args list; no implicit shell. Legacy app aliases remain, but ALL launches default to the ordinary shell-user token. Explicit elevated:true is required for regedit and other admin launches; token acquisition failures never fall back to admin. Paths/arguments are logged privately. Shell associations may reuse existing processes or request Windows consent; do not automate UAC.

Check launch_status, actual elevated and requested_elevated, window_process_id and reused_window. Elevation can be null if unknown and a reused destination can differ from the requested worker token. window_ready means a correlated window was focused; started_no_window means the program started but no GUI was correlated before timeout. Do not replay a command merely because window_id is null. timeout is a 0.2..30s window wait plus a separate worker-dispatch budget up to 20s. A Chrome CDP test must use a dedicated profile and a free localhost port, leaving existing listeners alone. No guaranteed new instance/document.

During wait_for, known transient UIA/COM probe errors are retried until the same deadline; transient_errors counts them. This never replays clicks. A failed probe is not evidence of absent: a valid no-match probe must confirm absence. Unknown/permanent errors, ambiguity or a last probe still failing at the deadline return the original error context, including HRESULT for COM. Do not treat all COM errors as a successful wait or silently extend the deadline.

Prefer desktop_windows(owner=TARGET_HWND) / CLI windows --owner TARGET_HWND for a target's popup. It implies include_popups and filters direct owner only; it does not enumerate a whole process. Tiny/degenerate and known service windows are removed from popup discovery. Null/other owner windows must not be assumed to belong to the target.

Read results[].foreground (id/title/bounds) and foreground_changed after actions. The response also contains the last foreground snapshot. If a button opens a terminal, inspect that new HWND before continuing. observe(window_id=OLD_HWND) returns 409 with requested_window_id and current foreground, or null if none exists. Inspect the reported window before deciding to continue there or refocus the old app; do not replay blindly. Foreground snapshots are not focus locks. HWND labels stay in JSON, not on images; draw_cursor=false removes the cursor marker too.

Do not call the application's own HTTP API, business APIs, DevTools page JS or edit application data files to achieve a GUI task. This would bypass the executor being tested. Use the existing MCP tools. Do not remove user data; clean up only your own test records.

Desktop is shared with the human. Stop on focus/hit mismatch; re-inspect before continuing. UAC, lock screen or unavailable interactive desktop requires the human. Do not change UAC policies or automate consent.

focus_window no longer presses Alt or Escape. If direct activation and temporary input-thread attachment fail, it retries after a zero-distance mouse movement (no click or relocation). Do not add an unconditional Escape workaround: it can dismiss a user dialog. A denied focus still stops input. HOME/END/arrows/DELETE and INSERT/PGUP/PGDN use extended key events; Shift navigation is supported. UIA null values are returned as empty strings. A provider traversal failure is 422 ui_element_error, not a malformed-request 400.

Never publish tokens, raw desktop screenshots or private field values. For code changes, run relevant regressions and document actual evidence and remaining limits.

### Input delivery and application effects

`click_element` / `click_text` with `method=invoke` dispatches Invoke Pattern **once**. The response includes `invoked:true`, `verified:false`, `effect_status:unconfirmed`. A completed transport/batch step does not confirm the application's handler ran. In Chromium/WebView2, the first Invoke after navigation can be ignored; use an explicit `wait_for` for the intended effect. On timeout inspect before recovery; never automatically repeat a destructive operation. Prefer the default physical `method=click` for WebView controls when reliable pointer delivery is needed. Offscreen Invoke now fails before calling the provider: scroll explicitly and inspect again. There is no automatic scroll, refocus or second click.

Selector-based keyboard input waits up to 0.8s for enabled, visible, nonempty, stable bounds and continuous keyboard focus (100ms). `replace:true` plus Value Pattern verifies the full untruncated value for 80ms within a 0.6s budget after input. A mismatch returns `ui_element_error` with `input_sent:true`, `verified:false`, `verification:value_mismatch`; later batch steps stop. No text is resent and private values are excluded from errors. Without Value Pattern, `verified:false,verification:not_available`; without replace, caret/selection is unknown so `verification:selection_unknown`. These cases still need explicit application-state verification. Stable UIA focus is a readiness heuristic, not proof the application is ready; automatic verification detects dropped input rather than promising prevention. Existing COM calls can exceed timing budgets.

## Linux (X11) differences

- Backend: `linux_backend.py` (EWMH windows, XTest input) and `atspi_backend.py` (AT-SPI2 selectors). Window ids are X11 window ids.
- Same selectors and roles; extra role `terminal` (VTE): its `value` is the visible terminal text, so command output can be read without screenshots.
- `automation_id` is the DOM `id` in Chromium and the accessible id in GTK; `class_name` is the HTML tag or CSS class.
- When several elements match, the single *showing* one is chosen (GTK keeps hidden duplicates); otherwise `ambiguous_selector`.
- `set_value` on Chromium inputs (no EditableText) falls back to focused keyboard input with value verification and reports `input_method: keyboard_fallback`.
- `launch_app`: no elevation (`elevated:true` is rejected), `notepad` resolves to gedit, `regedit` is Windows-only. Chromium needs `--force-renderer-accessibility`; on Xvfb also `--disable-gpu --password-store=basic`.
- Wayland is not supported: XTest reaches only XWayland windows. Use an X11 session or the isolated test desktop in `scripts/linux/`.
