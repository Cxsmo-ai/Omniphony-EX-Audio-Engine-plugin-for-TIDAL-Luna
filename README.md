<div align="center">

<img src="assets/header.svg" alt="Omniphony EX — RealMAX Multi-Source Spatial & Hi-Res Audio Engine for TIDAL Luna" width="100%" />

<br />

[![Plugin Version](https://img.shields.io/badge/Plugin-v2.1.39-00d2c4?style=for-the-badge&logo=tidal&logoColor=white)](https://github.com/Cxsmo-ai/Omniphony-EX-Audio-Engine-plugin-for-TIDAL-Luna)
[![API Service](https://img.shields.io/badge/API_Service-v2.1.27-31d8ff?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Cxsmo-ai/Omniphony-EX-Audio-Engine-plugin-for-TIDAL-Luna/releases)
[![Dolby Atmos](https://img.shields.io/badge/Dolby_Atmos-AC--4_IMS_%7C_E--AC--3_JOC-f5c400?style=for-the-badge)](docs/ARCHITECTURE.md)
[![Hi-Res Audio](https://img.shields.io/badge/FLAC-24--bit_%2F_192_kHz-00ff88?style=for-the-badge)](docs/ARCHITECTURE.md)
[![Bundled API](https://img.shields.io/badge/API-Python%20%2B%20python--tidal-00ff88?style=for-the-badge)](docs/ARCHITECTURE.md)

</div>

---

## Overview

**Omniphony EX** upgrades the official Windows **TIDAL Desktop** client (running **TidaLuna**) with a dedicated, multi-source spatial and Hi-Res audio engine (**OmniFusion Standalone Audio Engine**) while preserving 100% of native TIDAL UI behavior.

- **True Bit-Accurate & Spatial Playback:** Routes audio through headless **`libmpv`**, **OxideAV** (`AC-4 IMS` binaural stereo decoder), and in-process **`orender.dll`** connected to **Omniphony Studio** (`E-AC-3 JOC` 3D object spatializer with SOFA HRIR binaural headphone rendering).
- **WAV-Free AC-4 Playback:** OxideAV decodes the compressed AC-4 MP4 into a seekable stereo PCM stream for `libmpv`; playback does not write an intermediate WAV. Inactive encoded-source cache entries are pruned toward a 256 MiB limit. WAV remains an explicit downloader output choice.
- **Deep Web Audio Detachment (No OS Muting):** Detaches TIDAL's internal Chromium media output cleanly without muting your Windows mixer or breaking TIDAL's internal player state.
- **Sub-Millisecond UI & Lyrics Sync:** Polls `libmpv` IPC at 50 ms cadence and reconciles TIDAL Redux / Radiant player clocks every 100 ms—keeping word-by-word synced lyrics, click-to-seek lyrics, waveform scrubbing, and seamless queue auto-advance 1:1 with the audio engine.
- **RealMAX Native Song, Album & Playlist Downloader:** Directly integrated into TIDAL's native right-click context menu and settings surfaces.
  - **Dolby Atmos E-AC-3 JOC:** Rendered into bit-perfect 24-bit/48 kHz Binaural FLAC through `libmpv` (`--ad=orender`) using your active Omniphony Studio SOFA profile and headphone calibration.
  - **Dolby AC-4 IMS:** Decoded via OxideAV and exported as bit-perfect WAV or compressed 24-bit/48 kHz FLAC.
  - **Full Metadata & Artwork Tagging:** Automatically injects Vorbis comments (`TITLE`, `ARTIST`, `ALBUM`, `DATE`, `TRACKNUMBER`, `ISRC`, `SPATIAL_BINAURAL`, `LYRICS`), embeds high-resolution front cover artwork (type 3), and writes synchronized `.lrc` sidecar lyrics files.
  - **Customizable Folder & Path Templates:** Supports `{artist}/{album}/{trackNumber} - {title}` and native Windows folder picker (`showOpenDialog`).
- **RealMAX Multi-Source Resolution:** Dynamically evaluates all 24 user-selectable format priority orders across **TIDAL** (`EbbLabs/python-tidal`), user-supplied **Amazon Music Eclipse** addons, and an automatic **Tido Eclipse** fallback when the main TIDAL API is not yet linked.

---

## Live Screenshots & Real UI Walkthrough

All screenshots below are real captures taken directly from a live Windows 11 session running **TIDAL Desktop (TidaLuna Beta)**, **Omniphony EX v2.1.19**, **OmniFusion Service v2.1.25**, and **Omniphony Studio v0.7.0**.

### 1. Live TIDAL Desktop — Dolby Atmos `E-AC-3 JOC` (`769.215 kbps MPD · 48 kHz · 6 ch`)
*Full TIDAL Desktop window playing Imagine Dragons — Natural with the Omniphony EX Engine drawer open and the native bottom-right footer badge displaying live E-AC-3 JOC telemetry and confirmed `Omniphony Studio ↔ orender` connection.*

<p align="center">
  <img src="assets/screenshots/tidal-desktop-live.png" alt="Live TIDAL Desktop playing Dolby Atmos E-AC-3 JOC" width="96%" />
</p>

### 2. Live TIDAL Desktop — Dolby Atmos `AC-4 IMS` (`256 kbps avg · 48 kHz · 2 ch`)
*Full TIDAL Desktop window playing Imagine Dragons — Natural in verified Dolby AC-4 Immersive Stereo (`AC4_IMS`), decoded via OxideAV into 48 kHz binaural stereo.*

<p align="center">
  <img src="assets/screenshots/tidal-desktop-ac4-ims-live.png" alt="Live TIDAL Desktop playing Dolby AC-4 IMS" width="96%" />
</p>

### 3. Native Footer Telemetry Bar Across Formats & Providers
*Real close-up captures of the TIDAL player footer bar showing how the bottom-right Omniphony EX badge adapts across `[TIDAL] E-AC-3 JOC`, `[TIDAL] AC-4 IMS`, and `[AMAZON] Lossless / Hi-Res FLAC`:*

**`[TIDAL] E-AC-3 JOC · 769.215 kbps MPD · 48 kHz · 6 ch`**
<p align="center">
  <img src="assets/screenshots/real-tidal-player-footer-joc.png" alt="Real TIDAL Footer Badge — E-AC-3 JOC" width="96%" />
</p>

**`[TIDAL] AC-4 IMS · 256 kbps avg · 48 kHz · 2 ch`**
<p align="center">
  <img src="assets/screenshots/real-tidal-player-footer-ac4.png" alt="Real TIDAL Footer Badge — AC-4 IMS" width="96%" />
</p>

**`[AMAZON] Lossless FLAC · 1692.357 kbps reported · 44.1 kHz · 2 ch · 16-bit`**
<p align="center">
  <img src="assets/screenshots/real-tidal-player-footer-amazon.png" alt="Real TIDAL Footer Badge — Amazon Music Lossless FLAC" width="96%" />
</p>

### 4. In-TIDAL Omniphony EX Control Drawer (`E-AC-3 JOC` vs. `AC-4 IMS`)
*Side-by-side real captures of the floating Omniphony EX settings drawer showing live source/decoded/output telemetry, paused preparation control, 24-order RealMAX priority selector, RealMAX Source Provider selector (`Both` / `TIDAL Only` / `Amazon Music Only`), WASAPI device selector, 1-click TIDAL OAuth link, engine paths, and Eclipse addon manifest loader:*

| `E-AC-3 JOC` Active (`Omniphony Studio ↔ orender` Confirmed) | `AC-4 IMS` Active (`OxideAV` MP4 Packet Average Confirmed) |
| :---: | :---: |
| <img src="assets/screenshots/real-omniphony-settings-drawer.png" alt="Omniphony EX Drawer — E-AC-3 JOC" width="380" /> | <img src="assets/screenshots/real-drawer-ac4-telemetry.png" alt="Omniphony EX Drawer — AC-4 IMS" width="380" /> |

### 5. Omniphony Studio 3D Spatial Renderer (`Connected · mpv · SOFA HRIR Binaural`)
*Real capture of `omniphony-studio-egui.exe` connected to `libmpv` over loopback OSC (`127.0.0.1:9000`), rendering full 3D object-based audio, any speaker layout (`2.0` through `5.1.2`, `7.1.2`, `7.1.4`, `9.1.6`, `22.2`, or custom 3D speaker layouts), and direct SOFA HRIR binaural headphone rendering through the bundled Meta Audio SDK HRTF (`assets/hrtf/Meta_Universal_HRTF_192k_16384pts_512tap_32bit_Omniphony.sofa`):*

<p align="center">
  <img src="assets/screenshots/omniphony-studio-3d-spatial.png" alt="Omniphony Studio 3D Spatial Control Connected to mpv" width="96%" />
</p>

---

## Architecture & Signal Pipeline

```mermaid

flowchart TD
    subgraph TIDAL["TIDAL Desktop / TidaLuna"]
        UI["Native TIDAL UI, Queue and Click-to-Seek Lyrics"]
        DETACH["Web Audio and HTMLMediaElement Detachment"]
        SYNC["50ms IPC Clock Poll and 100ms Redux Reconciler"]
        BADGE["Footer Badge, Drawer and Luna UI Settings"]
    end

    subgraph SERVICE["OmniFusion Service - 127.0.0.1:8765"]
        PRIO["RealMAX Priority and Multi-Source Resolver"]
        DASH["TidalDashProxy - Zero-Disk Streaming"]
        OXIDE["OxideAV v0.1.71 - AC-4 DSI/TOC Decoder"]
    end

    subgraph SOURCES["Multi-Source Providers"]
        EBB["EbbLabs python-tidal - PKCE, Mobile OAuth, TV Atmos"]
        AMZM["Amazon Music Eclipse Addon - Optional"]
        TIDO["Tido Eclipse - Unauthenticated Fallback"]
    end

    subgraph ENGINE["Native Spatial Audio Engine"]
        MPV["Headless libmpv - WASAPI Audio Output"]
        ORENDER["In-Process orender.dll"]
        STUDIO["Omniphony Studio v0.7.0 - SOFA HRIR Binaural"]
    end

    UI --> DETACH
    DETACH -->|"Play, Seek, Pause, Resume"| PRIO

    PRIO --> EBB
    PRIO --> AMZM
    PRIO --> TIDO

    EBB -->|"AC-4 IMS MP4"| OXIDE
    EBB -->|"E-AC-3 JOC or FLAC MPD"| DASH
    TIDO -->|"E-AC-3 JOC or FLAC MPD"| DASH
    AMZM -->|"Direct HTTPS MP4 (lavf/mov)"| MPV

    OXIDE -->|"Verified 48kHz Binaural PCM"| MPV
    DASH -->|"HTTP Audio Segments"| MPV

    MPV -->|"EAC3_JOC"| ORENDER
    ORENDER -->|"OSC TCP Port 9000"| STUDIO
    STUDIO -->|"Spatial Rendering Control"| ORENDER

    MPV -->|"Clock and Status"| SYNC
    SYNC --> BADGE
```

---

## RealMAX Format Priority & Multi-Source Routing

### Supported Audio Formats & Verification Gates

| Priority Token | Format Name | Encoded Input | Channels & Sample Rate | Decoding & Verification Pipeline |
| :--- | :--- | :--- | :--- | :--- |
| **`AC4_IMS`** | **Dolby AC-4 IMS** *(Immersive Stereo)* | Dolby AC-4 (`ac-4`) | 2 ch Binaural · 48 kHz · ~256–320 kbps | **TIDAL Exclusive.** Verified via MP4 `dac4` DSI + bitstream TOC probe, decoded via **OxideAV** (`ac4-to-wav`) into 48 kHz binaural stereo. |
| **`EAC3_JOC`** | **Dolby Atmos** *(E-AC-3 Joint Object Coding)* | Dolby Digital Plus Atmos (`ec-3` / `eac3_joc`) | 6 ch (5.1 + JOC objects) · 48 kHz · ~768 kbps | Decoded in-process by `libmpv` (`--ad=orender`) with mandatory fail-closed verification of the live **Omniphony Studio** OSC link (`127.0.0.1:9000`) and SOFA HRIR binaural configuration. |
| **`HI_RES_LOSSLESS`** | **Hi-Res Lossless FLAC** *(Max / Ultra HD)* | Free Lossless Audio Codec (`flac`) | 2 ch · 24-bit / up to 192 kHz · ~1,600–9,200 kbps | Streamed directly over HTTPS (`DIRECT`) or loopback `TidalDashProxy` (`DASH`) with zero WAV disk conversion. |
| **`LOSSLESS`** | **Lossless FLAC** *(CD Quality)* | Free Lossless Audio Codec (`flac`) | 2 ch · 16-bit / 44.1 kHz · ~850–1,411 kbps | Bit-exact 16-bit/44.1 kHz lossless FLAC streamed directly to `libmpv`. |

### RealMAX Source Provider Modes (`realmax_source`)

| Mode | Selector Label | Routing Behavior |
| :--- | :--- | :--- |
| **`auto`** *(Default)* | **Both (TIDAL + Amazon Music / Eclipse)** | Evaluates each format in your exact 24-permutation priority order across both providers. If a track is stereo-only on TIDAL (e.g., *Imagine Dragons — Natural (Live in Vegas)*) but has a Dolby Atmos `E-AC-3 JOC` mix on your configured Amazon Music Eclipse addon, RealMAX automatically upgrades the track to the Amazon Atmos stream. |
| **`tidal_only`** | **TIDAL Only (Atmos AC-4/JOC + Hi-Res)** | Resolves exclusively through TIDAL (including cross-edition discovery for alternate Atmos / Deluxe Hi-Res album releases). |
| **`amazon_only`** | **Amazon Music Only (No AC-4 IMS)** | Resolves exclusively through your user-supplied Amazon Music Eclipse addon (`Atmos_EC-3`, `Max` 24-bit/192 kHz, and `Lossless` 16-bit/44.1 kHz). Automatically skips `AC4_IMS` since AC-4 IMS is exclusive to TIDAL. |

### Authentication & Automatic Unauthenticated Fallback

| Auth State | Active Resolver | Capabilities |
| :--- | :--- | :--- |
| **Main TIDAL API Linked** *(Recommended)* | Bundled **EbbLabs `python-tidal`** (`PKCE` + Mobile `OAuth` + `TV` Atmos profiles) | Full access to **Dolby AC-4 IMS**, **Dolby Atmos E-AC-3 JOC**, **24-bit/192 kHz Hi-Res FLAC**, **16-bit/44.1 kHz Lossless FLAC**, and cross-edition catalog discovery. |
| **Main TIDAL API Not Yet Linked** | Built-in **Tido Eclipse Fallback** (`com.yourname.tidal.hifi`) | Works out-of-the-box before linking a TIDAL account: resolves **Dolby Atmos E-AC-3 JOC**, **Hi-Res FLAC**, and **Lossless FLAC** (`AC-4 IMS` requires linking the main TIDAL API). |
| **User-Added Eclipse Manifest** *(Optional)* | **Amazon Music Eclipse Addon** (`com.yourname.amazonmusic`) | Users paste their own Eclipse `manifest.json` URL in Settings; OmniFusion handles the 2-step `X-Eclipse-Access` token handshake automatically in the background. |

---

## Installation & Setup Guide

### Step 1 — Install Omniphony EX
Install the plugin from the TidaLuna Plugin Store using the repository's latest store catalog:

```text
https://github.com/Cxsmo-ai/Omniphony-EX-Audio-Engine-plugin-for-TIDAL-Luna/releases/download/latest/store.json
```

The store installs one bundled TidaLuna module plus its package metadata. On first launch, the module fetches the version-pinned API runtime archive from the matching GitHub release and checks its SHA-256 before extraction. Users do not install Python, `python-tidal`, or a separate API service.

For local development, run `pnpm install` and `pnpm run watch` from the repository. TidaLuna exposes the loopback DEV store on port `3000`; install the Omniphony EX entry from Plugin Store. The server binds only to `127.0.0.1` and serves the generated catalog and the plugin's `.json`/`.mjs` pair.

The plugin-managed API archive includes the embedded Python runtime, EbbLabs `python-tidal`, its dependencies, and the licensed AC-4 and Harletty support binaries. It does not contain account tokens. Plugin settings, TIDAL auth sessions, logs, cache, and downloads are written under the TIDAL user-data plugin directory in `data/` and survive plugin code updates.

### Step 2 — Select the two external engine paths
In **TIDAL Settings → Plugins → Omniphony EX**, set:

1. **MPV Engine Folder** to the folder containing the `mpv.exe` and compatible `orender.dll` build you use.
2. **Omniphony Studio Path** to `omniphony-studio-egui.exe`.
3. **WASAPI Audio Output Device** to the intended endpoint, such as Voicemeeter Input.

MPV and Omniphony Studio remain separate user-provided applications. The Python API, TIDAL authentication flow, OxideAV adapter, and Harletty bridge libraries are managed by the plugin. No ASIO output selection is required for the configured WASAPI route.

### Step 3 — Authenticate and choose sources
1. Click **Link / Authenticate TIDAL Account** to authorize the bundled `python-tidal` API.
2. Set the RealMAX format order and source mode in plugin settings.
3. Optionally paste an Eclipse addon `manifest.json` URL under **Eclipse Addon Providers** for Amazon Music/Eclipse resolution.
4. Apply the engine paths and confirm the service health and selected output device before starting a track.

---

## Repository Layout

```text
omniphony-tidal-luna-plugin/
├── README.md                          # Flagship documentation, architecture & live UI gallery
├── package.json                       # Reproducible Luna builder and workspace commands
├── pnpm-lock.yaml                     # Pinned JavaScript dependency graph
├── pnpm-workspace.yaml                # Luna plugin workspace definition
├── esbuild.config.ts                  # Official luna/buildPlugins entry
├── plugins/OmniphonyEX/               # Luna plugin package metadata and source entry
├── index.js                           # TidaLuna lifecycle entry point (auto-spawns/stops bundled service)
├── omniphony.mjs                      # Frontend engine: Web Audio detachment, 50ms clock sync, UI & Settings
├── manifest.json                      # TidaLuna plugin manifest & SHA-256 module integrity hash
├── omniphony.json                     # Versioned module descriptor for TidaLuna loader
├── store.json                         # TidaLuna Plugin Store metadata
├── assets/
│   ├── header.svg                     # Crisp vector header banner
│   ├── hrtf/
│   │   └── Meta_Universal_HRTF_192k_16384pts_512tap_32bit_Omniphony.sofa # Bundled Meta Audio SDK 48kHz 16384-pt SOFA HRIR
│   └── screenshots/
│       ├── tidal-desktop-live.png             # Live TIDAL Desktop playing E-AC-3 JOC with drawer open
│       ├── tidal-desktop-ac4-ims-live.png     # Live TIDAL Desktop playing AC-4 IMS with drawer open
│       ├── real-omniphony-settings-drawer.png # Close-up of real Omniphony EX drawer (E-AC-3 JOC)
│       ├── real-drawer-ac4-telemetry.png      # Close-up of real Omniphony EX drawer (AC-4 IMS)
│       ├── real-tidal-player-footer-joc.png   # Real TIDAL player footer bar ([TIDAL] E-AC-3 JOC)
│       ├── real-tidal-player-footer-ac4.png   # Real TIDAL player footer bar ([TIDAL] AC-4 IMS)
│       ├── real-tidal-player-footer-amazon.png# Real TIDAL player footer bar ([AMAZON] Lossless FLAC)
│       └── omniphony-studio-3d-spatial.png    # Live Omniphony Studio 3D renderer connected to mpv
├── docs/
│   └── ARCHITECTURE.md                # Deep technical specification & loopback API reference
└── api/
    ├── omnifusion_service.py          # Local HTTP/IPC audio engine, RealMAX resolver & DASH proxy
    ├── launch_service.bat             # Standalone service launcher with health verification
    ├── runtime/                       # Embedded portable Python 3.14 Windows runtime
    └── vendor/                        # Bundled dependencies (tidalapi, mpegdash, psutil, requests)
```

`pnpm run build` writes TidaLuna's generated `store.json` and plugin `.json`/`.mjs` pair to `dist/`. GitHub Actions validates that package, builds and verifies the API runtime archive, then publishes the dotted plugin asset names expected by TidaLuna's remote-store URL resolver. `tools/verify-luna-store.py` checks that every catalog entry has both artifacts and valid metadata.

For deep protocol details, loopback HTTP endpoints, and verification gates, see [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md).

---

## License & Third-Party Notices

Omniphony EX is released under the [**MIT License**](LICENSE).

This project bundles and interoperates with third-party open-source components under their respective licenses:
- **Python Embedded Runtime (Python 3.14):** [Python Software Foundation License](THIRD-PARTY-NOTICES.md#1-python-software-foundation-license-python-314-embedded-runtime) (`api/runtime/LICENSE.txt`)
- **EbbLabs `python-tidal`:** [GNU Lesser General Public License v3.0+](THIRD-PARTY-NOTICES.md#2-ebblabs-python-tidal-lgpl-30-or-later) (`api/vendor/licenses/python-tidal-LICENSE`)
- **`psutil`:** [BSD-3-Clause License](THIRD-PARTY-NOTICES.md#3-psutil-bsd-3-clause)
- **`requests`:** [Apache-2.0 License](THIRD-PARTY-NOTICES.md#4-requests-apache-20)
- **`mpegdash`:** [MIT License](THIRD-PARTY-NOTICES.md#5-mpegdash-mit)
- **`certifi`:** [Mozilla Public License 2.0](THIRD-PARTY-NOTICES.md#6-certifi-mpl-20)
- **Meta Audio SDK / Universal HRTF:** [Derived Measurement & Interoperability Notice](THIRD-PARTY-NOTICES.md#7-meta-audio-sdk--sofa-reference)

For full license texts and attribution notices, see [**THIRD-PARTY-NOTICES.md**](THIRD-PARTY-NOTICES.md).
