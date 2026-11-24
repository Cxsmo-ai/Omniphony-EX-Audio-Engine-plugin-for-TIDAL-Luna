import io
import json
import sys
from pathlib import Path
from unittest.mock import patch


API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import omnifusion_service as service


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
            assert error == "account_not_verified"
