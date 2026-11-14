import json
import io
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import omnifusion_service as service
import tidalapi


class ServiceLifecycleTests(unittest.TestCase):
    def test_owner_resolution_falls_back_only_for_empty_environment_value(self):
        with patch.object(service, "_verified_tidal_parent_pid", return_value=4567) as resolve_parent:
            self.assertEqual(service._resolve_tidal_owner(""), (4567, "verified_parent_fallback"))
            self.assertEqual(service._resolve_tidal_owner(None), (None, "unavailable"))
            self.assertEqual(service._resolve_tidal_owner("not-a-pid"), (None, "invalid_environment"))
            self.assertEqual(service._resolve_tidal_owner("1234"), (1234, "environment"))
            self.assertEqual(service._resolve_tidal_owner("0"), (None, "invalid_environment"))
            resolve_parent.assert_called_once_with()
        with patch.object(service, "_verified_tidal_parent_pid", return_value=None):
            self.assertEqual(service._resolve_tidal_owner(""), (None, "unavailable"))

    def test_missing_owner_environment_uses_verified_tidal_parent(self):
        parent = Mock()
        parent.name.return_value = "TIDAL.exe"
        parent.exe.return_value = r"C:\Users\tester\AppData\Local\TIDAL\app-2.48.0\TIDAL.exe"
        parent.username.return_value = r"MACHINE\tester"
        parent.create_time.return_value = 100.0
        parent.pid = 4567
        child = Mock()
        child.username.return_value = r"MACHINE\tester"
        child.create_time.return_value = 200.0
        child.parent.return_value = parent

        with patch.object(service.os, "name", "nt"), \
                patch.object(service.os, "getpid", return_value=8901), \
                patch.dict(service.os.environ, {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}), \
                patch.object(service.psutil, "Process", return_value=child), \
                patch.object(service, "_process_session_id", side_effect=[1, 1]):
            self.assertEqual(service._verified_tidal_parent_pid(), 4567)

    def test_parent_fallback_rejects_wrong_executable_path_or_session(self):
        parent = Mock()
        parent.name.return_value = "TIDAL.exe"
        parent.exe.return_value = r"C:\Temp\TIDAL.exe"
        parent.username.return_value = r"MACHINE\tester"
        parent.create_time.return_value = 100.0
        parent.pid = 4567
        child = Mock()
        child.username.return_value = r"MACHINE\tester"
        child.create_time.return_value = 200.0
        child.parent.return_value = parent

        with patch.object(service.os, "name", "nt"), \
                patch.object(service.os, "getpid", return_value=8901), \
                patch.dict(service.os.environ, {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}), \
                patch.object(service.psutil, "Process", return_value=child), \
                patch.object(service, "_process_session_id", side_effect=[1, 1]):
            self.assertIsNone(service._verified_tidal_parent_pid())

    def test_tidal_watchdog_stops_when_its_owner_exits_even_if_another_tidal_process_exists(self):
        server = Mock()
        with patch.object(service, "_tidal_process_create_time", side_effect=[123.0, None]), \
                patch.object(service.time, "sleep"), \
                patch.object(service.psutil, "process_iter") as process_iter:
            service.tidal_process_watchdog(Mock(), server, owner_pid=4567)

        server.shutdown.assert_called_once_with()
        process_iter.assert_not_called()

    def test_tidal_watchdog_fails_closed_when_owner_cannot_be_verified(self):
        server = Mock()
        with patch.object(service, "_tidal_process_create_time", return_value=None), \
                patch.object(service.time, "sleep"), \
                patch("builtins.print"):
            service.tidal_process_watchdog(Mock(), server, owner_pid=4567)
        server.shutdown.assert_called_once_with()

    def test_tidal_watchdog_never_falls_back_to_any_running_tidal_process(self):
        server = Mock()
        with patch.object(service.time, "sleep"), \
                patch.object(service.psutil, "process_iter") as process_iter, \
                patch("builtins.print"):
            service.tidal_process_watchdog(Mock(), server, owner_pid=None)
        server.shutdown.assert_called_once_with()
        process_iter.assert_not_called()

    def test_run_refuses_to_bind_or_start_mpv_without_verified_owner(self):
        with patch.object(service, "TIDAL_OWNER_PID", None), \
                patch.object(service, "TIDAL_OWNER_SOURCE", "unavailable"), \
                patch.object(service.sys, "exit", side_effect=SystemExit(1)), \
                patch.object(service, "ensure_single_instance") as ensure_single_instance, \
                patch.object(service, "ThreadedHTTPServer") as server:
            with self.assertRaises(SystemExit) as raised:
                service.run()
        self.assertEqual(raised.exception.code, 1)
        ensure_single_instance.assert_not_called()
        server.assert_not_called()

    def test_watchdog_shutdown_after_serving_loop_is_ready_does_not_deadlock(self):
        server = service.ThreadedHTTPServer(("127.0.0.1", 0), service.RequestHandler)
        serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
        serve_thread.start()
        try:
            self.assertTrue(server.serve_ready.wait(timeout=2))
            with patch.object(service, "_tidal_process_create_time", return_value=None), \
                    patch("builtins.print"):
                service.tidal_process_watchdog(Mock(), server, owner_pid=4567)
            self.assertTrue(server.shutdown_requested.is_set())
            serve_thread.join(timeout=2)
            self.assertFalse(serve_thread.is_alive())
        finally:
            if serve_thread.is_alive():
                server.shutdown()
                serve_thread.join(timeout=2)
            server.server_close()

    def test_tidal_owner_pid_is_reported_by_health(self):
        handler = object.__new__(service.RequestHandler)
        handler.path = "/health"
        handler.headers = {}
        handler.reject_untrusted_origin = Mock(return_value=False)
        handler.send_json = Mock()

        with patch.object(service, "TIDAL_OWNER_PID", 4567), \
                patch.object(service, "TIDAL_OWNER_SOURCE", "verified_parent_fallback"):
            service.RequestHandler.do_GET(handler)

        payload = handler.send_json.call_args.args[0]
        self.assertEqual(payload["tidalOwnerPid"], 4567)
        self.assertEqual(payload["tidalOwnerSource"], "verified_parent_fallback")
        self.assertNotIn("shutdownToken", payload)

    def test_shutdown_requires_matching_secret_and_service_instance(self):
        with patch.object(service, "SHUTDOWN_TOKEN", "a" * 64), \
                patch.object(service, "SERVICE_INSTANCE_ID", "instance-1"):
            self.assertFalse(service.shutdown_request_authorized({}))
            self.assertFalse(service.shutdown_request_authorized({
                "Authorization": "Bearer " + "b" * 64,
                "X-Omniphony-Service-Instance": "instance-1",
            }))
            self.assertFalse(service.shutdown_request_authorized({
                "Authorization": "Bearer " + "a" * 64,
                "X-Omniphony-Service-Instance": "instance-2",
            }))
            self.assertTrue(service.shutdown_request_authorized({
                "Authorization": "Bearer " + "a" * 64,
                "X-Omniphony-Service-Instance": "instance-1",
            }))

    def test_shutdown_is_denied_when_no_launch_secret_was_configured(self):
        with patch.object(service, "SHUTDOWN_TOKEN", ""):
            self.assertFalse(service.shutdown_request_authorized({
                "Authorization": "Bearer " + "a" * 64,
                "X-Omniphony-Service-Instance": service.SERVICE_INSTANCE_ID,
            }))

    def test_shutdown_http_handler_rejects_unauthenticated_request_before_dispatch(self):
        handler = object.__new__(service.RequestHandler)
        handler.path = "/shutdown"
        handler.headers = {"Origin": "https://arbitrary.example", "Content-Length": "0"}
        handler.send_json = Mock()

        service.RequestHandler._do_POST(handler)

        handler.send_json.assert_called_once_with(
            {"error": "service shutdown authorization failed"}, status=403,
        )

    def test_shutdown_http_handler_dispatches_only_the_authenticated_instance(self):
        handler = object.__new__(service.RequestHandler)
        handler.path = "/shutdown"
        handler.headers = {
            "Origin": "http://127.0.0.1:8765",
            "Content-Length": "0",
            "Authorization": "Bearer " + "a" * 64,
            "X-Omniphony-Service-Instance": "instance-1",
        }
        handler.rfile = io.BytesIO()
        handler.connection = Mock()
        handler.server = Mock()
        handler.send_json = Mock()

        with patch.object(service, "SHUTDOWN_TOKEN", "a" * 64), \
                patch.object(service, "SERVICE_INSTANCE_ID", "instance-1"), \
                patch.object(service, "schedule_server_shutdown") as schedule:
            service.RequestHandler._do_POST(handler)

        handler.send_json.assert_called_once_with({"status": "shutting_down"})
        schedule.assert_called_once_with(handler.server)

    def test_shutdown_authentication_over_real_loopback_http(self):
        token = "c" * 64
        with patch.object(service, "SHUTDOWN_TOKEN", token), \
                patch.object(service, "SERVICE_INSTANCE_ID", "loopback-instance"):
            server = service.ThreadedHTTPServer(("127.0.0.1", 0), service.RequestHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                unauthorized = urllib.request.Request(
                    base + "/shutdown", data=b"{}", method="POST",
                    headers={"Origin": "https://untrusted.example", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as response:
                    urllib.request.urlopen(unauthorized, timeout=2)
                self.assertEqual(response.exception.code, 403)

                with urllib.request.urlopen(base + "/health", timeout=2) as health:
                    self.assertEqual(health.status, 200)

                authorized = urllib.request.Request(
                    base + "/shutdown", data=b"{}", method="POST",
                    headers={
                        "Origin": "http://127.0.0.1:8765",
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + token,
                        "X-Omniphony-Service-Instance": "loopback-instance",
                    },
                )
                with patch.object(service, "schedule_server_shutdown") as schedule:
                    with urllib.request.urlopen(authorized, timeout=2) as accepted:
                        self.assertEqual(accepted.status, 200)
                        self.assertEqual(json.load(accepted), {"status": "shutting_down"})
                    schedule.assert_called_once_with(server)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_shutdown_credentials_are_not_enabled_for_browser_cors(self):
        handler = object.__new__(service.RequestHandler)
        handler.headers = {
            "Origin": "https://arbitrary.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, x-omniphony-service-instance",
        }
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.send_json = Mock()

        service.RequestHandler.do_OPTIONS(handler)

        handler.send_json.assert_called_once_with(
            {"error": "preflight request is not allowed"}, status=403,
        )
        handler.send_response.assert_not_called()

    def test_cors_allows_any_origin_without_cookie_credentials(self):
        handler = object.__new__(service.RequestHandler)
        handler.headers = {"Origin": "https://unlisted-origin.example"}
        handler.send_json = Mock()

        with patch.object(service.RequestHandler, "send_header") as send_header, \
                patch.object(service.http.server.BaseHTTPRequestHandler, "end_headers") as base_end_headers:
            service.RequestHandler.end_headers(handler)

        send_header.assert_called_once_with("Access-Control-Allow-Origin", "*")
        base_end_headers.assert_called_once()
        self.assertFalse(service.RequestHandler.reject_untrusted_origin(handler))
        handler.send_json.assert_not_called()

    def test_cors_preflight_still_requires_an_origin_header(self):
        handler = object.__new__(service.RequestHandler)
        handler.headers = {}
        handler.send_json = Mock()

        self.assertTrue(service.RequestHandler.reject_untrusted_origin(handler, require_origin=True))
        handler.send_json.assert_called_once_with(
            {"error": "Origin header is required for preflight"}, status=403,
        )

    def test_mobile_ac4_profile_refreshes_with_mobile_client_before_stream_use(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "tidal-session-oauth.json"
            session_file.write_text(json.dumps({
                "is_pkce": {"data": False},
                "refresh_token": {"data": "refresh-token-fixture"},
            }), encoding="utf-8")
            session = Mock()
            session.request = Mock()
            session.token_type = "Bearer"
            session.access_token = "mobile-access-fixture"
            session.refresh_token = None
            session.expiry_time = None
            session.session_id = "session-fixture"
            session.is_pkce = False
            session.load_oauth_session.return_value = True
            calls = []

            def refresh(refresh_token):
                calls.append(("refresh", refresh_token))
                session.token_type = "Bearer"
                session.access_token = "mobile-access-fixture"
                session.refresh_token = "rotated-refresh-fixture"
                return True

            def load_oauth_session(*args, **kwargs):
                calls.append(("load", kwargs.get("is_pkce")))
                return True

            session.token_refresh.side_effect = refresh
            session.load_oauth_session.side_effect = load_oauth_session
            config = Mock()

            with patch.object(service, "SESSION_FILE", session_file), \
                    patch.object(service, "_TIDAL_MOBILE_API_SESSION", None), \
                    patch.object(service, "_restrict_windows_session_acl"), \
                    patch.object(service, "install_python_tidal_request_timeout"), \
                    patch.object(tidalapi, "Config", return_value=config), \
                    patch.object(tidalapi, "Session", return_value=session):
                loaded = service.get_python_tidal_mobile_session()
            persisted = json.loads(session_file.read_text(encoding="utf-8"))

        self.assertIs(loaded, session)
        self.assertEqual(config.client_id, "km8T1xS355y7dd3H")
        self.assertEqual(calls, [
            ("refresh", "refresh-token-fixture"),
            ("load", False),
        ])
        self.assertEqual(
            session.request.user_agent,
            "TIDAL_ANDROID/1057 CFNetwork/1410.0.3 Darwin/22.4.0",
        )
        self.assertEqual(
            persisted["refresh_token"]["data"],
            "rotated-refresh-fixture",
            "the EbbLabs client refresh rotation is persisted atomically",
        )

    def test_renderer_that_exits_after_snapshot_is_ignored(self):
        renderer_name = service.RENDERER_EXE.name
        with patch.object(service, "_windows_process_snapshot", return_value=[(4567, renderer_name)]), \
                patch.object(
                    service.psutil, "Process",
                    side_effect=service.psutil.NoSuchProcess(pid=4567),
                ):
            self.assertEqual(service._route_process_candidates(), [])

    def test_inaccessible_snapshotted_renderer_fails_route_inspection_closed(self):
        renderer_name = service.RENDERER_EXE.name
        with patch.object(service, "_windows_process_snapshot", return_value=[(4567, renderer_name)]), \
                patch.object(
                    service.psutil, "Process",
                    side_effect=service.psutil.AccessDenied(pid=4567),
                ):
            with self.assertRaises(service.psutil.AccessDenied):
                service._route_process_candidates()

    def test_route_requires_embedded_renderer_and_rejects_standalone_renderer(self):
        self.assertTrue(service.omniphony_route_is_ready(
            1, 1, True, True, True, standalone_renderer_absent=True,
        ))
        self.assertFalse(service.omniphony_route_is_ready(
            1, 1, True, True, True, standalone_renderer_absent=False,
        ))

    def test_joc_route_start_tracks_and_closes_only_its_owned_studio(self):
        engine = service.HeadlessAudioEngine()
        with TemporaryDirectory() as temp:
            studio_exe = Path(temp) / "omniphony-studio-egui.exe"
            studio_exe.touch()
            process = Mock(pid=5123)
            process.poll.return_value = None
            route_states = [
                {"ready": False, "checks": {"studio": False}},
                {"ready": True, "checks": {"studio": True, "studioRendererConnected": True}},
            ]
            with patch.object(service, "STUDIO_EXE", studio_exe), \
                    patch.object(service, "get_omniphony_route_status", side_effect=route_states) as route_probe, \
                    patch.object(service.subprocess, "Popen", return_value=process) as launch:
                route = engine.ensure_omniphony_route(timeout=0.5)

            self.assertTrue(route["ready"])
            self.assertEqual(engine.studio_proc, process)
            launch.assert_called_once_with(
                [str(studio_exe), "--register", service.STUDIO_REGISTER_TARGET],
                cwd=str(studio_exe.parent),
            )
            self.assertEqual(route_probe.call_count, 2)
            self.assertTrue(engine.stop_owned_omniphony_studio())

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)
        process.kill.assert_not_called()
        self.assertIsNone(engine.studio_proc)

    def test_existing_user_started_studio_is_never_claimed_or_closed(self):
        engine = service.HeadlessAudioEngine()
        engine.proc = Mock(pid=35128)
        with TemporaryDirectory() as temp:
            studio_exe = Path(temp) / "omniphony-studio-egui.exe"
            studio_exe.touch()
            with patch.object(service, "STUDIO_EXE", studio_exe), \
                    patch.object(service, "get_omniphony_route_status", return_value={
                        "ready": True, "checks": {"studio": True, "studioRendererConnected": True},
                    }) as route_probe, \
                    patch.object(service.subprocess, "Popen") as launch:
                route = engine.ensure_omniphony_route(timeout=0.1)

        self.assertTrue(route["ready"])
        self.assertEqual(route_probe.call_count, 2)
        self.assertEqual(route_probe.call_args_list[0].args, (35128,))
        self.assertEqual(route_probe.call_args_list[1].args, (35128,))
        launch.assert_not_called()
        self.assertIsNone(engine.studio_proc)
        self.assertFalse(engine.stop_owned_omniphony_studio())

    def test_owned_studio_stop_serializes_with_concurrent_route_start(self):
        engine = service.HeadlessAudioEngine()
        old_process = Mock(pid=5123)
        old_process.poll.return_value = None
        new_process = Mock(pid=6123)
        new_process.poll.return_value = None
        engine.studio_proc = old_process

        wait_started = threading.Event()
        allow_wait_finish = threading.Event()
        initial_route_observed = threading.Event()
        route_probe_count = 0
        results = {}
        errors = []

        def blocked_wait(timeout):
            self.assertEqual(timeout, 3)
            wait_started.set()
            if not allow_wait_finish.wait(timeout=2):
                raise AssertionError("test did not release the owned Studio stop")
            return 0

        old_process.wait.side_effect = blocked_wait

        def route_probe(_pid):
            nonlocal route_probe_count
            route_probe_count += 1
            if route_probe_count == 1:
                initial_route_observed.set()
                return {"ready": False, "checks": {"studio": False}}
            return {
                "ready": True,
                "checks": {"studio": True, "studioRendererConnected": True},
            }

        def stop_studio():
            try:
                results["stopped"] = engine.stop_owned_omniphony_studio()
            except Exception as exc:  # surfaced in the main test thread
                errors.append(exc)

        def start_route():
            try:
                results["route"] = engine.ensure_omniphony_route(timeout=1)
            except Exception as exc:  # surfaced in the main test thread
                errors.append(exc)

        with TemporaryDirectory() as temp:
            studio_exe = Path(temp) / "omniphony-studio-egui.exe"
            studio_exe.touch()
            with patch.object(service, "STUDIO_EXE", studio_exe), \
                    patch.object(service, "get_omniphony_route_status", side_effect=route_probe), \
                    patch.object(service.subprocess, "Popen", return_value=new_process) as launch:
                stopper = threading.Thread(target=stop_studio)
                stopper.start()
                self.assertTrue(wait_started.wait(timeout=1))

                starter = threading.Thread(target=start_route)
                starter.start()
                self.assertTrue(initial_route_observed.wait(timeout=1))
                self.assertFalse(launch.called, "route start must wait while owned Studio is stopping")

                allow_wait_finish.set()
                stopper.join(timeout=2)
                starter.join(timeout=2)

                self.assertFalse(stopper.is_alive())
                self.assertFalse(starter.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(results["stopped"])
                self.assertTrue(results["route"]["ready"])
                launch.assert_called_once_with(
                    [str(studio_exe), "--register", service.STUDIO_REGISTER_TARGET],
                    cwd=str(studio_exe.parent),
                )

        self.assertEqual(engine.studio_proc, new_process)

    def test_owned_studio_double_timeout_is_bounded_and_retains_process_handle(self):
        engine = service.HeadlessAudioEngine()
        process = Mock(pid=5123)
        process.poll.return_value = None
        timeout = service.subprocess.TimeoutExpired("omniphony-studio", 3)
        process.wait.side_effect = [timeout, timeout]
        engine.studio_proc = process

        self.assertFalse(engine.stop_owned_omniphony_studio())

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIs(engine.studio_proc, process)

    def _invalidated_joc_engine(self):
        engine = service.HeadlessAudioEngine()
        request_id = "invalidated-joc-request"
        engine.pending_generation = 8
        engine.pending_request_id = None
        engine.active_generation = 0
        engine.active_request_id = None
        engine.prepared_generation = 0
        engine.prepared_request_id = None
        engine.proc = Mock(pid=35128)
        engine.current_meta = {
            "trackId": "227401768",
            "generation": 7,
            "requestId": request_id,
            "format": "EAC3_JOC",
            "playbackState": "failed",
            "label": "Omniphony renderer disconnected",
            "lastFailure": "route disconnected",
            "omniphonyRoute": {"ready": False, "studioPid": 11844},
        }
        engine.cmd = Mock(return_value={"error": "success"})
        return engine, request_id

    def test_invalidated_joc_refresh_updates_only_route_telemetry(self):
        engine, request_id = self._invalidated_joc_engine()
        replacement_route = {
            "ready": True,
            "studioPid": 33888,
            "checks": {"studioRendererConnected": True},
        }
        original_state = dict(engine.current_meta)

        with patch.object(service, "get_omniphony_route_status", return_value=replacement_route) as probe:
            refreshed = service.refresh_invalidated_joc_route_telemetry(engine)

        self.assertTrue(refreshed)
        probe.assert_called_once_with(35128)
        self.assertEqual(engine.current_meta["omniphonyRoute"], replacement_route)
        for key in ("trackId", "generation", "requestId", "playbackState", "label", "lastFailure"):
            self.assertEqual(engine.current_meta[key], original_state[key])
        self.assertEqual(engine.pending_generation, 8)
        self.assertEqual(engine.active_generation, 0)
        self.assertEqual(engine.prepared_generation, 0)
        self.assertFalse(engine.control_matches("227401768", 7, request_id))
        engine.cmd.assert_not_called()

    def test_invalidated_joc_refresh_discards_result_after_track_replacement(self):
        engine, _ = self._invalidated_joc_engine()
        probe_started = threading.Event()
        allow_probe_finish = threading.Event()

        def delayed_probe(pid):
            self.assertEqual(pid, 35128)
            probe_started.set()
            self.assertTrue(allow_probe_finish.wait(timeout=2))
            return {"ready": True, "studioPid": 33888}

        with patch.object(service, "get_omniphony_route_status", side_effect=delayed_probe):
            worker = threading.Thread(
                target=lambda: service.refresh_invalidated_joc_route_telemetry(engine)
            )
            worker.start()
            self.assertTrue(probe_started.wait(timeout=2))
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    engine.pending_generation = 9
                    engine.pending_request_id = "new-track-request"
                    engine.current_meta = {
                        "trackId": "new-track",
                        "generation": 9,
                        "requestId": "new-track-request",
                        "format": "EAC3_JOC",
                        "playbackState": "prepared",
                        "omniphonyRoute": {"ready": False, "studioPid": 11844},
                    }
            allow_probe_finish.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(engine.current_meta["trackId"], "new-track")
        self.assertEqual(engine.current_meta["omniphonyRoute"]["studioPid"], 11844)
        engine.cmd.assert_not_called()

    def test_invalidated_joc_refresh_discards_result_after_mpv_replacement(self):
        engine, _ = self._invalidated_joc_engine()
        old_proc = engine.proc
        probe_started = threading.Event()
        allow_probe_finish = threading.Event()

        def delayed_probe(pid):
            probe_started.set()
            self.assertTrue(allow_probe_finish.wait(timeout=2))
            return {"ready": True, "studioPid": 33888}

        with patch.object(service, "get_omniphony_route_status", side_effect=delayed_probe):
            worker = threading.Thread(
                target=lambda: service.refresh_invalidated_joc_route_telemetry(engine)
            )
            worker.start()
            self.assertTrue(probe_started.wait(timeout=2))
            with engine.playback_lock:
                with engine.playback_generation_lock:
                    engine.proc = Mock(pid=35222)
            allow_probe_finish.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIsNot(engine.proc, old_proc)
        self.assertEqual(engine.current_meta["omniphonyRoute"]["studioPid"], 11844)
        engine.cmd.assert_not_called()

    def test_invalidated_joc_refresh_failure_does_not_change_playback(self):
        engine, _ = self._invalidated_joc_engine()
        original_meta = dict(engine.current_meta)
        with patch.object(service, "get_omniphony_route_status", side_effect=OSError("probe failed")):
            refreshed = service.refresh_invalidated_joc_route_telemetry(engine)
        self.assertFalse(refreshed)
        self.assertEqual(engine.current_meta, original_meta)
        engine.cmd.assert_not_called()

    def test_route_telemetry_refresh_ignores_active_joc_and_non_joc_tracks(self):
        engine, request_id = self._invalidated_joc_engine()
        cases = (
            ("EAC3_JOC", 7, 7, "failed"),
            ("AC4_IMS", 7, 8, "failed"),
            ("EAC3_JOC", 8, 8, "paused"),
        )
        with patch.object(service, "get_omniphony_route_status") as probe:
            for codec, generation, pending_generation, state in cases:
                with self.subTest(codec=codec, generation=generation, state=state):
                    engine.current_meta = {
                        "trackId": "227401768",
                        "generation": generation,
                        "requestId": request_id,
                        "format": codec,
                        "playbackState": state,
                        "omniphonyRoute": {"ready": False, "studioPid": 11844},
                    }
                    engine.pending_generation = pending_generation
                    self.assertFalse(service.refresh_invalidated_joc_route_telemetry(engine))
        probe.assert_not_called()
        engine.cmd.assert_not_called()

    def test_eclipse_manifest_urls_allow_https_and_local_development_only(self):
        self.assertEqual(
            service.validate_eclipse_addon_urls([
                " https://example.com/manifest.json ",
                "http://localhost:3000/manifest.json",
                "http://127.0.0.1:3000/manifest.json",
                "http://[::1]:3000/manifest.json",
            ]),
            [
                "https://example.com/manifest.json",
                "http://localhost:3000/manifest.json",
                "http://127.0.0.1:3000/manifest.json",
                "http://[::1]:3000/manifest.json",
            ],
        )

    def test_eclipse_manifest_urls_reject_unsafe_schemes_and_credentials(self):
        for value in (["javascript:alert(1)"], ["http://example.com/manifest.json"],
                      ["https://user:pass@example.com/manifest.json"], ["https://example.com/" + "a" * 2050]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_eclipse_addon_urls(value)

    def test_eclipse_manifest_urls_require_a_list_of_strings(self):
        for value in ("https://example.com/manifest.json", [None], ["https://example.com/" for _ in range(17)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_eclipse_addon_urls(value)

    def test_invalid_saved_eclipse_manifests_are_ignored(self):
        with TemporaryDirectory() as temp:
            settings_file = Path(temp) / "settings.json"
            settings_file.write_text(
                json.dumps({"eclipse_addons": ["javascript:alert(1)"]}),
                encoding="utf-8",
            )
            with patch.object(service, "SETTINGS_FILE", settings_file):
                settings = service.load_settings()
        self.assertEqual(settings["eclipse_addons"], [])

    def test_audio_device_default_is_portable_and_saved_device_is_preserved(self):
        self.assertEqual(service.DEFAULT_SETTINGS["audio_device"], "auto")
        with TemporaryDirectory() as temp:
            settings_file = Path(temp) / "settings.json"
            settings_file.write_text(
                json.dumps({"audio_device": "wasapi/test-device"}),
                encoding="utf-8",
            )
            with patch.object(service, "SETTINGS_FILE", settings_file):
                settings = service.load_settings()
        self.assertEqual(settings["audio_device"], "wasapi/test-device")

    def test_shutdown_is_scheduled_off_the_request_thread(self):
        completed = threading.Event()

        class FakeServer:
            def shutdown(self):
                completed.set()

        worker = service.schedule_server_shutdown(FakeServer())
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(completed.is_set())

    def test_unexpected_post_handler_error_returns_json_instead_of_dropping_connection(self):
        class FakeRequest:
            path = "/seek?diagnostic=ignored"
            close_connection = False

            def _do_POST(self):
                raise RuntimeError("do not expose this exception message")

            def send_json(self, payload, status=200):
                self.response = (payload, status)

        request = FakeRequest()
        with patch("builtins.print") as log:
            service.RequestHandler.do_POST(request)

        self.assertEqual(request.response, ({
            "error": "request failed unexpectedly",
            "code": "INTERNAL_ERROR",
            "errorType": "RuntimeError",
        }, 500))
        log.assert_called_once_with("[-] POST /seek handler failed unexpectedly (RuntimeError)")

    def test_root_launcher_checks_health_before_rebasing_config(self):
        root = Path(service.__file__).resolve().parent.parent
        launcher = root / "launch_service.bat"
        if not launcher.is_file():
            self.skipTest("standalone root launcher is not part of this repository layout")
        source = launcher.read_text(encoding="utf-8")
        self.assertLess(source.index("/health"), source.index("prepare_portable_omniphony_config.py"))
        self.assertIn(f'findstr /C:"{service.SERVICE_VERSION}"', source)

    def test_packaged_launcher_waits_for_health_and_rejects_other_build(self):
        root = Path(service.__file__).resolve().parent.parent
        service_dir = Path(service.__file__).resolve().parent
        candidates = [service_dir / "launch_service.bat", root / "api" / "launch_service.bat",
                      root / "plugin-dev" / "api" / "launch_service.bat"]
        launcher = next((candidate for candidate in candidates if candidate.is_file()), None)
        if launcher is None:
            self.skipTest("packaged API launcher is not present")
        source = launcher.read_text(encoding="utf-8")
        self.assertIn("for /l %%I in (1,1,20)", source)
        self.assertIn(f'findstr /C:"{service.SERVICE_VERSION}"', source)
        self.assertIn("different OmniFusion service build owns port 8765", source)
        self.assertIn("if not defined OMNIFUSION_TIDAL_PID", source)
        self.assertIn("must be started by the Omniphony EX plugin inside TIDAL", source)


if __name__ == "__main__":
    unittest.main()
