import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
from email.utils import formatdate
from unittest.mock import Mock, patch

import eclipse_streams
import omnifusion_service as service


class GravityResolverTests(unittest.TestCase):
    manifest_url = "https://gravity.example/account/manifest.json"

    def test_native_inventory_route_accepts_enabled_addon_outside_realmax_playback_order(self):
        provider_key = "eclipse:" + "a" * 20
        handler = object.__new__(service.RequestHandler)
        handler.path = f"/eclipse/streams?providerKey={provider_key}&trackId=track-1&operationId={'a' * 32}"
        handler.headers = {"Authorization": "Bearer native-test", "X-Omniphony-Service-Instance": "instance"}
        handler.reject_untrusted_origin = lambda: False
        handler.send_json = Mock()
        expected = {"complete": False, "reason": "upstream_rate_limited", "streams": [], "attempts": []}
        provider = {
            "addonId": "test.addon", "name": "Test", "manifestUrl": self.manifest_url,
            "available": True, "accountConnected": True, "source": "eclipse_account",
        }
        with patch.object(service, "eclipse_native_request_authorized", return_value=True), \
             patch.object(service, "load_settings", return_value={"realmax_source": "tidal_api"}), \
             patch.object(service, "realmax_provider_configuration", return_value={
                "configuredOrder": ["tidal_api"],
                 "_internal_records": {provider_key: provider},
             }), \
             patch.object(service, "discover_eclipse_addon_streams", return_value=expected) as discover:
            service.RequestHandler.do_GET(handler)

        self.assertEqual(handler.send_json.call_args.args[0], expected)
        self.assertEqual(discover.call_args.kwargs["provider_key"], provider_key)
        self.assertEqual(discover.call_args.kwargs["track_id"], "track-1")
        self.assertIsInstance(discover.call_args.kwargs["cancel_event"], threading.Event)

    def test_native_inventory_route_rejects_disabled_provider_before_discovery(self):
        provider_key = "eclipse:" + "a" * 20
        handler = object.__new__(service.RequestHandler)
        handler.path = f"/eclipse/streams?providerKey={provider_key}&trackId=track-1&operationId={'b' * 32}"
        handler.headers = {}
        handler.reject_untrusted_origin = lambda: False
        handler.send_json = Mock()
        with patch.object(service, "eclipse_native_request_authorized", return_value=True), \
             patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
             patch.object(service, "realmax_provider_configuration", return_value={
                 "configuredOrder": [provider_key],
                 "_internal_records": {provider_key: {
                     "addonId": "test.addon", "name": "Test", "manifestUrl": self.manifest_url,
                    "available": False, "accountConnected": True, "source": "eclipse_account",
                 }},
             }), \
             patch.object(service, "discover_eclipse_addon_streams") as discover:
            service.RequestHandler.do_GET(handler)

        self.assertEqual(handler.send_json.call_args.kwargs["status"], 404)
        discover.assert_not_called()

    def test_inventory_cancel_operation_is_bounded_and_scoped(self):
        operation_id = "c" * 32
        event = service._register_eclipse_inventory_operation(operation_id)
        self.assertIsInstance(event, threading.Event)
        self.assertFalse(event.is_set())
        self.assertTrue(service._cancel_eclipse_inventory_operation(operation_id))
        self.assertTrue(event.is_set())
        self.assertFalse(service._cancel_eclipse_inventory_operation("bad-id"))
        service._unregister_eclipse_inventory_operation(operation_id, event)
        self.assertTrue(service._cancel_eclipse_inventory_operation(operation_id))
        late_event = service._register_eclipse_inventory_operation(operation_id)
        self.assertTrue(late_event.is_set())
        service._unregister_eclipse_inventory_operation(operation_id, late_event)

    def test_cancel_arriving_before_inventory_registration_is_remembered(self):
        operation_id = "f" * 32
        self.assertTrue(service._cancel_eclipse_inventory_operation(operation_id))
        event = service._register_eclipse_inventory_operation(operation_id)
        self.assertIsInstance(event, threading.Event)
        self.assertTrue(event.is_set())
        service._unregister_eclipse_inventory_operation(operation_id, event)

    def test_canceled_inventory_exits_before_manifest_network_request(self):
        event = threading.Event()
        event.set()
        with patch.object(service, "_get_eclipse_manifest") as manifest:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                cancel_event=event,
            )
        self.assertEqual(inventory["reason"], "inventory_canceled")
        self.assertEqual(inventory["streams"], [])
        manifest.assert_not_called()

    def test_cancellation_during_manifest_fetch_discards_inventory_before_token_mint(self):
        event = threading.Event()

        def cancel_during_fetch(*_args, **_kwargs):
            event.set()
            return {"id": "test.addon", "settings": []}

        with patch.object(service, "_get_eclipse_manifest", side_effect=cancel_during_fetch), \
             patch.object(service, "_get_eclipse_addon_access_token") as token_request:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                cancel_event=event,
            )
        self.assertEqual(inventory["reason"], "inventory_canceled")
        token_request.assert_not_called()

    def test_native_cancel_route_sets_only_the_named_inventory_event(self):
        operation_id = "9" * 32
        event = service._register_eclipse_inventory_operation(operation_id)
        handler = object.__new__(service.RequestHandler)
        handler.path = "/eclipse/streams/cancel"
        handler.headers = {
            "Authorization": "Bearer native-test",
            "X-Omniphony-Service-Instance": "instance",
            "Content-Length": str(len(json.dumps({"operationId": operation_id}))),
        }
        handler.reject_untrusted_origin = lambda require_origin=False: False
        handler.connection = type("Connection", (), {"settimeout": lambda *_args: None})()
        handler.rfile = io.BytesIO(json.dumps({"operationId": operation_id}).encode())
        handler.send_json = Mock()
        other_id = "8" * 32
        other_event = service._register_eclipse_inventory_operation(other_id)
        with patch.object(service, "eclipse_native_request_authorized", return_value=True):
            service.RequestHandler._do_POST(handler)
        self.assertEqual(handler.send_json.call_args.args[0], {"canceled": True})
        self.assertTrue(event.is_set())
        self.assertFalse(other_event.is_set())
        service._unregister_eclipse_inventory_operation(operation_id, event)
        service._unregister_eclipse_inventory_operation(other_id, other_event)

    def test_inventory_keeps_enabled_addons_discoverable_outside_provider_order(self):
        addons = [
            {"addonId": "enabled.first", "manifestUrl": "https://one.example/manifest.json", "displayName": "First", "isEnabled": True},
            {"addonId": "enabled.second", "manifestUrl": "https://two.example/manifest.json", "displayName": "Second", "isEnabled": True},
            {"addonId": "disabled.third", "manifestUrl": "https://three.example/manifest.json", "displayName": "Disabled", "isEnabled": False},
        ]
        account = {"connected": True, "available": True, "addons": addons}
        for source_mode in ("tidal_api", "both", "eclipse_addons"):
            with self.subTest(source_mode=source_mode), \
                 patch.object(service, "eclipse_account_addons", return_value=account), \
                 patch.object(service, "is_main_tidal_api_authorized", return_value=True):
                config = service.realmax_provider_configuration({
                    "realmax_source": source_mode,
                    "realmax_provider_order": ["tidal_api"],
                })
            records = config["_internal_records"]
            self.assertEqual(config["configuredOrder"], ["tidal_api"])
            self.assertEqual(len([row for row in records.values() if row.get("available") is True]), 2)
            self.assertEqual(len([row for row in records.values() if row.get("available") is False]), 1)
            self.assertEqual(
                {row["addonId"] for row in records.values() if row.get("available") is True},
                {"enabled.first", "enabled.second"},
            )

    def test_short_inventory_deadline_is_not_raised_to_a_five_second_floor(self):
        manifest = {"id": "test.addon", "settings": []}

        def slow_manifest(*_args, **_kwargs):
            time.sleep(0.02)
            return manifest

        with patch.object(service, "_get_eclipse_manifest", side_effect=slow_manifest), \
             patch.object(service, "_get_eclipse_addon_access_token") as token_request:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                timeout_ms=5,
            )

        self.assertEqual(inventory["reason"], "discovery_deadline_exceeded")
        token_request.assert_not_called()

    def test_resource_parameters_replace_manifest_defaults_without_losing_unrelated_config(self):
        url = service._eclipse_worker_url(
            "https://gravity.example/account/manifest.json?quality=old&region=US",
            "stream/track-1",
            {"quality": "high"},
        )
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query),
            {"region": ["US"], "quality": ["high"]},
        )

    def test_quality_product_covers_declared_audio_fields_but_not_ambiguous_selects(self):
        manifest = {"settings": [
            {"key": "quality", "type": "select", "options": [{"value": "low"}, {"value": "high"}]},
            {"key": "audioQuality", "type": "select", "options": [{"value": "lossy"}, {"value": "lossless"}]},
            {"key": "normalization", "type": "select", "options": [{"value": "on"}, {"value": "off"}]},
            {"key": "videoQuality", "type": "select", "options": [{"value": "1080p"}]},
        ]}
        candidates = service.quality_candidates(manifest)
        self.assertEqual(len(candidates), 4)
        self.assertEqual(candidates[0]["settings"], {"quality": "low", "audioQuality": "lossy"})
        with_default = service.quality_candidates(manifest, include_default=True)
        self.assertEqual(len(with_default), 5)
        self.assertEqual(with_default[-1], {"requestedQuality": None, "settings": {}, "qualityKey": None})
        self.assertEqual(service.unclassified_select_settings(manifest), ["normalization"])
        with patch("eclipse_streams.MAX_QUALITY_OPTIONS", 2):
            self.assertTrue(service.quality_candidate_limit_exceeded(manifest))
            self.assertEqual(len(service.quality_candidates(manifest)), 2)
        boundary = {"settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}, {"value": "lossless"}],
        }]}
        with patch("eclipse_streams.MAX_QUALITY_OPTIONS", 2):
            self.assertEqual(service.quality_candidates(boundary, include_default=True), [
                {"requestedQuality": "Option 1", "requestedQualityLabel": "Option 1", "qualityKey": "quality",
                 "settings": {"quality": "high"}, "publicSettings": {"quality": "Option 1"}},
                {"requestedQuality": None, "settings": {}, "qualityKey": None},
            ])
            self.assertTrue(service.quality_candidate_limit_exceeded(boundary, include_default=True))
        with patch("eclipse_streams.MAX_QUALITY_OPTIONS", 1):
            self.assertEqual(service.quality_candidates(boundary, include_default=True), [
                {"requestedQuality": None, "settings": {}, "qualityKey": None},
            ])
        oversized = {"settings": [{
            "key": "quality", "type": "select",
            "options": [{"value": f"q{index}"} for index in range(65)],
        }]}
        self.assertEqual(len(service.quality_candidates(oversized)), 65)
        self.assertFalse(service.quality_candidate_limit_exceeded(oversized))
        over_limit = {"settings": [{
            "key": "quality", "type": "select",
            "options": [{"value": f"q{index}"} for index in range(eclipse_streams.MAX_QUALITY_OPTIONS + 1)],
        }]}
        self.assertEqual(len(service.quality_candidates(over_limit)), eclipse_streams.MAX_QUALITY_OPTIONS)
        self.assertTrue(service.quality_candidate_limit_exceeded(over_limit))
        unsafe_quality = {"settings": [{
            "key": "quality", "type": "select",
            "options": [{"value": "https://cdn.example/secret?token=private"}],
        }]}
        self.assertTrue(service.quality_options_unsafe(unsafe_quality))
        self.assertEqual(service.quality_candidates(unsafe_quality), [
            {"requestedQuality": None, "settings": {}, "qualityKey": None},
        ])
        duplicated = {"settings": [
            {"key": "quality", "type": "select", "options": [{"value": "high"}]},
            {"key": "QUALITY", "type": "select", "options": [{"value": "lossless"}]},
        ]}
        self.assertTrue(service.quality_options_unsafe(duplicated))
        self.assertEqual(service.quality_candidates(duplicated), [
            {"requestedQuality": None, "settings": {}, "qualityKey": None},
        ])
        malformed = {"settings": [
            {"type": "select", "options": [{"value": "high"}]},
            {"key": None, "type": "select", "options": [{"value": "high"}]},
            {"key": "x" * 97, "type": "select", "options": [{"value": "high"}]},
        ]}
        self.assertEqual(service.quality_candidates(malformed)[0]["settings"], {})

    def test_stream_request_variants_match_apk_declared_negotiation_fields(self):
        manifest = {
            "drmSystems": ["widevine"],
            "playbackProfiles": ["apple-cbcs-eac3-v1"],
        }
        quality = {"requestedQuality": "Atmos", "settings": {"quality": "Atmos"}}
        variants, truncated = eclipse_streams.stream_request_candidates(manifest, quality)

        self.assertFalse(truncated)
        self.assertEqual(variants, [
            {"settings": {"quality": "Atmos"}, "negotiation": {}},
            {"settings": {"quality": "Atmos", "drm": "widevine"}, "negotiation": {"drm": "widevine"}},
            {
                "settings": {
                    "quality": "Atmos", "drm": "widevine",
                    "playbackProfile": "apple-cbcs-eac3-v1", "dolbyCodecs": "eac3",
                },
                "negotiation": {
                    "drm": "widevine", "playbackProfile": "apple-cbcs-eac3-v1",
                    "dolbyCodecs": "eac3",
                },
            },
            {
                "settings": {
                    "quality": "Atmos", "playbackProfile": "apple-cbcs-eac3-v1",
                    "dolbyCodecs": "eac3",
                },
                "negotiation": {
                    "playbackProfile": "apple-cbcs-eac3-v1", "dolbyCodecs": "eac3",
                },
            },
        ])
        overflow, truncated = eclipse_streams.stream_request_candidates(
            {"drmSystems": ["widevine"], "playbackProfiles": [f"profile-{index}" for index in range(33)]},
            quality,
        )
        self.assertTrue(truncated)
        self.assertLessEqual(len(overflow), eclipse_streams.MAX_QUALITY_OPTIONS)

        profile_only, truncated = eclipse_streams.stream_request_candidates(
            {"playbackProfiles": ["apple-cbcs-eac3-v1"]}, quality,
        )
        self.assertFalse(truncated)
        self.assertEqual(profile_only[-1]["negotiation"], {
            "playbackProfile": "apple-cbcs-eac3-v1", "dolbyCodecs": "eac3",
        })

        colliding, truncated = eclipse_streams.stream_request_candidates(
            {"playbackProfiles": ["Atmos"]},
            {"requestedQuality": "Lossless", "settings": {"playbackProfile": "lossless-token"}},
        )
        self.assertTrue(truncated)
        self.assertEqual(len(colliding), 1)
        self.assertEqual(colliding[0]["settings"]["playbackProfile"], "lossless-token")
        malformed_tokens, malformed_truncated = eclipse_streams.stream_request_candidates(
            {"drmSystems": [" widevine "], "playbackProfiles": [" apple-cbcs-eac3-v1 "]}, quality,
        )
        self.assertTrue(malformed_truncated)
        self.assertEqual(malformed_tokens, [{"settings": {"quality": "Atmos"}, "negotiation": {}}])

    def test_gravity_negotiation_matches_apk_profile_and_platform_gates(self):
        manifest = {
            "drmSystems": ["widevine", "com.apple.fps"],
            "playbackProfiles": ["apple-cbcs-eac3-v1", "apple-fps-alac-v1", "eac3-joc"],
        }
        quality = {"requestedQuality": "Atmos", "settings": {"quality": "Atmos"}}
        variants, truncated = eclipse_streams.stream_request_candidates(
            manifest, quality, addon_id="app.eclipsemusic.gravity", fairplay_available=False,
        )
        self.assertFalse(truncated)
        negotiation = [item["negotiation"] for item in variants]
        self.assertIn({"drm": "widevine", "playbackProfile": "apple-cbcs-eac3-v1", "dolbyCodecs": "eac3"}, negotiation)
        self.assertNotIn({"drm": "widevine", "playbackProfile": "apple-fps-alac-v1"}, negotiation)
        self.assertNotIn({"playbackProfile": "eac3-joc", "dolbyCodecs": "eac3"}, negotiation)
        fairplay, _ = eclipse_streams.stream_request_candidates(
            manifest, quality, addon_id="app.eclipsemusic.gravity", fairplay_available=True,
        )
        self.assertIn(
            {"drm": "widevine", "playbackProfile": "apple-fps-alac-v1"},
            [item["negotiation"] for item in fairplay],
        )

    def test_quality_selector_label_map_marks_conflicting_values(self):
        manifest = {"settings": [
            {"key": "quality", "type": "select", "options": [
                {"value": "same", "label": "Lossless"},
                {"value": "opaque", "label": "Hi-Res"},
            ]},
            {"key": "audioQuality", "type": "select", "options": [
                {"value": "same", "label": "Atmos"},
            ]},
        ]}
        self.assertEqual(eclipse_streams.quality_selector_label_map(manifest), {
            "same": None, "opaque": "Hi-Res",
        })
        ambiguous = {"settings": [
            {"key": "quality", "type": "select", "options": [{"value": "ambiguous", "label": "High"}]},
            {"key": "QUALITY", "type": "select", "options": [{"value": "other", "label": "Lossless"}]},
            {"key": "audioQuality", "type": "select", "options": [{"value": "lossless", "label": "Lossless"}]},
        ]}
        self.assertEqual(eclipse_streams.quality_selector_label_map(ambiguous), {"lossless": "Lossless"})

    def test_invalid_declared_playback_capabilities_are_reported_incomplete(self):
        capabilities = service.declared_addon_capabilities({
            "drmSystems": ["widevine", {"not": "a string"}],
            "playbackProfiles": "apple-cbcs-eac3-v1",
        })
        self.assertFalse(capabilities["capabilitiesComplete"])
        self.assertEqual(set(capabilities["malformedCapabilityFields"]), {"drmSystems", "playbackProfiles"})
        self.assertEqual(capabilities["drmSystems"], ["widevine"])

    def test_reference_finalization_checks_cache_and_caps_ttl_by_provider_expiry(self):
        stream_store = {
            "fresh": {"accountGeneration": 7, "expiresAt": 250.0},
            "short": {"accountGeneration": 7, "expiresAt": 175.0},
            "other-account": {"accountGeneration": 8, "expiresAt": 250.0, "expires_at": 123.0},
            "expired": {"accountGeneration": 7, "expiresAt": 99.0},
        }
        descriptors = [
            {"streamRef": "fresh"}, {"streamRef": "short"},
            {"streamRef": "other-account"}, {"streamRef": "expired"},
            {"streamRef": "evicted"},
        ]
        complete = service._finalize_stream_reference_inventory(
            descriptors,
            stream_store, 7, 100.0,
        )
        self.assertFalse(complete)
        self.assertEqual(stream_store["fresh"]["expires_at"], 220.0)
        self.assertEqual(stream_store["short"]["expires_at"], 175.0)
        self.assertEqual(stream_store["other-account"]["expires_at"], 123.0)
        self.assertEqual(descriptors[0]["referenceExpiresAt"], 220.0)
        self.assertFalse(descriptors[0]["expired"])
        self.assertTrue(descriptors[3]["expired"])
        self.assertEqual(descriptors[3]["playbackCompatibility"], "expired")
        self.assertIsNone(descriptors[4]["streamRef"])
        self.assertEqual(descriptors[4]["availability"], "reference_unavailable")
        self.assertEqual(descriptors[4]["playbackCompatibility"], "stream_reference_not_retained")

    def test_reference_finalization_marks_all_handles_unavailable_when_store_is_missing(self):
        descriptors = [{
            "streamRef": "unretained", "availability": "returned",
            "playbackCompatibility": "authorized_protected_playback_required",
        }]
        complete = service._finalize_stream_reference_inventory(descriptors, {}, 7, 100.0)
        self.assertFalse(complete)
        self.assertIsNone(descriptors[0]["streamRef"])
        self.assertIsNone(descriptors[0]["referenceExpiresAt"])
        self.assertEqual(descriptors[0]["availability"], "reference_unavailable")
        self.assertEqual(descriptors[0]["playbackCompatibility"], "stream_reference_not_retained")

    def test_quality_selector_tokens_stay_native_and_renderer_receives_labels(self):
        token = "opaque-secret-value-132"
        manifest = {"settings": [{
            "key": "quality", "type": "select", "label": "Audio quality",
            "options": [{"value": token, "label": "Hi-Res Lossless"}],
        }]}
        candidate = eclipse_streams.quality_candidates(manifest)[0]
        self.assertEqual(candidate["settings"], {"quality": token})
        self.assertEqual(candidate["requestedQuality"], "Hi-Res Lossless")
        self.assertEqual(candidate["publicSettings"], {"quality": "Hi-Res Lossless"})
        public = json.dumps({
            "requestedQuality": candidate["requestedQuality"],
            "requestSettings": candidate["publicSettings"],
        })
        self.assertNotIn(token, public)

        requests = []
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}

        def fake_open(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps({
                "url": "https://cdn.example/hires.flac", "codec": "flac",
            }).encode("utf-8"))

        with patch.object(service, "_get_eclipse_manifest", return_value={"id": "test.addon", **manifest}), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
            )

        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query)["quality"],
            [token],
        )
        self.assertNotIn(token, json.dumps(inventory))
        self.assertIn("Hi-Res Lossless", json.dumps(inventory))

    def test_stream_request_plan_prioritizes_every_baseline_quality_and_reports_exact_limits(self):
        qualities = [
            {"requestedQuality": name, "qualityKey": "quality", "settings": {"quality": name}}
            for name in ("low", "high")
        ]
        plan, truncated = eclipse_streams.build_stream_request_plan(
            {"drmSystems": ["widevine"]}, qualities, limit=3,
        )
        self.assertEqual([item["settings"]["quality"] for item in plan[:2]], ["low", "high"])
        self.assertTrue(truncated)

        round_robin, round_robin_truncated = eclipse_streams.build_stream_request_plan(
            {"drmSystems": ["widevine"]}, qualities, limit=4,
        )
        self.assertFalse(round_robin_truncated)
        self.assertEqual([item.get("negotiation", {}).get("drm") for item in round_robin[2:]],
                         ["widevine", "widevine"])
        self.assertEqual([item["settings"]["quality"] for item in round_robin[2:]], ["low", "high"])

        exact_plan, exact_truncated = eclipse_streams.build_stream_request_plan(
            {}, qualities, limit=2,
        )
        self.assertEqual(len(exact_plan), 2)
        self.assertFalse(exact_truncated)

        mixed_quality = {"settings": [
            {"key": "quality", "type": "select", "options": [{"value": f"q{i}"} for i in range(100)]},
            {"key": "audioQuality", "type": "select", "options": [{"value": f"a{i}"} for i in range(100)]},
            {"key": "streamQuality", "type": "select", "options": []},
        ]}
        with patch("eclipse_streams.MAX_QUALITY_OPTIONS", 4096):
            self.assertTrue(eclipse_streams.quality_candidate_limit_exceeded(mixed_quality))

    def test_isrc_resolution_precedes_fuzzy_search_when_available(self):
        manifest = {"id": "test.addon", "resources": ["isrc", "search"]}
        requests = []
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}

        def fake_open(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps({"item": {"id": "track-isrc", "type": "track"}}).encode("utf-8"))

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test",
                title="Song", artist="Artist", isrc="USABC1234567",
            )

        self.assertEqual(len(requests), 1)
        self.assertTrue(urllib.parse.urlsplit(requests[0].full_url).path.endswith("/resolve-isrc"))
        self.assertEqual(inventory["trackId"], "track-isrc")

    def test_discovery_negotiates_declared_gravity_drm_profile_and_retains_protected_descriptor(self):
        manifest = {
            "id": "app.eclipsemusic.gravity",
            "resources": ["stream"],
            "settings": [{"key": "quality", "type": "select", "label": "Audio quality", "options": [
                {"value": "Atmos"},
            ]}],
            "drmSystems": ["widevine"],
            "playbackProfiles": ["apple-cbcs-eac3-v1"],
        }
        responses = iter([
            {"streams": []},
            {"url": "https://cdn.example/protected.m3u8", "manifest": "hls", "codec": "eac3_joc",
             "quality": "Atmos", "drm": {"system": "widevine", "licenseUrl": "https://license.example/license"}},
            {"url": "https://cdn.example/profile.m3u8", "manifest": "hls", "codec": "eac3_joc",
             "quality": "Atmos", "drm": {"system": "widevine", "licenseUrl": "https://license.example/license"}},
            {"streams": []}, {"streams": []}, {"streams": []},
        ])
        requests = []
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}

        def fake_open(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value="scoped-addon-token"), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_manifest_protection_status", return_value=False), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="app.eclipsemusic.gravity", addon_name="Gravity", track_id="track-1",
            )

        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) for request in requests]
        self.assertEqual(len(queries), 6)
        self.assertEqual(queries[0], {"quality": ["Atmos"]})
        self.assertEqual(queries[1], {})
        self.assertEqual(queries[2], {"quality": ["Atmos"], "drm": ["widevine"]})
        self.assertEqual(queries[3], {"drm": ["widevine"]})
        self.assertEqual(queries[4], {
            "quality": ["Atmos"], "drm": ["widevine"],
            "playbackProfile": ["apple-cbcs-eac3-v1"], "dolbyCodecs": ["eac3"],
        })
        self.assertEqual(queries[5], {
            "drm": ["widevine"], "playbackProfile": ["apple-cbcs-eac3-v1"], "dolbyCodecs": ["eac3"],
        })
        self.assertTrue(inventory["complete"], inventory)
        self.assertEqual(len(inventory["streams"]), 2)
        self.assertTrue(all(item["playbackCompatibility"] == "authorized_protected_playback_required"
                            for item in inventory["streams"]))
        self.assertEqual(inventory["attempts"][4]["negotiation"], {
            "drm": "widevine", "playbackProfile": "apple-cbcs-eac3-v1", "dolbyCodecs": "eac3",
        })

    def test_resolve_identity_uses_official_duration_ms_parameter(self):
        manifest = {"id": "test.addon", "resources": ["resolve"]}
        requests = []
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}

        def fake_open(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps({"item": {"id": "track-1", "type": "track"}}).encode("utf-8"))

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test",
                title="Song", artist="Artist", duration=212,
            )

        self.assertEqual(len(requests), 1)
        self.assertTrue(urllib.parse.urlsplit(requests[0].full_url).path.endswith("/resolve"))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query), {
            "title": ["Song"], "artist": ["Artist"], "durationMs": ["212000"],
        })
        self.assertEqual(inventory["trackId"], "track-1")

    def test_stream_resolution_retries_addon_preparation_503_with_retry_after(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        error = urllib.error.HTTPError(
            "https://addon.example/stream/track-1", 503, "Service Unavailable",
            {"Retry-After": "2"}, io.BytesIO(b"preparing"),
        )
        response = {"url": "https://cdn.example/track.mp3", "codec": "mp3", "quality": "high"}
        calls = []

        def fake_open(request, timeout=None):
            calls.append(request)
            if len(calls) == 1:
                raise error
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open), \
             patch.object(service.time, "sleep") as sleep:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
            )

        self.assertTrue(inventory["complete"])
        self.assertEqual(len(calls), 3)
        sleep.assert_called_once_with(2.0)

    def test_provider_specific_quality_keys_are_detected_from_clear_semantics(self):
        manifest = {"settings": [
            {"key": "maxQuality", "type": "select", "options": [
                {"value": "standard"}, {"value": "ultra"},
            ]},
            {"key": "profile", "label": "Audio Quality", "type": "select", "options": [
                {"value": "lossy"}, {"value": "lossless"},
            ]},
            {"key": "quality", "label": "Video Quality", "type": "select", "options": [
                {"value": "1080p"},
            ]},
            {"key": "normalization", "label": "Normalization", "type": "select", "options": [
                {"value": "on"}, {"value": "off"},
            ]},
        ]}

        candidates = service.quality_candidates(manifest)

        self.assertEqual(len(candidates), 4)
        self.assertEqual(candidates[0]["settings"], {"maxQuality": "standard", "profile": "lossy"})
        self.assertEqual(candidates[-1]["settings"], {"maxQuality": "ultra", "profile": "lossless"})
        self.assertEqual(service.unclassified_select_settings(manifest), ["normalization"])

    def test_quality_options_flag_boolean_duplicates_and_duplicate_keys_as_incomplete(self):
        manifest = {"settings": [
            {"key": "quality", "type": "select", "options": [{"value": False}, {"value": "Max"}]},
            {"key": "audioQuality", "type": "select", "options": [{"value": "lossless"}]},
            {"key": "quality", "type": "select", "options": [{"value": "Max"}]},
        ]}
        self.assertTrue(service.quality_options_unsafe(manifest))
        self.assertTrue(service.quality_candidate_limit_exceeded(manifest) is False)
        definitions = eclipse_streams._quality_settings(manifest)
        self.assertTrue(definitions[0]["optionsUnsafe"])

    def test_malformed_quality_setting_does_not_suppress_other_valid_quality_requests(self):
        manifest = {"settings": [
            {"key": "audioQuality", "type": "select", "options": [
                {"value": "lossless"}, {"value": "hires"},
            ]},
            {"key": "streamQuality", "type": "select", "options": "invalid"},
        ]}

        candidates = service.quality_candidates(manifest)

        self.assertEqual([item["settings"] for item in candidates], [
            {"audioQuality": "lossless"}, {"audioQuality": "hires"},
        ])
        self.assertTrue(service.quality_options_unsafe(manifest))

    def test_renderer_metadata_has_depth_and_total_node_limits(self):
        value = "leaf"
        for _ in range(1500):
            value = [value]
        self.assertIsNone(eclipse_streams._renderer_json_value(value))
        broad = {f"key-{index}": {"nested": index} for index in range(1000)}
        encoded = json.dumps(eclipse_streams._renderer_json_value(broad))
        self.assertLessEqual(len(encoded), 12_000)
        self.assertIsNone(eclipse_streams._renderer_json_value(2**60))

    def test_top_level_fairplay_markers_are_protected_and_not_reported_clear(self):
        for marker in ({"nativeFairPlay": {"enabled": True}}, {"initDataMapping": {"type": "skd"}}):
            with self.subTest(marker=marker):
                protection = eclipse_streams._protection({"encrypted": False, **marker})
                self.assertEqual(protection["status"], "protected")
                self.assertIsNone(protection["reportedStatus"])

    def test_conflicting_parent_and_child_protection_systems_remain_explicit(self):
        descriptor = service.normalize_stream(
            {"url": "https://cdn.example/audio.mpd", "drm": {"system": "widevine"}},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="lossless", request_settings={},
            parent={"drm": {"system": "fairplay"}},
        )
        self.assertEqual(descriptor["protection"]["status"], "protected")
        self.assertIsNone(descriptor["protection"]["system"])
        self.assertEqual(descriptor["protection"]["systems"], ["fairplay", "widevine"])
        self.assertTrue(descriptor["protection"]["conflictingProtectionMetadata"])

    def test_conflicting_protection_fields_in_one_descriptor_are_preserved(self):
        protection = eclipse_streams._protection({
            "drm": {"system": "widevine", "keySystem": "com.apple.fps"},
            "encrypted": True,
        })
        self.assertEqual(protection["status"], "protected")
        self.assertEqual(set(protection["systems"]), {"widevine", "fairplay"})
        self.assertTrue(protection["conflictingProtectionMetadata"])
        self.assertIsNone(protection["system"])

        scheme_conflict = eclipse_streams._protection({
            "drm": {"system": "widevine", "scheme": "cbcs", "encryptionScheme": "cenc"},
        })
        self.assertTrue(scheme_conflict["conflictingProtectionMetadata"])
        self.assertIn("encryptionScheme", scheme_conflict["conflictingProtectionFields"])
        self.assertIsNone(scheme_conflict["encryptionScheme"])

        system_scheme_conflict = eclipse_streams._protection({
            "drm": {"system": "widevine", "scheme": "com.apple.fps"},
        })
        self.assertTrue(system_scheme_conflict["conflictingProtectionMetadata"])
        self.assertEqual(set(system_scheme_conflict["systems"]), {"widevine", "fairplay"})
        self.assertIn("system", system_scheme_conflict["conflictingProtectionFields"])
        self.assertIsNone(system_scheme_conflict["system"])

    def test_inherited_protection_conflicts_and_encryption_scheme_conflicts_survive_merge(self):
        inherited = eclipse_streams._merge_protection(
            {"drm": {"system": "widevine", "keySystem": "com.apple.fps"}}, {},
        )
        self.assertTrue(inherited["conflictingProtectionMetadata"])
        self.assertIn("system", inherited["conflictingProtectionFields"])
        self.assertIsNone(inherited["system"])
        scheme = eclipse_streams._protection({
            "encrypted": True,
            "drm": {"system": "widevine", "encryptionScheme": "cenc"},
            "encryptionScheme": "cbcs",
        })
        self.assertTrue(scheme["conflictingProtectionMetadata"])
        self.assertIn("encryptionScheme", scheme["conflictingProtectionFields"])
        self.assertEqual(scheme["status"], "protected")
        merged = eclipse_streams._merge_protection(
            {"drm": {"system": "widevine", "encryptionScheme": "cenc"}},
            {"drm": {"system": "widevine", "encryptionScheme": "cbcs"}},
        )
        self.assertTrue(merged["conflictingProtectionMetadata"])
        self.assertIsNone(merged["encryptionScheme"])
        self.assertIn("encryptionScheme", merged["conflictingProtectionFields"])
        standalone = eclipse_streams._merge_protection(None, {
            "drm": {"system": "widevine", "keySystem": "fairplay",
                    "encryptionScheme": "cenc", "protectionScheme": "cbcs"},
        })
        self.assertTrue(standalone["conflictingProtectionMetadata"])
        self.assertIsNone(standalone["system"])
        self.assertIsNone(standalone["encryptionScheme"])

    def test_stream_quality_zero_and_precise_bitrate_are_not_lost(self):
        descriptor = service.normalize_stream(
            {"url": "https://cdn.example/audio.flac", "quality": 0, "bitrate": 1411200,
             "sampleRate": 22050},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="default", request_settings={},
        )
        self.assertEqual(descriptor["actualQuality"], "0")
        self.assertEqual(descriptor["bitrateBps"], 1411200)
        self.assertEqual(descriptor["sampleRate"], 22050)

    def test_media_manifest_inspector_blocks_encrypted_hls_and_follows_master_playlists(self):
        def response(request, *_args, **_kwargs):
            url = request.full_url
            if url.endswith("master.m3u8"):
                body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nvariant/clear.m3u8\n"
                return (body, {}) if _kwargs.get("return_headers") else body
            if url.endswith("clear.m3u8"):
                body = b"#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\nsegment.aac\n#EXT-X-ENDLIST\n"
                return (body, {}) if _kwargs.get("return_headers") else body
            if url.endswith("encrypted.m3u8"):
                body = b"#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"license\"\n"
                return (body, {}) if _kwargs.get("return_headers") else body
            raise AssertionError(url)
        with patch.object(service, "_bounded_eclipse_response", side_effect=response):
            self.assertFalse(service._eclipse_manifest_protection_status("https://cdn.example/master.m3u8"))
            self.assertTrue(service._eclipse_manifest_protection_status("https://cdn.example/encrypted.m3u8"))

    def test_media_manifest_inspector_enumerates_sanitized_hls_audio_variants(self):
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English, Stereo",LANGUAGE="en",CHANNELS="2",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=256000,CODECS="mp4a.40.2",AUDIO="audio"
audio-master.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="audio"
video-master.m3u8
'''

        def response(request, *_args, **kwargs):
            body = master if urllib.parse.urlsplit(request.full_url).path == "/master.m3u8" else b"#EXTM3U\n#EXTINF:4,\na.aac\n"
            return (body, {}) if kwargs.get("return_headers") else body

        variants = []
        with patch.object(service, "_bounded_eclipse_response", side_effect=response):
            protection = service._eclipse_manifest_protection_status(
                "https://cdn.example/master.m3u8", media_variants_out=variants,
            )

        self.assertFalse(protection)
        self.assertEqual(len(variants), 3)
        audio = next(item for item in variants if item["kind"] == "hls_audio_rendition")
        self.assertEqual(audio["name"], "English, Stereo")
        self.assertEqual(variants[0]["bandwidthScope"], "rendition_unspecified_scope")
        self.assertEqual(variants[1]["bandwidthScope"], "aggregate")
        self.assertNotIn("audio.m3u8", json.dumps(variants))

    def test_media_manifest_inspector_enumerates_dash_audio_representations_and_protection(self):
        mpd = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>
          <AdaptationSet mimeType="audio/mp4" lang="en" codecs="ec-3" audioSamplingRate="48000">
            <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
            <Representation id="a" bandwidth="128000"><AudioChannelConfiguration value="2"/></Representation>
            <Representation id="b" bandwidth="256000" audioSamplingRate="96000"/>
          </AdaptationSet>
          <AdaptationSet mimeType="video/mp4"><Representation id="v" bandwidth="2000000"/></AdaptationSet>
        </Period></MPD>'''
        with patch.object(service, "_bounded_eclipse_response", return_value=(mpd, {})):
            variants = []
            protected = service._eclipse_manifest_protection_status(
                "https://cdn.example/manifest.mpd", media_variants_out=variants,
            )

        self.assertTrue(protected)
        self.assertEqual(len(variants), 2)
        self.assertEqual([item["bandwidth"] for item in variants], [128000, 256000])
        self.assertEqual(variants[0]["channels"], 2)
        self.assertEqual(variants[1]["sampleRate"], 96000)

    def test_dash_audio_type_and_representation_attributes_are_inherited(self):
        mpd = b'''<MPD><Period>
          <AdaptationSet contentType="audio" lang="en">
            <Representation id="alac" mimeType="audio/mp4" codecs="alac" bandwidth="1400000"/>
          </AdaptationSet>
          <AdaptationSet><Representation id="unknown" bandwidth="1000"/></AdaptationSet>
          <AdaptationSet contentType="video"><Representation id="video" mimeType="video/mp4"/></AdaptationSet>
        </Period></MPD>'''
        with patch.object(service, "_bounded_eclipse_response", return_value=(mpd, {})):
            variants = []
            variants_complete = []
            protected = service._eclipse_manifest_protection_status(
                "https://cdn.example/audio.mpd", media_variants_out=variants,
                media_variants_complete_out=variants_complete,
            )

        self.assertFalse(protected)
        audio = next(item for item in variants if item["id"] == "alac")
        self.assertEqual(audio["kind"], "dash_audio_representation")
        self.assertEqual(audio["codecs"], "alac")
        self.assertEqual(audio["classificationStatus"], "audio")
        unknown = next(item for item in variants if item["id"] == "unknown")
        self.assertEqual(unknown["classificationStatus"], "unclassified")
        self.assertEqual(variants_complete, [False])
        self.assertNotIn("video", [item.get("id") for item in variants if item["kind"] == "dash_audio_representation"])

    def test_dash_mixed_audio_video_bandwidth_is_labeled_aggregate(self):
        mpd = b'''<MPD><Period><AdaptationSet>
          <Representation id="muxed" codecs="avc1.640028,mp4a.40.2" bandwidth="3000000"/>
        </AdaptationSet></Period></MPD>'''
        variants = []
        with patch.object(service, "_bounded_eclipse_response", return_value=(mpd, {})):
            service._eclipse_manifest_protection_status(
                "https://cdn.example/mixed.mpd", media_variants_out=variants,
            )
        self.assertEqual(variants[0]["kind"], "dash_mixed_representation")
        self.assertEqual(variants[0]["classificationStatus"], "mixed_audio_video")
        self.assertEqual(variants[0]["bandwidthScope"], "aggregate_audiovisual")
        self.assertEqual(variants[0]["bandwidth"], 3_000_000)

    def test_manifest_inspector_does_not_forward_credentials_to_cross_origin_children(self):
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",URI="https://other.example/audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp4a.40.2"
same-origin.m3u8
'''
        seen = {}

        def response(request, *_args, **kwargs):
            parsed = urllib.parse.urlsplit(request.full_url)
            seen[parsed.netloc + parsed.path] = request.get_header("Authorization")
            body = master if parsed.path == "/master.m3u8" else b"#EXTM3U\n#EXTINF:4,\na.aac\n"
            return (body, {}) if kwargs.get("return_headers") else body

        with patch.object(service, "_bounded_eclipse_response", side_effect=response):
            protected = service._eclipse_manifest_protection_status(
                "https://cdn.example/master.m3u8",
                request_headers={"Authorization": "Bearer secret"},
                media_variants_out=[],
            )

        self.assertFalse(protected)
        self.assertEqual(seen["cdn.example/master.m3u8"], "Bearer secret")
        self.assertEqual(seen["cdn.example/same-origin.m3u8"], "Bearer secret")
        self.assertIsNone(seen["other.example/audio.m3u8"])

    def test_manifest_header_cache_identity_uses_the_exact_dispatched_header_subset(self):
        fillers = {f"X-Header-{index:02d}": f"value-{index}" for index in range(32)}
        first = {"Authorization": "Bearer private", **fillers}
        reordered = {**fillers, "Authorization": "Bearer private"}
        first_dispatched = service._validated_eclipse_manifest_headers(first)
        reordered_dispatched = service._validated_eclipse_manifest_headers(reordered)
        self.assertEqual(len(first_dispatched), 32)
        self.assertEqual(len(reordered_dispatched), 32)
        self.assertIn("Authorization", first_dispatched)
        self.assertNotIn("Authorization", reordered_dispatched)
        self.assertNotEqual(
            {key.casefold(): value for key, value in first_dispatched.items()},
            {key.casefold(): value for key, value in reordered_dispatched.items()},
        )

    def test_extensionless_manifest_transport_is_sniffed_before_admission(self):
        def response(request, *_args, **kwargs):
            if kwargs.get("return_headers"):
                if request.full_url.endswith("/hls"):
                    return b"#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"skd://asset\"\n", {
                        "content-type": "application/octet-stream",
                    }
                if request.full_url.endswith("/dash"):
                    return b"<?xml version=\"1.0\"?><MPD><Period><AdaptationSet><ContentProtection schemeIdUri=\"urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed\"/></AdaptationSet></Period></MPD>", {
                        "content-type": "application/octet-stream",
                    }
                if request.full_url.endswith("/mpegurl"):
                    return b"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key\"\n", {
                        "content-type": "audio/x-mpegurl",
                    }
                if request.full_url.endswith("/mp4"):
                    return b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00", {
                        "content-type": "audio/mp4",
                    }
                return b"ID3\x04\x00\x00", {"content-type": "audio/mpeg"}
            raise AssertionError("extensionless manifest probing should reuse its bounded response")

        with patch.object(service, "_bounded_eclipse_response", side_effect=response):
            self.assertTrue(service._eclipse_manifest_protection_status("https://cdn.example/media/hls"))
            self.assertTrue(service._eclipse_manifest_protection_status("https://cdn.example/media/dash"))
            self.assertTrue(service._eclipse_manifest_protection_status("https://cdn.example/media/mpegurl"))
            self.assertFalse(service._eclipse_manifest_protection_status("https://cdn.example/media/song"))
            self.assertIsNone(service._eclipse_manifest_protection_status("https://cdn.example/media/mp4"))

    def test_incomplete_or_over_limit_hls_does_not_pass_as_clear(self):
        with patch.object(service, "_bounded_eclipse_response", return_value=(
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\n", {},
        )):
            self.assertIsNone(service._eclipse_manifest_protection_status("https://cdn.example/master.m3u8"))

        with patch.object(service, "_bounded_eclipse_response", return_value=(
            b"#EXTM3U\n" + (b"#EXTINF:1,\na.aac\n" * 60_000), {},
        )):
            self.assertIsNone(service._eclipse_manifest_protection_status("https://cdn.example/large.m3u8"))

    def test_direct_media_probes_do_not_dispatch_after_account_generation_changes(self):
        with patch.object(service, "_ECLIPSE_AUTH_GENERATION", 8), patch.object(
            service, "_eclipse_authenticated_urlopen"
        ) as open_request:
            self.assertEqual(
                service._probe_direct_audio_bitrate(
                    "https://cdn.example/song", 180, {}, expected_generation=7,
                ),
                (None, None),
            )
            self.assertEqual(
                service._probe_direct_media_transport(
                    "https://cdn.example/song", {}, expected_generation=7,
                ),
                "DIRECT",
            )
        open_request.assert_not_called()

    def test_protection_evidence_is_conservative_for_parent_child_and_license_metadata(self):
        inherited = service.normalize_stream(
            {"url": "https://cdn.example/child.m3u8", "encrypted": False, "drm": None},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="high", request_settings={"quality": "high"},
            parent={"encrypted": True, "drm": {"system": "com.widevine.alpha"}},
        )
        self.assertEqual(inherited["protection"]["status"], "protected")
        self.assertEqual(inherited["protection"]["system"], "widevine")

        cbcs = service.normalize_stream(
            {"url": "https://cdn.example/cbcs.m3u8", "drm": {"scheme": "cbcs"}},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="high", request_settings={"quality": "high"},
        )
        self.assertEqual(cbcs["protection"]["status"], "protected")
        self.assertIsNone(cbcs["protection"]["system"])
        self.assertEqual(cbcs["protection"]["encryptionScheme"], "cbcs")

        license_only = service.normalize_stream(
            {"url": "https://cdn.example/licensed.m3u8", "licenseUrl": "https://license.example/key"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="high", request_settings={"quality": "high"},
        )
        self.assertEqual(license_only["protection"]["status"], "protected")
        self.assertTrue(license_only["protection"]["licenseEndpointAvailable"])

        multi_quality = service.normalize_stream(
            {"url": "https://cdn.example/multi.flac", "quality": "lossless"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality={"quality": "high", "audioQuality": "lossless"},
            request_settings={"quality": "high", "audioQuality": "lossless"},
        )
        self.assertIsNone(multi_quality["qualityMatch"])

    def test_requested_quality_matches_selector_token_without_false_mismatch(self):
        token_match = service.normalize_stream(
            {"url": "https://cdn.example/master.m3u8", "quality": "jymaster"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Ultra-HD Master", request_settings={"quality": "Ultra-HD Master"},
            quality_selector_values=["jymaster"], quality_selector_labels={"jymaster": "Ultra-HD Master"},
        )
        unknown_semantics = service.normalize_stream(
            {"url": "https://cdn.example/master.m3u8", "quality": "provider-tier-1"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Ultra-HD Master", request_settings={"quality": "Ultra-HD Master"},
            quality_selector_values=["jymaster"],
        )
        partial_multi_selector = service.normalize_stream(
            {"url": "https://cdn.example/master.m3u8", "quality": "lossless"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Audio: Lossless; Rate: 192 kHz", request_settings={"audioQuality": "Lossless", "sampleRate": "192k"},
            quality_selector_values=["lossless", "192k"],
            quality_selector_labels={"lossless": "Lossless", "192k": "192 kHz"},
        )
        same_label_wrong_selector = service.normalize_stream(
            {"url": "https://cdn.example/master.m3u8", "quality": "second-token"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Lossless", request_settings={"quality": "Lossless"},
            quality_selector_values=["first-token"],
            quality_selector_labels={"first-token": "Lossless", "second-token": "Lossless"},
        )
        self.assertTrue(token_match["qualityMatch"])
        self.assertEqual(token_match["qualityMatchStatus"], "matched")
        self.assertEqual(token_match["actualQuality"], "Ultra-HD Master")
        self.assertNotIn("jymaster", json.dumps(token_match))
        self.assertIsNone(unknown_semantics["qualityMatch"])
        self.assertEqual(unknown_semantics["qualityMatchStatus"], "ambiguous")
        self.assertIsNone(partial_multi_selector["qualityMatch"])
        self.assertEqual(partial_multi_selector["qualityMatchStatus"], "ambiguous")
        self.assertFalse(same_label_wrong_selector["qualityMatch"])
        self.assertEqual(same_label_wrong_selector["qualityMatchStatus"], "mismatched")
        conflicting_label = service.normalize_stream(
            {"url": "https://cdn.example/master.m3u8", "quality": "conflicting-token"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Ultra-HD Master", request_settings={"quality": "selected-token"},
            quality_selector_values=["selected-token"],
            quality_selector_labels={"selected-token": "Ultra-HD Master", "conflicting-token": None},
        )
        self.assertIsNone(conflicting_label["qualityMatch"])
        self.assertEqual(conflicting_label["qualityMatchStatus"], "ambiguous")
        self.assertIsNone(conflicting_label["actualQuality"])

    def test_renderer_metadata_filters_urls_and_credential_shaped_values(self):
        descriptor = service.normalize_stream(
            {
                "url": "https://cdn.example/real-stream.m3u8?signature=private",
                "quality": "https://cdn.example/quality.flac?token=secret-value",
                "codec": "Bearer authorization-secret",
                "drm": {"system": "https://license.example/private", "licenseUrl": "https://license.example/key"},
                "trackId": {"title": "https://metadata.example/secret"},
            },
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality={"quality": "high", "access_token": "renderer-token"},
            request_settings={"quality": "high", "api_key": "renderer-key"},
        )
        encoded = json.dumps(descriptor)
        self.assertNotIn("cdn.example", encoded)
        self.assertNotIn("license.example", encoded)
        self.assertNotIn("authorization-secret", encoded)
        self.assertNotIn("renderer-token", encoded)
        self.assertNotIn("renderer-key", encoded)
        self.assertTrue(descriptor["protection"]["licenseEndpointAvailable"])
        self.assertTrue(descriptor["protection"]["status"] == "protected")

        malformed_marker = service.normalize_stream(
            {"url": "https://cdn.example/unknown-encryption.m3u8", "encrypted": False,
             "keySystem": "https://metadata.example/private?credential=secret"},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="high", request_settings={"quality": "high"},
        )
        self.assertEqual(malformed_marker["protection"]["status"], "protected")
        self.assertIsNone(malformed_marker["protection"]["system"])
        self.assertNotIn("metadata.example", json.dumps(malformed_marker))

        described = service.normalize_stream(
            {"url": "https://cdn.example/private.m3u8", "provider": "Gravity", "profile": "eac3-joc",
             "headers": {"Authorization": "Bearer hidden"}},
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="Atmos", request_settings={"quality": "Atmos"},
        )
        self.assertEqual(described["providerIdentity"], "Gravity")
        self.assertEqual(described["playbackProfile"], "eac3-joc")
        self.assertTrue(described["authorizedHeadersAvailable"])
        self.assertNotIn("hidden", json.dumps(described))

    def test_flatten_stream_payload_accepts_arrays_and_reports_bounds_and_bad_items(self):
        from eclipse_streams import flatten_stream_payload, stream_payload_has_malformed_items, stream_payload_truncated

        array = [{"url": "https://cdn.example/a.mp3"}, None]
        self.assertEqual(len(flatten_stream_payload(array)), 1)
        self.assertTrue(stream_payload_has_malformed_items(array))
        with patch("eclipse_streams.MAX_STREAM_VARIANTS", 1):
            self.assertTrue(stream_payload_truncated(array))

    def test_apk_manifest_capabilities_and_protected_extraction_descriptors_are_preserved(self):
        caps = service.declared_addon_capabilities({
            "resources": ["search", "stream", "isrc", "catalog", "notifications", "settings"],
            "catalogs": [{"id": "new", "type": "track", "name": "New"}],
            "contentType": "music",
            "drmSystems": ["com.apple.fps"],
            "playbackProfiles": ["apple-fps-alac-v1"],
        })
        self.assertTrue(caps["search"])
        self.assertTrue(caps["stream"])
        self.assertTrue(caps["resolveISRC"])
        self.assertTrue(caps["provideCatalogs"])
        self.assertTrue(caps["notify"])
        self.assertTrue(caps["settings"])
        self.assertEqual(caps["drmSystems"], ["com.apple.fps"])

        descriptor = service.normalize_stream(
            {
                "codec": "alac", "format": "m4a", "quality": "lossless",
                "expires_at": 4_102_444_800, "trackId": "different-track",
                "extract": {"formats": "streamingData.adaptiveFormats"},
                "drm": {
                    "system": "fairplay", "keySystem": "com.apple.fps",
                    "licenseUrl": "https://license.example/secret",
                    "certificateUrl": "https://license.example/cert",
                    "multiSession": True, "initDataMapping": {"type": "skd"},
                    "nativeFairPlay": {"enabled": True},
                },
                "url": "https://cdn.example/signed.m3u8?token=renderer-secret",
                "headers": {"Authorization": "Bearer renderer-secret"},
            },
            addon_id="test.addon", addon_name="Test", track_id="track-1",
            requested_quality="lossless", request_settings={"quality": "lossless"},
        )
        self.assertIsNotNone(descriptor)
        self.assertTrue(descriptor["extractAvailable"])
        self.assertTrue(descriptor["urlAvailable"])
        self.assertEqual(descriptor["trackIdentityStatus"], "mismatch")
        self.assertEqual(descriptor["playbackCompatibility"], "track_identity_mismatch")
        self.assertEqual(descriptor["protection"]["system"], "fairplay")
        self.assertTrue(descriptor["protection"]["multiSession"])
        self.assertTrue(descriptor["protection"]["initDataMappingAvailable"])
        self.assertTrue(descriptor["protection"]["nativeFairPlayAvailable"])
        self.assertNotIn("_native", descriptor)
        self.assertNotIn("renderer-secret", json.dumps(descriptor))
        self.assertNotIn("license.example", json.dumps(descriptor))

    def test_manifest_capability_content_type_rejects_url_and_credential_text(self):
        capabilities = service.declared_addon_capabilities({
            "contentType": "https://metadata.example/secret?token=hidden",
            "resources": ["stream", "Bearer hidden-secret"],
        })
        self.assertIsNone(capabilities["contentType"])
        self.assertEqual(capabilities["resources"], ["stream"])
        encoded = json.dumps(capabilities)
        self.assertNotIn("metadata.example", encoded)
        self.assertNotIn("hidden", encoded)

    def resolve_payload(self, payload, requested_fmt="EAC3_JOC", payload_sequence=None,
                        addon_id="app.eclipsemusic.gravity", addon_name="Gravity"):
        requests = []
        responses = iter(payload_sequence or [payload])

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))

        with patch.object(
            service,
            "_ECLIPSE_AUTH_CACHE",
            {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}},
        ), patch.object(service, "_get_eclipse_addon_access_token", return_value=None), patch.object(
            service, "_eclipse_authenticated_urlopen", side_effect=fake_urlopen
        ), patch.object(
            service, "_eclipse_manifest_protection_status", return_value=False
        ), patch.object(service, "_probe_direct_audio_bitrate", return_value=(768_000, "test")), patch.object(
            service, "_probe_direct_media_transport", return_value="DIRECT"
        ):
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                requested_fmt,
                explicit_track_id="gravity-natural",
                addon_id=addon_id,
                addon_name=addon_name,
            )
        return result, requests

    def test_gravity_requests_declared_atmos_quality_and_accepts_explicit_joc(self):
        result, requests = self.resolve_payload(
            {
                "url": "https://cdn.example/natural.m3u8",
                "codec": "eac3_joc",
                "format": "hls",
                "quality": "Atmos",
                "sampleRate": 48000,
                "channels": 6,
                "encrypted": False,
            }
        )

        self.assertEqual(result["format"], "EAC3_JOC")
        self.assertEqual(result["codec"], "eac3_joc")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query)["quality"], ["Atmos"])

    def test_atmos_label_or_generic_codec_does_not_prove_joc(self):
        for codec in ("eac3", "ec-3", "ac4", "aac", None):
            with self.subTest(codec=codec):
                result, _ = self.resolve_payload(
                    {
                        "url": "https://cdn.example/stream",
                        "codec": codec,
                        "format": "Atmos",
                        "quality": "Atmos",
                    }
                )
                self.assertIsNone(result)

    def test_gravity_rejects_protected_stream_for_desktop_route(self):
        result, _ = self.resolve_payload(
            {
                "url": "https://cdn.example/protected.m3u8",
                "codec": "eac3_joc",
                "format": "hls",
                "quality": "Atmos",
                "encrypted": True,
                "drm": {"system": "widevine"},
            }
        )

        self.assertIsNone(result)

    def test_parent_level_drm_metadata_rejects_nested_streams(self):
        result, _ = self.resolve_payload(
            {
                "drm": {"system": "widevine"},
                "streams": [
                    {
                        "url": "https://cdn.example/protected.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                    }
                ],
            }
        )

        self.assertIsNone(result)

    def test_lossless_fallback_parent_drm_is_checked_after_retry(self):
        result, requests = self.resolve_payload(
            {},
            requested_fmt="LOSSLESS",
            payload_sequence=[
                {
                    "url": "https://cdn.example/max.flac",
                    "codec": "flac",
                    "sampleRate": 96000,
                    "bitDepth": 24,
                },
                {
                    "drm": {"system": "widevine"},
                    "streams": [
                        {
                            "url": "https://cdn.example/lossless.m3u8",
                            "codec": "flac",
                            "format": "hls",
                        }
                    ],
                },
            ],
        )

        self.assertIsNone(result)
        self.assertEqual(len(requests), 2)

    def test_candidate_level_drm_skips_protected_stream_and_uses_clear_candidate(self):
        result, _ = self.resolve_payload(
            {
                "streams": [
                    {
                        "url": "https://cdn.example/protected.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                        "drm": {"system": "widevine"},
                    },
                    {
                        "url": "https://cdn.example/clear.m3u8",
                        "codec": "eac3_joc",
                        "format": "hls",
                    },
                ]
            }
        )

        self.assertEqual(result["uri"], "https://cdn.example/clear.m3u8")

    def test_non_gravity_protected_stream_is_rejected_before_media_probe(self):
        for response in (
            {"url": "https://cdn.example/protected.flac", "codec": "flac", "format": "flac", "encryptionScheme": "cbcs"},
            {"streams": [{"url": "https://cdn.example/protected.flac", "codec": "flac", "format": "flac", "nativeFairPlay": True}]},
            {"url": "https://cdn.example/opaque.m3u8", "codec": "flac", "format": "hls", "encrypted": False},
        ):
            with self.subTest(response=response):
                requests = []
                payloads = [response]
                cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}
                def fake_open(request, timeout=None):
                    requests.append(request)
                    return io.BytesIO(json.dumps(payloads[0]).encode("utf-8"))
                with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
                     patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
                     patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open), \
                     patch.object(service, "_eclipse_manifest_protection_status", return_value=True), \
                     patch.object(service, "_probe_direct_audio_bitrate") as bitrate_probe, \
                     patch.object(service, "_probe_direct_media_transport") as transport_probe:
                    result = service.resolve_eclipse_addon(
                        self.manifest_url, "USRC12345678", "LOSSLESS", explicit_track_id="track-1",
                        addon_id="com.example.non-gravity", addon_name="Other provider",
                    )
                self.assertIsNone(result)
                bitrate_probe.assert_not_called()
                transport_probe.assert_not_called()

    def test_atmos_flac_response_is_not_cached_as_an_unrequested_max_tier(self):
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps({
                 "url": "https://cdn.example/fallback.flac", "codec": "flac", "format": "flac",
                 "quality": "lossless", "sampleRate": 44100, "bitDepth": 16,
             }).encode("utf-8"))), \
             patch.object(service, "_probe_direct_audio_bitrate", return_value=(1_000_000, "test")), \
             patch.object(service, "_probe_direct_media_transport", return_value="DIRECT"):
            service.resolve_eclipse_addon(
                self.manifest_url, "USRC12345678", "EAC3_JOC", explicit_track_id="track-1",
                addon_id="com.example.provider", addon_name="Provider",
            )
        self.assertTrue(any(key.endswith("|Atmos_EC-3") for key in cache["stream_cache"]))
        self.assertFalse(any(key.endswith("|Max") for key in cache["stream_cache"]))

    def test_generic_resolver_discards_response_when_account_changes_in_flight(self):
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}
        generation = service._ECLIPSE_AUTH_GENERATION
        def switch_account_during_request(_request, timeout=None):
            service._invalidate_eclipse_account_cache()
            return io.BytesIO(json.dumps({
                "url": "https://cdn.example/old-account.flac", "codec": "flac",
                "format": "flac", "quality": "lossless", "sampleRate": 44100, "bitDepth": 16,
            }).encode("utf-8"))
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=switch_account_during_request), \
             patch.object(service, "_probe_direct_audio_bitrate") as bitrate_probe, \
             patch.object(service, "_probe_direct_media_transport") as transport_probe:
            result = service.resolve_eclipse_addon(
                self.manifest_url, "USRC12345678", "LOSSLESS", explicit_track_id="track-1",
                addon_id="com.example.provider", addon_name="Provider",
            )
        self.assertIsNone(result)
        bitrate_probe.assert_not_called()
        transport_probe.assert_not_called()
        self.assertEqual(cache["stream_cache"], {})

    def test_tido_malformed_quality_metadata_skips_candidate_and_tries_next(self):
        responses = iter([
            {"tracks": [{"id": "secondary-id", "title": "Song", "artist": "Artist"}]},
            {"url": "https://cdn.example/bad.flac", "codec": "flac", "sampleRate": "not-a-number"},
            {"url": "https://cdn.example/good.flac", "codec": "flac", "sampleRate": 96000, "bitDepth": 24},
        ])
        def fake_open(_request, timeout=None):
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))
        with patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open), \
             patch.object(service, "_probe_direct_audio_bitrate", return_value=(5_000_000, "fixture")), \
             patch.object(service, "_eclipse_manifest_protection_status", return_value=False):
            result = service.resolve_tido_eclipse_addon(
                "https://tido.example/account/manifest.json", "primary-id", "HI_RES_LOSSLESS",
                title="Song", artist="Artist",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["uri"], "https://cdn.example/good.flac")
        self.assertEqual(result["sourceSampleRate"], 96000)

    def test_gravity_rejects_ac4_even_when_sample_rate_looks_hires(self):
        result, _ = self.resolve_payload(
            {
                "url": "https://cdn.example/audio.ac4",
                "codec": "ac4",
                "format": "ac4",
                "sampleRate": 96000,
                "bitDepth": 24,
            },
            requested_fmt="HI_RES_LOSSLESS",
        )

        self.assertIsNone(result)

    def test_ac4_ims_never_requests_an_eclipse_stream(self):
        with patch.object(service, "_eclipse_authenticated_urlopen") as urlopen:
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                "AC4_IMS",
                explicit_track_id="gravity-natural",
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
            )

        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_http_415_is_a_clear_protected_playback_failure(self):
        error = urllib.error.HTTPError(
            "https://gravity.example/stream/gravity-natural?quality=Atmos",
            415,
            "Unsupported Media Type",
            {},
            io.BytesIO(b"protected playback required"),
        )
        with patch.object(
            service,
            "_ECLIPSE_AUTH_CACHE",
            {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}},
        ), patch.object(service, "_get_eclipse_addon_access_token", return_value=None), patch.object(
            service, "_eclipse_authenticated_urlopen", side_effect=error
        ):
            result = service.resolve_eclipse_addon(
                self.manifest_url,
                "USUM71806694",
                "EAC3_JOC",
                explicit_track_id="gravity-natural",
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
            )

        self.assertIsNone(result)

    def test_discovery_enumerates_declared_quality_options_and_protected_descriptors(self):
        manifest = {
            "id": "app.eclipsemusic.gravity",
            "name": "Gravity",
            "settings": [
                {"key": "quality", "type": "select", "label": "Audio quality", "default": "max", "options": [
                    {"value": "max", "label": "Maximum"}, {"value": "lossless", "label": "Lossless"},
                ]},
                {"key": "videoQuality", "type": "select", "label": "Video quality", "default": "1080p", "options": [
                    {"value": "1080p", "label": "1080p"},
                ]},
                {"key": "region", "type": "text", "label": "Region", "default": "US"},
            ],
        }
        responses = iter([
            {"url": "https://cdn.example/max.mp3", "codec": "mp3", "container": "mp3", "quality": "max", "encrypted": False},
            {"url": "https://cdn.example/lossless.m3u8", "codec": "alac", "container": "m4a", "manifest": "hls", "quality": "lossless", "encrypted": True, "drm": {"system": "com.apple.fps", "licenseUrl": "https://license.example/private"}},
            {"url": "https://cdn.example/max.mp3", "codec": "mp3", "container": "mp3", "quality": "max", "encrypted": False},
        ])
        requests = []
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}}

        def fake_open(request, timeout=None):
            requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode("utf-8"))

        def fake_manifest_inspection(_url, *_args, media_variants_out=None, **_kwargs):
            if media_variants_out is not None:
                media_variants_out.append({
                    "kind": "hls_audio_rendition", "name": "Lossless audio",
                    "channels": 2, "protectionStatus": "unknown",
                })
            return False

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value="secret-addon-token"), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_manifest_protection_status", side_effect=fake_manifest_inspection), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=fake_open):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url,
                addon_id="app.eclipsemusic.gravity",
                addon_name="Gravity",
                track_id="track-1",
            )

        self.assertTrue(inventory["complete"])
        self.assertEqual([x["requestedQuality"] for x in inventory["qualityOptions"]], ["Maximum", "Lossless", None])
        self.assertEqual(len(inventory["streams"]), 2)
        protected = inventory["streams"][1]
        self.assertEqual(protected["protection"]["system"], "fairplay")
        self.assertEqual(protected["playbackCompatibility"], "authorized_protected_playback_required")
        self.assertEqual(protected["mediaVariants"][0]["kind"], "hls_audio_rendition")
        self.assertEqual(protected["mediaVariants"][0]["protectionStatus"], "protected")
        self.assertTrue(inventory["completeness"]["mediaVariantsComplete"])
        self.assertFalse(inventory["completeness"]["providerParityVerified"])
        encoded = json.dumps(inventory)
        self.assertNotIn("cdn.example", encoded)
        self.assertNotIn("license.example", encoded)
        self.assertNotIn("secret-addon-token", encoded)
        self.assertNotIn("_native", encoded)
        private_entries = auth_cache["discovered_streams"]
        self.assertEqual(len(private_entries), 2)
        self.assertTrue(all("cdn.example" in json.dumps(entry["native"]) for entry in private_entries.values()))
        self.assertTrue(all("secret-addon-token" in json.dumps(entry["addonRequestHeaders"]) for entry in private_entries.values()))
        self.assertEqual(
            [urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query).get("quality", [None])[0] for request in requests],
            ["max", "lossless", None],
        )
        for request in requests:
            self.assertEqual(request.get_header("X-eclipse-access"), "secret-addon-token")
            self.assertEqual(request.get_header("Accept"), "application/json, text/plain, */*")
            self.assertIn("Chrome/130.0.0.0", request.get_header("User-agent"))
            self.assertNotIn("videoQuality", urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query))
            self.assertNotIn("region", urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query))

    def test_discovery_requires_https_and_does_not_promote_addon_claim_to_verified_clear(self):
        manifest = {"id": "test.addon", "settings": []}
        response = {"url": "https://cdn.example/audio.m3u8", "codec": "aac", "manifest": "hls", "encrypted": False}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                title="https://metadata.example/private?token=hidden",
                artist={"Authorization": "Bearer nested-secret"},
                duration={"token": "nested-duration-secret"},
            )
        self.assertEqual(inventory["streams"][0]["protection"]["status"], "unknown")
        self.assertEqual(inventory["streams"][0]["protection"]["reportedStatus"], "clear")
        self.assertEqual(inventory["streams"][0]["playbackCompatibility"], "protection_unverified")
        track_json = json.dumps(inventory["track"])
        self.assertNotIn("metadata.example", track_json)
        self.assertNotIn("nested-secret", track_json)
        self.assertNotIn("nested-duration-secret", track_json)

        response["url"] = "http://cdn.example/insecure.mp3"
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["streams"], [])

    def test_discovery_marks_auth_or_rate_limit_failures_incomplete(self):
        manifest = {
            "id": "test.addon", "settings": [
                {"key": "quality", "type": "select", "label": "Audio quality", "options": [
                    {"value": "high", "label": "High"},
                ]},
            ],
        }
        error = urllib.error.HTTPError("https://gravity.example/stream/x", 429, "Too Many Requests", {}, io.BytesIO())
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=error) as mocked:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url,
                addon_id="test.addon",
                addon_name="Test",
                track_id="track-1",
            )
            repeated = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertFalse(inventory["complete"])
        self.assertEqual(inventory["attempts"][0]["reason"], "http_429")
        self.assertEqual(inventory["attempts"][0]["availability"], "unknown")
        self.assertEqual(inventory["attempts"][0]["retryAfterSeconds"], 60)
        self.assertEqual(repeated["reason"], "upstream_rate_limited")
        self.assertEqual(mocked.call_count, 1)

    def test_discovery_honors_http_date_retry_after(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        retry_after = formatdate(time.time() + 180, usegmt=True)
        error = urllib.error.HTTPError("https://gravity.example/stream/x", 429, "Too Many Requests",
                                       {"Retry-After": retry_after}, io.BytesIO())
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=error):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertGreaterEqual(inventory["attempts"][0]["retryAfterSeconds"], 170)
        self.assertLessEqual(inventory["attempts"][0]["retryAfterSeconds"], 180)

    def test_bounded_eclipse_request_deadline_covers_slow_response_headers(self):
        request = urllib.request.Request("https://gravity.example/manifest.json")

        def slow_headers(*_args, **_kwargs):
            time.sleep(0.12)
            return io.BytesIO(b"{}")

        started = time.monotonic()
        with patch.object(service, "_eclipse_authenticated_urlopen", side_effect=slow_headers):
            with self.assertRaises(TimeoutError):
                service._bounded_eclipse_response(request, 1024, started + 0.025, 1.0)
        self.assertLess(time.monotonic() - started, 0.25)

    def test_search_stream_url_rejection_marks_inventory_incomplete(self):
        manifest = {"id": "test.addon", "canSearch": True, "canStream": False, "settings": []}
        search = {"tracks": [{"id": "track-1", "title": "Song", "streamURL": "http://cdn.example/insecure.mp3"}]}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(search).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", title="Song", artist="Artist"
            )
        self.assertFalse(inventory["complete"])
        self.assertEqual(inventory["streams"], [])
        self.assertEqual(inventory["reason"], "one_or_more_quality_requests_unverified")

    def test_discovery_capacity_is_bounded_and_manifest_identity_must_match_connection(self):
        with patch.object(service, "_ECLIPSE_STREAM_DISCOVERY_SEMAPHORE") as semaphore:
            semaphore.acquire.return_value = False
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["reason"], "discovery_capacity_reached")

        with patch.object(service, "_get_eclipse_manifest", return_value={"id": "manifest.addon"}):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="connected.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["reason"], "manifest_addon_identity_mismatch")
        with patch.object(service, "_get_eclipse_manifest", return_value={"settings": []}):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="connected.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["reason"], "manifest_identity_missing_or_invalid")
        long_identity = "x" * 257
        with patch.object(service, "_get_eclipse_manifest", return_value={"id": long_identity}):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id=long_identity, addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["reason"], "manifest_identity_missing_or_invalid")

    def test_native_stream_reference_is_scoped_to_current_selected_provider(self):
        provider_key = "eclipse:" + "a" * 20
        cache = {
            "discovered_streams": {
                "opaque-reference-abcdefghijkl": {
                    "providerKey": provider_key,
                    "accountGeneration": service._ECLIPSE_AUTH_GENERATION,
                    "expires_at": time.time() + 60,
                    "manifestHash": service.hashlib.sha256(self.manifest_url.encode("utf-8")).hexdigest(),
                    "native": {"url": "https://cdn.example/private.m3u8"},
                },
            },
        }
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ordered_resolution_providers", return_value=([provider_key], {provider_key: {"manifestUrl": self.manifest_url}})):
            self.assertIsNotNone(service.get_discovered_eclipse_stream("opaque-reference-abcdefghijkl", provider_key))
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ordered_resolution_providers", return_value=([], {})):
            self.assertIsNone(service.get_discovered_eclipse_stream("opaque-reference-abcdefghijkl", provider_key))

    def test_wrong_provider_cannot_evict_another_providers_stream_reference(self):
        provider_key = "eclipse:" + "a" * 20
        wrong_provider_key = "eclipse:" + "b" * 20
        ref = "opaque-reference-abcdefghijkl"
        cache = {"discovered_streams": {ref: {
            "providerKey": provider_key,
            "accountGeneration": service._ECLIPSE_AUTH_GENERATION,
            "expires_at": time.time() + 60,
        }}}
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache):
            self.assertIsNone(service.get_discovered_eclipse_stream(ref, wrong_provider_key))
        self.assertIn(ref, cache["discovered_streams"])

    def test_stream_reference_is_rechecked_after_provider_lookup_account_switch(self):
        provider_key = "eclipse:" + "a" * 20
        ref = "opaque-reference-abcdefghijkl"
        generation = service._ECLIPSE_AUTH_GENERATION
        cache = {"discovered_streams": {ref: {
            "providerKey": provider_key,
            "accountGeneration": generation,
            "expires_at": time.time() + 60,
            "manifestHash": service.hashlib.sha256(self.manifest_url.encode("utf-8")).hexdigest(),
            "native": {"url": "https://cdn.example/private.m3u8"},
        }}}
        def switch_during_provider_lookup(_settings):
            service._ECLIPSE_AUTH_GENERATION = generation + 1
            return [provider_key], {provider_key: {"manifestUrl": self.manifest_url}}
        with patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_ordered_resolution_providers", side_effect=switch_during_provider_lookup):
            self.assertIsNone(service.get_discovered_eclipse_stream(ref, provider_key))

    def test_discovery_discards_response_if_eclipse_account_changes_in_flight(self):
        manifest = {
            "id": "test.addon", "settings": [
                {"key": "quality", "type": "select", "options": [{"value": "high"}]},
            ],
        }
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}

        def switch_account_during_request(request, timeout=None):
            service._invalidate_eclipse_account_cache()
            return io.BytesIO(json.dumps({
                "url": "https://cdn.example/old-account.m3u8?signature=secret",
                "codec": "aac", "quality": "high",
            }).encode("utf-8"))

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value="old-account-token"), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=switch_account_during_request):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        self.assertFalse(inventory["complete"])
        self.assertEqual(inventory["reason"], "account_changed_during_discovery")
        self.assertEqual(inventory["streams"], [])
        self.assertEqual(auth_cache["discovered_streams"], {})
        self.assertNotIn("cdn.example", json.dumps(inventory))
        self.assertNotIn("old-account-token", json.dumps(inventory))

    def test_cold_account_generation_initialization_does_not_abort_first_inventory(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        generation = service._ECLIPSE_AUTH_GENERATION

        def initialize_account_generation(*_args, **_kwargs):
            service._ECLIPSE_AUTH_GENERATION = generation + 1
            cache["addon_tokens"]["test.addon"] = {
                "token": "verified-addon-token",
                "expires_at": time.time() + 300,
                "accountGeneration": generation + 1,
            }
            return "verified-addon-token"

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", side_effect=initialize_account_generation), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps({
                 "url": "https://cdn.example/first-inventory.mp3", "codec": "mp3", "quality": "high",
             }).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        self.assertTrue(inventory["complete"])
        self.assertEqual(inventory["attempts"][0]["status"], "returned")
        self.assertEqual(inventory["streams"][0]["codec"], "mp3")

    def test_account_switch_after_addon_token_mint_is_rejected_before_request(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        generation = service._ECLIPSE_AUTH_GENERATION

        def switch_after_token_mint(*_args, **_kwargs):
            cache["addon_tokens"]["test.addon"] = {
                "token": "old-account-addon-token",
                "expires_at": time.time() + 300,
                "accountGeneration": generation,
            }
            service._ECLIPSE_AUTH_GENERATION = generation + 1
            return "old-account-addon-token"

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", side_effect=switch_after_token_mint), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_eclipse_authenticated_urlopen") as upstream_request:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        self.assertEqual(inventory["reason"], "account_changed_during_discovery")
        self.assertEqual(inventory["streams"], [])
        upstream_request.assert_not_called()

    def test_missing_minted_token_generation_is_rejected_even_with_new_account_connection(self):
        manifest = {"id": "test.addon", "settings": []}
        jwt_b = "account-b-session"
        current_generation = service._ECLIPSE_AUTH_GENERATION + 1
        cache = {
            "jwt": jwt_b,
            "search_cache": {},
            "stream_cache": {},
            "addon_tokens": {},
            "discovered_streams": {},
            "account_addons": {"current": {
                "token_key": service.hashlib.sha256(jwt_b.encode("utf-8")).hexdigest(),
                "accountGeneration": current_generation,
                "expires_at": time.time() + 300,
                "result": {"addons": [{
                    "addonId": "test.addon", "manifestUrl": self.manifest_url,
                    "isEnabled": True,
                }]},
            }},
        }
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value="account-a-addon-token"), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", current_generation), \
             patch.object(service, "_eclipse_authenticated_urlopen") as upstream_request:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                provider_key="eclipse:" + "a" * 20,
            )

        self.assertEqual(inventory["reason"], "account_changed_during_discovery")
        upstream_request.assert_not_called()

    def test_inventory_requires_manifest_to_match_current_enabled_account_connection(self):
        manifest = {"id": "test.addon", "settings": []}
        jwt = "verified-account-session"
        cache = {
            "jwt": jwt,
            "search_cache": {},
            "stream_cache": {},
            "addon_tokens": {"test.addon": {
                "token": "fresh-addon-token", "expires_at": time.time() + 300,
                "accountGeneration": service._ECLIPSE_AUTH_GENERATION,
            }},
            "discovered_streams": {},
            "account_addons": {"current": {
                "token_key": service.hashlib.sha256(jwt.encode("utf-8")).hexdigest(),
                "accountGeneration": service._ECLIPSE_AUTH_GENERATION,
                "expires_at": time.time() + 300,
                "result": {"addons": [{
                    "addonId": "test.addon", "manifestUrl": "https://gravity.example/old-manifest.json",
                    "isEnabled": True,
                }]},
            }},
        }
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value="fresh-addon-token"), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen") as upstream_request:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                provider_key="eclipse:" + "a" * 20,
            )

        self.assertEqual(inventory["reason"], "connected_addon_changed")
        self.assertEqual(inventory["streams"], [])
        upstream_request.assert_not_called()

    def test_missing_addon_token_allows_only_current_generation_connected_public_addon(self):
        generation = service._ECLIPSE_AUTH_GENERATION
        jwt = "verified-account-session"
        cache = {
            "jwt": jwt,
            "addon_tokens": {},
            "discovered_streams": {},
            "search_cache": {},
            "stream_cache": {},
            "account_addons": {"current": {
                "token_key": service.hashlib.sha256(jwt.encode("utf-8")).hexdigest(),
                "accountGeneration": generation,
                "expires_at": time.time() + 300,
                "result": {"addons": [{
                    "addonId": "test.addon", "manifestUrl": self.manifest_url, "isEnabled": True,
                }]},
            }},
        }
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "public"}],
        }]}
        response = {"url": "https://cdn.example/public.mp3", "codec": "mp3", "quality": "public"}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1",
                provider_key="eclipse:" + "a" * 20,
            )

        self.assertTrue(inventory["complete"])
        self.assertEqual(inventory["streams"][0]["codec"], "mp3")

    def test_generationless_cached_addon_token_is_reminted(self):
        generation = 7
        jwt = "verified-account-session"
        user = {"id": "eclipse-user-1", "email": "listener@example.test"}
        cache = {
            "jwt": jwt,
            "addon_tokens": {"test.addon": {
                "token": "legacy-token", "expires_at": time.time() + 300,
            }},
        }
        response = json.dumps({"token": "generation-bound-token", "expiresIn": 300}).encode("utf-8")
        with patch.object(service, "_verified_eclipse_session_snapshot", return_value=(jwt, user, generation)), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", generation), \
             patch.object(service, "_ECLIPSE_ACCOUNT_STATUS", {"user": user}), \
             patch.object(service, "_bounded_eclipse_response", return_value=response) as mint_request:
            token = service._get_eclipse_addon_access_token("test.addon")

        self.assertEqual(token, "generation-bound-token")
        mint_request.assert_called_once()
        self.assertEqual(cache["addon_tokens"]["test.addon"]["accountGeneration"], generation)

    def test_discovery_deduplicates_identical_streams_and_preserves_quality_associations(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select",
            "options": [{"value": "high", "label": "high"}, {"value": "max", "label": "max"}],
        }]}
        responses = iter([
            {"url": "https://cdn.example/shared.mp3", "codec": "mp3", "quality": "same"},
            {"url": "https://cdn.example/shared.mp3", "codec": "mp3", "quality": "same"},
        ])
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps(next(responses)).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(len(inventory["streams"]), 1)
        self.assertEqual([item["quality"] for item in inventory["streams"][0]["requestedQualities"]], ["high", "max"])

    def test_discovery_matches_returned_quality_to_private_selector_token(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [
                {"value": "jymaster", "label": "Ultra-HD Master"},
                {"value": "high", "label": "High"},
            ],
        }]}
        responses = iter([
            {"url": "https://cdn.example/shared.m3u8", "codec": "flac", "quality": "jymaster"},
            {"url": "https://cdn.example/shared.m3u8", "codec": "flac", "quality": "jymaster"},
        ])
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_manifest_protection_status", return_value=False), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps(next(responses)).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        stream = inventory["streams"][0]
        self.assertEqual(stream["requestedQualities"][0]["quality"], "Ultra-HD Master")
        self.assertTrue(stream["requestedQualities"][0]["qualityMatch"])
        self.assertFalse(stream["requestedQualities"][1]["qualityMatch"])
        self.assertIsNone(stream["qualityMatch"])
        self.assertNotIn("jymaster", json.dumps(inventory))

    def test_extensionless_adaptive_manifest_is_inspected_in_inventory(self):
        manifest = {"id": "test.addon", "settings": []}
        response = {"url": "https://cdn.example/content/object-47", "codec": "aac"}
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        inspected = []

        def inspect(url, manifest_type=None, *, media_variants_out=None, **_kwargs):
            inspected.append((url, manifest_type))
            return False

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_manifest_protection_status", side_effect=inspect), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        self.assertEqual(inspected, [(response["url"], None)])
        self.assertTrue(inventory["streams"][0]["mediaVariantsInspectionComplete"])

    def test_manifest_inspection_cache_is_scoped_to_headers_and_manifest_type(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [
                {"value": "first", "label": "First"}, {"value": "second", "label": "Second"},
            ],
        }]}
        responses = iter([
            {"url": "https://cdn.example/shared.mpd", "codec": "alac", "manifest": "dash",
             "headers": {"Authorization": "Bearer first"}},
            {"url": "https://cdn.example/shared.mpd", "codec": "alac", "manifest": "dash",
             "headers": {"Authorization": "Bearer second"}},
        ])
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        inspected_headers = []

        def inspect(_url, _manifest_type=None, *, request_headers=None, media_variants_out=None, **_kwargs):
            inspected_headers.append(dict(request_headers or {}))
            return "first" in str((request_headers or {}).get("Authorization"))

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_manifest_protection_status", side_effect=inspect), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps(next(responses)).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )

        self.assertEqual([item["Authorization"] for item in inspected_headers], ["Bearer first", "Bearer second"])
        self.assertEqual([item["manifestProtectionStatus"] for item in inventory["streams"]], ["protected", "no_protection_signal_found"])

    def test_discovery_does_not_call_metadata_only_json_a_valid_stream_result(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        auth_cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        response = {"codec": "flac", "quality": "high"}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", auth_cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertFalse(inventory["complete"])
        self.assertEqual(inventory["attempts"][0]["status"], "invalid_stream_descriptors")
        self.assertEqual(inventory["completeness"]["malformedStreamCandidates"], True)

    def test_discovery_stops_after_resolve_isrc_authorization_failure(self):
        manifest = {"id": "test.addon", "canSearch": False, "canResolveISRC": True,
                    "canResolveIdentity": True, "settings": []}
        error = urllib.error.HTTPError("https://gravity.example/resolve-isrc", 401, "Unauthorized", {}, io.BytesIO())
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=error) as mocked:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test",
                isrc="USRC17607839", title="Track", artist="Artist",
            )
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(inventory["reason"], "track_resolve_isrc_http_401")
        self.assertEqual(inventory["streams"], [])

    def test_discovery_marks_mixed_valid_and_invalid_variants_incomplete(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        response = [
            {"url": "https://cdn.example/valid.mp3", "codec": "mp3"},
            {"codec": "flac", "quality": "high"},
        ]
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(response).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(len(inventory["streams"]), 1)
        self.assertFalse(inventory["complete"])
        self.assertTrue(inventory["completeness"]["malformedStreamCandidates"])

    def test_stale_account_rate_limit_does_not_replace_new_account_cooldown(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        account_generation = service._ECLIPSE_AUTH_GENERATION
        newer_cooldown = {"accountGeneration": account_generation + 1, "retry_at": time.time() + 900}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {},
                 "addon_cooldowns": {"test.addon": newer_cooldown}}
        error = urllib.error.HTTPError("https://gravity.example/stream/track-1", 429, "Too Many Requests", {}, io.BytesIO())

        def switch_account_then_rate_limit(*_args, **_kwargs):
            service._ECLIPSE_AUTH_GENERATION = account_generation + 1
            raise error

        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_ECLIPSE_AUTH_GENERATION", account_generation), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=switch_account_then_rate_limit):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(inventory["reason"], "account_changed_during_discovery")
        self.assertIs(cache["addon_cooldowns"]["test.addon"], newer_cooldown)

    def test_deduplication_ignores_sibling_stream_lists(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [
                {"value": "high", "label": "high"}, {"value": "max", "label": "max"},
            ],
        }]}
        responses = iter([
            {"streams": [{"url": "https://cdn.example/shared.mp3", "codec": "mp3"},
                         {"url": "https://cdn.example/first-sibling.flac", "codec": "flac"}]},
            {"streams": [{"url": "https://cdn.example/shared.mp3", "codec": "mp3"},
                         {"url": "https://cdn.example/other-sibling.flac", "codec": "flac"}]},
        ])
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=lambda *_a, **_k: io.BytesIO(json.dumps(next(responses)).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        shared = [stream for stream in inventory["streams"] if stream["urlAvailable"] and stream["codec"] == "mp3"]
        self.assertEqual(len(shared), 1)
        self.assertEqual([item["quality"] for item in shared[0]["requestedQualities"]], ["high", "max"])

    def test_discovery_stops_after_upstream_rate_limit_and_cache_stays_bounded(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select",
            "options": [{"value": "high"}, {"value": "max"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        for index in range(service.MAX_ECLIPSE_STREAM_REQUESTS):
            cache["discovered_streams"][f"old-{index}"] = {"expires_at": time.time() + 600}
        error = urllib.error.HTTPError("https://gravity.example/stream/x", 429, "Too Many Requests", {}, io.BytesIO())
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", side_effect=error) as mocked:
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(inventory["attempts"][1]["status"], "not_attempted")
        self.assertLessEqual(len(cache["discovered_streams"]), service.MAX_ECLIPSE_STREAM_REQUESTS)

    def test_discovery_evicts_old_native_entries_and_reports_stream_result_truncation(self):
        manifest = {"id": "test.addon", "settings": [{
            "key": "quality", "type": "select", "options": [{"value": "high"}],
        }]}
        cache = {"search_cache": {}, "stream_cache": {}, "addon_tokens": {}, "discovered_streams": {}}
        for index in range(service.MAX_ECLIPSE_STREAM_REQUESTS):
            cache["discovered_streams"][f"old-{index}"] = {"expires_at": time.time() + 600}
        one_response = {"url": "https://cdn.example/new.mp3", "codec": "mp3"}
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(one_response).encode("utf-8"))):
            service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertEqual(len(cache["discovered_streams"]), service.MAX_ECLIPSE_STREAM_REQUESTS)
        self.assertNotIn("old-0", cache["discovered_streams"])

        variants = [
            {"url": f"https://cdn.example/{index}.mp3", "codec": "mp3"}
            for index in range(eclipse_streams.MAX_STREAM_VARIANTS + 1)
        ]
        cache["discovered_streams"].clear()
        with patch.object(service, "_get_eclipse_manifest", return_value=manifest), \
             patch.object(service, "_get_eclipse_addon_access_token", return_value=None), \
             patch.object(service, "_ECLIPSE_AUTH_CACHE", cache), \
             patch.object(service, "_eclipse_authenticated_urlopen", return_value=io.BytesIO(json.dumps(variants).encode("utf-8"))):
            inventory = service.discover_eclipse_addon_streams(
                self.manifest_url, addon_id="test.addon", addon_name="Test", track_id="track-1"
            )
        self.assertFalse(inventory["complete"])
        self.assertTrue(inventory["completeness"]["streamResultsTruncated"])
        self.assertEqual(len(inventory["streams"]), eclipse_streams.MAX_STREAM_VARIANTS)
        self.assertEqual(len(cache["discovered_streams"]), eclipse_streams.MAX_STREAM_VARIANTS)


if __name__ == "__main__":
    unittest.main()
