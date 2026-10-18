"""Deterministic regressions. No desktop input or application changes."""
import base64
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import captures
from client import save_image
import client

if sys.platform == "win32":
    import server


class CaptureTests(unittest.TestCase):
    def test_cursor_badge_does_not_obscure_adjacent_pixels(self):
        source=Image.new("RGB",(200,100),"white")
        result=captures.encode(source,[0,0],{"format":"png"},{"x":40,"y":40,"root_window_id":12345})
        encoded=Image.open(io.BytesIO(base64.b64decode(result['image_base64'])))
        self.assertEqual(encoded.getpixel((60,55)),(255,255,255))
        self.assertEqual(result['cursor']['root_window_id'],12345)

    def test_mcp_child_receives_tunnel_and_token_configuration(self):
        if not importlib.util.find_spec("mcp"):
            self.skipTest("Optional MCP SDK not installed")
        import check_mcp
        settings={"DESKTOP_BRIDGE_URL":"http://127.0.0.1:18765", "DESKTOP_BRIDGE_TOKEN":"test-secret",
                  "DESKTOP_BRIDGE_TOKEN_FILE":"/secure/token", "UNRELATED_SECRET":"do-not-forward"}
        with patch.dict(check_mcp.os.environ,settings,clear=True):
            params=check_mcp.stdio_parameters()
        self.assertEqual(params.env,{k:v for k,v in settings.items() if k.startswith("DESKTOP_BRIDGE_")})
        self.assertNotIn("UNRELATED_SECRET",params.env)

    def test_generated_mcp_config_is_absolute_and_contains_no_token(self):
        import configure_mcp
        with tempfile.TemporaryDirectory() as folder:
            token=Path(folder)/"token"
            token.write_text("secret-value-must-not-be-in-config")
            result=configure_mcp.configuration(token)
            entry=result['mcpServers']['windows_desktop']
            self.assertTrue(Path(entry['command']).is_absolute())
            self.assertTrue(Path(entry['args'][0]).is_absolute())
            self.assertEqual(entry['env']['DESKTOP_BRIDGE_TOKEN_FILE'],str(token.resolve()))
            self.assertNotIn(token.read_text(),json.dumps(result))

    def test_client_token_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            token=Path(folder)/"token"; token.write_text("file-token\n")
            with patch.dict(client.os.environ,{"DESKTOP_BRIDGE_TOKEN_FILE":str(token)},clear=True):
                self.assertEqual(client.Client().token,"file-token")
                with patch.dict(client.os.environ,{"DESKTOP_BRIDGE_TOKEN":"env-token"}):
                    self.assertEqual(client.Client().token,"env-token")
                    self.assertEqual(client.Client(token="explicit").token,"explicit")

    def test_resized_coordinates_negative_origin_and_cursor(self):
        source = Image.new("RGB", (1920, 1080), "white")
        result = captures.encode(source, [-1920, -100], {}, {"x": -960, "y": 440, "root_window_id": 7})
        self.assertEqual((result["width"], result["height"]), (1280, 720))
        self.assertEqual(result["coordinate_scale"], [1.5, 1.5])
        self.assertEqual(result["cursor"]["image_position"], [640, 360])
        self.assertEqual(result["origin"][0] + 640 * result["coordinate_scale"][0], -960)
        self.assertTrue(base64.b64decode(result["image_base64"]).startswith(b"\xff\xd8"))
        self.assertEqual(result["mime_type"], "image/jpeg")

    def test_native_png_and_cursor_outside(self):
        result = captures.encode(Image.new("RGB", (100, 70)), [10, 20],
                                 {"format": "png", "max_width": 0}, {"x": 1, "y": 1})
        self.assertEqual(result["coordinate_scale"], [1, 1])
        self.assertFalse(result["cursor"]["in_capture"])
        self.assertIsNone(result["cursor"]["image_position"])
        self.assertTrue(base64.b64decode(result["image_base64"]).startswith(b"\x89PNG"))

    def test_invalid_options(self):
        for options in ({"scope": "all"}, {"max_width": -1}, {"max_width": True},
                        {"format": "gif"}, {"quality": 100}, {"point": {"x": 2}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                captures.validate_options(options)

    def test_nested_action_image_saved_once_with_correct_extension(self):
        image = captures.encode(Image.new("RGB", (20, 10)), [0, 0], {})
        response = {"status": "completed", "observation": image}
        with tempfile.TemporaryDirectory() as folder:
            path = save_image(response, str(Path(folder)/"nested.png"))
            self.assertEqual(path.suffix, ".jpg")
            self.assertTrue(path.read_bytes().startswith(b"\xff\xd8"))
            self.assertNotIn("image_base64", response["observation"])
            self.assertIsNone(save_image(response))

    def test_top_level_png_saved(self):
        response = captures.encode(Image.new("RGB", (20, 10)), [0, 0], {"format": "png"})
        with tempfile.TemporaryDirectory() as folder:
            path = save_image(response, str(Path(folder)/"top.png"))
            self.assertTrue(path.exists())
            self.assertEqual(response["saved_image"], str(path))

    def test_cli_json_survives_non_ascii_control_names(self):
        stream = io.StringIO()
        with patch.object(sys,"argv",["client.py","status"]), patch.object(client,"Client") as api, redirect_stdout(stream):
            api.return_value.call.return_value = {"title":"Проверка ✕ ✓"}
            self.assertEqual(client.main(),0)
        self.assertEqual(json.loads(stream.getvalue()),{"title":"Проверка ✕ ✓"})
        stream.getvalue().encode("ascii")

    def test_cli_error_json_preserves_partial_results_and_unicode(self):
        payload={"error":"Проверка ✕","completed_count":1,"failed_index":1}
        error=urllib.error.HTTPError("http://localhost",409,"Conflict",{},
            io.BytesIO(json.dumps(payload,ensure_ascii=False).encode("utf-8")))
        stream=io.StringIO()
        with patch.object(sys,"argv",["client.py","status"]), patch.object(client,"Client") as api, redirect_stderr(stream):
            api.return_value.call.side_effect=error
            self.assertEqual(client.main(),1)
        self.assertEqual(json.loads(stream.getvalue()),payload)
        stream.getvalue().encode("ascii")

    @unittest.skipUnless(importlib.util.find_spec("mcp"),"Optional MCP SDK not installed")
    def test_mcp_preserves_partial_failure_details(self):
        import mcp_adapter
        error=urllib.error.HTTPError("http://localhost",409,"Conflict",{},
            io.BytesIO(b'{"completed_count":1,"failed_index":1,"code":"target_mismatch"}'))
        with patch.object(mcp_adapter.client,"call",side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'"failed_index":1'):
                mcp_adapter.call("actions",{})


@unittest.skipUnless(sys.platform == "win32", "HTTP execution tests import the Win32 backend")
class RequestTests(unittest.TestCase):
    def setUp(self):
        server.LEASE.update(id=None, expires=0)
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.token = "local-unit-test"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:"+str(self.http.server_port)
        self.original_action = server.desktop.action
        self.action = patch.object(server.desktop, "action", return_value={"type": "move"}).start()
        patch.object(server.desktop,"require_interactive_desktop").start()
        patch.object(server.desktop,"active_window",return_value={"id":123,"title":"Test window"}).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def request(self, path, data=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer local-unit-test"
        req = urllib.request.Request(self.url+"/v1/"+path,
            data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def lease(self):
        return self.request("lease", {"ttl": 60})[1]["lease_id"]

    def test_authentication_and_competing_lease(self):
        self.assertEqual(self.request("status", auth=False)[0], 401)
        lease = self.lease()
        self.assertEqual(self.request("lease", {})[0], 409)
        self.assertEqual(self.request("release", {"lease_id": "wrong"})[0], 409)
        self.assertEqual(self.request("release", {"lease_id": lease})[0], 200)

    def test_wait_for_retries_missing_then_matches_without_lease_or_image(self):
        import uia_backend as ui
        element={'name':'Ready','offscreen':False,'enabled':False}
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=[ui.ElementNotFound('missing'),element]) as selected, \
             patch.object(server,'observe') as observe:
            status,result=self.request('wait_for',{'window_id':123,'selector':{'name':'Ready'},'timeout':1})
        self.assertEqual(status,200)
        self.assertTrue(result['matched'])
        self.assertEqual(result['attempts'],2)
        self.assertEqual(result['element'],element)
        selected.assert_called_with(123,{'name':'Ready'},allow_disabled=True)
        observe.assert_not_called()
        self.action.assert_not_called()

    def test_wait_for_visibility_absence_and_zero_timeout(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'):
            with patch.object(ui,'selected',return_value={'offscreen':True}):
                status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'timeout':0})
                self.assertEqual(status,200);self.assertFalse(r['matched']);self.assertEqual(r['attempts'],1)
                self.assertTrue(self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'timeout':0,'state':'exists'})[1]['matched'])
            with patch.object(ui,'selected',side_effect=ui.ElementNotFound('missing')):
                r=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'state':'absent','timeout':0})[1]
                self.assertTrue(r['matched']);self.assertIsNone(r['element'])

    def test_wait_for_ambiguity_is_not_silently_retried(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=ui.UIError('Ambiguous selector')) as selected:
            status,result=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'timeout':10})
        self.assertEqual(status,422)
        selected.assert_called_once()

    def test_wait_for_validates_deadline_and_selector_before_probe(self):
        import uia_backend as ui
        with patch.object(ui,'selected') as selected:
            for change in ({'timeout':31},{'timeout':True},{'timeout':float('nan')},{'state':'unknown'},{'selector':{}}):
                status,_=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},**change})
                self.assertEqual(status,400)
        selected.assert_not_called()

    def test_wait_for_releases_lock_between_probes(self):
        import uia_backend as ui
        entered=threading.Event();resume=threading.Event();out=[]
        original_sleep=time.sleep
        def pause(seconds):
            entered.set();resume.wait(2)
            original_sleep(seconds)
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=[ui.ElementNotFound('missing'),{'offscreen':False}]), \
             patch.object(server.time,'sleep',side_effect=pause):
            waiter=threading.Thread(target=lambda:out.append(self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'timeout':2})))
            waiter.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request('status')[0],200)
            finally:
                resume.set();waiter.join(3)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(out[0][1]['matched'])

    def test_non_ascii_authorization_is_rejected_without_closing_connection(self):
        request=urllib.request.Request(self.url+"/v1/status",headers={"Authorization":"Bearer é"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,401)

    def test_batch_observes_once_and_preserves_results(self):
        lease = self.lease()
        with patch.object(server, "observe", return_value={"sample": True}) as observe:
            status, result = self.request("actions", {"lease_id": lease, "observe_after": True,
                "actions": [{"type": "move", "x": 1, "y": 2}, {"type": "press_keys", "window_id": 123, "keys": ["TAB"]}]})
            self.assertEqual(status, 200)
            self.assertEqual(result["completed_count"], 2)
            observe.assert_called_once()
            self.assertEqual(self.action.call_count, 2)

    def test_no_observation_by_default(self):
        with patch.object(server, "observe") as observe:
            status, result = self.request("actions", {"lease_id": self.lease(),
                "action": {"type": "move", "x": 1, "y": 2}})
            self.assertEqual(status, 200)
            self.assertNotIn("observation", result)
            observe.assert_not_called()

    def test_action_reports_new_foreground_without_screenshot(self):
        old={"id":123,"title":"Docker Desktop"}; new={"id":456,"title":"Windows Terminal"}
        with patch.object(server.desktop,"active_window",side_effect=[old,new]):
            status,result=self.request("actions",{"lease_id":self.lease(),"action":{"type":"move","x":1,"y":2}})
        self.assertEqual(status,200)
        self.assertEqual(result['foreground'],new)
        self.assertTrue(result['results'][0]['foreground_changed'])
        self.assertEqual(result['results'][0]['foreground'],new)
        self.assertNotIn('observation',result)

    def test_observe_mismatch_reports_requested_and_current_window_before_capture(self):
        with patch.object(server.desktop,"require_interactive_desktop"), \
             patch.object(server.desktop,"active_window",return_value={"id":456,"title":"Windows Terminal"}), \
             patch.object(server.ImageGrab,"grab") as grab:
            status,result=self.request('observe',{'window_id':123})
        self.assertEqual(status,409)
        self.assertEqual(result['requested_window_id'],123)
        self.assertEqual(result['foreground'],{'id':456,'title':'Windows Terminal'})
        self.assertIn('456',result['error'])
        self.assertIn('Windows Terminal',result['error'])
        grab.assert_not_called()

    def test_settle_range_error_names_limit_before_any_action(self):
        status,result=self.request('actions',{'lease_id':self.lease(),'action':{'type':'move','x':1,'y':2,'settle_ms':1500}})
        self.assertEqual(status,400)
        self.assertIn('0–1000',result['error'])
        self.action.assert_not_called()

    def test_invalid_later_action_prevents_all_input(self):
        status, _ = self.request("actions", {"lease_id": self.lease(),
            "actions": [{"type": "move", "x": 1, "y": 2}, {"type": "type_text"}]})
        self.assertEqual(status, 400)
        self.action.assert_not_called()

    def test_failure_reports_completed_prefix_not_success(self):
        self.action.side_effect = [{"type": "move"}, server.desktop.TargetError("another window covers point")]
        status, result = self.request("actions", {"lease_id": self.lease(),
            "actions": [{"type": "move", "x": 1, "y": 2}, {"type": "click", "window_id": 123, "x": 1, "y": 2}]})
        self.assertEqual(status, 409)
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["failed_index"], 1)
        self.assertEqual(result["code"], "target_mismatch")
        self.assertTrue(result["partial"])

    def test_screenshot_failure_does_not_turn_success_into_replayable_error(self):
        with patch.object(server, "observe", side_effect=server.desktop.TargetError("focus changed")):
            status, result = self.request("actions", {"lease_id": self.lease(), "observe_after": True,
                "action": {"type": "move", "x": 1, "y": 2}})
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["observation_error"]["code"], "target_mismatch")

    def test_lease_expiry_stops_remaining_batch(self):
        def expire(_):
            server.LEASE["expires"] = 0
            return {"type": "move"}
        self.action.side_effect = expire
        status, result = self.request("actions", {"lease_id": self.lease(),
            "actions": [{"type": "move", "x": 1, "y": 2}, {"type": "move", "x": 3, "y": 4}]})
        self.assertEqual(status, 409)
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(self.action.call_count, 1)

    def test_secure_desktop_failure_is_423(self):
        self.action.side_effect = PermissionError("secure desktop")
        status, result = self.request("actions", {"lease_id": self.lease(),
            "action": {"type": "move", "x": 1, "y": 2}})
        self.assertEqual(status, 423)
        self.assertEqual(result["code"], "interactive_desktop_unavailable")

    def test_single_and_batch_together_rejected(self):
        status, _ = self.request("actions", {"lease_id": self.lease(), "action": {},
                                           "actions": [{"type": "move", "x": 1, "y": 2}]})
        self.assertEqual(status, 400)
        self.action.assert_not_called()

    def test_target_guard_prevents_misrouted_click(self):
        with patch.object(server.desktop.u, "GetSystemMetrics", side_effect=lambda i: {76:0, 77:0, 78:1920, 79:1080}[i]), \
             patch.object(server.desktop, "require_foreground"), \
             patch.object(server.desktop, "target_at", return_value={"root_window_id": 999}), \
             patch.object(server.desktop, "send") as send, \
             patch.object(server.desktop.u, "SetCursorPos") as move:
            with self.assertRaises(server.desktop.TargetError):
                server.desktop.click(100, 100, hwnd=123)
            send.assert_not_called()
            move.assert_not_called()

    def test_maximized_window_not_restored(self):
        with patch.object(server.desktop, "require_window"), \
             patch.object(server.desktop.u, "IsIconic", return_value=False), \
             patch.object(server.desktop.u, "GetForegroundWindow", return_value=123), \
             patch.object(server.desktop.u, "ShowWindow") as show, \
             patch.object(server.desktop, "window_info", return_value={"id":123}):
            server.desktop.focus_window(123)
            show.assert_not_called()

    def test_navigation_chords_use_extended_down_and_up_packets(self):
        d=server.desktop
        for name in ('HOME','END','LEFT','UP','RIGHT','DOWN','DELETE','INSERT','PGUP','PGDN'):
            with self.subTest(name=name),patch.object(d,'send') as send:
                d.press(['SHIFT',name])
                down=send.call_args_list[0].args[0];up=send.call_args_list[1].args[0]
                self.assertEqual([(e.ki.wVk,e.ki.dwFlags) for e in down],[(16,0),(d.KEYS[name],1)])
                self.assertEqual([(e.ki.wVk,e.ki.dwFlags) for e in up],[(d.KEYS[name],3),(16,2)])

    def test_focus_fallback_attaches_and_detaches_without_sending_keys(self):
        d=server.desktop
        with patch.object(d,'require_window'),patch.object(d.u,'IsIconic',return_value=False), \
             patch.object(d.u,'GetForegroundWindow',side_effect=[111,111,111,123,123]), \
             patch.object(d.u,'SetForegroundWindow'),patch.object(d.u,'GetWindowThreadProcessId',return_value=20), \
             patch.object(d.kernel32,'GetCurrentThreadId',return_value=10),patch.object(d.u,'PeekMessageW'), \
             patch.object(d.u,'AttachThreadInput',return_value=True) as attach, \
             patch.object(d,'window_info',return_value={'id':123}),patch.object(d,'send') as send:
            self.assertEqual(d.focus_window(123),{'id':123})
            self.assertEqual([c.args for c in attach.call_args_list],[(10,20,True),(10,20,False)])
            send.assert_not_called()

    def test_focus_fallback_detaches_when_activation_raises(self):
        d=server.desktop
        with patch.object(d,'require_window'),patch.object(d.u,'IsIconic',return_value=False), \
             patch.object(d.u,'GetForegroundWindow',return_value=111), \
             patch.object(d.u,'SetForegroundWindow',side_effect=[None,RuntimeError('failure')]), \
             patch.object(d.u,'GetWindowThreadProcessId',return_value=20), \
             patch.object(d.kernel32,'GetCurrentThreadId',return_value=10),patch.object(d.u,'PeekMessageW'), \
             patch.object(d.u,'AttachThreadInput',return_value=True) as attach,patch.object(d,'send') as send:
            with self.assertRaises(RuntimeError):d.focus_window(123)
            self.assertEqual(attach.call_args.args,(10,20,False))
            send.assert_not_called()

    def test_focus_zero_motion_fallback_does_not_click_or_send_menu_keys(self):
        d=server.desktop
        with patch.object(d,'require_window'),patch.object(d.u,'IsIconic',return_value=False), \
             patch.object(d.u,'GetForegroundWindow',side_effect=[111,111,111,111,123]), \
             patch.object(d.u,'SetForegroundWindow'),patch.object(d.u,'GetWindowThreadProcessId',return_value=20), \
             patch.object(d.kernel32,'GetCurrentThreadId',return_value=10),patch.object(d.u,'PeekMessageW'), \
             patch.object(d.u,'AttachThreadInput',return_value=False),patch.object(d,'send') as send, \
             patch.object(d,'window_info',return_value={'id':123}):
            d.focus_window(123)
        packets=send.call_args.args[0]
        self.assertEqual(len(packets),1)
        self.assertEqual((packets[0].type,packets[0].mi.dx,packets[0].mi.dy,packets[0].mi.dwFlags),(0,0,0,1))

    def test_cyrillic_mnemonic_uses_target_layout_and_required_modifiers(self):
        d=server.desktop
        with patch.object(d.u,'GetWindowThreadProcessId',return_value=88) as thread, \
             patch.object(d.u,'GetKeyboardLayout',return_value=77) as layout, \
             patch.object(d.u,'VkKeyScanExW',return_value=0x143) as scan,patch.object(d,'send') as send:
            d.press(['С'],hwnd=123)
        thread.assert_called_once_with(123,None);layout.assert_called_once_with(88);scan.assert_called_once_with('С',77)
        self.assertEqual([(p.ki.wVk,p.ki.dwFlags) for p in send.call_args_list[0].args[0]],[(16,0),(67,0)])
        self.assertEqual([(p.ki.wVk,p.ki.dwFlags) for p in send.call_args_list[1].args[0]],[(67,2),(16,2)])

    def test_unmapped_mnemonic_sends_nothing(self):
        d=server.desktop
        with patch.object(d.u,'GetWindowThreadProcessId',return_value=88), \
             patch.object(d.u,'GetKeyboardLayout',return_value=77),patch.object(d.u,'VkKeyScanExW',return_value=-1), \
             patch.object(d,'send') as send:
            with self.assertRaises(ValueError):d.press(['с'],hwnd=123)
        send.assert_not_called()

    def test_popup_enumeration_includes_untitled_menu_and_owner(self):
        d=server.desktop
        from types import SimpleNamespace as NS
        def cls(hwnd,buffer,size):buffer.value='#32768';return 6
        def thread(hwnd,pid):pid._obj.value=77;return 88
        def rect(hwnd,ptr):ptr._obj.right=200;ptr._obj.bottom=100;return True
        with patch.object(d.u,'EnumWindows',side_effect=lambda callback,param:callback(999,0)), \
             patch.object(d.u,'GetWindowTextLengthW',return_value=0),patch.object(d.u,'IsWindowVisible',return_value=True), \
             patch.object(d.u,'GetWindowTextW'),patch.object(d.u,'GetWindowRect',side_effect=rect), \
             patch.object(d.u,'GetForegroundWindow',return_value=123),patch.object(d.u,'GetClassNameW',side_effect=cls), \
             patch.object(d.u,'GetWindowThreadProcessId',side_effect=thread),patch.object(d.u,'GetWindow',return_value=0), \
             patch.object(d,'gui_thread_info',return_value=NS(hwndMenuOwner=123,hwndActive=123)):
            self.assertEqual(d.windows(),[])
            row=d.windows(include_popups=True)[0]
        self.assertEqual((row['id'],row['owner'],row['class_name'],row['process_id'],row['popup']),(999,123,'#32768',77,True))

    def test_transient_com_probe_is_not_absence_and_next_valid_probe_matches(self):
        import uia_backend as ui
        error=ui.comtypes.COMError(0x80040201-2**32,'Element destroyed during redraw',('description','source',None,0,None))
        with patch.object(server.desktop,'require_window'), \
             patch.object(ui,'selected',side_effect=[error,ui.ElementNotFound('missing')]) as selected:
            status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'state':'absent','timeout':1})
        self.assertEqual(status,200);self.assertTrue(r['matched'])
        self.assertEqual((r['attempts'],r['transient_errors'],selected.call_count),(2,1,2))

    def test_batch_retries_only_wait_probe_not_previous_mutations(self):
        import uia_backend as ui
        error=ui.comtypes.COMError(0x80040201-2**32,'Redraw',('description','source',None,0,None))
        with patch.object(server.desktop,'require_window'), \
             patch.object(ui,'selected',side_effect=[error,{'enabled':True,'offscreen':False}]):
            status,r=self.request('actions',{'lease_id':self.lease(),'actions':[{'type':'move','x':1,'y':2},
                {'type':'wait_for','window_id':123,'selector':{'name':'item'},'state':'enabled','timeout':1},
                {'type':'move','x':3,'y':4}]})
        self.assertEqual(status,200);self.assertEqual(r['completed_count'],3)
        self.assertEqual(r['results'][1]['transient_errors'],1)
        self.assertEqual(self.action.call_count,2)

    def test_com_failure_keeps_original_message_hresult_and_error_context(self):
        import uia_backend as ui
        for hresult in (0x80070005,0x80040201):
            with self.subTest(hresult=hresult),patch.object(server.desktop,'require_window'), \
                 patch.object(ui,'selected',side_effect=ui.comtypes.COMError(hresult-2**32,'Original COM failure',('tuple','source',None,0,None))):
                status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'item'},'state':'absent','timeout':0})
            self.assertEqual(status,500)
            self.assertIn('Original COM failure',r['error'])
            self.assertEqual(r['hresult_hex'],f'0x{hresult:08X}')
            self.assertEqual((r['status'],r['matched'],r['attempts']),('error',False,1))

    def test_tuple_com_details_survive_action_inspect_and_capture_errors(self):
        import uia_backend as ui
        error=ui.comtypes.COMError(0x80070005-2**32,'Original provider denied',('tuple','source',None,0,None))
        self.action.side_effect=error
        status,r=self.request('actions',{'lease_id':self.lease(),'action':{'type':'move','x':1,'y':2}})
        self.assertEqual(status,500);self.assertIn('Original provider denied',r['error'])
        self.assertEqual(r['failed_index'],0)
        self.request('release',{'lease_id':server.LEASE['id']})
        with patch.object(server.desktop,'require_window'),patch.object(ui,'selected',side_effect=error):
            status,r=self.request('inspect',{'window_id':123,'selector':{'name':'x'}})
        self.assertEqual(status,500);self.assertIn('Original provider denied',r['error'])
        self.action.side_effect=None
        with patch.object(server,'observe',side_effect=error):
            status,r=self.request('actions',{'lease_id':self.lease(),'action':{'type':'move','x':1,'y':2},'observe_after':True})
        self.assertEqual(status,200);self.assertIn('Original provider denied',r['observation_error']['error'])
        self.assertEqual(r['observation_error']['hresult_hex'],'0x80070005')

    def test_popup_noise_filter_and_owner_filter_preserve_real_menus(self):
        d=server.desktop
        from types import SimpleNamespace as NS
        def thread(hwnd,pid):pid._obj.value=77;return 88
        def cls(hwnd,buffer,size):buffer.value={1:'WorkerW',2:'DummyDWMListenerWindow',3:'#32768',4:'#32768',5:'Popup'}[hwnd];return len(buffer.value)
        def rect(hwnd,ptr):ptr._obj.right=1 if hwnd==5 else 200;ptr._obj.bottom=100;return True
        def enumerate_windows(callback,param):
            for hwnd in range(1,6):callback(hwnd,0)
            return True
        with patch.object(d.u,'EnumWindows',side_effect=enumerate_windows), \
             patch.object(d.u,'GetWindowTextLengthW',return_value=0),patch.object(d.u,'IsWindowVisible',return_value=True), \
             patch.object(d.u,'GetWindowTextW'),patch.object(d.u,'GetWindowRect',side_effect=rect), \
             patch.object(d.u,'GetForegroundWindow',return_value=123),patch.object(d.u,'GetClassNameW',side_effect=cls), \
             patch.object(d.u,'GetWindowThreadProcessId',side_effect=thread), \
             patch.object(d.u,'GetWindow',side_effect=lambda hwnd,flag:123 if hwnd==3 else 456):
            self.assertEqual([w['id'] for w in d.windows(include_popups=True)],[3,4])
            self.assertEqual([w['id'] for w in d.windows(owner=123)],[3])

    def test_windows_owner_query_validation_and_forwarding(self):
        with patch.object(server.desktop,'windows',return_value=[]) as windows:
            self.assertEqual(self.request('windows?owner=123')[0],200)
            windows.assert_called_once_with(include_popups=False,owner=123)
            self.assertEqual(self.request('windows?owner=bad')[0],400)
            self.assertEqual(self.request('windows?owner=-1')[0],400)

    def test_wait_ambiguity_has_uniform_context_and_match_count(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=ui.AmbiguousSelector(2)):
            status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'duplicate'},'timeout':5})
        self.assertEqual(status,422)
        self.assertEqual((r['status'],r['matched'],r['code'],r['matches'],r['attempts']),('error',False,'ambiguous_selector',2,1))
        self.assertIn('elapsed_ms',r)

    def test_wait_enabled_and_value_do_not_match_disabled_hidden_or_wrong_value(self):
        import uia_backend as ui
        values=[{'offscreen':False,'enabled':False,'value':'ready'}, {'offscreen':True,'enabled':True,'value':'ready'},
                {'offscreen':False,'enabled':True,'value':'old'},{'offscreen':False,'enabled':True,'value':'ready'}]
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=values):
            status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'button'},'state':'enabled','value':'ready','timeout':2})
        self.assertEqual(status,200);self.assertTrue(r['matched']);self.assertEqual(r['attempts'],4)

    def test_batch_wait_timeout_stops_before_next_mutation(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=ui.ElementNotFound('missing')):
            status,r=self.request('actions',{'lease_id':self.lease(),'actions':[{'type':'move','x':1,'y':2},
                {'type':'wait_for','window_id':123,'selector':{'name':'missing'},'timeout':0},
                {'type':'move','x':3,'y':4}]})
        self.assertEqual(status,408)
        self.assertEqual((r['code'],r['status'],r['completed_count'],r['failed_index']),('wait_timeout','error',1,1))
        self.assertFalse(r['matched']);self.assertEqual(self.action.call_count,1)

    def test_batch_wait_matches_then_executes_next_step(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',return_value={'offscreen':False,'enabled':True}):
            status,r=self.request('actions',{'lease_id':self.lease(),'actions':[
                {'type':'wait_for','window_id':123,'selector':{'name':'ready'},'state':'enabled','timeout':0},
                {'type':'move','x':1,'y':2}]})
        self.assertEqual(status,200);self.assertEqual(r['completed_count'],2)
        self.assertTrue(r['results'][0]['matched']);self.assertEqual(self.action.call_count,1)

    def test_invalid_wait_in_batch_prevents_earlier_mutation(self):
        status,r=self.request('actions',{'lease_id':self.lease(),'actions':[{'type':'move','x':1,'y':2},
            {'type':'wait_for','window_id':123,'selector':{'name':'x'},'timeout':31}]})
        self.assertEqual(status,400);self.action.assert_not_called()

    def test_batch_wait_checks_expired_lease_before_retry(self):
        import uia_backend as ui
        def expire(*args,**kwargs):
            server.LEASE['expires']=0
            raise ui.ElementNotFound('missing')
        with patch.object(server.desktop,'require_window'),patch.object(server.desktop,'require_interactive_desktop'), \
             patch.object(ui,'selected',side_effect=expire):
            status,r=self.request('actions',{'lease_id':self.lease(),'actions':[
                {'type':'wait_for','window_id':123,'selector':{'name':'x'},'timeout':2},{'type':'move','x':1,'y':2}]})
        self.assertEqual(status,409);self.assertEqual(r['code'],'lease_required');self.action.assert_not_called()

    def test_status_is_available_during_batch_wait(self):
        import uia_backend as ui
        entered=threading.Event();resume=threading.Event();out=[]
        sleep=time.sleep
        def pause(seconds):entered.set();resume.wait(2);sleep(seconds)
        with patch.object(server.desktop,'require_window'), \
             patch.object(ui,'selected',side_effect=[ui.ElementNotFound('missing'),{'offscreen':False}]), \
             patch.object(server.time,'sleep',side_effect=pause):
            lease=self.lease()
            waiter=threading.Thread(target=lambda:out.append(self.request('actions',{'lease_id':lease,'action':
                {'type':'wait_for','window_id':123,'selector':{'name':'ready'},'timeout':2}})))
            waiter.start()
            try:
                self.assertTrue(entered.wait(1));self.assertEqual(self.request('status')[0],200)
            finally:resume.set();waiter.join(3)
        self.assertFalse(waiter.is_alive());self.assertEqual(out[0][0],200)

    def test_wait_error_schema_for_invalid_timeout_has_zero_attempts(self):
        status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'x'},'timeout':31})
        self.assertEqual(status,400)
        self.assertEqual((r['status'],r['matched'],r['attempts']),('error',False,0))
        self.assertIn('elapsed_ms',r)

    def test_unmapped_mnemonic_in_batch_blocks_previous_actions(self):
        d=server.desktop
        with patch.object(d.u,'GetWindowThreadProcessId',return_value=88), \
             patch.object(d.u,'GetKeyboardLayout',return_value=77),patch.object(d.u,'VkKeyScanExW',return_value=-1):
            status,r=self.request('actions',{'lease_id':self.lease(),'actions':[{'type':'move','x':1,'y':2},
                {'type':'press_keys','window_id':123,'keys':['с']}]})
        self.assertEqual(status,400);self.action.assert_not_called()

    def test_wait_value_does_not_match_truncated_prefix(self):
        import uia_backend as ui
        with patch.object(server.desktop,'require_window'), \
             patch.object(ui,'selected',return_value={'offscreen':False,'value':'x'*500,'value_truncated':True}):
            status,r=self.request('wait_for',{'window_id':123,'selector':{'name':'x'},'value':'x'*500,'timeout':0})
        self.assertEqual(status,200);self.assertFalse(r['matched'])

    def test_inspect_tree_normalizes_null_value_and_name(self):
        import uia_backend as ui
        from contextlib import nullcontext
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        element=NS(CurrentBoundingRectangle=NS(left=0,top=0,right=10,bottom=10),CurrentControlType=50004,
                   CurrentName=None,CurrentAutomationId=None,CurrentClassName=None,CurrentIsEnabled=True,
                   CurrentIsOffscreen=False,CurrentHasKeyboardFocus=False,CurrentIsPassword=False)
        automation=Mock();automation.ElementFromHandle.return_value=element
        automation.ControlViewWalker.GetFirstChildElement.return_value=None
        with patch.object(ui,'session',return_value=nullcontext(automation)), \
             patch.object(ui,'value_pattern',return_value=NS(CurrentValue=None,CurrentIsReadOnly=False)):
            result=ui.inspect(123,max_depth=1)
        self.assertFalse(result['truncated'])
        self.assertEqual(result['elements'][0]['value'],'')
        self.assertEqual(result['elements'][0]['name'],'')

    def test_tree_provider_type_error_is_422_not_invalid_request(self):
        import uia_backend as ui
        from contextlib import nullcontext
        from unittest.mock import Mock
        automation=Mock()
        with patch.object(ui,'session',return_value=nullcontext(automation)), \
             patch.object(ui,'describe',side_effect=TypeError('broken provider')), \
             patch.object(server.desktop,'require_window'):
            status,result=self.request('inspect',{'window_id':123})
        self.assertEqual(status,422)
        self.assertEqual(result['code'],'ui_element_error')

    def test_drag_releases_button_when_pointer_motion_fails(self):
        d=server.desktop
        with patch.object(d,"require_window"), patch.object(d,"require_interactive_desktop"), \
             patch.object(d.u,"GetSystemMetrics",side_effect=lambda i:{76:0,77:0,78:1920,79:1080}[i]), \
             patch.object(d,"target_at",side_effect=[{"root_window_id":1},{"root_window_id":2},{"root_window_id":1}]), \
             patch.object(d.u,"SetCursorPos",return_value=True), \
             patch.object(d.time,"sleep"), patch.object(d,"send",side_effect=[None,RuntimeError("motion failed"),None]) as send:
            with self.assertRaises(RuntimeError):
                d.drag(100,100,200,200,1,2)
            self.assertEqual(send.call_args_list[0].args[0][0].mi.dwFlags,2)
            self.assertEqual(send.call_args_list[-1].args[0][0].mi.dwFlags,4)

    def test_drag_rejects_wrong_drop_before_button_down(self):
        d=server.desktop
        with patch.object(d,"require_window"), \
             patch.object(d.u,"GetSystemMetrics",side_effect=lambda i:{76:0,77:0,78:1920,79:1080}[i]), \
             patch.object(d,"target_at",side_effect=[{"root_window_id":1},{"root_window_id":999}]), \
             patch.object(d,"send") as send:
            with self.assertRaises(d.TargetError):
                d.drag(100,100,200,200,1,2)
            send.assert_not_called()

    def test_drag_schema_requires_destination_and_bounded_duration(self):
        valid={"type":"drag","window_id":1,"target_window_id":2,"x":1,"y":2,"to_x":3,"to_y":4}
        server.desktop.validate_action(valid)
        for change in ({"duration_ms":0},{"target_window_id":None},{"to_x":True}):
            with self.assertRaises(ValueError):
                server.desktop.validate_action({**valid,**change})

    def test_pointer_action_does_not_activate_shell_target(self):
        with patch.object(server.desktop, "require_interactive_desktop"), \
             patch.object(server.desktop, "require_window") as valid, \
             patch.object(server.desktop, "focus_window") as focus, \
             patch.object(server.desktop, "click", return_value={"root_window_id":123}), \
             patch.object(server.desktop.time, "sleep"):
            self.original_action({"type":"click","window_id":123,"x":100,"y":100})
            valid.assert_called_once_with(123)
            focus.assert_not_called()

    def test_pointer_click_checks_target_twice_without_foreground(self):
        with patch.object(server.desktop.u,"GetSystemMetrics",side_effect=lambda i:{76:0,77:0,78:1920,79:1080}[i]), \
             patch.object(server.desktop,"require_foreground") as foreground, \
             patch.object(server.desktop,"target_at",side_effect=[{"root_window_id":123},{"root_window_id":999}]), \
             patch.object(server.desktop.u,"SetCursorPos",return_value=True), \
             patch.object(server.desktop,"send") as send:
            with self.assertRaises(server.desktop.TargetError):
                server.desktop.click(100,100,hwnd=123)
            foreground.assert_not_called()
            send.assert_not_called()

    def test_launch_waits_for_app_activation_and_returns_reused_hwnd(self):
        import launcher
        old={"id":77,"active":False}
        active={"id":77,"active":True}
        def pid(hwnd,pointer):pointer._obj.value=77;return 1
        with patch.object(server.desktop,"windows",side_effect=[[old],[old],[active]]), \
             patch.object(server.desktop,"process_name",return_value="notepad.exe"), \
             patch.object(server.desktop.u,"GetWindowThreadProcessId",side_effect=pid), \
             patch.object(launcher,"dispatch",return_value={"process_id":42,"elevated":False,"expected_executable":"notepad.exe"}), \
             patch.object(launcher,"processes",return_value={42:{"parent":1,"name":"notepad.exe"}}), \
             patch.object(launcher,"process_elevated",return_value=False), \
             patch.object(server.desktop,"focus_window",return_value=active) as focus, \
             patch.object(server.desktop.time,"sleep"):
            result=server.desktop.launch_app({"app":"notepad"})
        self.assertEqual(result["window_id"],77)
        self.assertTrue(result["reused_window"])
        self.assertTrue(result["foreground_verified"])
        focus.assert_called_once_with(77)

    def test_launch_no_window_does_not_claim_foreground_success(self):
        import launcher
        with patch.object(server.desktop,"windows",return_value=[]), \
             patch.object(launcher,"dispatch",return_value={"process_id":42,"elevated":False,"expected_executable":None}), \
             patch.object(launcher,"processes",return_value={}), \
             patch.object(server.desktop.time,"monotonic",side_effect=[0,1]):
            result=server.desktop.launch_app({"path":"cmd","timeout":.2})
        self.assertEqual(result['launch_status'],'started_no_window')
        self.assertIsNone(result['window_id'])
        self.assertFalse(result['foreground_verified'])

    def test_launch_defaults_to_normal_token_even_for_regedit_alias(self):
        import launcher
        for path in ('chrome','regedit'):
            self.assertFalse(launcher.normalize({'app':path})['elevated'])
        self.assertTrue(launcher.normalize({'path':'regedit','elevated':True})['elevated'])

    def test_launch_schema_prevents_command_string_args_and_implicit_shell(self):
        import launcher
        for change in ({'args':'--flag'},{'args':['bad\0arg']},{'elevated':'true'},{'timeout':float('nan')},
                       {'cwd':'relative'},{'app':'notepad'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                launcher.normalize({'path':'chrome',**change})

    def test_normal_worker_dispatch_never_falls_back_to_admin(self):
        import launcher
        with patch.object(launcher,'start_medium_worker',side_effect=RuntimeError('No medium token')), \
             patch.object(launcher.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError,'No medium token'):
                launcher.dispatch(launcher.normalize({'path':'chrome'}))
        spawn.assert_not_called()

    def test_worker_passes_argv_without_command_shell_or_requoting(self):
        import launcher
        from unittest.mock import Mock
        process=Mock(pid=42,_handle=99)
        args=['--user-data-dir=C:\\path with spaces','a"b','&echo unsafe']
        with patch.object(launcher,'elevated_handle',return_value=False), \
             patch.object(launcher,'resolve',return_value='C:\\app.exe'), \
             patch.object(launcher.subprocess,'Popen',return_value=process) as spawn:
            result=launcher.worker(launcher.normalize({'path':'app','args':args}))
        self.assertFalse(result['elevated'])
        spawn.assert_called_once_with(['C:\\app.exe',*args],cwd=None,shell=False,close_fds=True)

    def test_process_tree_tracks_children_after_launcher_disappears(self):
        import launcher
        known=launcher.descendants({2:{'parent':1},3:{'parent':2},9:{'parent':8}},{1})
        self.assertEqual(known,{1,2,3})
        self.assertEqual(launcher.descendants({4:{'parent':3}},known),{1,2,3,4})

    def test_window_correlation_rejects_unrelated_fresh_window(self):
        import launcher
        def pid(hwnd,pointer):pointer._obj.value=999;return 1
        with patch.object(server.desktop.u,'GetWindowThreadProcessId',side_effect=pid), \
             patch.object(server.desktop,'process_name',return_value='unrelated.exe'):
            result=launcher.choose_window([{'id':99,'active':True}],[],{42},'chrome.exe',server.desktop)
        self.assertIsNone(result)

    def test_already_active_delegated_window_needs_explicit_fallback(self):
        import launcher
        row={'id':99,'active':True}
        def pid(hwnd,pointer):pointer._obj.value=77;return 1
        with patch.object(server.desktop.u,'GetWindowThreadProcessId',side_effect=pid), \
             patch.object(server.desktop,'process_name',return_value='chrome.exe'):
            self.assertIsNone(launcher.choose_window([row],[row],{42},'chrome.exe',server.desktop))
            match=launcher.choose_window([row],[row],{42},'chrome.exe',server.desktop,allow_unchanged_active=True)
        self.assertEqual(match,(row,77))

    def test_normal_launch_refuses_elevated_shell_token_and_closes_handles(self):
        import launcher
        def pid(hwnd,pointer):pointer._obj.value=44;return 1
        def token(handle,rights,pointer):pointer._obj.value=100;return True
        with patch.object(launcher.u,'GetShellWindow',return_value=77), \
             patch.object(launcher.u,'GetWindowThreadProcessId',side_effect=pid), \
             patch.object(launcher.k,'OpenProcess',return_value=99), \
             patch.object(launcher.a,'OpenProcessToken',side_effect=token), \
             patch.object(launcher,'token_elevated',return_value=True), \
             patch.object(launcher,'grant_exchange_directory') as grant, \
             patch.object(launcher.a,'CreateProcessWithTokenW') as create,patch.object(launcher.k,'CloseHandle') as close:
            with self.assertRaisesRegex(RuntimeError,'shell token is elevated'):
                launcher.start_medium_worker(['python','worker'],'exchange')
        grant.assert_not_called();create.assert_not_called()
        self.assertEqual([c.args[0].value if hasattr(c.args[0],'value') else c.args[0] for c in close.call_args_list],[100,99])

    def test_shell_launch_uses_open_verb_and_quoted_parameters(self):
        import launcher
        captured={}
        def shell_execute(pointer):
            info=pointer._obj;captured.update(file=info.lpFile,verb=info.lpVerb,args=info.lpParameters);return True
        with patch.object(launcher,'elevated_handle',return_value=False), \
             patch.object(launcher,'resolve',return_value='https://example.com'), \
             patch.object(launcher,'association_executable',return_value='browser.exe'), \
             patch.object(launcher.shell,'ShellExecuteExW',side_effect=shell_execute), \
             patch.object(launcher.subprocess,'Popen') as spawn:
            result=launcher.worker(launcher.normalize({'path':'https://example.com','args':['space arg','a"b']}))
        self.assertEqual(captured,dict(file='https://example.com',verb='open',args=launcher.subprocess.list2cmdline(['space arg','a"b'])))
        self.assertFalse(result['worker_elevated']);spawn.assert_not_called()

    def test_uwp_window_correlation_requires_matching_child_process(self):
        import launcher
        with patch.object(server.desktop,'process_name',side_effect=lambda hwnd:'systemsettings.exe' if hwnd==9 else 'applicationframehost.exe'), \
             patch.object(server.desktop.u,'EnumChildWindows',side_effect=lambda hwnd,callback,param:callback(9,0)):
            self.assertTrue(launcher.window_matches_executable(1,'systemsettings.exe',server.desktop))
            self.assertFalse(launcher.window_matches_executable(1,'unrelated.exe',server.desktop))

    def test_launch_permission_error_is_not_reported_as_locked_desktop(self):
        import launcher
        with patch.object(launcher,'launch',side_effect=PermissionError('Program access denied')):
            with self.assertRaises(RuntimeError) as raised:
                server.desktop.launch_app({'path':'app'})
        self.assertEqual(server.error_status(raised.exception)[0],500)
        self.assertIn('Program access denied',str(raised.exception))

    def test_unknown_process_elevation_is_null_and_handle_is_closed(self):
        import launcher
        with patch.object(launcher.k,'OpenProcess',return_value=99), \
             patch.object(launcher,'elevated_handle',side_effect=RuntimeError('Cannot query token')), \
             patch.object(launcher.k,'CloseHandle') as close:
            self.assertIsNone(launcher.process_elevated(42))
        close.assert_called_once_with(99)

    def test_unscoped_physical_input_rejected_before_any_mutation(self):
        for action in ({"type":"click","x":1,"y":2}, {"type":"type_text","text":"unsafe"},
                       {"type":"press_keys","keys":["ENTER"]}, {"type":"scroll","delta":120}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                server.desktop.validate_action(action)

    def test_within_selectors_validate_and_ambiguous_scope_is_not_empty(self):
        import uia_backend as ui
        ui.validate_selector({"name":"✕","role":"button","within":{"role":"listitem","name":"test✕"}})
        with self.assertRaises(ValueError):
            ui.validate_selector({"within":{"name":"test"}})

    def test_keyboard_does_not_send_text_if_field_focus_is_unverified(self):
        import uia_backend as ui
        # Restore the actual dispatcher while keeping all OS effects mocked.
        with patch.object(server.desktop, "require_interactive_desktop"), \
             patch.object(server.desktop, "focus_window"), \
             patch.object(ui,"focus_element",side_effect=ui.UIError("no focus")), \
             patch.object(ui,"click_point",return_value={"x":1,"y":2}), \
             patch.object(server.desktop,"click"), \
             patch.object(ui,"selected",return_value={"focused":False}), \
             patch.object(server.desktop,"type_text") as typed:
            # Access the original saved by patch, not the HTTP mock.
            original = self.original_action
            with self.assertRaises(server.desktop.TargetError):
                original({"type":"type_text","window_id":123,"selector":{"automation_id":"input"},
                          "method":"keyboard","text":"must not type"})
            typed.assert_not_called()

    def test_value_pattern_transient_match_is_not_verified_or_retried(self):
        import uia_backend as ui
        from contextlib import nullcontext
        from unittest.mock import Mock
        import itertools
        class Pattern:
            CurrentIsReadOnly=False
            reads=0
            SetValue=Mock()
            @property
            def CurrentValue(self):
                self.reads+=1
                return "wanted" if self.reads==1 else "tail"
        pattern=Pattern(); element=Mock()
        clock=itertools.count(0,.04)
        with patch.object(ui,"session",return_value=nullcontext(object())), \
             patch.object(ui,"find",return_value=element), \
             patch.object(ui,"value_pattern",return_value=pattern), \
             patch.object(ui,"describe") as describe, \
             patch.object(ui.time,"monotonic",side_effect=lambda:next(clock)), \
             patch.object(ui.time,"sleep"):
            with self.assertRaises(ui.UIError):
                ui.set_value(123,{"automation_id":"input"},"wanted")
        pattern.SetValue.assert_called_once_with("wanted")
        describe.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "UI Automation delivery tests need comtypes")
class InputDeliveryTests(unittest.TestCase):
    def row(self, **changes):
        return dict(dict(focused=True, enabled=True, offscreen=False, bounds=[0,0,100,20]), **changes)

    def test_invoke_once_reports_unconfirmed_not_application_success(self):
        import uia_backend as ui
        from unittest.mock import MagicMock
        element, pattern = MagicMock(), MagicMock()
        element.CurrentIsOffscreen=False
        element.GetCurrentPattern.return_value.QueryInterface.return_value=pattern
        with patch.object(ui, "session") as session, patch.object(ui,"find",return_value=element), patch.object(ui,"describe",return_value={}):
            result=ui.invoke(1,{"name":"delete"})
        pattern.Invoke.assert_called_once_with()
        self.assertFalse(result["verified"])
        self.assertEqual(result["effect_status"],"unconfirmed")
        element.SetFocus.assert_not_called()

    def test_offscreen_invoke_does_not_scroll_or_mutate(self):
        import uia_backend as ui
        from unittest.mock import MagicMock
        element=MagicMock(); element.CurrentIsOffscreen=True
        with patch.object(ui,"session"), patch.object(ui,"find",return_value=element):
            with self.assertRaisesRegex(ui.UIError,"offscreen"):
                ui.invoke(1,{"name":"delete"})
        element.GetCurrentPattern.assert_not_called()
        element.SetFocus.assert_not_called()

    def test_keyboard_readiness_waits_for_focus_and_stable_bounds(self):
        import uia_backend as ui
        rows=[self.row(focused=False),self.row(bounds=[1,0,100,20]),self.row(),self.row(),self.row()]
        with patch.object(ui,"selected",side_effect=rows), patch.object(ui.time,"monotonic",side_effect=[0,0,.05,.1,.16,.22]), patch.object(ui.time,"sleep"):
            self.assertEqual(ui.ready_keyboard(1,{"name":"Search"})["bounds"],[0,0,100,20])

    def test_offscreen_keyboard_field_never_becomes_ready(self):
        import uia_backend as ui
        with patch.object(ui,"selected",return_value=self.row(offscreen=True)), patch.object(ui.time,"monotonic",side_effect=[0,0,1]), patch.object(ui.time,"sleep"):
            with self.assertRaises(ui.UIError): ui.ready_keyboard(1,{"name":"Search"})

    def test_keyboard_delayed_value_verified_without_resending(self):
        import uia_backend as ui
        with patch.object(ui,"selected",return_value=self.row()), patch.object(ui,"keyboard_value",side_effect=["","wanted","wanted"]), patch.object(ui.time,"monotonic",side_effect=[0,0,.05,.15]), patch.object(ui.time,"sleep"):
            result=ui.verify_keyboard(1,{"name":"Search"},"wanted")
        self.assertTrue(result["verified"])

    def test_keyboard_mismatch_is_error_with_input_sent_context(self):
        import uia_backend as ui
        with patch.object(ui,"selected",return_value=self.row()), patch.object(ui,"keyboard_value",return_value=""), patch.object(ui.time,"monotonic",side_effect=[0,0,1]), patch.object(ui.time,"sleep"):
            with self.assertRaisesRegex(ui.UIError,"value mismatch") as caught:
                ui.verify_keyboard(1,{"name":"Search"},"wanted")
        self.assertTrue(caught.exception.details["input_sent"])
        self.assertNotIn("wanted",str(caught.exception))

    def test_keyboard_dispatch_verifies_replace_and_never_replays_mismatch(self):
        import uia_backend as ui
        desktop=server.desktop
        for mismatch in (False,True):
            with self.subTest(mismatch=mismatch), patch.object(desktop,"require_interactive_desktop"), patch.object(desktop,"focus_window"), patch.object(desktop,"require_foreground"), patch.object(desktop,"press") as press, patch.object(desktop,"type_text") as typed, patch.object(ui,"focus_element"), patch.object(ui,"keyboard_value",return_value="old"), patch.object(ui,"selected",return_value=self.row()), patch.object(ui,"verify_keyboard",side_effect=ui.UIError("value mismatch") if mismatch else None,return_value={"verified":True}) as verified:
                action={"type":"type_text","window_id":1,"selector":{"name":"Search"},"method":"keyboard","replace":True,"text":"wanted","settle_ms":0}
                if mismatch:
                    with self.assertRaisesRegex(ui.UIError,"mismatch"): desktop.action(action)
                else:
                    self.assertTrue(desktop.action(action)["verified"])
                typed.assert_called_once()
                press.assert_called_once_with(["CTRL","A"])
                verified.assert_called_once_with(1,{"name":"Search"},"wanted")

    def test_keyboard_lost_focus_stops_verification(self):
        import uia_backend as ui
        with patch.object(ui,"selected",return_value=self.row(focused=False)):
            with self.assertRaisesRegex(ui.UIError,"lost focus"):
                ui.verify_keyboard(1,{"name":"Search"},"wanted")


if __name__ == "__main__":
    unittest.main()
