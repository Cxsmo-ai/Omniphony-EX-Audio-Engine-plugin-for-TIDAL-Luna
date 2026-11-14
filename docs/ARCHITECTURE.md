# Omniphony EX & OmniFusion Engine — Technical Architecture

## 1. System Identity & Responsibilities

| Component | File / Binary | Role |
| :--- | :--- | :--- |
| **Omniphony EX Frontend** | `omniphony.mjs`, `index.js` | Intercepts TIDAL Desktop playback intents, detaches Chromium audio output without muting, synchronizes `libmpv` timeline back into TIDAL Redux/Radiant state, and renders the native footer badge, floating drawer, and `@luna/ui` Settings modal. |
| **OmniFusion Audio Service** | `api/omnifusion_service.py` | Local loopback HTTP server (`127.0.0.1:8765`) managing RealMAX multi-source stream resolution, `TidalDashProxy`, OxideAV AC-4 IMS decoding to a seekable stereo raw-PCM stream, and named-pipe JSON IPC (`//./pipe/omnifusion-standalone.mpv`) to `libmpv`. AC-4 playback creates no WAV; inactive encoded-MP4 cache entries are pruned toward a 256 MiB limit. |
| **Headless `libmpv` + `orender`** | `mpv/mpv.exe`, `orender.dll` | Executes bit-accurate FLAC and E-AC-3 JOC decoding, negotiates shared WASAPI output on the user-selected endpoint, and streams spatial object beds/objects to Omniphony Studio. |
| **Omniphony Studio** | `studio/omniphony-studio-egui.exe` | Real-time 3D object spatializer and SOFA HRIR binaural headphone processor communicating with `orender.dll` over loopback TCP port `9000`. |
| **OxideAV AC-4 Adapter** | `oxideav/adapter-v0.1.71/ac4-to-wav.exe` | Probes MP4 `dac4` DSI and AC-4 TOC bitstreams to verify genuine 2-channel Dolby Atmos Immersive Stereo (`AC4_IMS`) before decoding to 48 kHz binaural PCM. |

---

## 1.1 Per-Stream MPV Demuxer Selection

The service configures the existing headless MPV instance for each resolved transport before calling `loadfile`:

- **E-AC-3 JOC and FLAC DASH:** retain MPV's automatic demuxer selection and set the Lavf format to `dash`; the loopback `TidalDashProxy` supplies the validated manifest and segment URLs.
- **Amazon Eclipse extensionless MP4:** the resolver requires a valid byte-range response, `audio/mp4` MIME type, and an ISO-BMFF `ftyp` box. MPV is set to `lavf` with the `mov` format so an extensionless HTTPS URL is parsed as MP4. The next non-MP4 stream resets MPV to automatic demuxer selection.
- **AC-4 IMS:** OxideAV reads the encoded MP4 and exposes seekable stereo raw PCM; MPV uses the `rawaudio` demuxer. Playback does not create an intermediate WAV.

---

## 2. Sub-Millisecond UI & Lyrics Synchronization

1. **Non-Destructive Media Detachment:**
   - Rather than muting the Windows mixer or zeroing `<audio>` element volume, `omniphony.mjs` detaches TIDAL's media element playback pipeline while keeping TIDAL's internal track state active.
2. **Dual-Cadence Loopback Sync:**
   - **`GET /clock` (50 ms cadence):** Read-only high-frequency position sample returning `timePos`, `timePosSampledAtUnixMs`, and IPC request bracket duration so the native progress bar and word-by-word synced lyrics advance with sub-millisecond smoothness.
   - **`GET /status` (100 ms cadence):** Full transport and telemetry reconciliation including `format`, `sourceProvider`, `sourceBitrate`, `sourceSampleRate`, `sourceChannels`, `sourceBitDepth`, `audioParams`, `audioOutputParams`, and `omniphonyRoute`.
3. **Paused Track Preparation (`startPaused: true`):**
   - Users can arm **Prepare next selection paused** in the drawer to resolve, probe, and load a track into `libmpv` while holding playback paused until Play is explicitly clicked.

---

## 3. Loopback HTTP API (`127.0.0.1:8765`)

The loopback API responds with `Access-Control-Allow-Origin: *` for any browser origin, without credentialed cookies. Preflight remains limited to `GET`, `POST`, and the `Content-Type` / `Cache-Control` request headers.

| Method & Path | Purpose |
| :--- | :--- |
| `GET /health` | Returns service version (`2.1.32`), the owning TIDAL PID, ownership source and process creation time, whether that exact process is still alive, plugin/data/MPV/Studio path identities, `serviceInstanceId`, active `mpvInstanceId`, and capability flags (`prepared-playback-v1`, `playback-clock-v1`). It never returns the shutdown credential. |
| `GET /clock` | Lightweight 50 ms position and pause-state sample with IPC timing brackets. |
| `GET /status` | Full track, codec, bitrate, WASAPI output, and Omniphony Studio route telemetry. |
| `GET /config` / `POST /config` | Reads or atomically updates `priority`, `realmax_source` (`auto`, `tidal_only`, `amazon_only`), and `eclipse_addons`. |
| `GET /devices` / `POST /device` | Enumerates and configures `libmpv` WASAPI audio endpoints. |
| `POST /play` | Resolves the requested track via `resolve_with_priority()`, initializes `TidalDashProxy`, validated MP4, or AC-4 raw-PCM transport, verifies WASAPI + Omniphony Studio routing, and loads the stream into `libmpv`. |
| `POST /pause` / `POST /resume` / `POST /seek` | Generation-checked transport controls with settled seek verification. |
| `POST /login/start` / `GET /login/status` | Initiates and polls EbbLabs `python-tidal` device link authorization. |
| `POST /shutdown` | Gracefully terminates `libmpv` and exits the service. Requires the per-launch bearer secret and matching `X-Omniphony-Service-Instance` header; the credential is used only by the native supervisor and is not exposed to browser code. |

The loopback service intentionally keeps wildcard origin support for the normal API, but CORS preflight does not allow the shutdown authorization headers. The plugin renderer does not request shutdown on unload; the API watchdog binds cleanup to the exact owner process. A new TIDAL process never adopts or shuts down a service owned by another live process. Legacy services with no owner identity are reported as unverifiable and must be allowed to exit through their existing lifecycle before the new service starts. The API refuses to bind or start `mpv` without a verified TIDAL owner; it never falls back to watching any process named `TIDAL.exe`.
