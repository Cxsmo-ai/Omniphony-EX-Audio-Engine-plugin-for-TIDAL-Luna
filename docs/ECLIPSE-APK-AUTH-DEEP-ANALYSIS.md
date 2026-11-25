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

Relevant APK artifacts are the decoded `AndroidManifest.xml`, the
`MainActivity` callback dispatch and `onNewIntent` smali, the callback coroutine
and URI parser smali, and the `AuthApi` interface. These are identified by
class and method because local decompilation paths vary by machine.
