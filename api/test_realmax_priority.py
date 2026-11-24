"""Provider and quality ordering regressions for RealMAX resolution."""

from __future__ import annotations

import itertools
import unittest
from unittest.mock import patch

import omnifusion_service as service


FORMATS = ("AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS")
ADDON_KEYS = (
    "eclipse:gravity",
)
PROVIDERS = ("tidal_api", *ADDON_KEYS)


class _Artist:
    name = "Test Artist"


class _Track:
    id = 424242
    name = "Test Track"
    full_name = "Test Track"
    isrc = "USXXX2400001"
    duration = 180
    artist = _Artist()


class _Session:
    @staticmethod
    def track(_track_id):
        return _Track()

    @staticmethod
    def search(*_args, **_kwargs):
        return {"tracks": []}


def _provider_rows():
    return {
        key: {
            "addonId": f"test.{key.rsplit(':', 1)[-1]}",
            "manifestUrl": f"https://example.invalid/{key.rsplit(':', 1)[-1]}/manifest.json",
            "displayName": key,
        }
        for key in ADDON_KEYS
    }


class RealmaxPriorityTests(unittest.TestCase):
    def _resolver_patches(self, order, support):
        rows = _provider_rows()

        def tidal(fmt):
            if fmt in support["tidal_api"]:
                return {"format": fmt, "sourceProvider": "TIDAL"}
            return None

        def eclipse(_url, _isrc, fmt, *_args, **kwargs):
            addon_id = kwargs.get("addon_id", "")
            key = next((key for key, row in rows.items() if row["addonId"] == addon_id), None)
            if key and fmt in support[key]:
                return {"format": fmt, "sourceProvider": key}
            return None

        return (
            patch.object(service, "_ordered_resolution_providers", return_value=(list(order), rows)),
            patch.object(service, "is_main_tidal_api_authorized", return_value=True),
            patch.object(service, "get_python_tidal_session", return_value=_Session()),
            patch.object(service, "resolve_eac3_joc", side_effect=lambda _tid: tidal("EAC3_JOC")),
            patch.object(service, "resolve_ac4_ims", side_effect=lambda _tid: tidal("AC4_IMS")),
            patch.object(
                service,
                "resolve_hi_res_or_lossless",
                side_effect=lambda _tid, fmt: tidal(fmt),
            ),
            patch.object(service, "resolve_eclipse_addon", side_effect=eclipse),
            patch.object(service, "_is_tido_eclipse_addon", return_value=False),
        )

    def test_all_provider_and_quality_order_permutations(self):
        """Exercise both live providers in both orders against all 24 qualities."""
        support = {
            "tidal_api": {"AC4_IMS", "LOSSLESS"},
            "eclipse:gravity": {"EAC3_JOC", "HI_RES_LOSSLESS"},
        }
        cases = 0
        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch("builtins.print"):
            for provider_order in itertools.permutations(PROVIDERS):
                for quality_order in itertools.permutations(FORMATS):
                    patches = self._resolver_patches(provider_order, support)
                    with patches[0], patches[1], patches[2], patches[3], patches[4], \
                            patches[5], patches[6], patches[7]:
                        result = service.resolve_with_priority(424242, list(quality_order))

                    expected = next(
                        (
                            (fmt, provider)
                            for fmt in quality_order
                            for provider in provider_order
                            if fmt in support[provider]
                            and (fmt != "AC4_IMS" or provider == "tidal_api")
                        ),
                        None,
                    )
                    self.assertIsNotNone(expected)
                    self.assertEqual(result["format"], expected[0])
                    expected_source = "TIDAL" if expected[1] == "tidal_api" else expected[1]
                    self.assertEqual(result["sourceProvider"], expected_source)
                    cases += 1

        self.assertEqual(cases, 2 * 24)

    def test_eclipse_only_and_tidal_only_modes_cover_every_quality_order(self):
        support = {
            "tidal_api": {"AC4_IMS", "LOSSLESS"},
            "eclipse:gravity": {"EAC3_JOC", "HI_RES_LOSSLESS"},
        }
        cases = 0
        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch("builtins.print"):
            for provider_order in itertools.permutations(ADDON_KEYS):
                for quality_order in itertools.permutations(FORMATS):
                    patches = self._resolver_patches(provider_order, support)
                    with patches[0], patches[1], patches[2], patches[3], patches[4], \
                            patches[5], patches[6], patches[7]:
                        result = service.resolve_with_priority(424242, list(quality_order))

                    expected = next(
                        (
                            (fmt, provider)
                            for fmt in quality_order
                            for provider in provider_order
                            if fmt != "AC4_IMS" and fmt in support[provider]
                        ),
                        None,
                    )
                    self.assertIsNotNone(expected)
                    self.assertEqual(result["format"], expected[0])
                    self.assertEqual(result["sourceProvider"], expected[1])
                    cases += 1

            tidal_order = ["tidal_api"]
            for quality_order in itertools.permutations(FORMATS):
                patches = self._resolver_patches(tidal_order, support)
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6], patches[7]:
                    result = service.resolve_with_priority(424242, list(quality_order))

                expected = next(
                    (fmt for fmt in quality_order if fmt in support["tidal_api"]),
                    None,
                )
                self.assertIsNotNone(expected)
                self.assertEqual(result["format"], expected)
                self.assertEqual(result["sourceProvider"], "TIDAL")
                cases += 1

        self.assertEqual(cases, 24 + 24)

    def test_explicit_tidal_result_uses_tidal_for_supported_quality(self):
        rows = _provider_rows()
        order = ("tidal_api", *ADDON_KEYS)
        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=(list(order), rows)), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "get_python_tidal_session", return_value=_Session()), \
                patch.object(service, "resolve_eac3_joc", return_value={"format": "EAC3_JOC", "sourceProvider": "TIDAL"}), \
                patch.object(service, "resolve_eclipse_addon") as addon:
            result = service.resolve_with_priority(
                424242,
                ["EAC3_JOC"],
                provider_key="tidal_api",
            )

        self.assertEqual(result["sourceProvider"], "TIDAL")
        addon.assert_not_called()

    def test_tidal_search_result_falls_through_by_quality_in_both_mode(self):
        key = ADDON_KEYS[0]
        provider = {
            "manifestUrl": "https://example.invalid/gravity/manifest.json",
            "addonId": "test.gravity",
            "displayName": "Gravity",
        }
        session = _Session()
        expected = {"format": "EAC3_JOC", "sourceProvider": key}
        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch.object(
                    service,
                    "_ordered_resolution_providers",
                    return_value=(["tidal_api", key], {key: provider}),
                ), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "get_python_tidal_session", return_value=session), \
                patch.object(service, "resolve_eac3_joc", return_value=None), \
                patch.object(service, "resolve_eclipse_addon", return_value=expected) as addon:
            result = service.resolve_with_priority(
                424242,
                ["EAC3_JOC", "LOSSLESS"],
                provider_key="tidal_api",
            )

        self.assertEqual(result, expected)
        addon.assert_called_once()

    def test_explicit_eclipse_result_never_falls_through_to_tidal(self):
        rows = _provider_rows()
        order = ("tidal_api", *ADDON_KEYS)
        with patch.object(service, "load_settings", return_value={"realmax_source": "both"}), \
                patch.object(service, "_ordered_resolution_providers", return_value=(list(order), rows)), \
                patch.object(service, "is_main_tidal_api_authorized", return_value=True), \
                patch.object(service, "resolve_eclipse_addon", return_value=None), \
                patch.object(service, "resolve_ac4_ims") as tidal:
            with self.assertRaisesRegex(RuntimeError, "selected Eclipse provider"):
                service.resolve_with_priority(
                    424242,
                    ["AC4_IMS", "EAC3_JOC"],
                    provider_key=ADDON_KEYS[0],
                )

        tidal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
