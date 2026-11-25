# Eclipse stream inventory

The service exposes a native-only, authenticated inventory route:

```text
GET /eclipse/streams?providerKey=eclipse:<20-hex-key>&operationId=<opaque-id>&trackId=<addon-track-id>
```

Use either a provider-local `trackId`, or `title`, `artist`, and optionally
`isrc` to identify the recording. Requests must include the current service
launch bearer token and `X-Omniphony-Service-Instance` headers. The provider key
comes from `/realmax/providers`; the tokenized manifest URL is never accepted
from the renderer.

The route reads the account-connected provider's manifest and enumerates
`select` settings with the recognized audio-quality keys `quality`,
`audioQuality`, and `streamQuality` (including underscore and hyphen spellings).
It requests the exact option values declared by the addon. When an addon
declares DRM systems or playback profiles, the resolver also tries bounded
negotiation variants derived only from those declared values. Other selectable
settings are reported as unclassified and are not overwritten with manifest
defaults. The account connections response does not provide the user's current
per-addon setting values; manifest query configuration is retained.
Exact manifest quality selector values are used only by the native request
planner. The renderer receives the manifest's display label (or a generic
option ordinal), not the selector token.

Returned descriptors contain addon and track identity, requested and returned
quality labels, codec/container/manifest, reported audio parameters, expiration,
protection state, sanitized HLS/DASH audio representation summaries, and a
short-lived opaque `streamRef`. Raw media URLs, license
URLs, provider headers, and the original addon response stay in the native
service's short-lived in-memory store. The inventory route does not serve that
private record to the renderer and does not authorize playback by itself.
Manifest selector tokens and media rendition URLs remain native-only. The
renderer gets provider display labels and rendition facts, with aggregate HLS
bandwidth explicitly distinguished from audio bitrate.

Playback admission now uses the same provider-neutral protection classifier for
Gravity, Tido, and other Eclipse playback paths. It rejects protected response
metadata and checks HLS/DASH protection declarations before using those
manifests. Manifest inspection is bounded, uses public HTTPS destinations with
TLS hostname checks, and follows only same-origin redirects for each request.
An unreadable, malformed, encrypted, or over-limit manifest remains ineligible
for this MPV/OxideAV route. This does not establish that arbitrary direct media
files are DRM-free; an add-on's `encrypted: false` remains only a provider
claim in the inventory.

The legacy playback resolvers now bind search/stream cache entries and queued
network work to the current Eclipse account generation. Stale responses are
discarded before caching or playback admission. Quality values that are
ambiguous or malformed are marked incomplete instead of silently omitted, and
manifest capability parsing recognizes the official `isrc` resource name.
Discovery snapshots the Eclipse account generation, rejects manifest/add-on
identity mismatches, caps concurrent discoveries at two, quality/setting
combinations at 4096, protocol request plans at 4096, returned descriptors per
inventory at 2048, and the native descriptor cache at 4096 entries. Reaching a
bound marks the result incomplete/truncated. Repeated 401/403/429 responses
stop quality probing; 429 starts a per-addon cooldown using `Retry-After` when
available. HTTP 503 responses receive up to two bounded retries, respecting
`Retry-After` up to 30 seconds and the overall discovery deadline. HTTPS
requests resolve and pin only globally routable destination addresses,
preserve TLS hostname verification, disable system proxy tunneling, and reject
cross-origin redirects.
For adaptive media, the shared bounded inspector traverses at most 16 HLS
playlist references, returns at most 256 HLS/DASH representation summaries per
manifest, and inspects at most 64 unique manifests per inventory. Exceeding a
bound or failing to inspect a declared manifest leaves `mediaVariantsComplete`
false. HLS rendition bandwidth is never presented as audio bitrate unless the
playlist identifies it as an audio rendition; DASH audio representation
bandwidth is scoped to that representation. Unclassified DASH representations
are reported as such and make adaptive coverage incomplete. Extensionless
direct URLs are sniffed with a bounded range request before adaptive coverage is
claimed. Provider headers are sent to an HLS child playlist only when it shares
the original manifest's origin; manifest inspection cache entries include a
private digest of URL, manifest type, and request headers. Parent protection
metadata is inherited by each adaptive rendition. Quality selector values are
matched against returned values only inside the service. `qualityMatchStatus`
distinguishes `matched`, `mismatched` (only when the returned value is another
known selector token), `ambiguous`, and `unreported`; display-label collisions
are marked ambiguous. Malformed or truncated DRM/profile arrays set
`addonCapabilitiesComplete` false. Gravity negotiation follows the observed
APK gates: the CBCS E-AC-3 profile requires declared `widevine`, the exact
declared Gravity profile, and local E-AC-3 support. The FairPlay ALAC profile
is platform-blocked when native FairPlay is unavailable. Opaque stream
references get their local lifetime when the inventory response completes,
capped by provider expiry. Finalization verifies every returned reference is
still present and account-bound; an evicted reference is marked
`reference_unavailable` and makes the inventory incomplete. Provider expiry is
re-evaluated at finalization, and `referenceExpiresAt` reports the local handle
deadline separately from provider `expiresAt`.

## Completeness fields

- `qualityRequestsComplete` means each generated declared-quality request
  returned valid JSON. It does not prove a stream exists for every option.
- For addons with quality selectors, inventory also requests one default
  response without overriding the add-on's existing manifest URL settings.
  That response is labeled as default/unselected; it is not counted as a
  named selector match. Playback resolution continues using its configured
  quality order and does not add a default-quality fallback.
- `qualityEnumerationTruncated` reports when the bounded option-product limit
  was reached.
- `protocolRequestTruncated` reports when negotiation variants or the bounded
  request plan prevented some combinations from being tried.
- `streamResultsTruncated` reports when returned variants exceeded the bounded
  response limit; `malformedStreamCandidates` reports non-descriptor entries.
- Identical native stream responses are deduplicated by a service-only digest;
  `requestedQualities` retains each quality/configuration that returned that
  same stream.
- `mediaVariantsComplete` covers the bounded, recognized audio representation
  metadata parsed from returned HLS/DASH manifests. It does not prove the
  upstream client exposes the same variant list; `providerParityVerified`
  remains independently false until compared with Eclipse.
- `paginationComplete` is currently unknown for track search.
- `providerParityVerified` remains `false` until account-authorized live
  comparisons with the official client are completed.
- Top-level `complete` is scoped to declared options and supported request
  variants. `providerCoverageStatus` remains `unknown`; this does not claim all
  account-hidden tiers, pagination, or official-client parity were exhausted.
- Manifest acquisition failures return sanitized `reason` categories such as
  `manifest_http_403`, `manifest_timeout`, `manifest_tls_error`, or
  `manifest_invalid_json`. These categories omit the account manifest URL,
  authorization values, and provider response bodies. Older installed bundles
  still report generic `manifest_unavailable`.
- `addonCapabilitiesComplete` and `malformedAddonCapabilityFields` report
  invalid/truncated DRM or playback-profile declarations.
- `nativeReferencesComplete` confirms every returned stream handle was present
  and account-bound when the inventory response was finalized.
- `platformBlockedProfiles` names declared profiles that cannot be negotiated
  here because the required native platform capability is unavailable.
- An addon `encrypted: false` claim is preserved as `reportedStatus: "clear"`,
  while the effective protection status remains `unknown` until media
  inspection verifies it. Protected streams include a normalized DRM system
  when supplied but are never handed to the MPV/Omniphony playback route.

## Current verification matrix

| Source / path | Current evidence | Status |
| --- | --- | --- |
| TIDAL API AC-4 IMS | Live playback observed on Imagine Dragons — “Natural” (`227401768`) through the mobile OAuth profile; OxideAV produced a verified 48 kHz, two-channel program. Earlier live seek verification remains recorded in the APK analysis note. | Verified playback; full regression cycle still pending |
| TIDAL API E-AC-3 JOC | Live on “Natural” (`227401768`) and “Boomerang” (`227401769`), default priority restored after a JOC-first probe. MPD/EC-3 source was six-channel; MPV reported the Omniphony two-channel binaural program, `orender` active in-process, one registered Studio instance, SOFA container valid, Studio↔MPV link ready, and no standalone `orender.exe`. A 112-second seek, pause hold, and resume clock advance were verified against both the TIDAL footer and service clock; no failure was reported. | Live playback and transport verified; broader lifecycle matrix pending |
| TIDAL API FLAC tiers | Existing resolver and fixture coverage; no complete live tier enumeration in this work | Supported, live verification pending |
| Eclipse account connection | Operator completed Discord sign-in. The service reports a connected session and two enabled account add-ons: Gravity and NetEase Cloud Music; the TIDAL panel agrees. No personal account identifiers are recorded. | Authenticated session and add-on discovery verified |
| Gravity manifest qualities | For Imagine Dragons — “Natural”, the manifest exposed six audio labels: Dolby Atmos, Hi-Res Lossless up to 192 kHz, Hi-Res Lossless up to 96 kHz, Lossless up to 48 kHz, AAC High Quality, and HE-AAC Data Saver. It also exposed four cover-art sizes. | Manifest options observed; audio availability is not established by declarations |
| Gravity stream resolution | The last bounded account-authenticated probe returned 12 descriptors from 28 requests; request 27 received HTTP 429, the default/unselected quality was not attempted, and descriptor track identity was not established. No new probe was run after the rate-limited result. | Partial inventory only; quality coverage, track identity, expiry/renewal, and official-client parity remain unverified |
| Gravity E-AC-3 JOC / Atmos | APK confirms Gravity's protected CBCS E-AC-3 profile and its Widevine/profile/local-codec conditions. | Static support evidence only; fresh descriptor not observed; protected playback remains unavailable in this route |
| Gravity ALAC | APK gates the FairPlay ALAC profile on native FairPlay availability. | Windows FairPlay capability unavailable; do not claim playable |
| NetEase Cloud Music stream qualities | On 2026-11-25, a single-provider inventory for Imagine Dragons — “Natural” on the installed v2.1.82 bundle reached `manifest_unavailable`, with 0 quality attempts and 0 descriptors. Candidate v2.1.83 adds sanitized manifest-fetch categories and passed offline validation; the connected TIDAL session still loaded its cached v2.1.82 module, so no live diagnostic result is available yet. | No stream result; fetch cause and provider qualities remain unknown |
| Disabled Eclipse addons | Not enumerated or enabled automatically | Connected-disabled; untested |
| FairPlay / Widevine / PlayReady | Protection metadata retained; authorized platform playback is not implemented by this route | Descriptor-only; protected playback unavailable here |

The Omniphony playback-details panel now provides a connected-addon inventory
form for a provider-local track ID or title/artist (with optional ISRC). It
lists every enabled connection on the signed-in Eclipse account. The native
service accepts an enabled account connection even when it is not selected in
the RealMAX playback order; a disabled or disconnected addon remains rejected.
When several addons are enabled, use title/artist or ISRC so each provider can
resolve its own track identity. The request is made by the TidaLuna native
supervisor with its per-launch service credential; the renderer receives only
the sanitized inventory summary. Signed URLs, provider headers, and opaque
stream references remain in native memory. The inventory layer preserves
addon-returned formats without mapping them to a RealMAX playback quality. The
existing player continues to admit only its explicitly supported playback
candidates and retains its established TIDAL AC-4 IMS and E-AC-3 JOC routes.
The inventory view has an add-on selector, so provider-local IDs can be used
safely with multiple account connections. Refresh failures clear provider
eligibility until a successful refresh. A session-generation check discards
pending results after account changes, disconnects, panel closure, or plugin
unload. Cancellation is sent through a second native-authenticated request and
checked between addon requests; an already-running upstream HTTP request is
bounded by its existing per-request timeout and may take up to that timeout to
return. Inventory output reports identity, declared-request completion,
blocked profiles, media inspection completeness, expiry, and truncation.
HLS rendition bandwidth is labeled as bandwidth, not audio bitrate.

The native supervisor now identifies a service by both API version and the
SHA-256 digest of the verified runtime archive. It passes that digest to the
service at launch, and `/health` reports it without exposing credentials. A
same-version process with a missing or different digest is treated as stale;
the supervisor replaces it only when the process is its authenticated,
same-TIDAL-owned child. The bundle completeness check also requires
`eclipse_streams.py`, preventing an older runtime from being treated as a
complete Eclipse build.

### Earlier pre-authentication snapshot (superseded)

At the initial v2.1.82 verification snapshot, TIDAL displayed the Eclipse
account as connected but its most recent Discord sign-in attempt was canceled.
The service then reported API version 2.1.45 and runtime digest
`198EEB068B63C6C365B219101B07F5C4CB11E07C3BA118F2D451B3377E51FC98`. The
connected add-on list contained NetEase Cloud Music and Gravity. The resolver
at that point combined artwork settings with audio-quality settings, produced
100 attempts, received HTTP 415 for the first 25 and HTTP 429 afterward, and
returned no descriptors. NetEase returned `manifest_unavailable` before any
quality request. Those responses did not establish that provider qualities
were absent.

At that snapshot the v2.1.82 runtime had been built but was not yet loaded; the
exact TIDAL module version was unknown and no GitHub release had been
published. The later authenticated continuation below supersedes those
runtime and login observations.

Verification recorded at that snapshot: `api/test_eclipse_gravity.py` **90
passed** including the artwork-selector regression; the complete Python suite
**237 passed, 1 skipped**; `node --test tests/*.test.mjs` **13 passed**; the
runtime archive verifier passed with **269 members** and matching build and
pinned SHA-256; the Luna store manifest verifier passed; the isolated loopback
shutdown-authentication test passed. These checks do not establish live stream
parity. Gravity's earlier HTTP 415 remains unexplained. The network-free artwork-selector regression was re-run against the current
v2.1.82 source and passed. The bounded NetEase inventory above stopped at the
manifest stage; no new Gravity stream inventory was run. After the NetEase
probe, TIDAL footer and service status still agreed on “Bullet In A Gun”
(`227401775`) playing as AC-4 IMS at 48 kHz/two decoded channels, with no
reported failure.

### 2026-11-25 authenticated continuation

After the operator completed Eclipse Discord sign-in, the live native service
reported the Eclipse account as connected (session generation 1) with the
Gravity and NetEase Cloud Music account add-ons enabled. The sanitized addon
status endpoint returned both connections as available; no account identifiers
or credentials are recorded here.

At this authenticated snapshot, TIDAL had loaded plugin v2.1.82 and service
2.1.45 from its cached GitHub Store module. The workspace candidate is v2.1.83
with service 2.1.46. Its 269-member runtime archive was built and verified
with SHA-256
`036DE57636D6199EF200F5938D10699E31BC00C19141719CB7057F9AE7271F15` and
staged under `runtime-bundles/2.1.83`; existing plugin files were backed up
before staging. TIDAL still launched the cached v2.1.82 service after restart,
so the candidate's manifest-failure categories have not been exercised live.
The Discord-authenticated Eclipse session remains connected. The store refresh
must load the published v2.1.83 package before a bounded NetEase diagnostic
can identify the manifest-fetch failure.

At the first live snapshot TIDAL was playing “Machine” as AC-4 IMS; while the
read-only check continued, it advanced normally to “Bad Liar” (`227401772`).
The TIDAL footer and service status agreed on “Bad Liar”, which remained
playing at about 170/261 seconds with no reported failure. The decoder
reported 48 kHz, two channels; the program was stereo before Windows endpoint
formatting, while the WASAPI device reported 96 kHz/eight channels. The Studio
process had a child `orender.exe` renderer at this snapshot. It was not stopped
while playback was active; the no-standalone-renderer/ASIO condition still
needs a paused-state check. The configured source order was TIDAL API, NetEase
Cloud Music, then Gravity. This snapshot does not add a new Gravity or NetEase
stream-resolution result and does not change the existing 429/unknown-track
limitations above.

On the Oracle ARM64 VM, Waydroid remained running with official Eclipse Android
APK v0.6.75 installed (versionCode 155, target SDK 36). The Fire TV was not
accessed.
