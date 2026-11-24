import json
import io
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import omnifusion_service as service
import tidalapi


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._eclipse_account_linking_supported = service.ECLIPSE_ACCOUNT_LINKING_SUPPORTED
        service.ECLIPSE_ACCOUNT_LINKING_SUPPORTED = True

    def tearDown(self):
        service.ECLIPSE_ACCOUNT_LINKING_SUPPORTED = self._eclipse_account_linking_supported

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
        self.assertEqual(payload["capabilities"], ["prepared-playback-v1", "playback-clock-v1"])
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
                shutdown_scheduled = threading.Event()

                def record_shutdown_scheduled(_server):
                    shutdown_scheduled.set()

                with patch.object(
                    service,
                    "schedule_server_shutdown",
                    side_effect=record_shutdown_scheduled,
                ) as schedule:
                    with urllib.request.urlopen(authorized, timeout=2) as accepted:
                        self.assertEqual(accepted.status, 200)
                        self.assertEqual(json.load(accepted), {"status": "shutting_down"})
                    self.assertTrue(
                        shutdown_scheduled.wait(timeout=2),
                        "the authenticated handler did not schedule shutdown",
                    )
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
            1, 1, True, True, True,
            sofa_file_container_verified=True,
            standalone_renderer_absent=True,
        ))
        self.assertFalse(service.omniphony_route_is_ready(
            1, 1, True, True, True,
            sofa_file_container_verified=True,
            standalone_renderer_absent=False,
        ))
        self.assertFalse(service.omniphony_route_is_ready(
            1, 1, True, True, True,
            sofa_file_container_verified=False,
            standalone_renderer_absent=True,
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

    def test_account_manifest_urls_require_https_and_reject_embedded_credentials(self):
        self.assertEqual(
            service._validate_account_manifest_url(" https://example.com/manifest.json "),
            "https://example.com/manifest.json",
        )
        for value in ("javascript:alert(1)", "http://example.com/manifest.json",
                      "https://user:pass@example.com/manifest.json", "https://example.com/" + "a" * 2050,
                      "https://example.com/manifest.json#fragment"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service._validate_account_manifest_url(value)

    def test_eclipse_authenticated_redirects_are_same_origin_only(self):
        handler = service._SameOriginRedirectHandler()
        request = service.urllib.request.Request("https://api.eclipsemusic.app/account", headers={
            "Authorization": "Bearer secret",
        })
        headers = {}
        self.assertIsNotNone(handler.redirect_request(
            request, None, 302, "Found", headers, "https://api.eclipsemusic.app/next"
        ))
        self.assertIsNone(handler.redirect_request(
            request, None, 302, "Found", headers, "https://attacker.example/steal"
        ))
        self.assertIsNone(handler.redirect_request(
            request, None, 302, "Found", headers, "http://api.eclipsemusic.app/insecure"
        ))

    def test_eclipse_worker_base_preserves_tokenized_manifest_path(self):
        self.assertEqual(
            service._eclipse_worker_base_url(
                "https://amazon-music-eclipse-addon.cyrusna29.workers.dev/0dd9c90858c14f98be288c82b747fbb9/manifest.json"
            ),
            "https://amazon-music-eclipse-addon.cyrusna29.workers.dev/0dd9c90858c14f98be288c82b747fbb9",
        )
        self.assertEqual(
            service._eclipse_worker_base_url(
                "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df/manifest.json"
            ),
            "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df",
        )
        self.assertEqual(
            service._eclipse_worker_url(
                "https://addons.example/account/manifest.json?edition=exclusive&token=scope",
                "search",
                {"q": "Natural & Imagine Dragons"},
            ),
            "https://addons.example/account/search?edition=exclusive&token=scope&q=Natural+%26+Imagine+Dragons",
        )

    def test_eclipse_jwt_force_refresh_reloads_saved_token_without_network_login(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            session_file.write_text(json.dumps({
                "jwt": "saved-token-from-session-file",
            }), encoding="utf-8")
            auth_cache = {
                "jwt": "stale-in-memory-token",
                "addon_tokens": {},
                "manifest_cache": {},
                "search_cache": {},
                "stream_cache": {},
            }
            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service.urllib.request, "urlopen") as urlopen:
                self.assertEqual(
                    service._get_eclipse_jwt(force_refresh=True),
                    "saved-token-from-session-file",
                )
            urlopen.assert_not_called()
            self.assertEqual(auth_cache["jwt"], "saved-token-from-session-file")
            self.assertEqual(auth_cache["addon_tokens"], {})
            self.assertEqual(auth_cache["search_cache"], {})
            self.assertEqual(auth_cache["stream_cache"], {})

    def test_eclipse_jwt_loader_does_not_attempt_placeholder_login_or_signup(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            session_file.write_text(json.dumps({
                "email": "synthetic@example.invalid",
                "password": "synthetic-placeholder-password",
            }), encoding="utf-8")
            auth_cache = {
                "jwt": None,
                "addon_tokens": {},
                "manifest_cache": {},
                "search_cache": {},
                "stream_cache": {},
            }
            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service.urllib.request, "urlopen") as urlopen:
                self.assertIsNone(service._get_eclipse_jwt(force_refresh=True))
            urlopen.assert_not_called()
            self.assertIsNone(auth_cache["jwt"])

    def test_eclipse_jwt_force_refresh_clears_stale_account_cache_when_saved_token_is_unavailable(self):
        invalid_session_contents = (None, "not-json", json.dumps({"jwt": "  "}))
        for contents in invalid_session_contents:
            with self.subTest(contents=contents), TemporaryDirectory() as temp:
                session_file = Path(temp) / "eclipse-session.json"
                if contents is not None:
                    session_file.write_text(contents, encoding="utf-8")
                auth_cache = {
                    "jwt": "stale-token",
                    "addon_tokens": {"addon": {"token": "stale-addon-token"}},
                    "manifest_cache": {"manifest": {"expires_at": 9999999999}},
                    "search_cache": {"search": "old-account-result"},
                    "stream_cache": {"stream": "old-account-stream"},
                }
                with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                        patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                        patch.object(service.urllib.request, "urlopen") as urlopen:
                    self.assertIsNone(service._get_eclipse_jwt(force_refresh=True))
                    self.assertIsNone(service._get_eclipse_jwt())
                urlopen.assert_not_called()
                self.assertIsNone(auth_cache["jwt"])
                self.assertEqual(auth_cache["addon_tokens"], {})
                self.assertEqual(auth_cache["search_cache"], {})
                self.assertEqual(auth_cache["stream_cache"], {})
                self.assertIn("manifest", auth_cache["manifest_cache"])

    def test_eclipse_jwt_reload_invalidates_account_specific_caches_when_account_changes(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            session_file.write_text(json.dumps({"jwt": "new-account-token"}), encoding="utf-8")
            auth_cache = {
                "jwt": "old-account-token",
                "addon_tokens": {"addon": {"token": "old-addon-token"}},
                "manifest_cache": {"manifest": {"expires_at": 9999999999}},
                "search_cache": {"search": "old-account-result"},
                "stream_cache": {"stream": "old-account-stream"},
            }
            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache):
                self.assertEqual(service._get_eclipse_jwt(force_refresh=True), "new-account-token")

            self.assertEqual(auth_cache["jwt"], "new-account-token")
            self.assertEqual(auth_cache["addon_tokens"], {})
            self.assertEqual(auth_cache["search_cache"], {})
            self.assertEqual(auth_cache["stream_cache"], {})
            self.assertIn("manifest", auth_cache["manifest_cache"])

    def test_eclipse_login_persists_only_dpapi_protected_account_token(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            user = {"id": "acct-1", "email": "listener@example.test", "name": "Listener"}
            auth_cache = {
                "jwt": "old-account-token", "addon_tokens": {"old": {"token": "stale"}},
                "manifest_cache": {}, "search_cache": {"old": {}}, "stream_cache": {"old": {}},
            }
            account_status = {"user": None, "checked_at": 0.0, "session_mtime": None}
            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "CONFIG_DIR", Path(temp)), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service, "_ECLIPSE_ACCOUNT_STATUS", account_status), \
                    patch.object(service, "_eclipse_token_protect", return_value="protected-dpapi-value"), \
                    patch.object(service, "_verify_eclipse_account_token", return_value=(True, user, "rotated-account-token")):
                connected, saved_user, error = service._complete_eclipse_login("callback-account-token")

            self.assertTrue(connected)
            self.assertIsNone(error)
            self.assertEqual(saved_user, user)
            persisted = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["jwt_dpapi"], "protected-dpapi-value")
            self.assertNotIn("jwt", persisted)
            self.assertNotIn("callback-account-token", session_file.read_text(encoding="utf-8"))
            self.assertNotIn("rotated-account-token", session_file.read_text(encoding="utf-8"))
            self.assertEqual(auth_cache["jwt"], "rotated-account-token")
            self.assertEqual(auth_cache["addon_tokens"], {})
            self.assertEqual(auth_cache["search_cache"], {})
            self.assertEqual(auth_cache["stream_cache"], {})

    def test_eclipse_account_status_verifies_session_and_returns_no_token(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            session_file.write_text(json.dumps({"version": 1, "jwt_dpapi": "protected-value"}), encoding="utf-8")
            user = {"id": "acct-2", "email": "listener@example.test"}
            auth_cache = {"jwt": None, "addon_tokens": {}, "manifest_cache": {}, "search_cache": {}, "stream_cache": {}}
            account_status = {"user": None, "checked_at": 0.0, "session_mtime": None}
            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service, "_ECLIPSE_ACCOUNT_STATUS", account_status), \
                    patch.object(service, "_eclipse_token_unprotect", return_value="private-account-token"), \
                    patch.object(service, "_verify_eclipse_account_token", return_value=(True, user, None)):
                result = service.eclipse_account_status()

            self.assertEqual(result, {"connected": True, "user": user})
            self.assertNotIn("token", result)
            self.assertEqual(auth_cache["jwt"], "private-account-token")

    def test_public_eclipse_auth_status_exposes_only_safe_login_error_code(self):
        with patch.object(service, "eclipse_account_status", return_value={"connected": False, "user": None}), \
                patch.object(service, "_ECLIPSE_LAST_LOGIN_ERROR", "unauthorized"):
            status = service.public_eclipse_auth_status()

        self.assertEqual(status["lastLoginError"], "unauthorized")
        self.assertNotIn("token", json.dumps(status).lower())
        self.assertNotIn("jwt", json.dumps(status).lower())

    def test_eclipse_account_status_does_not_clear_newer_account_after_stale_401(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            session_file.write_text(json.dumps({"version": 1, "jwt_dpapi": "old-protected"}), encoding="utf-8")
            auth_cache = {"jwt": "old-account-token", "addon_tokens": {}, "account_addons": {}, "manifest_cache": {}, "search_cache": {}, "stream_cache": {}}
            status = {"user": None, "checked_at": 0.0, "session_mtime": None}

            def stale_unauthorized(_token):
                with service._ECLIPSE_AUTH_LOCK:
                    service._ECLIPSE_AUTH_CACHE["jwt"] = "new-account-token"
                    service._invalidate_eclipse_account_cache(clear_jwt=False)
                return False, None, "unauthorized"

            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service, "_ECLIPSE_ACCOUNT_STATUS", status), \
                    patch.object(service, "_eclipse_token_unprotect", return_value="old-account-token"), \
                    patch.object(service, "_verify_eclipse_account_token", side_effect=stale_unauthorized):
                result = service.eclipse_account_status(force_refresh=True)

            self.assertEqual(result.get("reason"), "account_changed")
            self.assertTrue(session_file.exists())
            self.assertEqual(auth_cache["jwt"], "new-account-token")

    def test_eclipse_login_does_not_overwrite_account_changed_during_verification(self):
        with TemporaryDirectory() as temp:
            session_file = Path(temp) / "eclipse-session.json"
            auth_cache = {"jwt": "old-account-token", "addon_tokens": {}, "account_addons": {}, "manifest_cache": {}, "search_cache": {}, "stream_cache": {}}

            def stale_success(_token):
                with service._ECLIPSE_AUTH_LOCK:
                    service._ECLIPSE_AUTH_CACHE["jwt"] = "new-account-token"
                    service._invalidate_eclipse_account_cache(clear_jwt=False)
                return True, {"id": "old"}, "rotated-old-token"

            with patch.object(service, "ECLIPSE_SESSION_FILE", session_file), \
                    patch.object(service, "CONFIG_DIR", Path(temp)), \
                    patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                    patch.object(service, "_eclipse_token_protect", return_value="protected"), \
                    patch.object(service, "_verify_eclipse_account_token", side_effect=stale_success):
                connected, _, error = service._complete_eclipse_login("old-callback-token")

            self.assertFalse(connected)
            self.assertEqual(error, "account_changed")
            self.assertFalse(session_file.exists())
            self.assertEqual(auth_cache["jwt"], "new-account-token")

    def test_eclipse_account_addons_follow_connected_enabled_state_and_hide_token(self):
        account_token = "private-eclipse-session-token"
        addon_url = "https://supporter.example/account-secret/manifest.json"
        payload = {
            "addons": [
                {"addonId": "supporter.music", "displayName": "Supporter Music", "kind": "community", "manifestUrl": addon_url, "isEnabled": True},
                {"addonId": "disabled.music", "displayName": "Disabled Music", "kind": "community", "manifestUrl": "https://disabled.example/manifest.json", "isEnabled": False},
                {"addonId": "bad.url", "displayName": "Bad URL", "manifestUrl": "http://not-allowed.example/manifest.json", "isEnabled": True},
            ],
            "order": ["supporter.music", "disabled.music", "bad.url"],
        }
        auth_cache = {"jwt": account_token, "addon_tokens": {}, "account_addons": {}}
        account_status = {"user": {"id": "account-1", "email": "listener@example.test"}}
        opened = []

        def fake_urlopen(request, timeout=None):
            opened.append((request, timeout))
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch.object(service, "eclipse_account_status", return_value={"connected": True, **account_status}), \
                patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                patch.object(service, "_ECLIPSE_ACCOUNT_STATUS", account_status), \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen):
            result = service.eclipse_account_addons()
            cached = service.eclipse_account_addons()

        self.assertEqual(result, cached)
        self.assertEqual(len(opened), 1)
        request, timeout = opened[0]
        self.assertEqual(request.full_url, service.ECLIPSE_ACCOUNT_ADDONS_URL)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {account_token}")
        self.assertLessEqual(timeout, 8.0)
        self.assertEqual([addon["addonId"] for addon in result["addons"]], ["supporter.music", "disabled.music"])
        self.assertTrue(result["addons"][0]["isEnabled"])
        self.assertFalse(result["addons"][1]["isEnabled"])
        self.assertNotIn(account_token, json.dumps(result))

    def test_eclipse_account_addon_response_is_discarded_if_account_disconnects_mid_request(self):
        account_token = "account-A-token"
        auth_cache = {"jwt": account_token, "addon_tokens": {}, "account_addons": {}}
        started = threading.Event()
        release = threading.Event()
        result_holder = {}

        def fake_urlopen(request, timeout=None):
            started.set()
            self.assertTrue(release.wait(2.0))
            return io.BytesIO(json.dumps({"addons": [{
                "addonId": "supporter.music",
                "displayName": "Supporter Music",
                "manifestUrl": "https://supporter.example/account-a/manifest.json",
                "isEnabled": True,
            }]}).encode("utf-8"))

        with patch.object(service, "_get_eclipse_jwt", return_value=account_token), \
                patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
                patch.object(service, "_ECLIPSE_AUTH_GENERATION", 0), \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen):
            worker = threading.Thread(target=lambda: result_holder.setdefault("result", service.eclipse_account_addons()))
            worker.start()
            self.assertTrue(started.wait(2.0))
            with service._ECLIPSE_AUTH_LOCK:
                service._invalidate_eclipse_account_cache(clear_jwt=True)
            release.set()
            worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder["result"]["reason"], "account_changed")
        self.assertEqual(result_holder["result"]["addons"], [])
        self.assertEqual(auth_cache["account_addons"], {})

    def test_public_eclipse_addons_redacts_account_manifest_urls(self):
        secret_url = "https://addons.example/private-account-path/manifest.json?token=secret"
        result = service.public_eclipse_account_addons({
            "connected": True,
            "available": True,
            "order": ["supporter.music"],
            "addons": [{
                "addonId": "supporter.music",
                "displayName": "Supporter Music",
                "kind": "community",
                "manifestUrl": secret_url,
                "isEnabled": True,
            }],
        })

        serialized = json.dumps(result)
        self.assertNotIn(secret_url, serialized)
        self.assertNotIn("manifestUrl", result["addons"][0])
        self.assertEqual(result["addons"][0]["key"], service._eclipse_provider_key(secret_url, "supporter.music"))
        self.assertEqual(result["addons"][0]["addonId"], "supporter.music")

    def test_realmax_provider_configuration_defaults_to_tidal_then_enabled_eclipse_order(self):
        account_url = "https://account.example/private/manifest.json"
        account_addons = {
            "connected": True,
            "available": True,
            "addons": [
                {"addonId": "account.first", "displayName": "Account First", "kind": "community", "manifestUrl": account_url, "isEnabled": True},
                {"addonId": "account.off", "displayName": "Account Off", "kind": "community", "manifestUrl": "https://account.example/off.json", "isEnabled": False},
            ],
            "order": ["account.first", "account.off"],
        }
        settings = {"realmax_provider_order": None}
        with patch.object(service, "eclipse_account_addons", return_value=account_addons), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "_get_eclipse_manifest", return_value={"id": "manual.music", "name": "Manual Music"}):
            config = service.realmax_provider_configuration(settings)

        account_key = service._eclipse_provider_key(account_url, "account.first")
        self.assertEqual(config["order"], ["tidal_api", account_key])
        self.assertEqual([row["name"] for row in config["providers"] if row["selected"]], [
            "TIDAL API (EbbLabs / python-tidal)", "Account First"
        ])
        self.assertNotIn("manifestUrl", config["providers"][1])
        self.assertEqual(config["_internal_records"][account_key]["manifestUrl"], account_url)
        self.assertFalse(any(row.get("source") == "manual" for row in config["providers"]))

    def test_realmax_provider_order_validation_accepts_account_keys_and_rejects_duplicates(self):
        account_key = "eclipse:" + "a" * 20
        self.assertIsNone(service.validate_realmax_provider_order(None))
        self.assertEqual(service.validate_realmax_provider_order(["tidal_api", account_key]), ["tidal_api", account_key])
        with self.assertRaises(ValueError):
            service.validate_realmax_provider_order([account_key, account_key])
        with self.assertRaises(ValueError):
            service.validate_realmax_provider_order(["eclipse:bad"])
        capacity_keys = [f"eclipse:{index:020x}" for index in range(81)]
        with self.assertRaises(ValueError):
            service.validate_realmax_provider_order(capacity_keys + ["eclipse:" + "f" * 20])
        self.assertEqual(len(service.validate_realmax_provider_order(capacity_keys)), 81)

    def test_realmax_provider_configuration_preserves_unavailable_saved_provider_keys(self):
        account_key = "eclipse:" + "c" * 20
        settings = {"realmax_provider_order": ["tidal_api", account_key]}
        with patch.object(service, "eclipse_account_addons", return_value={
                "connected": True, "available": False, "addons": [], "order": [], "reason": "account_addons_unavailable"}), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True):
            config = service.realmax_provider_configuration(settings)

        self.assertEqual(config["configuredOrder"], ["tidal_api", account_key])
        self.assertEqual(config["order"], ["tidal_api"])
        unavailable = next(row for row in config["providers"] if row["key"] == account_key)
        self.assertFalse(unavailable["available"])
        self.assertTrue(unavailable["selected"])

    def test_realmax_provider_mode_filters_sources_without_changing_format_order(self):
        tidal = {"key": "tidal_api", "type": "tidal_api", "available": True}
        account_key = "eclipse:" + "b" * 20
        addon = {"key": account_key, "type": "eclipse_account", "available": True}
        records = {account_key: {"manifestUrl": "https://account.example/manifest.json"}}
        config = {"providers": [tidal, addon], "order": ["tidal_api", account_key], "_internal_records": records}
        with patch.object(service, "realmax_provider_configuration", return_value=config):
            order, by_key = service._ordered_resolution_providers({"realmax_source": "eclipse_only"})
        self.assertEqual(order, [account_key])
        self.assertEqual(by_key, records)

    def test_realmax_both_mode_preserves_saved_tidal_and_addon_priority(self):
        account_key = "eclipse:" + "d" * 20
        expected = [account_key, "tidal_api"]
        records = {account_key: {"manifestUrl": "https://account.example/manifest.json"}}
        config = {"providers": [], "order": expected, "_internal_records": records}
        with patch.object(service, "realmax_provider_configuration", return_value=config):
            order, by_key = service._ordered_resolution_providers({"realmax_source": "both"})
        self.assertEqual(order, expected)
        self.assertEqual(by_key, records)

    def test_tidal_only_resolution_skips_eclipse_provider_discovery(self):
        with patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "realmax_provider_configuration") as discover:
            order, records = service._ordered_resolution_providers({"realmax_source": "tidal_only"})
        self.assertEqual(order, ["tidal_api"])
        self.assertEqual(records, {})
        discover.assert_not_called()

    def test_tidal_only_respects_explicitly_empty_provider_selection(self):
        with patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "realmax_provider_configuration") as discover:
            order, records = service._ordered_resolution_providers({
                "realmax_source": "tidal_only",
                "realmax_provider_order": [],
            })
        self.assertEqual(order, [])
        self.assertEqual(records, {})
        discover.assert_not_called()

    def test_tidal_search_selection_keeps_quality_first_cross_provider_resolution_in_both_mode(self):
        key = "eclipse:" + "e" * 20
        provider = {"manifestUrl": "https://amazon.example/account/manifest.json", "addonId": "com.example.amazon", "name": "Amazon Music"}
        track = Mock()
        track.id = 123
        track.name = "Natural"
        track.full_name = "Natural"
        track.duration = 189
        track.artist = Mock(name="artist")
        track.artist.name = "Imagine Dragons"
        session = Mock()
        session.track.return_value = track
        session.search.return_value = {"tracks": []}
        expected = {"format": "EAC3_JOC", "sourceProvider": "AMAZON", "sourceName": "Amazon Music"}

        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=(["tidal_api", key], {key: provider})), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "get_python_tidal_session", return_value=session), \
                patch.object(service, "get_python_tidal_mobile_session", return_value=None), \
                patch.object(service, "resolve_eac3_joc", return_value=None), \
                patch.object(service, "resolve_eclipse_addon", return_value=expected) as resolve_addon:
            result = service.resolve_with_priority(123, ["EAC3_JOC", "LOSSLESS"], provider_key="tidal_api")

        self.assertEqual(result, expected)
        resolve_addon.assert_called_once()

    def test_eclipse_only_does_not_request_tidal_track_metadata(self):
        key = "eclipse:" + "f" * 20
        provider = {"manifestUrl": "https://addons.example/manifest.json", "addonId": "com.example.addon", "name": "Example"}
        with patch.object(service, "load_settings", return_value={"realmax_source": "eclipse_addons"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=([key], {key: provider})), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "get_python_tidal_session") as tidal_session, \
                patch.object(service, "get_python_tidal_mobile_session") as mobile_session, \
                patch.object(service, "resolve_eclipse_addon", return_value={"format": "EAC3_JOC", "uri": "https://cdn.example/stream"}):
            result = service.resolve_with_priority(321, ["EAC3_JOC"])
        self.assertEqual(result["format"], "EAC3_JOC")
        tidal_session.assert_not_called()
        mobile_session.assert_not_called()

    def test_eclipse_search_track_rejects_unrelated_or_conflicting_recordings(self):
        unrelated = [{"id": "wrong", "title": "Different Song", "artist": "Different Artist", "isrc": "USAAA0000001"}]
        conflicting = [{"id": "wrong-isrc", "title": "Natural", "artist": "Imagine Dragons", "isrc": "USAAA0000001"}]
        self.assertIsNone(service._select_eclipse_search_track(unrelated, "Natural", "Imagine Dragons", "EAC3_JOC", "USAAA0000002", 189))
        self.assertIsNone(service._select_eclipse_search_track(conflicting, "Natural", "Imagine Dragons", "EAC3_JOC", "USAAA0000002", 189))
        self.assertEqual(
            service._select_eclipse_search_track(
                [{"id": "match", "title": "Natural", "artist": "Imagine Dragons", "duration": 189}],
                "Natural", "Imagine Dragons", "EAC3_JOC", duration=189,
            )["id"],
            "match",
        )

    def test_tidal_alternate_edition_match_requires_same_recording_identity(self):
        original = Mock(id=100, name="Natural", duration=189, isrc="USAAA0000001")
        original.name = "Natural"
        original.artist.name = "Imagine Dragons"
        wrong_artist = Mock(id=101, name="Natural", duration=189, isrc="")
        wrong_artist.name = "Natural"
        wrong_artist.artist.name = "Different Artist"
        conflicting_isrc = Mock(id=102, name="Natural", duration=189, isrc="USAAA0000002")
        conflicting_isrc.name = "Natural"
        conflicting_isrc.artist.name = "Imagine Dragons"
        matching_isrc = Mock(id=103, name="Natural", duration=189, isrc="USAAA0000001")
        matching_isrc.name = "Natural"
        matching_isrc.artist.name = "Another credited performer"

        self.assertFalse(service._tidal_alternate_track_matches(original, wrong_artist))
        self.assertFalse(service._tidal_alternate_track_matches(original, conflicting_isrc))
        self.assertTrue(service._tidal_alternate_track_matches(original, matching_isrc))

        no_isrc_original = Mock(id=200, name="Natural", duration=189, isrc="")
        no_isrc_original.name = "Natural"
        no_isrc_original.artist.name = "Imagine Dragons"
        same_artist = Mock(id=201, name="Natural", duration=194, isrc="")
        same_artist.name = "Natural"
        same_artist.artist.name = "Imagine Dragons"
        self.assertTrue(service._tidal_alternate_track_matches(no_isrc_original, same_artist))

    def test_realmax_resolves_in_format_order_then_selected_provider_order(self):
        first_key = "eclipse:" + "1" * 20
        second_key = "eclipse:" + "2" * 20
        providers = {
            first_key: {"manifestUrl": "https://first.example/manifest.json", "displayName": "First"},
            second_key: {"manifestUrl": "https://second.example/manifest.json", "displayName": "Second"},
        }
        attempts = []

        def resolve_addon(url, isrc, fmt, *args, **kwargs):
            attempts.append((url, fmt))
            return {"format": fmt, "sourceProvider": "second"} if url == providers[second_key]["manifestUrl"] else None

        with patch.object(service, "load_settings", return_value={"realmax_source": "auto"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=([first_key, second_key, "tidal_api"], providers)), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "get_python_tidal_session", return_value=None), \
                patch.object(service, "get_python_tidal_mobile_session", return_value=None), \
                patch.object(service, "resolve_eclipse_addon", side_effect=resolve_addon), \
                patch.object(service, "resolve_eac3_joc") as resolve_tidal:
            result = service.resolve_with_priority(12345, ["EAC3_JOC", "LOSSLESS"])

        self.assertEqual(result["format"], "EAC3_JOC")
        self.assertEqual(attempts, [
            (providers[first_key]["manifestUrl"], "EAC3_JOC"),
            (providers[second_key]["manifestUrl"], "EAC3_JOC"),
        ])
        resolve_tidal.assert_not_called()

    def test_search_result_provider_key_keeps_numeric_eclipse_id_on_its_originating_addon(self):
        first_key = "eclipse:" + "a" * 20
        second_key = "eclipse:" + "b" * 20
        providers = {
            first_key: {"manifestUrl": "https://first.example/manifest.json", "addonId": "com.example.first", "name": "First"},
            second_key: {"manifestUrl": "https://second.example/manifest.json", "addonId": "com.example.second", "name": "Second"},
        }
        attempts = []

        def resolve_addon(url, isrc, fmt, *args, **kwargs):
            attempts.append((url, kwargs.get("explicit_track_id"), kwargs.get("addon_id")))
            return {"format": fmt, "uri": "https://cdn.example/first.flac"} if url == providers[first_key]["manifestUrl"] else None

        with patch.object(service, "load_settings", return_value={"realmax_source": "auto"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=([first_key, second_key, "tidal_api"], providers)), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "resolve_eclipse_addon", side_effect=resolve_addon), \
                patch.object(service, "resolve_eac3_joc") as resolve_tidal:
            result = service.resolve_with_priority(
                123456,
                ["AC4_IMS", "EAC3_JOC", "LOSSLESS"],
                provider_key=first_key,
            )

        self.assertEqual(result["format"], "EAC3_JOC")
        self.assertEqual(attempts, [(providers[first_key]["manifestUrl"], "123456", "com.example.first")])
        resolve_tidal.assert_not_called()

    def test_eclipse_login_rejects_malformed_callback_token_without_network(self):
        with patch.object(service, "_verify_eclipse_account_token") as verify:
            for value in ("", "bad\ntoken", "x" * 8193):
                with self.subTest(value_length=len(value)):
                    connected, user, error = service._complete_eclipse_login(value)
                    self.assertFalse(connected)
                    self.assertIsNone(user)
                    self.assertEqual(error, "invalid_token")
            verify.assert_not_called()

    @unittest.skipUnless(service.os.name == "nt", "Windows DPAPI is used for the persisted Eclipse session")
    def test_eclipse_token_dpapi_round_trip(self):
        token = "test-only-eclipse-account-token"
        protected = service._eclipse_token_protect(token)
        self.assertNotEqual(protected, token)
        self.assertEqual(service._eclipse_token_unprotect(protected), token)

    def test_multi_search_queries_each_configured_eclipse_provider_with_its_manifest_id(self):
        amazon_url = "https://amazon.example/token/manifest.json"
        tido_url = "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df/manifest.json"
        manifests = {
            amazon_url: {"id": "com.yourname.amazonmusic", "name": "AMZM"},
            tido_url: {"id": "com.yourname.tidal.hifi", "name": "Tido"},
        }
        response_by_path = {
            "/token/search?q=Natural": {"tracks": [{"id": "amz-1", "title": "Natural", "artist": "Imagine Dragons"}]},
        }
        seen_urls = []

        def fake_urlopen(request, timeout=None):
            seen_urls.append(request.full_url)
            parsed = urllib.parse.urlparse(request.full_url)
            payload = response_by_path.get(parsed.path + "?" + parsed.query)
            return io.BytesIO(json.dumps(payload or {"tracks": [{"id": "97874144", "title": "Natural", "artist": "Imagine Dragons"}]}).encode("utf-8"))

        account_addons = {"connected": True, "available": True, "order": ["com.yourname.amazonmusic", "com.yourname.tidal.hifi"], "addons": [
            {"addonId": "com.yourname.amazonmusic", "displayName": "AMZM", "manifestUrl": amazon_url, "isEnabled": True},
            {"addonId": "com.yourname.tidal.hifi", "displayName": "Tido", "manifestUrl": tido_url, "isEnabled": True},
        ]}
        with patch.object(service, "get_python_tidal_session", return_value=None), \
                patch.object(service, "load_settings", return_value={"realmax_provider_order": None}), \
                patch.object(service, "eclipse_account_addons", return_value=account_addons), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=False), \
                patch.object(service, "_get_eclipse_manifest", side_effect=lambda url: manifests[url]), \
                patch.object(service, "_get_eclipse_addon_access_token", side_effect=lambda addon_id: None) as token_for, \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen):
            result = service.execute_multi_source_search("Natural", limit=10)

        self.assertEqual({item["source"] for item in result["tracks"]}, {"AMAZON", "TIDAL_ECLIPSE"})
        token_for.assert_any_call("com.yourname.amazonmusic")
        token_for.assert_any_call("com.yourname.tidal.hifi")
        self.assertIn("https://amazon.example/token/search?q=Natural", seen_urls)
        self.assertTrue(all(item.get("providerKey") for item in result["tracks"]))
        self.assertIn(
            "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df/search?q=Natural",
            seen_urls,
        )

    def test_multi_search_keeps_results_from_distinct_addons_with_same_display_name(self):
        first = "eclipse:" + "1" * 20
        second = "eclipse:" + "2" * 20
        records = {
            first: {"manifestUrl": "https://one.example/manifest.json", "addonId": "one", "name": "Shared Name"},
            second: {"manifestUrl": "https://two.example/manifest.json", "addonId": "two", "name": "Shared Name"},
        }

        def fake_open(request, timeout=None):
            track_id = "one-track" if "one.example" in request.full_url else "two-track"
            return io.BytesIO(json.dumps({"tracks": [{"id": track_id, "title": "Same Song", "artist": "Same Artist"}]}).encode())

        with patch.object(service, "load_settings", return_value={}), \
                patch.object(service, "_ordered_resolution_providers", return_value=([first, second], records)), \
                patch.object(service, "get_python_tidal_session", return_value=None), \
                patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            result = service.execute_multi_source_search("Same Song", limit=10)

        self.assertEqual({item["id"] for item in result["tracks"]}, {"one-track", "two-track"})

    def test_generic_eclipse_resolution_uses_provider_token_and_label(self):
        manifest_url = "https://apple.example/account-scope/manifest.json"
        access_token = "scoped-short-lived-token"
        seen_requests = []
        payload = {"streams": [{
            "url": "https://cdn.example/track.flac",
            "codec": "flac",
            "quality": "Lossless",
            "sampleRate": 44100,
            "bitDepth": 16,
            "channels": 2,
        }]}

        def fake_urlopen(request, timeout=None):
            seen_requests.append(request)
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch.object(service, "_ECLIPSE_AUTH_CACHE", {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}), \
                patch.object(service, "_get_eclipse_addon_access_token", return_value=access_token) as get_token, \
                patch.object(service, "_probe_direct_audio_bitrate", return_value=(1_411_200, "test")), \
                patch.object(service, "_probe_direct_media_transport", return_value="DIRECT"), \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen):
            result = service.resolve_eclipse_addon(
                manifest_url,
                "USABC1234567",
                "LOSSLESS",
                explicit_track_id="apple-track-1",
                addon_id="com.example.applemusic",
                addon_name="Apple Music Eclipse",
            )

        self.assertIsNotNone(result)
        get_token.assert_called_once_with("com.example.applemusic")
        self.assertEqual(len(seen_requests), 1)
        self.assertEqual(
            next(value for key, value in seen_requests[0].header_items() if key.lower() == "x-eclipse-access"),
            access_token,
        )
        self.assertEqual(result["sourceProvider"], "ECLIPSE")
        self.assertEqual(result["sourceName"], "Apple Music Eclipse")
        self.assertTrue(result["label"].startswith("[Apple Music Eclipse]"))

    def test_multi_search_follows_all_selected_eclipse_providers_when_tidal_api_is_authorized(self):
        amazon_url = "https://amazon.example/token/manifest.json"
        tido_url = "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df/manifest.json"
        manifests = {
            amazon_url: {"id": "com.yourname.amazonmusic", "name": "AMZM"},
            tido_url: {"id": "com.yourname.tidal.hifi", "name": "Tido"},
        }
        urls = []

        def fake_urlopen(request, timeout=None):
            urls.append(request.full_url)
            return io.BytesIO(json.dumps({"tracks": []}).encode("utf-8"))

        account_addons = {"connected": True, "available": True, "order": ["com.yourname.amazonmusic", "com.yourname.tidal.hifi"], "addons": [
            {"addonId": "com.yourname.amazonmusic", "displayName": "AMZM", "manifestUrl": amazon_url, "isEnabled": True},
            {"addonId": "com.yourname.tidal.hifi", "displayName": "Tido", "manifestUrl": tido_url, "isEnabled": True},
        ]}
        with patch.object(service, "get_python_tidal_session", return_value=None), \
                patch.object(service, "load_settings", return_value={"realmax_provider_order": None}), \
                patch.object(service, "eclipse_account_addons", return_value=account_addons), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "_get_eclipse_manifest", side_effect=lambda url: manifests[url]) as get_manifest, \
                patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
                patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen):
            service.execute_multi_source_search("Natural", limit=10)

        get_manifest.assert_not_called()
        self.assertCountEqual(urls, [
            "https://amazon.example/token/search?q=Natural",
            "https://tido.tido.tido.rickyaddons.dpdns.org/bd6335b8e3c74a388134bf5f5bc3b3df/search?q=Natural",
        ])

    def test_legacy_manual_eclipse_manifests_are_removed_from_saved_settings(self):
        with TemporaryDirectory() as temp:
            settings_file = Path(temp) / "settings.json"
            settings_file.write_text(
                json.dumps({"eclipse_addons": ["https://legacy.example/private/path?token=secret"], "volume": 71}),
                encoding="utf-8",
            )
            with patch.object(service, "SETTINGS_FILE", settings_file):
                settings = service.load_settings()
            persisted_text = settings_file.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)
        self.assertNotIn("eclipse_addons", settings)
        self.assertNotIn("eclipse_addons", persisted)
        self.assertNotIn("secret", persisted_text)
        self.assertEqual(settings["volume"], 71)

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
        self.assertIn("check_service_health.py", source)
        self.assertNotIn("findstr /C:", source)

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
        self.assertIn("check_service_health.py", source)
        self.assertIn(f"--version {service.SERVICE_VERSION}", source)
        self.assertIn("OMNIFUSION_TIDAL_PID", source)
        self.assertIn("OMNIFUSION_PLUGIN_DIR", source)
        self.assertIn('if not "!HEALTH_CHECK!"=="2"', source)
        self.assertNotIn("findstr /C:", source)
        self.assertIn("if not defined OMNIFUSION_TIDAL_PID", source)
        self.assertIn("must be started by the Omniphony EX plugin inside TIDAL", source)


if __name__ == "__main__":
    unittest.main()
