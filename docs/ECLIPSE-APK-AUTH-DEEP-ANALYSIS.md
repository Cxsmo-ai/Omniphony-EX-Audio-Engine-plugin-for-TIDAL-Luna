# Eclipse Discord sign-in: Android APK evidence

## Scope

This note records static inspection of Eclipse Android APK version 0.6.75
(`com.eclipse.android`, version code 155). The auth findings come from its
Android manifest, smali, and JADX cross-reference. No account data, browser
storage, Windows Eclipse login state, or live access token was inspected.

## APK auth contract

- The manifest registers `debridmusic://auth/callback` on `MainActivity` with
  `singleTask` launch behavior. The activity also handles `onNewIntent`, so the
  callback is accepted by an existing app process.
- The callback parser requires nonempty `token`, `userId`, and `email`; it
  rejects an `error` parameter. `name` and `image` are optional.
- The APK opens Discord sign-in at
  `https://api.eclipsemusic.app/api/auth/mobile/discord`.
- Its Retrofit `AuthApi` declares `GET
  https://api.eclipsemusic.app/api/auth/mobile/verify`. A successful response
  contains `success`, `token`, `user`, and `error`; the client saves a returned
  replacement token. The inspected APK contains no separate refresh-token
  endpoint or refresh-token field.
- The Android client stores the token and user ID in AndroidX encrypted
  preferences backed by Android Keystore. That storage mechanism is Android
  specific; the Windows plugin instead uses current-user DPAPI for the verified
  token.

These are observed client behaviors from this APK, not a server-side guarantee
that any arbitrary desktop client is registered or supported. The APK itself
does not define a TIDAL-specific client, redirect URI, PKCE/state exchange, or
desktop callback listener.

## Plugin implementation boundary

The plugin mirrors the APK's Discord start URL, callback scheme and required
fields, and token verification endpoint. It accepts the callback only in a
user-started, isolated sign-in window, verifies the token and returned identity
before saving it, and keeps the token out of renderer messages and logs. Local
service requests are authenticated with the service's per-launch credential.

Receiving `debridmusic://auth/callback` in the desktop sign-in window is an
adapter around the APK contract; it is not behavior established by the APK.
The Windows/TIDAL callback and account session have since been verified in the
running plugin. This demonstrates current compatibility with the inspected
first-party flow, not official registration or support for TIDAL Luna as an
Eclipse client.

## Runtime verification — 2026-11-24

The installed plugin is version 2.1.76 at commit `ca06506`. Its package
metadata declares service version 2.1.45; the version difference is intentional
in the bundle metadata. The installed service source matches the repository
source after normalizing Windows line endings, and the packaged runtime archive
matches its recorded SHA-256.

The live Eclipse session reports connected and returns the account's enabled
add-ons. Gravity is enabled and selected after the TIDAL API provider; three
NetEase add-ons are disabled. Account connectivity and add-on discovery are
verified. The account's supporter entitlement and access to a supporter-only
resource were not independently verified. No account identifiers, tokens, or
tokenized manifest URLs are recorded here.

Live TIDAL playback verified both native spatial routes:

| Track | Provider/auth profile | Format and verified behavior |
| --- | --- | --- |
| Imagine Dragons — “Natural” (`227401768`) | TIDAL API / `mobile_oauth` | AC-4 IMS, 48 kHz stereo; playback, seek, and pause succeeded. |
| Imagine Dragons — “Boomerang” (`227401769`) | TIDAL API / `python_tidal_tv` | E-AC-3 JOC over the DASH proxy; `orender` active and playback, seek, and pause succeeded. |

The current paused E-AC-3 JOC status reports the Omniphony route ready, with
Studio, MPV, embedded renderer, renderer connection, headphone binaural mode,
SOFA validation, and active decoder checks passing. The standalone
`orender.exe` is absent. The output profile reports a two-channel stereo
program; the device API may expose eight output channels while the headphone
renderer is active.

The current RealMAX settings are source mode `both`, quality order
`AC4_IMS`, `EAC3_JOC`, `HI_RES_LOSSLESS`, `LOSSLESS`, and provider order TIDAL
API followed by Gravity. The provider/quality regression test enumerates the
format and provider permutations using controlled candidate capabilities. It
verifies ordering and fall-through, but it does not prove that every real
provider has a playable stream for every format.

Gravity's observed manifest advertised Apple E-AC-3 with CBCS protection and
Apple ALAC with FairPlay protection. The resolver accepts an explicitly
identified E-AC-3 JOC stream when it is clear, rejects protected media, and
skips a protected candidate if the add-on also returns a clear candidate. The
Gravity resolver tests cover those cases with fixtures. A separate HTTP 415
test fixture verifies the clear failure path; it is not a live Gravity
playback result. No AAC-binaural profile was observed. This is not evidence
that AAC binaural is absent from every Gravity catalog item. No compatible,
unprotected Gravity stream has been verified live, so Gravity playback through
Omniphony EX remains unestablished.

Verification performed against this package:

- `api/test_realmax_priority.py`: 5 tests passed, covering 96 controlled
  provider/quality resolution cases.
- `python -m unittest discover -s api -p 'test*.py'`: 147 tests passed,
  1 skipped.
- `tests/eclipse-auth.native.test.mjs` and
  `tests/service-supervisor.test.mjs`: passed.

TIDAL token expiry/refresh was not forced live during this verification. The
`test_mobile_ac4_profile_refreshes_with_mobile_client_before_stream_use` API
fixture test exercises mobile OAuth refresh and persistence of a rotated
refresh token. The original APK's signing certificate and SHA-256 were not
recorded, so its
package identity and inspected contents are documented, but full artifact
provenance remains unverified. Public Eclipse documentation also does not
establish official third-party client authorization; the observed endpoints
remain first-party implementation details that may change.

## Static-analysis references

## Stream protocol findings — official APK 0.6.75

The official download URL `https://updates.eclipsemusic.app/android/eclipse0.6.75.apk`
was fetched again over HTTPS on 2026-11-25. The fetched artifact is
211,860,329 bytes and SHA-256
`1406AA117204746D88779FAD1B993CEFA3EC800B29D20AA89DC237FCBD1E188A`, matching
the previously inspected local APK byte for byte. APKTool 3.0.3 metadata
identifies package `com.eclipse.android`, version `0.6.75` / code `155`,
minimum SDK 26 and target SDK 36. The APK contains two DEX files and native
libraries for ARM64 and ARMv7, including `libeclipseac4`, `libeclipsealac`,
`libeclipseaudio` and `libeclipsefairplay`.

The JADX output and APKTool Smali output were cross-checked for the addon model
and DRM data structures. `EclipseAddonManifest` models `resources`, content
type, DRM systems, playback profiles, settings and catalogs, plus explicit
capability booleans for search, stream, browse, ISRC/identity resolution,
catalogs and notices. `EclipseAddonStreamResponse` models direct URL, provider
attempts, extraction descriptors, video, format, codec, bit depth, sample rate,
manifest type, quality, both `expiresAt` and `expires_at`, chapters and DRM
metadata. `AddonDrmInfo` includes system/key system, license and certificate
endpoints, service certificate, multi-session, initialization-data mapping and
native FairPlay configuration. The service inventory now preserves this
information in a native-only descriptor while returning an allowlisted summary.

The decompiled FairPlay bridge declares an Android native FairPlay client,
checks local availability, creates a per-session challenge and enforces
initialization-size and session-limit checks. This confirms that the APK uses a
platform-specific protected playback interface; it does not provide evidence
that its FairPlay session can be reproduced by the Windows MPV route. No DRM
keys or protected payloads were extracted or decoded.

### Native library metadata — Rizin 0.9.1

The ARM64 libraries extracted from this hash-verified APK were inspected with
Rizin 0.9.1 for ELF headers, dynamic symbols, imports, dependencies, and
readable strings. This was metadata and interface analysis, not an attempt to
recover DRM secrets or reproduce protected playback.

- `libeclipsefairplay.so` is an ELF64 AArch64 library and exports JNI methods
  for `nativeInit`, `nativeCreate`, `nativeChallenge`, `nativeInstall`,
  `nativeDecrypt`, and `nativeClose`. The Java declarations and native exports
  agree on a platform FairPlay session lifecycle. This is evidence of an
  official Android-specific decrypt operation inside its authorized session;
  it is not an entitlement or license interface available to this Windows
  plugin.
- `libeclipseac4.so` exports `ea_ac4_abi`, `ea_ac4_probe`, `ea_ac4_create`,
  `ea_ac4_decode`, `ea_ac4_destroy`, and `ea_ac4_reset`, plus JNI wrappers.
  Its strings include A-JOC parsing and validation labels. This supports the
  APK's own AC-4 decoder path but says nothing about which addon streams an
  account may access.
- `libeclipseaudio.so` exports JNI audio processing and analysis entry points.
  It is an audio/DSP interface, not evidence that protected stream descriptors
  can be transferred into MPV or the Omniphony route.

Rizin's ELF dependency tables list `libdl.so`, `libm.so`, and `libc.so` for
`libeclipsefairplay.so` and `libeclipseac4.so`; `libeclipseaudio.so` also lists
`liblog.so`. The FairPlay library imports `dlopen`. These are Android dynamic
dependencies and do not establish a portable desktop playback contract.

Rizin's dependency and export views do not expose a documented public Windows
playback contract. No decryption routines were patched, invoked against
protected media, or reconstructed outside the official client.

### Addon stream request contract observed in the APK

The JADX/Smali cross-reference also confirms the official add-on resource paths
and negotiation query fields:

- Search: `GET <baseURL>search?q=<encoded query>`.
- ISRC resolution: `GET <baseURL>resolve-isrc?isrc=<encoded ISRC>` when the
  manifest exposes the `isrc` resource.
- Stream resolution: `GET <baseURL>stream/<encoded track id>`.
- The current official addon guide recommends resolving a known ISRC first
  when the addon exposes that resource; otherwise Eclipse searches and checks
  the candidate recording identity. The local inventory follows that order,
  then falls back to search and the manifest-declared identity resolver.
- `gv2.m6070c(InstalledAddon, url)` appends the add-on's saved manifest settings
  as query parameters, encoding spaces as `%20` and replacing duplicate keys.
- The official Gravity resolver can append `drm`, `dolbyCodecs`, and
  `playbackProfile`. In the observed Android code, the E-AC-3 Gravity profile is
  requested only when the manifest advertises `apple-cbcs-eac3-v1` and the
  device's Dolby codec list contains `eac3`. The Apple FairPlay profile is
  gated by the platform's native FairPlay availability. These are negotiation
  inputs; they do not make a protected response playable on Windows.
- `gv2.m6071d` mints a per-addon token through the Eclipse account API and sends
  it as `X-Eclipse-Access` to the add-on. The account bearer token is used only
  for the token-mint request.

The inventory implementation retains the ordinary quality request and only
uses manifest-declared negotiation values. For Gravity, the observed APK gate
is applied: `apple-cbcs-eac3-v1` requires the Gravity identity, declared
`widevine`, the exact declared profile, and local E-AC-3 support; the
`apple-fps-alac-v1` request also requires native FairPlay availability. The
Windows runtime does not advertise that FairPlay capability, so it reports
this profile as platform-blocked instead of trying the request. Protected
responses remain inventory descriptors and are not handed to
MPV/OxideAV/Studio. Generic add-ons retain manifest-driven negotiation.

APKTool 3.0.3 Smali and JADX 1.5.7 outputs for both DEX files are present
locally. Rizin 0.9.1 was used for the native ELF metadata/symbol pass above.
Android SDK APK Analyzer, ADB, Ghidra, and MobSF are not installed in this
environment; no isolated emulator, profiling, or Logcat session was available.
The APK was retrieved from Eclipse's official HTTPS distribution URL and its
downloaded hash matched the existing analysis record. APK signing-certificate
verification was unavailable, so provenance is URL-and-hash verified rather
than cryptographically signer-verified.

Relevant APK artifacts are the decoded `AndroidManifest.xml`, the
`MainActivity` callback dispatch and `onNewIntent` smali, the callback coroutine
and URI parser smali, and the `AuthApi` interface. These are identified by
class and method because local decompilation paths vary by machine.
