import io
import json
import socketserver
import sys
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request
from unittest.mock import patch
import pytest


API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import omnifusion_service as service


@pytest.fixture(autouse=True)
def enable_experimental_auth_helpers_for_unit_tests(monkeypatch, tmp_path):
    """Exercise the APK-backed flow while keeping each test off the user's session file."""
    monkeypatch.setattr(service, "ECLIPSE_ACCOUNT_LINKING_SUPPORTED", True)
    monkeypatch.setattr(service, "ECLIPSE_SESSION_FILE", tmp_path / "eclipse-session.json")


def test_apk_backed_account_linking_is_enabled():
    assert service.ECLIPSE_ACCOUNT_LINKING_SUPPORTED is True


def test_apk_callback_identity_must_match_verified_token(monkeypatch):
    attempt_id = "f" * 32
    monkeypatch.setattr(service, "_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID", attempt_id)
    monkeypatch.setattr(service, "_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT", time.time())
    with patch.object(
        service,
        "_verify_eclipse_account_token",
        return_value=(True, {"id": "verified-user", "email": "user@example.com"}, None),
    ), patch.object(service, "_write_eclipse_session") as save_session:
        connected, user, error = service._complete_eclipse_login(
            "verified-token", attempt_id, "different-user", "user@example.com"
        )
    assert connected is False
    assert user is None
    assert error == "identity_mismatch"
    save_session.assert_not_called()


def test_apk_login_posts_require_current_native_service_credential(monkeypatch):
    shutdown_token = "a" * 64
    service_instance_id = "service-instance-test"
    monkeypatch.setattr(service, "SHUTDOWN_TOKEN", shutdown_token)
    monkeypatch.setattr(service, "SERVICE_INSTANCE_ID", service_instance_id)
    monkeypatch.setattr(service, "_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID", None)
    monkeypatch.setattr(service, "_ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT", 0.0)
    monkeypatch.setattr(service, "_ECLIPSE_FINALIZED_LOGIN_ATTEMPTS", {})

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), service.RequestHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    attempt_id = "e" * 32

    def post(route, body, extra_headers=None):
        headers = {"Origin": base_url, "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            base_url + route,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    headers = {
        "Authorization": f"Bearer {shutdown_token}",
        "X-Omniphony-Service-Instance": service_instance_id,
    }
    try:
        flow_body = {"reason": "started", "attemptId": attempt_id}
        completion_body = {
            "token": "apk-returned-token",
            "userId": "account-id",
            "email": "listener@example.test",
            "attemptId": attempt_id,
        }
        assert post("/eclipse-auth/flow-status", flow_body)[0] == 403
        assert post("/eclipse-auth/flow-status", flow_body, {
            **headers,
            "Authorization": "Bearer " + "b" * 64,
        })[0] == 403
        assert post("/eclipse-auth/flow-status", flow_body, {
            **headers,
            "X-Omniphony-Service-Instance": "another-service",
        })[0] == 403
        assert service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is None

        assert post("/eclipse-auth/flow-status", flow_body, headers) == (200, {"recorded": True})
        assert post("/eclipse-auth/complete", completion_body)[0] == 403
        with patch.object(
            service,
            "_complete_eclipse_login",
            return_value=(True, {"id": "account-id"}, None),
        ) as complete_login:
            status, result = post("/eclipse-auth/complete", completion_body, headers)
        assert status == 200
        assert result == {"connected": True, "user": {"id": "account-id"}}
        complete_login.assert_called_once_with(
            "apk-returned-token", attempt_id, "account-id", "listener@example.test"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_failed_login_status_is_visible_while_old_session_remains_connected():
    previous_error = service._ECLIPSE_LAST_LOGIN_ERROR
    previous_attempt = service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID
    previous_status_fn = service.eclipse_account_status
    try:
        service.eclipse_account_status = lambda: {
            "connected": True,
            "user": {"id": "old-session", "name": "Old session"},
        }
        attempt_id = "a" * 32
        assert service.record_eclipse_login_flow_status("started", attempt_id) == "recorded"
        assert service.record_eclipse_login_flow_status("callback_missing", attempt_id) == "recorded"
        status = service.public_eclipse_auth_status()
        assert status["connected"] is True
        assert status["lastLoginError"] == "callback_missing"
        assert status["loginAttemptActive"] is False
    finally:
        service.eclipse_account_status = previous_status_fn
        service._ECLIPSE_LAST_LOGIN_ERROR = previous_error
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = previous_attempt


def test_login_flow_status_rejects_arbitrary_values():
    previous_error = service._ECLIPSE_LAST_LOGIN_ERROR
    previous_attempt = service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID
    try:
        attempt_id = "b" * 32
        assert service.record_eclipse_login_flow_status("started", attempt_id) == "recorded"
        for invalid in (None, True, 123, [], {}, {"nested": []}, "https://example.invalid/?token=secret"):
            assert service.record_eclipse_login_flow_status(invalid, attempt_id) == "invalid"
        assert service._ECLIPSE_LAST_LOGIN_ERROR is None
        assert service.record_eclipse_login_flow_status("oauth_denied", "c" * 32) == "stale"
        assert service.record_eclipse_login_flow_status("oauth_denied", attempt_id) == "recorded"
        assert service._ECLIPSE_LAST_LOGIN_ERROR == "oauth_denied"
    finally:
        service._ECLIPSE_LAST_LOGIN_ERROR = previous_error
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = previous_attempt


def test_stale_login_attempt_is_rejected_before_token_verification():
    previous_attempt = service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID
    try:
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = "d" * 32
        with patch.object(
            service,
            "_verify_eclipse_account_token",
            side_effect=AssertionError("stale login attempts must not reach Eclipse verification"),
        ):
            connected, user, error = service._complete_eclipse_login("stale-account-token", "e" * 32)
        assert connected is False
        assert user is None
        assert error == "account_changed"
    finally:
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = previous_attempt


def test_login_attempt_start_is_idempotent_and_never_replaces_an_active_flow():
    previous = (
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID,
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT,
        dict(service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS),
        service._ECLIPSE_LAST_LOGIN_ERROR,
    )
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = None
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = 0.0
    service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS = {}
    try:
        first = "1" * 32
        second = "2" * 32
        assert service.record_eclipse_login_flow_status("started", first) == "recorded"
        assert service.record_eclipse_login_flow_status("started", first) == "recorded"
        assert service.record_eclipse_login_flow_status("started", second) == "busy"
        assert service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID == first
        assert service.record_eclipse_login_flow_status("oauth_cancelled", first) == "recorded"
        assert service.record_eclipse_login_flow_status("started", first) == "stale"
        assert service.record_eclipse_login_flow_status("started", second) == "recorded"
    finally:
        (service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID,
         service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT,
         finalized, service._ECLIPSE_LAST_LOGIN_ERROR) = previous
        service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS = finalized


def test_login_completion_commits_session_and_attempt_state_together(tmp_path):
    previous = {
        "session_file": service.ECLIPSE_SESSION_FILE,
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "auth_generation": service._ECLIPSE_AUTH_GENERATION,
        "session_generation": service._ECLIPSE_ACCOUNT_SESSION_GENERATION,
        "attempt": service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID,
        "attempt_started": service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT,
        "finalized": dict(service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS),
        "error": service._ECLIPSE_LAST_LOGIN_ERROR,
    }
    service.ECLIPSE_SESSION_FILE = tmp_path / "eclipse-session.json"
    service._ECLIPSE_AUTH_CACHE = {"jwt": None, "addon_tokens": {}, "account_addons": {},
                                   "manifest_cache": {}, "search_cache": {}, "stream_cache": {},
                                   "tido_manifest_url": None}
    service._ECLIPSE_ACCOUNT_STATUS = {"user": None, "checked_at": 0.0, "session_mtime": None}
    service._ECLIPSE_AUTH_GENERATION = 0
    service._ECLIPSE_ACCOUNT_SESSION_GENERATION = 7
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = None
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = 0.0
    service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS = {}
    user = {"id": "real-account", "email": "listener@example.test"}
    attempt_id = "3" * 32
    try:
        assert service.record_eclipse_login_flow_status("started", attempt_id) == "recorded"

        def persist(token, persisted_user):
            assert token == "verified-account-token"
            assert persisted_user == user
            service.ECLIPSE_SESSION_FILE.write_text("saved", encoding="utf-8")

        with patch.object(service, "_verify_eclipse_account_token", return_value=(True, user, None)), \
                patch.object(service, "_write_eclipse_session", side_effect=persist):
            connected, verified_user, error = service._complete_eclipse_login("verified-account-token", attempt_id)

        assert (connected, verified_user, error) == (True, user, None)
        assert service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID is None
        assert service._ECLIPSE_LAST_LOGIN_ERROR is None
        assert service._ECLIPSE_AUTH_CACHE["jwt"] == "verified-account-token"
        assert service._ECLIPSE_ACCOUNT_STATUS["user"] == user
        assert service._ECLIPSE_ACCOUNT_SESSION_GENERATION == 8
        assert attempt_id in service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS
    finally:
        service.ECLIPSE_SESSION_FILE = previous["session_file"]
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["auth_generation"]
        service._ECLIPSE_ACCOUNT_SESSION_GENERATION = previous["session_generation"]
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = previous["attempt"]
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = previous["attempt_started"]
        service._ECLIPSE_FINALIZED_LOGIN_ATTEMPTS = previous["finalized"]
        service._ECLIPSE_LAST_LOGIN_ERROR = previous["error"]


def test_expired_login_attempt_cannot_commit_a_verified_token():
    previous = (
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID,
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT,
    )
    attempt_id = "4" * 32
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID = attempt_id
    service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = time.time() - service.ECLIPSE_LOGIN_ATTEMPT_TTL_SECONDS - 1
    try:
        with patch.object(service, "_verify_eclipse_account_token", side_effect=AssertionError("expired flow reached verification")):
            connected, user, error = service._complete_eclipse_login("expired-token", attempt_id)
        assert connected is False and user is None and error == "account_changed"
    finally:
        service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_ID, service._ECLIPSE_ACTIVE_LOGIN_ATTEMPT_STARTED_AT = previous




def test_verified_login_requires_a_stable_account_identity():
    for raw_user in ({}, {"name": "Listener"}, {"email": "  listener@example.test  ", "name": "Listener\x00"}):
        payload = json.dumps({"success": True, "user": raw_user}).encode("utf-8")
        with patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(payload)):
            connected, user, error = service._verify_eclipse_account_token("account-token")
        if raw_user.get("email"):
            assert connected is True
            assert user == {"email": "listener@example.test"}
            assert error is None
        else:
            assert connected is False
            assert user is None
            assert error == "invalid_identity"


def test_synthetic_identity_is_a_permanent_verification_failure():
    payload = json.dumps({
        "success": True,
        "user": {
            "id": "8bd1a1d7-c9c1-437e-94c4-26c966a87aa4",
            "name": "OmniUser",
            "email": "omni_test_user_7721@test.com",
        },
    }).encode("utf-8")
    with patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(payload)):
        assert service._verify_eclipse_account_token("account-token") == (
            False,
            None,
            "synthetic_account_rejected",
        )


def test_explicit_verification_rejection_and_malformed_response_are_distinct():
    rejected = json.dumps({"success": False}).encode("utf-8")
    with patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(rejected)):
        assert service._verify_eclipse_account_token("account-token") == (False, None, "unauthorized")

    malformed = json.dumps({"message": "verification response changed"}).encode("utf-8")
    with patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(malformed)):
        assert service._verify_eclipse_account_token("account-token") == (
            False,
            None,
            "verification_failed",
        )


def test_verified_account_may_rotate_its_session_token():
    user = {"id": "account-1", "email": "listener@example.test"}
    payload = json.dumps({"success": True, "user": user, "token": "rotated-token"}).encode("utf-8")
    with patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(payload)):
        assert service._verify_eclipse_account_token("old-token") == (True, user, "rotated-token")


def test_verified_identity_rejects_synthetic_omni_test_account():
    assert service._normalize_verified_eclipse_user({
        "id": "8bd1a1d7-c9c1-437e-94c4-26c966a87aa4",
        "name": "OmniUser",
        "email": "omni_test_user_7721@test.com",
    }) is None
    assert service._normalize_verified_eclipse_user({
        "id": "another-id",
        "name": "Different display name",
        "email": "omni_test_user_7721@test.com",
    }) is None


def test_cached_synthetic_account_is_reverified_and_never_reported_connected(tmp_path):
    previous = {
        "session_file": service.ECLIPSE_SESSION_FILE,
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "get_token": service._get_eclipse_jwt,
        "verify": service._verify_eclipse_account_token,
    }
    session_file = tmp_path / "eclipse-session.json"
    session_file.write_text("{}", encoding="utf-8")
    service.ECLIPSE_SESSION_FILE = session_file
    service._ECLIPSE_AUTH_CACHE = {
        "jwt": "synthetic-token",
        "addon_tokens": {},
        "account_addons": {},
        "manifest_cache": {},
        "search_cache": {},
        "stream_cache": {},
        "tido_manifest_url": None,
    }
    service._ECLIPSE_ACCOUNT_STATUS = {
        "user": {
            "id": "8bd1a1d7-c9c1-437e-94c4-26c966a87aa4",
            "name": "OmniUser",
            "email": "omni_test_user_7721@test.com",
        },
        "checked_at": 9999999999,
        "session_mtime": session_file.stat().st_mtime,
    }
    service._ECLIPSE_AUTH_GENERATION = 0
    try:
        with patch.object(service, "_get_eclipse_jwt", return_value="synthetic-token") as get_token, \
                patch.object(service, "_verify_eclipse_account_token", return_value=(False, None, "synthetic_account_rejected")) as verify:
            status = service.eclipse_account_status()
        assert status == {"connected": False, "user": None, "reason": "synthetic_account_rejected"}
        assert get_token.call_count == 1
        assert verify.call_count == 1
        assert not session_file.exists()
    finally:
        service.ECLIPSE_SESSION_FILE = previous["session_file"]
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service._get_eclipse_jwt = previous["get_token"]
        service._verify_eclipse_account_token = previous["verify"]


def test_failed_placeholder_session_cleanup_still_clears_memory_auth(tmp_path):
    previous = {
        "session_file": service.ECLIPSE_SESSION_FILE,
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "get_token": service._get_eclipse_jwt,
    }
    session_file = tmp_path / "eclipse-session.json"
    session_file.write_text("preserved for the simulated file lock", encoding="utf-8")
    service.ECLIPSE_SESSION_FILE = session_file
    service._ECLIPSE_AUTH_CACHE = {"jwt": "stale-token", "addon_tokens": {"addon": "stale"}}
    service._ECLIPSE_ACCOUNT_STATUS = {"user": None, "checked_at": 0.0, "session_mtime": None}
    service._ECLIPSE_AUTH_GENERATION = 0
    try:
        with patch.object(service, "_get_eclipse_jwt", return_value="stale-token"), \
                patch.object(service, "_verify_eclipse_account_token", return_value=(False, None, "synthetic_account_rejected")), \
                patch.object(Path, "unlink", side_effect=PermissionError("file in use")):
            status = service.eclipse_account_status()
        assert status == {"connected": False, "user": None, "reason": "session_cleanup_failed"}
        assert service._ECLIPSE_AUTH_CACHE["jwt"] is None
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"] == {}
        assert session_file.exists()
    finally:
        service.ECLIPSE_SESSION_FILE = previous["session_file"]
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service._get_eclipse_jwt = previous["get_token"]


def test_transient_verification_failure_clears_verified_identity_and_retries(tmp_path):
    previous = {
        "session_file": service.ECLIPSE_SESSION_FILE,
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "get_token": service._get_eclipse_jwt,
        "verify": service._verify_eclipse_account_token,
    }
    session_file = tmp_path / "eclipse-session.json"
    session_file.write_text("preserved", encoding="utf-8")
    service.ECLIPSE_SESSION_FILE = session_file
    service._ECLIPSE_AUTH_CACHE = {
        "jwt": "verified-token",
        "addon_tokens": {"addon": {"token": "cached-addon-token"}},
        "account_addons": {"current": {"result": {"addons": [
            {"manifestUrl": "https://example.test/manifest.json"},
        ]}}},
        "manifest_cache": {},
        "search_cache": {},
        "stream_cache": {},
        "tido_manifest_url": None,
    }
    service._ECLIPSE_ACCOUNT_STATUS = {
        "user": {"id": "account-1", "email": "listener@example.test"},
        "checked_at": 9999999999,
        "session_mtime": session_file.stat().st_mtime,
    }
    service._ECLIPSE_AUTH_GENERATION = 0
    verified_user = {"id": "account-1", "email": "listener@example.test"}
    try:
        with patch.object(service, "_get_eclipse_jwt", return_value="verified-token"), \
                patch.object(service, "_verify_eclipse_account_token", side_effect=[
                    (False, None, "verification_failed"),
                    (True, verified_user, None),
                ]) as verify, \
                patch.object(service, "_write_eclipse_session"):
            failed = service.eclipse_account_status(force_refresh=True)
            assert failed == {"connected": False, "user": None, "reason": "verification_failed"}
            assert service._ECLIPSE_ACCOUNT_STATUS["user"] is None
            assert service._ECLIPSE_AUTH_CACHE["jwt"] == "verified-token"
            assert session_file.read_text(encoding="utf-8") == "preserved"

            recovered = service.eclipse_account_status()

        assert recovered == {"connected": True, "user": verified_user}
        assert verify.call_count == 2
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"] == {}
        assert service._ECLIPSE_AUTH_CACHE["account_addons"] == {}
    finally:
        service.ECLIPSE_SESSION_FILE = previous["session_file"]
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service._get_eclipse_jwt = previous["get_token"]
        service._verify_eclipse_account_token = previous["verify"]


def test_account_addons_and_cached_addon_token_require_verified_session():
    previous_cache = service._ECLIPSE_AUTH_CACHE
    service._ECLIPSE_AUTH_CACHE = {
        "jwt": "stale-account-token",
        "addon_tokens": {"com.yourname.amazonmusic": {"token": "cached-addon-token", "expires_at": 9999999999}},
    }
    try:
        with patch.object(service, "eclipse_account_status", return_value={
            "connected": False,
            "user": None,
            "reason": "synthetic_account_rejected",
        }), patch.object(
            service,
            "_get_eclipse_jwt",
            side_effect=AssertionError("an unverified account must not reach account APIs"),
        ):
            assert service.eclipse_account_addons() == {
                "connected": False,
                "available": False,
                "addons": [],
                "order": [],
                "reason": "synthetic_account_rejected",
            }
            assert service._get_eclipse_addon_access_token() is None
    finally:
        service._ECLIPSE_AUTH_CACHE = previous_cache


def test_reloading_a_changed_token_invalidates_previous_account_caches(tmp_path):
    previous = {
        "session_file": service.ECLIPSE_SESSION_FILE,
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
    }
    session_file = tmp_path / "eclipse-session.json"
    session_file.write_text("placeholder", encoding="utf-8")
    service.ECLIPSE_SESSION_FILE = session_file
    service._ECLIPSE_AUTH_CACHE = {
        "jwt": "old-token",
        "addon_tokens": {"addon": {"token": "old-addon-token"}},
        "account_addons": {"current": {"result": {"addons": [
            {"manifestUrl": "https://example.test/manifest.json"},
        ]}}},
        "manifest_cache": {"https://example.test/manifest.json": {"name": "Old account addon"}},
        "search_cache": {"old": "old"},
        "stream_cache": {"old": "old"},
        "tido_manifest_url": None,
    }
    service._ECLIPSE_ACCOUNT_STATUS = {
        "user": {"id": "old-account", "email": "old@example.test"},
        "checked_at": 9999999999,
        "session_mtime": session_file.stat().st_mtime,
    }
    service._ECLIPSE_AUTH_GENERATION = 0
    try:
        with patch.object(service, "_read_eclipse_session", return_value={"jwt": "new-token", "user": None}):
            assert service._get_eclipse_jwt(force_refresh=True) == "new-token"
        assert service._ECLIPSE_ACCOUNT_STATUS["user"] is None
        assert service._ECLIPSE_AUTH_CACHE["jwt"] == "new-token"
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"] == {}
        assert service._ECLIPSE_AUTH_CACHE["account_addons"] == {}
        assert service._ECLIPSE_AUTH_CACHE["manifest_cache"] == {}
        assert service._ECLIPSE_AUTH_CACHE["search_cache"] == {}
        assert service._ECLIPSE_AUTH_CACHE["stream_cache"] == {}
    finally:
        service.ECLIPSE_SESSION_FILE = previous["session_file"]
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]


def test_addon_access_token_network_request_runs_outside_auth_lock_and_caches_token():
    user = {"id": "account-a", "email": "listener@example.test"}
    previous = {
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "lock": service._ECLIPSE_AUTH_LOCK,
        "status_fn": service.eclipse_account_status,
    }

    class InspectingLock:
        depth = 0

        def __enter__(self):
            self.depth += 1
            return self

        def __exit__(self, _kind, _value, _traceback):
            self.depth -= 1

    lock = InspectingLock()
    service._ECLIPSE_AUTH_LOCK = lock
    service._ECLIPSE_AUTH_CACHE = {"jwt": "account-jwt", "addon_tokens": {}}
    service._ECLIPSE_ACCOUNT_STATUS = {"user": user, "checked_at": 0, "session_mtime": 1}
    service._ECLIPSE_AUTH_GENERATION = 5
    service.eclipse_account_status = lambda force_refresh=False: {"connected": True, "user": user}
    try:
        def fetch(request, timeout):
            assert lock.depth == 0, "remote addon-token requests must not hold the account lock"
            assert request.full_url.startswith("https://api.eclipsemusic.app/")
            assert request.get_header("Authorization") == "Bearer account-jwt"
            return io.BytesIO(json.dumps({"token": "addon-scoped-token", "expiresIn": 300}).encode())

        with patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fetch):
            assert service._get_eclipse_addon_access_token("community.addon") == "addon-scoped-token"
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"]["community.addon"]["token"] == "addon-scoped-token"
    finally:
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service._ECLIPSE_AUTH_LOCK = previous["lock"]
        service.eclipse_account_status = previous["status_fn"]


def test_addon_access_token_result_is_discarded_after_account_changes():
    user = {"id": "account-a", "email": "listener@example.test"}
    previous = {
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "status_fn": service.eclipse_account_status,
    }
    service._ECLIPSE_AUTH_CACHE = {"jwt": "account-jwt-a", "addon_tokens": {}}
    service._ECLIPSE_ACCOUNT_STATUS = {"user": user, "checked_at": 0, "session_mtime": 1}
    service._ECLIPSE_AUTH_GENERATION = 5
    service.eclipse_account_status = lambda force_refresh=False: {"connected": True, "user": user}
    try:
        def switch_account(_request, timeout):
            service._ECLIPSE_AUTH_CACHE["jwt"] = "account-jwt-b"
            service._ECLIPSE_ACCOUNT_STATUS["user"] = {"id": "account-b", "email": "other@example.test"}
            service._ECLIPSE_AUTH_GENERATION += 1
            return io.BytesIO(b'{"token":"stale-addon-token","expiresIn":300}')

        with patch.object(service, "_eclipse_authenticated_urlopen", side_effect=switch_account):
            assert service._get_eclipse_addon_access_token("community.addon") is None
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"] == {}
    finally:
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service.eclipse_account_status = previous["status_fn"]


def test_addon_access_token_retry_never_switches_to_a_different_account():
    first_user = {"id": "account-a", "email": "listener@example.test"}
    second_user = {"id": "account-b", "email": "other@example.test"}
    previous = {
        "auth_cache": service._ECLIPSE_AUTH_CACHE,
        "account_status": service._ECLIPSE_ACCOUNT_STATUS,
        "generation": service._ECLIPSE_AUTH_GENERATION,
        "status_fn": service.eclipse_account_status,
    }
    service._ECLIPSE_AUTH_CACHE = {"jwt": "account-jwt-a", "addon_tokens": {}}
    service._ECLIPSE_ACCOUNT_STATUS = {"user": first_user, "checked_at": 0, "session_mtime": 1}
    service._ECLIPSE_AUTH_GENERATION = 5

    def status(force_refresh=False):
        if force_refresh:
            service._ECLIPSE_AUTH_CACHE["jwt"] = "account-jwt-b"
            service._ECLIPSE_ACCOUNT_STATUS["user"] = second_user
            service._ECLIPSE_AUTH_GENERATION += 1
            return {"connected": True, "user": second_user}
        return {"connected": True, "user": first_user}

    service.eclipse_account_status = status
    try:
        calls = []

        def rejected(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        with patch.object(service, "_eclipse_authenticated_urlopen", side_effect=rejected):
            assert service._get_eclipse_addon_access_token("community.addon") is None
        assert len(calls) == 1, "a retry must stop when the verified account identity changed"
        assert service._ECLIPSE_AUTH_CACHE["addon_tokens"] == {}
    finally:
        service._ECLIPSE_AUTH_CACHE = previous["auth_cache"]
        service._ECLIPSE_ACCOUNT_STATUS = previous["account_status"]
        service._ECLIPSE_AUTH_GENERATION = previous["generation"]
        service.eclipse_account_status = previous["status_fn"]
