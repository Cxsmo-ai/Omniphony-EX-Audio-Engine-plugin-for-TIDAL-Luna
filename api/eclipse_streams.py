"""Safe, provider-neutral normalization for Eclipse addon stream discovery.

This module deliberately keeps media URLs and authorization material out of
renderer-facing inventory results. It describes availability independently
from the current playback engine's ability to consume that stream.
"""

from __future__ import annotations

import math
import re
import secrets
import time
from itertools import product
from typing import Any
from urllib.parse import urlsplit


# Large protocol bounds preserve real provider catalogs while keeping a single
# untrusted addon response from expanding without limit. Reaching either bound
# is surfaced as incomplete discovery, never reported as exhaustive.
MAX_QUALITY_OPTIONS = 4096
MAX_STREAM_VARIANTS = 2048
MAX_METADATA_TEXT = 512
MAX_METADATA_DEPTH = 6
MAX_METADATA_NODES = 128
_QUALITY_KEYS = {"quality", "audioquality", "audio_quality", "audio-quality", "streamquality", "stream_quality", "stream-quality"}
_VIDEO_KEY = re.compile(r"video|resolution|subtitle", re.I)
_NON_AUDIO_QUALITY = re.compile(r"\b(?:cover|artwork|image|thumbnail|poster|video|resolution|subtitle)\b", re.I)
_AUDIO_QUALITY_LABEL = re.compile(r"(?:audio|stream|music).{0,24}quality|quality.{0,24}(?:audio|stream|music)", re.I)
_KNOWN_DRM_SYSTEMS = {
    "com.apple.fps": "fairplay",
    "fairplay": "fairplay",
    "com.widevine.alpha": "widevine",
    "widevine": "widevine",
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "widevine",
    "playready": "playready",
    "com.microsoft.playready": "playready",
}
_SENSITIVE_METADATA = re.compile(
    r"(?i)(?:https?://|\bBearer\s+|\b(?:authorization|cookie|access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|signature|credential|license[_-]?url)\s*[:=]|\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{8,}\b)"
)
_SENSITIVE_KEY = re.compile(r"(?i)^(?:authorization|cookie|.*(?:token|secret|password|api[_-]?key|credential|signature).*)$")


def _clean_text(value: Any, limit: int = MAX_METADATA_TEXT) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    result = str(value).strip()
    if not result or len(result) > limit or any(ord(c) < 0x20 for c in result):
        return None
    return result


def _renderer_text(value: Any, limit: int = MAX_METADATA_TEXT) -> str | None:
    """Allow ordinary labels while rejecting URL/credential-shaped metadata."""
    result = _clean_text(value, limit)
    if result is None or _SENSITIVE_METADATA.search(result):
        return None
    return result


def _renderer_json_value(value: Any, *, _depth: int = 0, _budget: list[int] | None = None) -> Any:
    """Copy bounded scalar metadata without permitting deeply nested payloads."""
    if _budget is None:
        _budget = [MAX_METADATA_NODES]
    if _depth > MAX_METADATA_DEPTH or _budget[0] <= 0:
        return None
    _budget[0] -= 1
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:16]:
            if _budget[0] <= 0:
                break
            clean_key = _renderer_text(key, 96)
            if clean_key is None or _SENSITIVE_KEY.fullmatch(clean_key):
                continue
            clean_value = _renderer_json_value(item, _depth=_depth + 1, _budget=_budget)
            if clean_value is not None:
                result[clean_key] = clean_value
        return result or None
    if isinstance(value, list):
        result = [
            _renderer_json_value(item, _depth=_depth + 1, _budget=_budget)
            for item in value[:16]
            if _budget[0] > 0
        ]
        result = [item for item in result if item is not None]
        return result or None
    if isinstance(value, str):
        return _renderer_text(value, 160)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= (2**53 - 1) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None:
        return None
    return None


def _renderer_duration(value: Any) -> float | None:
    duration = _positive_number(value)
    return duration if duration is not None and duration <= 86_400 else None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _bounded_integer(value: Any, *, minimum: int, maximum: int) -> int | None:
    number = _positive_number(value)
    if number is None or not number.is_integer():
        return None
    integer = int(number)
    return integer if minimum <= integer <= maximum else None


def _quality_settings(manifest: dict) -> list[dict]:
    """Return declared audio/stream quality select settings and exact values."""
    settings = manifest.get("settings")
    if not isinstance(settings, list):
        return []
    result = []
    seen_keys: dict[str, dict] = {}
    for setting in settings:
        if not isinstance(setting, dict) or str(setting.get("type", "")).lower() != "select":
            continue
        key = _renderer_text(setting.get("key"), 96)
        if not key:
            continue
        label = _renderer_text(setting.get("label"), 160)
        key_token = key.lower().replace(" ", "")
        category = str(setting.get("category") or setting.get("purpose") or "").strip().lower()
        label_text = label or ""
        # Manifest setting keys can be camelCase (for example
        # `coverQuality`), so split those words before checking whether a
        # generic quality selector actually belongs to artwork or video.
        setting_hints = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", f"{key} {label_text} {category}")
        # Addons may use provider-specific setting keys. Recognize those only
        # when the key itself contains "quality", the category explicitly
        # names audio quality, or the human label ties quality to audio/music
        # or streams. A video/resolution/subtitle marker always wins.
        declared_audio_quality = (
            key_token in _QUALITY_KEYS
            or "quality" in key_token
            or category.replace("_", "-") in {"audio-quality", "stream-quality", "music-quality"}
            or bool(_AUDIO_QUALITY_LABEL.search(label_text))
        )
        if (
            not key
            or _VIDEO_KEY.search(key)
            or _VIDEO_KEY.search(label_text)
            or _VIDEO_KEY.search(category)
            or _NON_AUDIO_QUALITY.search(setting_hints)
            or not declared_audio_quality
        ):
            continue
        folded_key = key.casefold()
        if folded_key in seen_keys:
            seen_keys[folded_key]["optionsUnsafe"] = True
            seen_keys[folded_key]["selectorAmbiguous"] = True
            continue
        options = setting.get("options")
        if not isinstance(options, list):
            definition = {
                "key": key, "label": label or key, "default": None,
                "options": [], "optionsTruncated": False, "optionsUnsafe": True,
                "selectorAmbiguous": False,
            }
            result.append(definition)
            seen_keys[folded_key] = definition
            continue
        choices = []
        seen = set()
        options_unsafe = False
        for option in options:
            raw_value = option.get("value") if isinstance(option, dict) else option
            if isinstance(raw_value, bool) or raw_value is None or isinstance(raw_value, (dict, list)):
                options_unsafe = True
                continue
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                options_unsafe = True
                continue
            value = _renderer_text(raw_value, 160)
            if not value or value in seen:
                options_unsafe = True
                continue
            if isinstance(raw_value, str) and value != raw_value:
                options_unsafe = True
                continue
            if isinstance(raw_value, int) and abs(raw_value) > (2**53 - 1):
                options_unsafe = True
                continue
            seen.add(value)
            display = _renderer_text(option.get("label"), 160) if isinstance(option, dict) else None
            # Keep exact option values native-only. A manifest can use an
            # opaque value as its selector token; the label is the renderer's
            # display contract. If no label exists, expose an ordinal rather
            # than guessing that the raw value is safe to show.
            choices.append({"value": value, "label": display or f"Option {len(choices) + 1}"})
            if len(choices) >= MAX_QUALITY_OPTIONS:
                break
        if not choices:
            definition = {
                "key": key, "label": label or key, "default": None,
                "options": [], "optionsTruncated": False, "optionsUnsafe": True,
                "selectorAmbiguous": False,
            }
            result.append(definition)
            seen_keys[folded_key] = definition
            continue
        default = _renderer_text(setting.get("default"), 160)
        definition = {
            "key": key,
            "label": label or key,
            "default": default,
            "options": choices,
            "optionsTruncated": len(seen) > MAX_QUALITY_OPTIONS or len(options) > MAX_QUALITY_OPTIONS,
            "optionsUnsafe": options_unsafe,
            "selectorAmbiguous": False,
        }
        result.append(definition)
        seen_keys[folded_key] = definition
    return result


def quality_candidates(manifest: dict, *, include_default: bool = False) -> list[dict]:
    """Build bounded exact request configurations from addon-declared defaults/options.

    Only confidently identified audio/stream quality settings are varied.
    Other settings are left untouched because the account connection response
    does not expose the user's selected values.
    """
    quality_defs = _quality_settings(manifest)
    if not quality_defs:
        return [{"requestedQuality": None, "settings": {}, "qualityKey": None}]
    # An invalid or empty setting must not erase the useful combinations from
    # other valid settings. Omit only the unsafe key; completeness reporting
    # still marks the inventory as partial through quality_options_unsafe().
    valid_defs = [definition for definition in quality_defs
                  if definition["options"] and not definition.get("selectorAmbiguous")]
    if not valid_defs:
        # Still ask the add-on for its default stream. Unsafe/empty selectors
        # keep the inventory partial; they must not hide an otherwise usable
        # default response.
        return [{"requestedQuality": None, "settings": {}, "qualityKey": None}]
    # Query the manifest-configured/default response as well as explicit
    # selectors. Eclipse appends saved add-on settings to this request; the
    # account API does not expose those saved values, so the baseline preserves
    # the URL's existing configuration without inventing a selector.
    candidates = []
    explicit_limit = MAX_QUALITY_OPTIONS - (1 if include_default else 0)
    option_groups = [definition["options"] for definition in valid_defs]
    for combination in product(*option_groups):
        if len(candidates) >= explicit_limit:
            break
        params = {definition["key"]: option["value"] for definition, option in zip(valid_defs, combination)}
        label = combination[0]["label"] if len(combination) == 1 else "; ".join(
            f"{definition['label']}: {option['label']}" for definition, option in zip(valid_defs, combination)
        )
        display_settings = {
            definition["key"]: option["label"]
            for definition, option in zip(valid_defs, combination)
        }
        candidates.append({
            "requestedQuality": label,
            "requestedQualityLabel": label,
            "qualityKey": valid_defs[0]["key"] if len(valid_defs) == 1 else [item["key"] for item in valid_defs],
            "settings": params,
            "publicSettings": display_settings,
        })
    if include_default:
        candidates.append({"requestedQuality": None, "settings": {}, "qualityKey": None})
    return candidates


def quality_candidate_limit_exceeded(manifest: dict, *, include_default: bool = False) -> bool:
    definitions = _quality_settings(manifest)
    groups = [item["options"] for item in definitions
              if item["options"] and not item.get("selectorAmbiguous")]
    return any(item.get("optionsTruncated") for item in definitions) or (
        bool(groups) and math.prod(len(group) for group in groups) + int(include_default) > MAX_QUALITY_OPTIONS
    )


def quality_options_unsafe(manifest: dict) -> bool:
    return any(item.get("optionsUnsafe") for item in _quality_settings(manifest))


def quality_selector_label_map(manifest: dict) -> dict[str, str | None]:
    """Map selector tokens to labels, marking conflicting labels as ambiguous."""
    labels_by_value: dict[str, set[str]] = {}
    for definition in _quality_settings(manifest):
        if definition.get("selectorAmbiguous"):
            continue
        for option in definition["options"]:
            value, label = option.get("value"), option.get("label")
            if isinstance(value, str) and isinstance(label, str):
                labels_by_value.setdefault(value, set()).add(label)
    return {value: next(iter(labels)) if len(labels) == 1 else None for value, labels in labels_by_value.items()}


def stream_request_candidates(
    manifest: dict,
    quality_candidate: dict,
    *,
    addon_id: str | None = None,
    fairplay_available: bool = False,
    dolby_codecs: tuple[str, ...] = ("eac3", "ac4"),
) -> tuple[list[dict], bool]:
    """Expand a declared quality request with APK-observed playback negotiation.

    Eclipse Android appends `drm`, `dolbyCodecs`, and `playbackProfile` to
    `/stream/{trackId}` requests. Only values explicitly declared by the addon
    manifest are sent. The base request remains first so providers that do not
    require negotiation keep their ordinary behavior.
    """
    base = dict(quality_candidate.get("settings") or {})
    candidates = [{"settings": base, "negotiation": {}}]
    drm_systems = manifest.get("drmSystems")
    profiles = manifest.get("playbackProfiles")
    declared_drm_count = len(drm_systems) if isinstance(drm_systems, list) else 0
    declared_profile_count = len(profiles) if isinstance(profiles, list) else 0
    drm_systems = [
        value for item in drm_systems[:32] if isinstance(item, str)
        for value in [_renderer_text(item, 96)] if value and value == item
    ] if isinstance(drm_systems, list) else []
    profiles = [
        value for item in profiles[:32] if isinstance(item, str)
        for value in [_renderer_text(item, 128)] if value and value == item
    ] if isinstance(profiles, list) else []
    seen = {tuple(sorted(base.items()))}
    truncated = (declared_drm_count > 32 or declared_profile_count > 32
                 or len(drm_systems) != min(declared_drm_count, 32)
                 or len(profiles) != min(declared_profile_count, 32))

    def add(negotiation: dict) -> None:
        nonlocal truncated
        if any(key in base and base[key] != value for key, value in negotiation.items()):
            # Never let a protocol hint silently replace an exact quality
            # selector supplied by the addon manifest.
            truncated = True
            return
        settings = {**base, **negotiation}
        key = tuple(sorted(settings.items()))
        if key in seen:
            return
        if len(candidates) >= MAX_QUALITY_OPTIONS:
            truncated = True
            return
        seen.add(key)
        candidates.append({"settings": settings, "negotiation": dict(negotiation)})

    is_gravity = addon_id == "app.eclipsemusic.gravity"
    for drm_system in drm_systems:
        add({"drm": drm_system})
        for profile in profiles:
            if is_gravity:
                if drm_system.casefold() != "widevine":
                    continue
                if profile == "apple-fps-alac-v1" and not fairplay_available:
                    continue
                if profile == "apple-cbcs-eac3-v1" and "eac3" not in dolby_codecs:
                    continue
                if profile not in {"apple-fps-alac-v1", "apple-cbcs-eac3-v1"}:
                    continue
            negotiation = {"drm": drm_system, "playbackProfile": profile}
            # The Android client passes its locally supported Dolby codec list
            # when negotiating Atmos variants. Omniphony's existing decoder
            # routes support E-AC-3 and AC-4; advertise a codec only when the
            # addon itself declares a matching playback profile.
            codec = next((token for token in ("eac3", "ac4")
                          if re.search(rf"(?:^|[^a-z0-9]){token}(?:$|[^a-z0-9])", profile, re.I)), None)
            if codec and (not is_gravity or codec in dolby_codecs):
                negotiation["dolbyCodecs"] = codec
            add(negotiation)
    # A profile can be meaningful without an explicit DRM systems list.
    # Keep these after DRM-bound combinations to preserve the former request
    # order for manifests that declare both fields.
    for profile in profiles:
        if is_gravity:
            continue
        negotiation = {"playbackProfile": profile}
        codec = next((token for token in ("eac3", "ac4")
                      if re.search(rf"(?:^|[^a-z0-9]){token}(?:$|[^a-z0-9])", profile, re.I)), None)
        if codec:
            negotiation["dolbyCodecs"] = codec
        add(negotiation)
    return candidates, truncated


def build_stream_request_plan(
    manifest: dict,
    quality_requests: list[dict],
    *,
    limit: int,
    addon_id: str | None = None,
    fairplay_available: bool = False,
    dolby_codecs: tuple[str, ...] = ("eac3", "ac4"),
) -> tuple[list[dict], bool]:
    """Schedule every baseline quality before spending budget on negotiation."""
    if limit < 1:
        return [], bool(quality_requests)
    plan: list[dict] = []
    truncated = False
    for quality in quality_requests:
        if len(plan) >= limit:
            return plan, True
        plan.append({
            **quality,
            "settings": dict(quality.get("settings") or {}),
            "negotiation": {},
        })
    expanded = []
    for quality in quality_requests:
        variants, variants_truncated = stream_request_candidates(
            manifest, quality, addon_id=addon_id,
            fairplay_available=fairplay_available, dolby_codecs=dolby_codecs,
        )
        truncated = truncated or variants_truncated
        expanded.append((quality, [variant for variant in variants if variant.get("negotiation")]))
    max_variants = max((len(variants) for _, variants in expanded), default=0)
    for variant_index in range(max_variants):
        for quality, variants in expanded:
            if variant_index >= len(variants):
                continue
            variant = variants[variant_index]
            if len(plan) >= limit:
                return plan, True
            plan.append({
                **quality,
                "settings": dict(variant.get("settings") or {}),
                "negotiation": dict(variant.get("negotiation") or {}),
            })
    return plan, truncated


def unclassified_select_settings(manifest: dict) -> list[str]:
    """List non-video select keys whose semantics are not confidently quality."""
    settings = manifest.get("settings")
    if not isinstance(settings, list):
        return []
    classified = {item["key"] for item in _quality_settings(manifest)}
    result = []
    for setting in settings:
        if not isinstance(setting, dict) or str(setting.get("type", "")).lower() != "select":
            continue
        key = _renderer_text(setting.get("key"), 96)
        label = _renderer_text(setting.get("label"), 160) or ""
        category = str(setting.get("category") or setting.get("purpose") or "")
        if (
            key and key not in classified
            and not _VIDEO_KEY.search(key)
            and not _VIDEO_KEY.search(label)
            and not _VIDEO_KEY.search(category)
        ):
            result.append(key)
    return result


def declared_addon_capabilities(manifest: dict) -> dict:
    """Normalize only documented manifest capability fields and resource names."""
    resources = manifest.get("resources")
    resource_set = {
        safe.lower() for item in resources[:64] if isinstance(item, str)
        for safe in [_renderer_text(item, 96)] if safe
    } if isinstance(resources, list) else set()
    names = (
        "search", "stream", "browse", "resolveISRC", "resolveIdentity",
        "provideCatalogs", "notify", "settings",
    )
    bool_fields = {
        "search": "canSearch",
        "stream": "canStream",
        "browse": "canBrowse",
        "resolveISRC": "canResolveISRC",
        "resolveIdentity": "canResolveIdentity",
        "provideCatalogs": "canProvideCatalogs",
        "notify": "canNotify",
        "settings": "hasSettings",
    }
    resource_aliases = {
        "browse": {"browse", "catalog"},
        "resolveISRC": {"isrc", "resolve-isrc"},
        "resolveIdentity": {"resolve", "resolve-identity"},
        "provideCatalogs": {"catalog"},
        "notify": {"notifications"},
    }
    normalized = {}
    for name in names:
        field = bool_fields[name]
        value = manifest.get(field)
        resource_names = resource_aliases.get(name, {name.lower()})
        if isinstance(value, bool):
            normalized[name] = value
        elif isinstance(resources, list):
            inferred = bool(resource_names & resource_set)
            if name == "provideCatalogs":
                inferred = inferred and isinstance(manifest.get("catalogs"), list) and bool(manifest["catalogs"])
            normalized[name] = inferred
        else:
            normalized[name] = None
    malformed_capability_fields = []
    normalized_capabilities = {}
    for field, maximum, length in (("drmSystems", 96, 32), ("playbackProfiles", 128, 32)):
        if field not in manifest:
            normalized_capabilities[field] = []
            continue
        declared = manifest.get(field)
        if not isinstance(declared, list):
            malformed_capability_fields.append(field)
            normalized_capabilities[field] = []
            continue
        clean = []
        malformed = len(declared) > length
        for item in declared[:length]:
            safe = _renderer_text(item, maximum) if isinstance(item, str) else None
            if not safe or safe != item:
                malformed = True
                continue
            clean.append(safe)
        if malformed:
            malformed_capability_fields.append(field)
        normalized_capabilities[field] = clean
    return {
        **normalized,
        "resources": sorted(resource_set) if isinstance(resources, list) else None,
        "contentType": _renderer_text(manifest.get("contentType"), 96),
        **normalized_capabilities,
        "capabilitiesComplete": not malformed_capability_fields,
        "malformedCapabilityFields": malformed_capability_fields,
    }


def _url_is_http(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() == "https" and bool(parsed.hostname)
    except ValueError:
        return False


def _protection(raw: dict) -> dict:
    drm = raw.get("drm")
    systems: list[str] = []
    system = None
    encryption = None
    drm_scheme = drm.get("scheme") if isinstance(drm, dict) else None
    if isinstance(drm, dict):
        system = drm.get("system") or drm.get("keySystem")
        scheme = drm_scheme
        encryption = drm.get("encryptionScheme") or drm.get("method")
        if scheme and str(scheme).strip().lower() not in _KNOWN_DRM_SYSTEMS:
            encryption = encryption or scheme
        else:
            system = system or scheme
    elif isinstance(drm, str) and drm.strip():
        system = drm.strip()
    raw_systems = [
        system,
        drm.get("system") if isinstance(drm, dict) else None,
        drm.get("keySystem") if isinstance(drm, dict) else None,
        drm_scheme if isinstance(drm_scheme, str) and drm_scheme.strip().lower() in _KNOWN_DRM_SYSTEMS else None,
        raw.get("keySystem"), raw.get("drmSystem"),
    ]
    for candidate in raw_systems:
        text = _renderer_text(candidate, 160)
        if text:
            normalized = _KNOWN_DRM_SYSTEMS.get(text.lower(), text.lower())
            if normalized not in systems:
                systems.append(normalized)
    system = system or raw.get("keySystem") or raw.get("drmSystem")
    encryption_values = []
    for candidate in (
        drm.get("encryptionScheme") if isinstance(drm, dict) else None,
        drm.get("method") if isinstance(drm, dict) else None,
        drm.get("protectionScheme") if isinstance(drm, dict) else None,
        drm_scheme if isinstance(drm_scheme, str) and drm_scheme.strip().lower() not in _KNOWN_DRM_SYSTEMS else None,
        raw.get("encryptionScheme"), raw.get("encryptionMethod"), raw.get("protectionScheme"),
    ):
        text = _renderer_text(candidate, 160)
        if text and all(text.casefold() != current.casefold() for current in encryption_values):
            encryption_values.append(text)
    encryption = encryption or (encryption_values[0] if encryption_values else None)
    system_indicator_present = bool(system) or bool(raw.get("keySystem")) or bool(raw.get("drmSystem"))
    encryption_indicator_present = bool(encryption) or any(
        bool(raw.get(key)) for key in ("encryptionScheme", "encryptionMethod", "protectionScheme")
    )
    system_text = _renderer_text(system, 160)
    encryption_text = _renderer_text(encryption, 160)
    normalized_system = _KNOWN_DRM_SYSTEMS.get((system_text or "").lower(), (system_text or "").lower() or None)
    license_available = bool(
        (isinstance(drm, dict) and (drm.get("licenseUrl") or drm.get("licenseURL")))
        or raw.get("licenseUrl") or raw.get("licenseURL")
    )
    certificate_available = bool(
        (isinstance(drm, dict) and (drm.get("certificateUrl") or drm.get("certificateURL")))
        or raw.get("certificateUrl") or raw.get("certificateURL")
    )
    service_certificate_available = bool(
        (isinstance(drm, dict) and (drm.get("serviceCertificate") or drm.get("serviceCertificateUrl") or drm.get("serviceCertificateURL")))
        or raw.get("serviceCertificate") or raw.get("serviceCertificateUrl") or raw.get("serviceCertificateURL")
    )
    init_mapping_available = bool(
        raw.get("initDataMapping") or (isinstance(drm, dict) and drm.get("initDataMapping"))
    )
    native_fairplay_available = bool(
        raw.get("nativeFairPlay") or (isinstance(drm, dict) and drm.get("nativeFairPlay"))
    )
    encrypted = (
        raw.get("encrypted") is True or bool(drm) or system_indicator_present
        or encryption_indicator_present or bool(system_text) or bool(encryption_text)
        or license_available or certificate_available or service_certificate_available
        or init_mapping_available or native_fairplay_available
    )
    reported_clear = (
        raw.get("encrypted") is False and not bool(drm) and not system_indicator_present
        and not encryption_indicator_present and not system_text and not encryption_text
        and not license_available and not certificate_available and not service_certificate_available
        and not init_mapping_available and not native_fairplay_available
    )
    conflicting_systems = len(systems) > 1
    conflicting_encryption = len(encryption_values) > 1
    if conflicting_systems:
        normalized_system = None
    if conflicting_encryption:
        encryption_text = None
    return {
        "status": "protected" if encrypted else "unknown",
        "reportedStatus": "clear" if reported_clear else None,
        "evidence": "addon_response" if encrypted or reported_clear else "not_provided",
        "system": normalized_system,
        "encryptionScheme": encryption_text,
        "licenseEndpointAvailable": license_available,
        "certificateEndpointAvailable": certificate_available,
        "serviceCertificateAvailable": service_certificate_available,
        "multiSession": drm.get("multiSession") if isinstance(drm, dict) and isinstance(drm.get("multiSession"), bool) else None,
        "initDataMappingAvailable": init_mapping_available,
        "nativeFairPlayAvailable": native_fairplay_available,
        "conflictingProtectionMetadata": conflicting_systems or conflicting_encryption,
        "conflictingProtectionFields": (["system"] if conflicting_systems else [])
        + (["encryptionScheme"] if conflicting_encryption else []),
        "systems": systems or ([normalized_system] if normalized_system else []),
    }


def _merge_protection(parent: dict | None, child: dict) -> dict:
    """Combine inherited and per-variant protection without permitting downgrades."""
    parent_info = _protection(parent) if isinstance(parent, dict) else None
    child_info = _protection(child)
    if parent_info is None:
        return child_info
    merged = dict(child_info)
    if parent_info["status"] == "protected":
        merged["status"] = "protected"
        merged["reportedStatus"] = None
        merged["evidence"] = "inherited_and_addon_response"
    elif child_info["status"] != "protected" and not child_info["reportedStatus"] and parent_info["reportedStatus"]:
        merged["reportedStatus"] = parent_info["reportedStatus"]
    for key in (
        "system", "encryptionScheme", "multiSession",
    ):
        if merged.get(key) is None:
            merged[key] = parent_info.get(key)
    for key in ("licenseEndpointAvailable", "certificateEndpointAvailable", "serviceCertificateAvailable", "initDataMappingAvailable", "nativeFairPlayAvailable"):
        merged[key] = bool(merged.get(key) or parent_info.get(key))
    parent_system = parent_info.get("system")
    child_system = child_info.get("system")
    parent_scheme = parent_info.get("encryptionScheme")
    child_scheme = child_info.get("encryptionScheme")
    conflicts = list(dict.fromkeys(
        value for info in (parent_info, child_info)
        for value in (info.get("conflictingProtectionFields") or [])
        if isinstance(value, str)
    ))
    if parent_system and child_system and parent_system.casefold() != child_system.casefold():
        conflicts.append("system")
    if parent_scheme and child_scheme and parent_scheme.casefold() != child_scheme.casefold():
        conflicts.append("encryptionScheme")
    systems = list(dict.fromkeys(
        value for value in [*(parent_info.get("systems") or []), *(child_info.get("systems") or [])]
        if isinstance(value, str) and value
    ))
    merged["systems"] = systems
    if len(systems) > 1 and "system" not in conflicts:
        conflicts.append("system")
    if len(systems) > 1:
        merged["system"] = None
    if conflicts:
        if "system" in conflicts:
            merged["system"] = None
        if "encryptionScheme" in conflicts:
            merged["encryptionScheme"] = None
        merged["conflictingProtectionMetadata"] = True
        merged["conflictingProtectionFields"] = list(dict.fromkeys(conflicts))
        merged["status"] = "protected"
        merged["reportedStatus"] = None
    else:
        merged["conflictingProtectionMetadata"] = False
        merged["conflictingProtectionFields"] = []
    return merged


def stream_has_protection_indicators(raw: dict, parent: dict | None = None) -> bool:
    """True when addon metadata declares protection; false does not prove clear media."""
    if not isinstance(raw, dict):
        return True
    protection = _merge_protection(parent, raw)
    return protection["status"] == "protected" or protection.get("conflictingProtectionMetadata") is True


def _normalize_stream(raw: dict, *, addon_id: str, addon_name: str, track_id: str,
                     requested_quality: str | None, request_settings: dict,
                     quality_selector_values: list[str] | None = None,
                     quality_selector_labels: dict[str, str | None] | None = None,
                     source: str = "stream", parent: dict | None = None) -> tuple[dict, dict] | None:
    """Build safe metadata and its service-private playback descriptor."""
    if not isinstance(raw, dict):
        return None
    combined = {**(parent if isinstance(parent, dict) else {}), **raw}
    media_url = combined.get("url") or combined.get("streamURL") or combined.get("streamUrl")
    extraction = combined.get("extract") if isinstance(combined.get("extract"), dict) else None
    direct_url_valid = _url_is_http(media_url)
    if not direct_url_valid and extraction is None:
        return None
    codec = _renderer_text(combined.get("codec"), 96)
    fmt = _renderer_text(combined.get("format"), 96)
    container = _renderer_text(combined.get("container"), 96)
    manifest_type = _renderer_text(combined.get("manifest"), 32)
    if not manifest_type and isinstance(media_url, str):
        manifest_type = "hls" if ".m3u8" in media_url.lower() else "dash" if ".mpd" in media_url.lower() else None
    actual_quality_value = combined.get("quality")
    if actual_quality_value is None:
        actual_quality_value = combined.get("audioQuality")
    raw_actual_quality = _clean_text(actual_quality_value, 160)
    actual_quality = (
        quality_selector_labels.get(raw_actual_quality)
        if raw_actual_quality is not None and isinstance(quality_selector_labels, dict)
        and raw_actual_quality in quality_selector_labels
        else _renderer_text(raw_actual_quality, 160)
    )
    protection = _merge_protection(parent, raw)
    expiry = _positive_number(combined.get("expiresAt") or combined.get("expires_at"))
    if expiry is not None and expiry > 10_000_000_000:
        expiry /= 1000.0
    expired = expiry is not None and expiry <= time.time()
    if extraction is not None and not direct_url_valid:
        engine = "provider_extraction_descriptor_required"
    elif protection["status"] == "protected":
        engine = "authorized_protected_playback_required"
    else:
        engine = "protection_unverified"
    stream_ref = secrets.token_urlsafe(24)
    response_track_id = combined.get("trackId") or combined.get("track_id") or combined.get("itemId")
    response_track_id = _renderer_text(response_track_id, 256)
    safe_request_settings = _renderer_json_value(request_settings) or {}
    safe_requested_quality = _renderer_json_value(requested_quality)
    identity_status = (
        "unknown" if response_track_id is None
        else "verified" if response_track_id == str(track_id)
        else "mismatch"
    )
    if identity_status == "mismatch":
        engine = "track_identity_mismatch"
    if not quality_selector_values:
        quality_match_status = "unreported" if raw_actual_quality is None else "ambiguous"
    elif len(quality_selector_values) != 1:
        quality_match_status = "ambiguous" if raw_actual_quality is not None else "unreported"
    elif raw_actual_quality is None:
        quality_match_status = "unreported"
    else:
        if raw_actual_quality == quality_selector_values[0]:
            quality_match_status = "matched"
        elif (isinstance(quality_selector_labels, dict)
              and quality_selector_labels.get(raw_actual_quality) is not None):
            quality_match_status = "mismatched"
        else:
            quality_match_status = "ambiguous"
    descriptor = {
        "streamRef": stream_ref,
        "addonId": _renderer_text(addon_id, 256),
        "addonName": _renderer_text(addon_name, 256),
        "trackId": _renderer_text(track_id, 256),
        "reportedTrackId": response_track_id,
        "trackIdentityStatus": identity_status,
        "availability": "returned",
        "requestedQuality": safe_requested_quality,
        "actualQuality": actual_quality,
        "qualityMatch": True if quality_match_status == "matched" else False if quality_match_status == "mismatched" else None,
        "qualityMatchStatus": quality_match_status,
        "codec": codec,
        "format": fmt,
        "container": container,
        "manifestType": manifest_type,
        "bitrate": _bounded_integer(combined.get("bitrate"), minimum=1, maximum=100_000_000),
        "bitrateBps": _bounded_integer(combined.get("bitrate"), minimum=1, maximum=100_000_000),
        "sampleRate": _bounded_integer(combined.get("sampleRate"), minimum=8_000, maximum=768_000),
        "bitDepth": _bounded_integer(combined.get("bitDepth"), minimum=1, maximum=64),
        "channels": _bounded_integer(combined.get("channels"), minimum=1, maximum=64),
        "spatialFormat": _renderer_text(combined.get("spatialFormat") or combined.get("audioMode"), 96),
        "contentType": _renderer_text(combined.get("contentType"), 64),
        "hasVideo": isinstance(combined.get("video"), dict),
        "chapterCount": len(combined.get("chapters")) if isinstance(combined.get("chapters"), list) else None,
        "providerAttemptCount": len(combined.get("attempts")) if isinstance(combined.get("attempts"), list) else None,
        "protection": protection,
        "expiresAt": expiry,
        "expired": expired,
        "playbackCompatibility": "expired" if expired else engine,
        "source": source,
        "requestSettings": safe_request_settings,
        "providerIdentity": _renderer_text(
            combined.get("providerId") or combined.get("provider") or combined.get("sourceId"), 256
        ),
        "playbackProfile": _renderer_text(
            combined.get("playbackProfile") or combined.get("profile"), 128
        ),
        "authorizedHeadersAvailable": any(
            isinstance(combined.get(key), dict) and bool(combined.get(key))
            for key in ("headers", "httpHeaders", "requestHeaders")
        ),
        "urlAvailable": direct_url_valid,
        "extractAvailable": extraction is not None,
    }
    return descriptor, {
        "url": media_url,
        "descriptor": combined,
        "effectiveProtection": protection,
    }


def normalize_stream(raw: dict, *, addon_id: str, addon_name: str, track_id: str,
                    requested_quality: str | None, request_settings: dict,
                    quality_selector_values: list[str] | None = None,
                    quality_selector_labels: dict[str, str] | None = None,
                    source: str = "stream", parent: dict | None = None) -> dict | None:
    """Return renderer-safe metadata; raw URLs and descriptors stay private."""
    normalized = _normalize_stream(
        raw, addon_id=addon_id, addon_name=addon_name, track_id=track_id,
        requested_quality=requested_quality, request_settings=request_settings,
        quality_selector_values=quality_selector_values,
        quality_selector_labels=quality_selector_labels,
        source=source, parent=parent,
    )
    return normalized[0] if normalized else None


def flatten_stream_payload(payload: Any) -> list[dict]:
    """Flatten a conforming stream response while retaining inherited metadata."""
    if isinstance(payload, list):
        return [dict(item) for item in payload[:MAX_STREAM_VARIANTS] if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    streams = payload.get("streams")
    if isinstance(streams, list):
        return [dict(item, _parent=payload) for item in streams[:MAX_STREAM_VARIANTS] if isinstance(item, dict)]
    return [payload]


def stream_payload_truncated(payload: Any) -> bool:
    """Report when a valid stream array was bounded before normalization."""
    if isinstance(payload, list):
        return len(payload) > MAX_STREAM_VARIANTS
    if isinstance(payload, dict):
        streams = payload.get("streams")
        return isinstance(streams, list) and len(streams) > MAX_STREAM_VARIANTS
    return False


def stream_payload_has_malformed_items(payload: Any) -> bool:
    if isinstance(payload, list):
        return any(not isinstance(item, dict) for item in payload)
    if isinstance(payload, dict) and isinstance(payload.get("streams"), list):
        return any(not isinstance(item, dict) for item in payload["streams"][:MAX_STREAM_VARIANTS])
    return False
