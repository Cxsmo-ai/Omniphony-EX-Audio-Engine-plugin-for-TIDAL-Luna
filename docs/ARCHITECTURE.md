# Omniphony EX and OmniFusion Architecture

## Components

| Component | Location | Responsibility |
| --- | --- | --- |
| TIDAL Luna plugin | `omniphony.mjs` | Native TIDAL playback integration, RealMAX controls, Eclipse account UI, transport synchronization and status display. |
| OmniFusion service | `api/omnifusion_service.py` | Loopback API on `127.0.0.1:8765`, TIDAL and Eclipse provider resolution, playback telemetry, and lifecycle ownership. |
| `libmpv` | User-selected MPV folder | Audio decoding and output to the selected Windows audio endpoint. E-AC-3 JOC is routed through the configured Omniphony renderer. |
| Omniphony Studio | User-selected executable | Headphone spatialization and SOFA profile processing for supported E-AC-3 JOC playback. |
| OxideAV AC-4 adapter | Bundled service runtime | Probes AC-4 IMS and exposes seekable stereo PCM to MPV without creating an intermediate WAV during playback. |

MPV and Omniphony Studio remain user-selected applications. ASIO is not required; output selection is handled through the configured audio device.

## Playback source selection

RealMAX evaluates the configured format priority across the selected providers:

- **TIDAL API:** bundled EbbLabs `python-tidal` integration. AC-4 IMS remains available through this source when TIDAL returns it.
- **Eclipse account add-ons:** the user signs into Eclipse with Discord in the plugin's sign-in window. The service verifies the account, protects its session with current-user Windows DPAPI, and reads enabled add-ons and their account playback order from Eclipse. Add-on access tokens remain in the service and are not returned to the TIDAL renderer.
- **Account connections:** Amazon Music, TIDAL-compatible Eclipse add-ons, and other enabled account connections are discovered from the signed-in account. No manifest URL is manually entered in the current settings UI. Tido is usable when present as an enabled account connection; the plugin does not inject an unauthenticated hard-coded fallback.

The source selector offers exactly **Both**, **TIDAL API**, and **Eclipse add-ons**. In Both mode, format priority is the outer loop and provider priority is the inner loop: for each requested quality, the first provider able to return it wins; when none can, RealMAX tries the next quality. TIDAL API mode limits the provider list to TIDAL. Eclipse add-ons mode follows the account playback order or the user's saved provider priority. An explicit empty provider order means no provider is selected. AC-4 IMS is not requested from Eclipse add-ons.

## Eclipse account and request handling

The plugin opens Eclipse's regular sign-in page in an isolated sign-in window and completes authorization through the service callback. It does not read the user's Edge profile or depend on the Eclipse Windows application. Account verification, connection discovery, add-on token exchange, and provider requests run in the service. Tokens are DPAPI-protected at rest; public settings responses contain add-on identity and status but omit account-scoped manifest URLs and credentials.

Account changes invalidate account-specific token, inventory, search, and stream caches. Asynchronous verification and inventory requests check the captured authentication generation before changing persisted session state or publishing results, preventing delayed responses for an old account from replacing a newer sign-in.

Resource requests retain query configuration supplied by the account's manifest URL and merge it with operation parameters such as search term and quality. Signed requests use same-origin HTTPS redirect handling so bearer and add-on access headers are not forwarded to a different origin. The bottom-right TIDAL badge identifies the actual source provider and connected add-on name as well as the format and measured/reported bitrate.

Legacy manually entered Eclipse manifest settings are removed during settings migration. The current UI deliberately uses Eclipse account connections instead of a manual manifest editor.

## Playback transports and synchronization

- E-AC-3 JOC and FLAC DASH streams are served through `TidalDashProxy` and loaded by MPV.
- Eclipse extensionless MP4 streams are validated as MP4 and assigned MPV's `lavf`/`mov` demuxer for that stream; later formats return to automatic demuxer selection.
- AC-4 IMS is decoded by OxideAV to seekable stereo PCM and streamed to MPV; playback does not write an intermediate WAV file.
- The plugin polls the lightweight playback clock and full service status to synchronize TIDAL progress, lyrics, seek, and codec/output telemetry. Status distinguishes encoded source details from decoded/output details.

For E-AC-3 JOC, stereo headphone output is expected after Omniphony processing even when the selected Windows endpoint reports a multichannel device format.

## Loopback service API

The loopback API intentionally responds with `Access-Control-Allow-Origin: *` and does not use credentialed browser cookies. Preflight permits the plugin's required methods and request headers.

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Reports service version, exact TIDAL owner identity, runtime paths, service and MPV instance identities, and supported capabilities. Shutdown credentials are never returned. |
| `GET /clock` | Lightweight playback time and pause-state sample. |
| `GET /status` | Track, codec, bitrate, output-device and Omniphony route telemetry. |
| `GET /config`, `POST /config` | Reads or updates RealMAX format priority, source mode, and selected account provider order. |
| `GET /devices`, `POST /device` | Lists and selects MPV audio endpoints. |
| `GET /eclipse-auth/status`, `GET /eclipse-auth/addons` | Reports redacted Eclipse account state and enabled account connections. |
| `POST /eclipse-auth/complete`, `POST /eclipse-auth/disconnect` | Verifies and stores an Eclipse sign-in or disconnects the account. |
| `POST /play` | Resolves a track in RealMAX order and loads the selected TIDAL or Eclipse transport. |
| `POST /pause`, `/resume`, `/seek` | Generation-checked transport controls. |
| `POST /login/start`, `GET /login/status` | Starts and polls bundled EbbLabs `python-tidal` authorization. |
| `POST /shutdown` | Authenticated native-supervisor shutdown; the per-launch credential is not exposed to browser code. |

The service watches the exact TIDAL process that owns it. It does not adopt or stop another live TIDAL instance, and it refuses to start playback infrastructure without a verified owner process.
