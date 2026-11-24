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
Only an actual sign-in attempt can prove that the running Windows/TIDAL
environment delivers the callback as expected. Live Discord authorization,
account verification, supporter entitlement, and add-on loading remain runtime
checks until each succeeds in the plugin.

## Static-analysis references

Relevant APK artifacts are the decoded `AndroidManifest.xml`, the
`MainActivity` callback dispatch and `onNewIntent` smali, the callback coroutine
and URI parser smali, and the `AuthApi` interface. These are identified by
class and method because local decompilation paths vary by machine.
