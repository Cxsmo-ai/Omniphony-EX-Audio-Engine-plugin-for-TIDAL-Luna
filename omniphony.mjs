/**
 * Omniphony EX - TidaLuna Ultra-Native Audio Engine Bridge
 * 
 * Playback bridge:
 *   Keeps TidaLuna's native queue and controls as the UI, routes audio through
 *   the local libmpv service, and mirrors the confirmed engine clock/state back
 *   into TIDAL. Native stream privilege and HTML audio starts are intercepted;
 *   TIDAL audio is not muted or routed through a zero-gain Web Audio graph.
 *
 * RealMax Format Prioritization:
 *   Resolves and plays discrete Dolby Atmos (E-AC-3 JOC 15 Objs), Dolby AC-4 IMS,
 *   or Hi-Res FLAC via local headless libmpv engine.
 *
 * Full Device Selection:
 *   Native UI settings panel with real-time WASAPI / Voicemeeter device switching.
 */

const API_BASE = "http://127.0.0.1:8765";
const REQUIRED_SERVICE_VERSION = "2.1.29";
const REQUIRED_SERVICE_CAPABILITY = "prepared-playback-v1";
const PLUGIN_BUILD = "2.1.43";
function parseEclipseManifestUrl(value) {
  const parsed = new URL(String(value ?? "").trim());
  const hostname = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  const localHttp = parsed.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(hostname);
  if ((parsed.protocol !== "https:" && !localHttp) || !parsed.hostname || parsed.username || parsed.password) {
    throw new Error("Enter an HTTPS manifest URL (HTTP is allowed only for localhost development).");
  }
  return parsed.href;
}
const NATIVE_PLAYER_GUARD_MODULE_ID = "OmniphonyEX/player-guard.native.js";
const NATIVE_PLAYER_GUARD_MODULE_CODE = String.raw`module.exports = {
  install(token) {
    const path = require("path");
    const controllerPath = path.join(process.resourcesPath, "original.asar", "app", "main", "playback", "player", "PlayerController.js");
    const PlayerController = require(controllerPath).default;
    const prototype = PlayerController && PlayerController.prototype;
    if (!prototype) throw new Error("TIDAL PlayerController was not found");
    const shared = globalThis.luna.modules.__omniphonyPlayerGuardState ||
      (globalThis.luna.modules.__omniphonyPlayerGuardState = { tokens: new Set() });
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "onPlayerMessage");
    if (shared.prototype === prototype && descriptor?.get === shared.getter) {
      shared.tokens.add(String(token));
      return { installed: true, target: "TIDAL PlayerController.onPlayerMessage", strategy: "filter media.play before native Player.send" };
    }
    if (!descriptor || typeof descriptor.value !== "function") {
      throw new Error("TIDAL PlayerController.onPlayerMessage has an unsupported shape");
    }
    if (!/this\.sendPlayerMessage\s*\(\s*message\s*\)/.test(Function.prototype.toString.call(descriptor.value))) {
      throw new Error("TIDAL PlayerController no longer forwards player messages through the verified native-player path");
    }
    if (shared.prototype !== prototype || !descriptor.get || descriptor.get !== shared.getter) {
      const originalDescriptor = descriptor;
      let baseMethod = descriptor.value;
      let guardedMethod;
      const makeGuard = (method) => function omniphonyGuardedPlayerMessage(event, message) {
        let command = null;
        try {
          const parsed = typeof message === "string" ? JSON.parse(message) : message;
          command = parsed && parsed.command;
        } catch (_) {}
        if (command === "media.play") {
          try {
            globalThis.luna.sendToRender("omniphony:native-player-play", { source: "tidal-player-controller" });
          } catch (error) {
            console.error("[Omniphony EX] Could not forward intercepted TIDAL play intent:", error);
          }
          return;
        }
        return method.apply(this, arguments);
      };
      guardedMethod = makeGuard(baseMethod);
      const getter = function omniphonyPlayerMessageGetter() { return guardedMethod; };
      const setter = function omniphonyPlayerMessageSetter(nextMethod) {
        if (typeof nextMethod === "function" && nextMethod !== guardedMethod) {
          baseMethod = nextMethod;
          guardedMethod = makeGuard(nextMethod);
        }
      };
      Object.defineProperty(prototype, "onPlayerMessage", {
        configurable: true,
        enumerable: originalDescriptor.enumerable,
        get: getter,
        set: setter,
      });
      shared.prototype = prototype;
      shared.originalDescriptor = originalDescriptor;
      shared.getBaseMethod = () => baseMethod;
      shared.getter = getter;
      shared.setter = setter;
      shared.tokens = shared.tokens || new Set();
    }
    shared.tokens.add(String(token));
    const active = Object.getOwnPropertyDescriptor(prototype, "onPlayerMessage");
    if (active?.get !== shared.getter) throw new Error("TIDAL native-player guard was replaced during installation");
    return { installed: true, target: "TIDAL PlayerController.onPlayerMessage", strategy: "filter media.play before native Player.send" };
  },
  uninstall(token) {
    const shared = globalThis.luna.modules.__omniphonyPlayerGuardState;
    if (!shared || !shared.tokens) return { installed: false, reason: "guard state not found" };
    shared.tokens.delete(String(token));
    if (shared.tokens.size > 0) return { installed: true, remainingUsers: shared.tokens.size };
    const descriptor = Object.getOwnPropertyDescriptor(shared.prototype, "onPlayerMessage");
    if (descriptor?.get === shared.getter) {
      Object.defineProperty(shared.prototype, "onPlayerMessage", {
        ...shared.originalDescriptor,
        value: shared.getBaseMethod(),
      });
    }
    delete globalThis.luna.modules.__omniphonyPlayerGuardState;
    return { installed: false, restored: true };
  },
};`;
const NATIVE_SERVICE_SUPERVISOR_MODULE_ID = "OmniphonyEX/service-supervisor.native.js";
const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String.raw`module.exports = {
  async ensureService(requiredVersion, runtimeConfig = {}) {
    const fs = require("fs");
    const path = require("path");
    const http = require("http");
    const { spawn, spawnSync } = require("child_process");
    const app = require("electron").app;
    const stateKey = Symbol.for("omniphony-ex.native-service-supervisor");
    const shared = globalThis[stateKey] ||
      (globalThis[stateKey] = { startPromise: null, child: null });

    if (!app || typeof app.getPath !== "function") {
      return { ready: false, reason: "electron_user_data_unavailable" };
    }

    const pluginRoot = path.resolve(app.getPath("userData"), "plugins", "omniphony-tidal-luna-plugin");
    const dataRoot = path.join(pluginRoot, "data");
    const runtimePathsFile = path.join(dataRoot, "config", "runtime-paths.json");
    const bundledRuntimeRoot = path.join(pluginRoot, "runtime-bundles", "2.1.43");
    const bundledApiRoot = path.join(bundledRuntimeRoot, "api");
    const localApiRoot = path.join(pluginRoot, "api");
    const bundleUrl = "https://github.com/Cxsmo-ai/Omniphony-EX-Audio-Engine-plugin-for-TIDAL-Luna/releases/download/v2.1.43/OmniphonyEX-API-win-x64.tar.gz";
    const bundleSha256 = "6EABE859ACC5EA5983680B2709FA6A54170CE47F2B4CE089DF687135C47E730D";
    const readPersistedRuntimePaths = () => {
      try {
        const saved = JSON.parse(fs.readFileSync(runtimePathsFile, "utf8"));
        return {
          mpvDir: typeof saved?.mpvDir === "string" ? saved.mpvDir.trim() : "",
          studioExe: typeof saved?.studioExe === "string" ? saved.studioExe.trim() : "",
        };
      } catch (_) {
        return { mpvDir: "", studioExe: "" };
      }
    };
    const savedRuntimePaths = readPersistedRuntimePaths();
    const requestedMpvDir = String(runtimeConfig?.mpvDir || savedRuntimePaths.mpvDir || "").trim();
    const requestedStudioExe = String(runtimeConfig?.studioExe || savedRuntimePaths.studioExe || "").trim();
    const mpvDir = requestedMpvDir
      ? path.resolve(requestedMpvDir)
      : path.join(pluginRoot, "external", "mpv");
    const studioExe = requestedStudioExe
      ? path.resolve(requestedStudioExe)
      : path.join(pluginRoot, "external", "omniphony-studio-egui.exe");
    const samePath = (left, right) => {
      if (!left || !right) return false;
      return path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
    };
    const runtimeFiles = [
      path.join(mpvDir, "mpv.exe"),
      path.join(mpvDir, "orender.dll"),
      studioExe,
    ];
    const missingRuntimeFiles = runtimeFiles.filter((file) => !fs.existsSync(file));
    if (missingRuntimeFiles.length) {
      return {
        ready: false,
        reason: "runtime_paths_invalid",
        missingFiles: missingRuntimeFiles,
      };
    }
    try {
      fs.mkdirSync(path.dirname(runtimePathsFile), { recursive: true });
      const temporaryRuntimePathsFile = runtimePathsFile + "." + process.pid + "." + Date.now() + ".tmp";
      fs.writeFileSync(temporaryRuntimePathsFile, JSON.stringify({ mpvDir, studioExe }, null, 2), "utf8");
      fs.renameSync(temporaryRuntimePathsFile, runtimePathsFile);
    } catch (error) {
      return { ready: false, reason: "runtime_paths_persist_failed", message: String(error) };
    }

    const hasApiBundle = (root) => [
      "runtime/python.exe",
      "omnifusion_service.py",
      "vendor/tidalapi/__init__.py",
      "oxideav/adapter-v0.1.71/ac4-to-wav.exe",
      "oxideav/adapter-v0.1.71/omnifusion_ac4_adapter.dll",
      "oxideav/adapter-v0.1.71/LICENSE",
      "harletty/v0.9.0/harletty_dolby_bridge.dll",
      "harletty/v0.9.0/harletty_dts_bridge.dll",
      "harletty/v0.9.0/harletty_iamf_bridge.dll",
      "harletty/v0.9.0/LICENSE",
    ].every((relative) => fs.existsSync(path.join(root, ...relative.split("/"))));
    const hasCompatibleLocalApiBundle = () => {
      if (!fs.existsSync(path.join(pluginRoot, ".git")) || !hasApiBundle(localApiRoot)) return false;
      try {
        const serviceSource = fs.readFileSync(path.join(localApiRoot, "omnifusion_service.py"), "utf8");
        const declaredVersion = serviceSource.match(/^SERVICE_VERSION\s*=\s*["']([^"']+)["']\s*$/m)?.[1];
        return declaredVersion === requiredVersion;
      } catch (_) {
        return false;
      }
    };
    const hasVerifiedRuntimeBundle = () => {
      try {
        const marker = JSON.parse(fs.readFileSync(path.join(bundledRuntimeRoot, "bundle-version.json"), "utf8"));
        return marker.pluginVersion === "2.1.43"
          && marker.serviceVersion === requiredVersion
          && marker.sha256 === bundleSha256;
      } catch (_) {
        return false;
      }
    };

    const downloadBundle = (url, outputPath, redirects = 0) => new Promise((resolve, reject) => {
      if (redirects > 6) return reject(new Error("too many release redirects"));
      let parsed;
      try { parsed = new URL(url); } catch (error) { return reject(error); }
      if (parsed.protocol !== "https:") return reject(new Error("runtime bundle URL must use HTTPS"));
      const https = require("https");
      const crypto = require("crypto");
      const request = https.get(parsed, (response) => {
        if ([301, 302, 303, 307, 308].includes(response.statusCode) && response.headers.location) {
          response.resume();
          return downloadBundle(new URL(response.headers.location, parsed).toString(), outputPath, redirects + 1)
            .then(resolve, reject);
        }
        if (response.statusCode !== 200) {
          response.resume();
          return reject(new Error("runtime bundle download returned HTTP " + response.statusCode));
        }
        const hash = crypto.createHash("sha256");
        const output = fs.createWriteStream(outputPath, { flags: "wx" });
        let bytes = 0;
        response.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > 512 * 1024 * 1024) request.destroy(new Error("runtime bundle exceeded 512 MiB"));
          else hash.update(chunk);
        });
        response.on("error", (error) => output.destroy(error));
        output.on("error", reject);
        output.on("finish", () => resolve({ bytes, sha256: hash.digest("hex").toUpperCase() }));
        response.pipe(output);
      });
      request.setTimeout(30000, () => request.destroy(new Error("runtime bundle download timed out")));
      request.on("error", reject);
    });

    const installRuntimeBundle = async () => {
      if (hasApiBundle(bundledApiRoot) && hasVerifiedRuntimeBundle()) return bundledApiRoot;
      fs.mkdirSync(path.dirname(bundledRuntimeRoot), { recursive: true });
      const archive = path.join(pluginRoot, ".omniphony-runtime-" + process.pid + "-" + Date.now() + ".tar.gz");
      const staging = path.join(path.dirname(bundledRuntimeRoot), ".stage-" + process.pid + "-" + Date.now());
      try {
        const downloaded = await downloadBundle(bundleUrl, archive);
        if (downloaded.sha256 !== bundleSha256) throw new Error("runtime bundle SHA-256 did not match the pinned release digest");
        const tarExe = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
        if (!fs.existsSync(tarExe)) throw new Error("Windows tar.exe is unavailable for runtime bundle extraction");
        const listing = spawnSync(tarExe, ["-tzf", archive], {
          encoding: "utf8", windowsHide: true, timeout: 120000, maxBuffer: 4 * 1024 * 1024,
        });
        if (listing.error || listing.status !== 0) throw new Error("runtime bundle archive validation failed: " + (listing.error || listing.stderr));
        const entries = String(listing.stdout || "").split(/\r?\n/).filter(Boolean);
        if (!entries.length || entries.some((entry) => {
          const normalized = entry.replace(/\\/g, "/");
          return normalized.startsWith("/") || /^[A-Za-z]:/.test(normalized)
            || normalized.split("/").some((part) => part === "..")
            || !(normalized === "api" || normalized.startsWith("api/"));
        })) throw new Error("runtime bundle contains an unexpected path");
        fs.mkdirSync(staging, { recursive: true });
        const extracted = spawnSync(tarExe, ["-xzf", archive, "-C", staging], {
          encoding: "utf8", windowsHide: true, timeout: 120000, maxBuffer: 4 * 1024 * 1024,
        });
        if (extracted.error || extracted.status !== 0) throw new Error("runtime bundle extraction failed: " + (extracted.error || extracted.stderr));
        if (!hasApiBundle(path.join(staging, "api"))) throw new Error("runtime bundle is missing the Python API payload");
        const marker = { pluginVersion: "2.1.43", serviceVersion: requiredVersion, sha256: bundleSha256 };
        fs.writeFileSync(path.join(staging, "bundle-version.json"), JSON.stringify(marker, null, 2), "utf8");
        if (fs.existsSync(bundledRuntimeRoot)) {
          const backup = bundledRuntimeRoot + ".previous-" + Date.now();
          fs.renameSync(bundledRuntimeRoot, backup);
        }
        fs.renameSync(staging, bundledRuntimeRoot);
        return bundledApiRoot;
      } finally {
        try { fs.rmSync(archive, { force: true }); } catch (_) {}
        try { fs.rmSync(staging, { recursive: true, force: true }); } catch (_) {}
      }
    };

    const readHealth = () => new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      const request = http.get({ hostname: "127.0.0.1", port: 8765, path: "/health", timeout: 1200 }, (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
          if (body.length > 16384) request.destroy(new Error("health response too large"));
        });
        response.on("end", () => {
          try {
            const health = JSON.parse(body);
            finish(response.statusCode === 200 ? health : null);
          } catch (_) {
            finish(null);
          }
        });
      });
      request.on("error", () => finish(null));
      request.on("timeout", () => request.destroy(new Error("health request timed out")));
    });

    const stopOwnedService = () => new Promise((resolve) => {
      const request = http.request({
        hostname: "127.0.0.1", port: 8765, path: "/shutdown", method: "POST", timeout: 1500,
        headers: { Origin: "http://127.0.0.1:8765" },
      }, (response) => {
        response.resume();
        response.on("end", () => resolve(true));
      });
      request.on("error", () => resolve(false));
      request.on("timeout", () => request.destroy(new Error("shutdown request timed out")));
      request.end();
    });

    const waitUntilStopped = async () => {
      for (let attempt = 0; attempt < 20; attempt += 1) {
        if (!(await readHealth())) return true;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      return false;
    };

    if (shared.startPromise) return shared.startPromise;
    shared.startPromise = (async () => {
      fs.mkdirSync(pluginRoot, { recursive: true });
      let apiRoot = hasCompatibleLocalApiBundle()
        ? localApiRoot
        : (hasApiBundle(bundledApiRoot) && hasVerifiedRuntimeBundle() ? bundledApiRoot : null);
      if (!apiRoot) apiRoot = await installRuntimeBundle();
      const pythonExe = path.join(apiRoot, "runtime", "python.exe");
      const serviceScript = path.join(apiRoot, "omnifusion_service.py");
      let existing = await readHealth();
      if (existing?.status === "ok") {
        if (!samePath(existing.pluginRoot, pluginRoot)) {
          return {
            ready: false,
            reason: "service_owned_elsewhere",
            version: existing.version || null,
            servicePid: existing.servicePid || null,
          };
        }
        const settingsMatch = samePath(existing.mpvDirectory, mpvDir)
          && samePath(existing.studioExecutable, studioExe);
        if (existing.version === requiredVersion && settingsMatch) {
          return {
            ready: true,
            version: existing.version,
            alreadyRunning: true,
            mpvDirectory: existing.mpvDirectory,
            studioExecutable: existing.studioExecutable,
          };
        }
        await stopOwnedService();
        if (!(await waitUntilStopped())) {
          return { ready: false, reason: "owned_service_would_not_stop", version: existing.version || null };
        }
      }

      if (!fs.existsSync(pythonExe) || !fs.existsSync(serviceScript)) {
        return { ready: false, reason: "service_bundle_missing", pluginRoot };
      }
      fs.mkdirSync(dataRoot, { recursive: true });

      let spawnError = null;
      let child;
      try {
        child = spawn(pythonExe, [serviceScript], {
          cwd: apiRoot,
          detached: true,
          windowsHide: true,
          stdio: "ignore",
          env: {
            ...process.env,
            OMNIFUSION_PLUGIN_DIR: pluginRoot,
            OMNIFUSION_DATA_DIR: dataRoot,
            OMNIFUSION_MPV_DIR: mpvDir,
            OMNIFUSION_STUDIO_EXE: studioExe,
            OMNIFUSION_OXIDE_DIR: path.join(apiRoot, "oxideav"),
            OMNIFUSION_HARLETTY_DIR: path.join(apiRoot, "harletty", "v0.9.0"),
          },
        });
        child.once("error", (error) => { spawnError = error; });
        child.unref();
        shared.child = child;
      } catch (error) {
        return { ready: false, reason: "spawn_failed", message: String(error) };
      }

      for (let attempt = 0; attempt < 30; attempt += 1) {
        if (spawnError) {
          return { ready: false, reason: "spawn_failed", message: String(spawnError) };
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
        const health = await readHealth();
        if (health?.status === "ok") {
          if (health.version !== requiredVersion || !samePath(health.pluginRoot, pluginRoot)
              || !samePath(health.dataRoot, dataRoot) || !samePath(health.mpvDirectory, mpvDir)
              || !samePath(health.studioExecutable, studioExe)) {
            return { ready: false, reason: "incompatible_service", version: health.version || null };
          }
          return {
            ready: true,
            version: health.version,
            alreadyRunning: false,
            mpvDirectory: health.mpvDirectory,
            studioExecutable: health.studioExecutable,
          };
        }
      }
      return { ready: false, reason: "health_timeout" };
    })();

    try {
      return await shared.startPromise;
    } finally {
      shared.startPromise = null;
    }
  },
};`;
const SYNC_POLL_INTERVAL_MS = 100;
const CLOCK_POLL_INTERVAL_MS = 50;
const PLAY_INTENT_RECONCILIATION_WINDOW_MS = 1000;
const EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS = 15000;

let pollTimer = null;
let clockPollTimer = null;
let mediaInterceptTimer = null;
const mediaEventHandlers = new Map();
const nativeMediaClockEventNames = ["timeupdate", "seeking", "seeked", "playing", "pause"];
const nativeMediaClockObserver = { media: null, listeners: null, counts: null };
let enginePlayGeneration = 0;
let enginePlayPending = null;
let enginePlayPendingTrackId = null;
let enginePlayAbortController = null;
let enginePlayRequestId = null;
let enginePlayRequestTrackId = null;
let enginePlayAllowPaused = false;
let lastPlayAttempt = { trackId: null, at: 0 };
let lastMirroredPlaybackState = null;
let clockOrderingInstanceId = null;
let clockOrderingMpvInstanceId = null;
let clockOrderingSequence = -1;
let activeClockServiceInstanceId = null;
let activeClockMpvInstanceId = null;
let lastFastClockSample = null;
let desiredPlaybackIntent = null;
let mirroringTidalPlaybackState = false;
let playIntentChangedAt = 0;
let pendingExplicitPlayIntent = null;
let transportIntentRevision = 0;
const transportCommandQueues = new Map();
const transportCommandsInFlight = new Map();
let prepareNextSelectionPaused = false;
let autoAdvanceEnabled = true;
let lastAutoAdvancedPlaybackIdentity = null;
let lastAutoAdvanceUnavailableIdentity = null;
let pausedSelectionPending = null;
let pausedSelectionConfirmedTrackId = null;
let pausedSelectionUserPlayAuthorization = null;
let stalledPlayAuthorization = null;
let lastMirroredUiTime = null;
let lastMirroredUiDurationKey = null;
let lastMirroredUiDurationAttemptKey = null;
let lastMirroredUiDurationAttemptAt = -Infinity;
let lastTidalDurationMirrorDiagnostic = null;
let lastEngineStateCommandAt = 0;
let previousEngineSample = null;
let lastTidalSeekIntent = null;
let seekToEndPlaybackIdentity = null;
let seekToEndTargetPosition = null;
let seekToEndProgressObserved = false;
let pluginDisposed = false;
let currentFormatLabel = "Omniphony EX";
let currentStatusData = {};
let settingsButtonTimer = null;
let settingsUiVerified = false;
let nativePlayerCommandHookInstalled = false;
let nativePlayerCommandHookDiagnostic = "not installed";
let configLoadGeneration = 0;
const supportedFormats = ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"];
const SEEK_RECONCILIATION_WINDOW_MS = 2000;
const SEEK_RECONCILIATION_TOLERANCE_SECONDS = 0.25;
const REQUIRED_AUDIO_DEVICE = "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}";
const formatLabels = {
  EAC3_JOC: "Atmos JOC",
  AC4_IMS: "AC-4 IMS",
  HI_RES_LOSSLESS: "Hi-Res FLAC",
  LOSSLESS: "Lossless FLAC"
};
function permutations(items) {
  if (items.length <= 1) return [items.slice()];
  return items.flatMap((item, index) =>
    permutations(items.filter((_, candidate) => candidate !== index))
      .map((tail) => [item, ...tail])
  );
}
const priorityCompactLabels = {
  AC4_IMS: "AC-4",
  EAC3_JOC: "JOC",
  HI_RES_LOSSLESS: "Hi-Res",
  LOSSLESS: "Lossless",
};
const priorityPresets = permutations(supportedFormats).map((value) => ({
  label: value.map((format) => priorityCompactLabels[format]).join(" → "),
  fullLabel: value.map((format) => formatLabels[format]).join(" → "),
  value
}));
let currentConfig = {
  priority: ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
  audio_device: "auto"
};
let availableDevices = [];
let lastSettingsFetchFailure = null;
let lastDeviceFetchFailure = null;
let activeNativeQualityPill = null;
const nativeQualityPillStates = new Map();
let lastBadgeMountSignature = null;
let nativeTimelineProjection = null;
let nativeTimelineFrameId = null;
let nativeTimelineRangeObserver = null;
let nativeTimelineObservedFooter = null;
let nativeTimelineRangeObserverCleanup = null;
let lastNativeTimelineProjectionDiagnostic = null;
let nativeTimelineDispatchSequence = 0;
const nativeTimelineDispatchHistory = [];
const NATIVE_TIMELINE_DIAGNOSTIC_INTERVAL_MS = 2000;
const NATIVE_PRESENTATION_DIAGNOSTIC_INTERVAL_MS = 2000;
let lastNativePresentationDiagnosticAt = -Infinity;
let nativeProgressFillProjectionState = null;
let nativeTimelineDiagnosticWindow = {
  startedAt: null,
  previousFrameAt: null,
  frames: 0,
  syncWrites: 0,
  reduxUpdates: 0,
  sampleAgeCount: 0,
  sampleBracketCount: 0,
  preWriteTargetDifferenceCount: 0,
  postWriteReadbackDifferenceCount: 0,
  customSliderAriaValueSampleCount: 0,
  skipped: {},
  maxSampleAgeMs: 0,
  maxFrameIntervalMs: 0,
  maxPreWriteTargetDifferenceMs: 0,
  maxPostWriteReadbackDifferenceMs: 0,
  maxCustomSliderAriaTargetDifferenceMs: 0,
  domCommitCount: 0,
  maxDomCommitDelayMs: 0,
  maxDomSliderTargetDifferenceMs: 0,
  nativeFillProjectionCount: 0,
  maxNativeFillPreCorrectionErrorMs: 0,
  maxNativeFillPostCorrectionErrorMs: 0,
  lastNativeFillProjection: null,
  maxSampleBracketHalfWidthMs: 0,
  last: null,
  lastDomCommit: null,
};

export const unloads = new Set();

function schedulePluginTimeout(callback, delayMs) {
  let timer;
  const cleanup = () => clearTimeout(timer);
  timer = setTimeout(() => {
    unloads.delete(cleanup);
    callback();
  }, delayMs);
  unloads.add(cleanup);
  return timer;
}

function advanceRadiantLyricsObserverGeneration(expectedGeneration = null) {
  const key = Symbol.for("omniphony-ex.radiant-lyrics-observer-generation");
  const current = Number(globalThis[key]) || 0;
  if (expectedGeneration !== null && current !== expectedGeneration) return current;
  const next = current + 1;
  return Reflect.set(globalThis, key, next, globalThis) ? next : null;
}

function isRadiantLyricsObserverGenerationCurrent(generation) {
  const key = Symbol.for("omniphony-ex.radiant-lyrics-observer-generation");
  return Number(globalThis[key]) === generation;
}

const SERVICE_STARTUP_STATE_KEY = Symbol.for("omniphony-ex.service-startup-state");

function getConfiguredRuntimePaths() {
  try {
    return {
      mpvDir: String(localStorage.getItem("omniphony_mpv_path") || "").trim(),
      studioExe: String(localStorage.getItem("omniphony_studio_path") || "").trim(),
    };
  } catch (_) {
    return { mpvDir: "", studioExe: "" };
  }
}

async function ensureLocalOmniFusionService() {
  const sharedState = globalThis[SERVICE_STARTUP_STATE_KEY] ||
    (globalThis[SERVICE_STARTUP_STATE_KEY] = { starting: false, promise: null });
  if (sharedState.starting && sharedState.promise) return sharedState.promise;

  sharedState.starting = true;
  sharedState.promise = (async () => {
    const ipc = window?.__ipcRenderer;
    if (typeof ipc?.invoke !== "function") {
      console.error("[Omniphony EX] Cannot auto-start OmniFusion: TidaLuna native IPC is unavailable.");
      return false;
    }

    try {
      const channel = await ipc.invoke(
        "__Luna.registerNative",
        NATIVE_SERVICE_SUPERVISOR_MODULE_ID,
        NATIVE_SERVICE_SUPERVISOR_MODULE_CODE,
      );
      if (typeof channel !== "string" || !channel.startsWith("__LunaNative.")) {
        throw new Error("TidaLuna did not return the native service-supervisor IPC channel");
      }
      const result = await ipc.invoke(
        channel,
        "ensureService",
        REQUIRED_SERVICE_VERSION,
        getConfiguredRuntimePaths(),
      );
      if (result?.ready === true) {
        try {
          if (typeof result.mpvDirectory === "string") {
            localStorage.setItem("omniphony_mpv_path", result.mpvDirectory);
          }
          if (typeof result.studioExecutable === "string") {
            localStorage.setItem("omniphony_studio_path", result.studioExecutable);
          }
        } catch (_) {}
        console.info(`[Omniphony EX] OmniFusion service ${result.version} is healthy${result.alreadyRunning ? " (already running)" : " (started)"}.`);
        return true;
      }
      console.error("[Omniphony EX] Native service supervisor could not start OmniFusion:", result);
      return false;
    } catch (error) {
      console.error("[Omniphony EX] Native service startup request failed:", error);
      return false;
    }
  })();

  try {
    return await sharedState.promise;
  } finally {
    sharedState.starting = false;
    sharedState.promise = null;
  }
}

function onLoad() {
  pluginDisposed = false;
  console.log(`[Omniphony EX] Initializing ultra-native TidaLuna audio bridge (${PLUGIN_BUILD})...`);
  void ensureLocalOmniFusionService().catch((error) => {
    console.error("[Omniphony EX] Unexpected local service startup failure:", error);
  });
  tracePlaybackEvent("plugin-initialized");
  try {
    if (window.Syllables && typeof window.Syllables === "object") {
      window.Syllables.log = true;
    }
  } catch (_) {}
  // Observe the official lyrics integration without initiating or changing its
  // requests. This lets OmniFusion mirror the lyrics TIDAL/Radiant already
  // fetched into the native lyrics state while preserving the original response.
  if (!installRadiantLyricsFetchObserver()) {
    console.debug("[Omniphony EX] Native lyrics fetch observer unavailable; leaving TIDAL/Radiant untouched.");
  }

  // UI and audio interception must not wait for the local service to respond.
  interceptMediaElements();
  installLyricsClickListener();
  injectUI();
  startSyncLoop();
}

unloads.add(() => {
  pluginDisposed = true;
  console.log("[Omniphony EX] Cleaning up plugin resources...");
  // Pause background libmpv engine so audio stops when TIDAL closes or plugin unloads
  try {
    if (typeof navigator !== "undefined" && navigator.sendBeacon) {
      const blob = new Blob([JSON.stringify({
        trackId: currentStatusData?.trackId,
        playbackGeneration: currentStatusData?.playbackGeneration,
        requestId: currentStatusData?.requestId
      })], { type: 'application/json' });
      navigator.sendBeacon(`${API_BASE}/shutdown`, blob);
    } else {
      fetch(`${API_BASE}/shutdown`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          trackId: currentStatusData?.trackId,
          playbackGeneration: currentStatusData?.playbackGeneration,
          requestId: currentStatusData?.requestId
        }),
        keepalive: true
      }).catch(() => {});
    }
  } catch (_) {}
  if (pollTimer) clearInterval(pollTimer);
  if (clockPollTimer) clearInterval(clockPollTimer);
  if (mediaInterceptTimer) clearInterval(mediaInterceptTimer);
  if (settingsButtonTimer) clearInterval(settingsButtonTimer);
  clearNativeTimelineProjection();
  disconnectNativeTimelineRangeObserver();
  prepareNextSelectionPaused = false;
  pausedSelectionPending = null;
  pausedSelectionConfirmedTrackId = null;
  pausedSelectionUserPlayAuthorization = null;
  restoreNativeMediaPlayback();
  enginePlayGeneration += 1;
  enginePlayAbortController?.abort();
  enginePlayAbortController = null;
  configLoadGeneration += 1;
  restoreNativeQualityPills();
  const badge = document.getElementById("omniphony-badge");
  if (badge) badge.remove();
  const infoContainer = document.getElementById("omniphony-stream-info");
  if (infoContainer) infoContainer.remove();
  const panel = document.getElementById("omniphony-settings-panel");
  if (panel) {
    if (panel._omniphonyResizeHandler) {
      window.removeEventListener("resize", panel._omniphonyResizeHandler);
      delete panel._omniphonyResizeHandler;
    }
    panel.remove();
  }
});

async function requestWithTimeout(url, options = {}, timeoutMs = 3500) {
  const controller = new AbortController();
  const { signal: externalSignal, ...fetchOptions } = options;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...fetchOptions, signal: controller.signal });
  } finally {
    clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

function localRequestFailureSummary(stage, error) {
  const name = String(error?.name || "Error");
  if (Number.isInteger(error?.httpStatus)) return `${stage}: HTTP ${error.httpStatus}`;
  if (name === "AbortError") return `${stage}: request timed out or was canceled`;
  if (name === "TypeError") return `${stage}: browser network/CORS fetch failed`;
  if (name === "SyntaxError") return `${stage}: invalid JSON response`;
  if (error?.code === "INVALID_RESPONSE") return `${stage}: invalid response shape`;
  return `${stage}: ${name}`;
}

async function fetchConfig(retries = 2) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await requestWithTimeout(`${API_BASE}/config`, { cache: "no-store" });
      if (!res.ok) throw Object.assign(new Error("HTTP response"), { httpStatus: res.status });
      const loaded = await res.json();
      if (!Array.isArray(loaded.priority)
          || loaded.priority.length !== supportedFormats.length
          || !supportedFormats.every((format) => loaded.priority.includes(format))) {
        throw Object.assign(new Error("Invalid settings"), { code: "INVALID_RESPONSE" });
      }
      lastSettingsFetchFailure = null;
      return loaded;
    } catch (error) {
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
        continue;
      }
      lastSettingsFetchFailure = localRequestFailureSummary("GET /config", error);
      console.warn("[Omniphony EX] Could not load saved settings:", lastSettingsFetchFailure);
      return null;
    }
  }
}

async function checkPlaybackReadiness() {
  const badge = document.getElementById("omniphony-badge");
  const footer = getPlaybackFooter();
  let ancestor = badge?.parentElement || null;
  while (ancestor && ancestor !== footer) ancestor = ancestor.parentElement;
  if (!badge || badge.isConnected === false || !footer || ancestor !== footer) {
    return { ready: false, reason: "The Omniphony badge is not mounted in TIDAL's playback footer." };
  }
  if (!hasVisibleLayout(badge)) {
    return { ready: false, reason: "The Omniphony badge is mounted but has no visible footer layout." };
  }

  const controls = footer.querySelectorAll?.('button,[role="button"]');
  if (!controls?.length) {
    return { ready: false, reason: "TIDAL playback controls are not available in the verified footer." };
  }
  if (!Array.from(controls).some((control) => hasVisibleLayout(control))) {
    return { ready: false, reason: "TIDAL playback controls are present but not visibly laid out." };
  }
  if (!nativePlayerCommandHookInstalled) {
    return {
      ready: false,
      reason: `TIDAL's synchronous native-player guard is unavailable (${nativePlayerCommandHookDiagnostic}); playback remains blocked.`,
    };
  }

  try {
    const [healthResponse, config, devicesResponse] = await Promise.all([
      requestWithTimeout(`${API_BASE}/health`, { cache: "no-store" }, 2500),
      fetchConfig(),
      requestWithTimeout(`${API_BASE}/devices`, { cache: "no-store" }, 2500)
    ]);
    if (!healthResponse.ok) {
      return { ready: false, reason: `OmniFusion health check returned HTTP ${healthResponse.status}.` };
    }
    const health = await healthResponse.json();
    if (health?.version !== REQUIRED_SERVICE_VERSION) {
      return {
        ready: false,
        reason: `OmniFusion ${REQUIRED_SERVICE_VERSION} is required; the active service reports ${health?.version || "unknown"}.`
      };
    }
    if (!Array.isArray(health?.capabilities)
        || !health.capabilities.includes(REQUIRED_SERVICE_CAPABILITY)) {
      return {
        ready: false,
        reason: `OmniFusion does not advertise the required ${REQUIRED_SERVICE_CAPABILITY} protocol.`
      };
    }
    if (!config) return { ready: false, reason: "Saved RealMax priority could not be verified." };
    if (config.audio_device !== REQUIRED_AUDIO_DEVICE) {
      return { ready: false, reason: "The configured audio device does not match the required WASAPI endpoint." };
    }
    if (!devicesResponse.ok) {
      return { ready: false, reason: `OmniFusion device check returned HTTP ${devicesResponse.status}.` };
    }
    const devicesPayload = await devicesResponse.json();
    if (!Array.isArray(devicesPayload?.devices)) {
      return { ready: false, reason: "OmniFusion returned an invalid audio device list." };
    }
    if (!devicesPayload.devices.some((device) => device?.name === config.audio_device)) {
      return { ready: false, reason: "The configured WASAPI endpoint is not present in OmniFusion's active device list." };
    }
    return { ready: true, config };
  } catch (error) {
    const summary = localRequestFailureSummary("playback preflight", error);
    console.warn(`[Omniphony EX] Playback preflight failed: ${summary}`);
    return { ready: false, reason: `Playback preflight failed (${summary}).` };
  }
}

function reportPlaybackReadinessBlock(reason) {
  const formatElement = document.getElementById("omniphony-panel-format");
  const detailElement = document.getElementById("omniphony-panel-sub");
  const dot = document.getElementById("omniphony-panel-status-dot");
  if (formatElement) formatElement.innerText = "Playback blocked";
  if (detailElement) detailElement.innerText = reason;
  if (dot) {
    dot.style.background = "#ffc34d";
    dot.style.boxShadow = "0 0 8px #ffc34d";
  }
}

function verifySettingsPanelControls(panel) {
  const priority = document.getElementById("omniphony-priority-select");
  const priorityStatus = document.getElementById("omniphony-priority-save-status");
  const device = document.getElementById("omniphony-device-select");
  const format = document.getElementById("omniphony-panel-format");
  const detail = document.getElementById("omniphony-panel-sub");
  const dot = document.getElementById("omniphony-panel-status-dot");
  const expectedPriority = currentConfig.priority.join(",");
  const endpointDetected = availableDevices.some((item) => item.name === REQUIRED_AUDIO_DEVICE);
  return Boolean(
    panel?.isConnected
    && priority?.children?.length === 24
    && priority.disabled === false
    && priority.value === expectedPriority
    && priorityStatus?.innerText?.includes("Saved priority loaded")
    && device?.disabled === false
    && device.value === REQUIRED_AUDIO_DEVICE
    && currentConfig.audio_device === REQUIRED_AUDIO_DEVICE
    && endpointDetected
    && format && detail && dot
  );
}

async function fetchDevices(retries = 2) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await requestWithTimeout(`${API_BASE}/devices`, { cache: "no-store" });
      if (!res.ok) throw Object.assign(new Error("HTTP response"), { httpStatus: res.status });
      const data = await res.json();
      if (!Array.isArray(data.devices)) {
        throw Object.assign(new Error("Invalid device list"), { code: "INVALID_RESPONSE" });
      }
      availableDevices = data.devices;
      lastDeviceFetchFailure = null;
      const select = document.getElementById("omniphony-device-select");
      if (select) populateDeviceSelector(select);
      return true;
    } catch (error) {
      if (attempt < retries) {
        await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
        continue;
      }
      lastDeviceFetchFailure = localRequestFailureSummary("GET /devices", error);
      console.warn("[Omniphony EX] Could not load output devices:", lastDeviceFetchFailure);
      return false;
    }
  }
}

function populateDeviceSelector(select, preferredDevice = currentConfig.audio_device) {
  select.replaceChildren();
  const device = availableDevices.find((item) => item.name === REQUIRED_AUDIO_DEVICE);
  const option = document.createElement("option");
  option.value = REQUIRED_AUDIO_DEVICE;
  option.innerText = device?.description || "Voicemeeter Input · WASAPI (not detected)";
  option.disabled = !device;
  select.appendChild(option);
  select.value = REQUIRED_AUDIO_DEVICE;
  select.disabled = !device;
  select.title = "Locked to the requested Voicemeeter Input WASAPI endpoint; ASIO and system-default routing are disabled.";
}

/**
 * Keep TIDAL's native media element from producing a second audible stream.
 * libmpv remains the audio renderer; native TIDAL timing is mirrored separately.
 */
function preventNativeMediaPlayback(media) {
  if (mediaEventHandlers.has(media)) return;
  const ownPlayDescriptor = Object.getOwnPropertyDescriptor(media, "play");
  const originalPlay = media.play.bind(media);
  const blockUnexpectedPlay = () => {
    if (!pluginDisposed) media.pause();
  };
  media.play = function omniphonyPreventNativePlay() {
    if (pluginDisposed) return originalPlay();
    media.pause();
    return Promise.resolve();
  };
  media.addEventListener("play", blockUnexpectedPlay, true);
  mediaEventHandlers.set(media, { ownPlayDescriptor, blockUnexpectedPlay });
}

function detachNativeMediaClockObserver() {
  const { media, listeners } = nativeMediaClockObserver;
  if (media && listeners) {
    for (const [type, listener] of listeners) {
      media.removeEventListener(type, listener, true);
    }
  }
  nativeMediaClockObserver.media = null;
  nativeMediaClockObserver.listeners = null;
  nativeMediaClockObserver.counts = null;
}

function readNativeMediaClockDiagnostic({ resetCounts = false } = {}) {
  let candidates = [];
  try {
    candidates = Array.from(document.querySelectorAll("audio")).filter((media) =>
      media?.isConnected !== false
      && typeof media.currentSrc === "string"
      && media.currentSrc.length > 0
      && Number(media.readyState) >= 1,
    );
  } catch (_) {
    candidates = [];
  }

  const media = candidates.length === 1 ? candidates[0] : null;
  if (media !== nativeMediaClockObserver.media) {
    detachNativeMediaClockObserver();
    if (media) {
      const counts = Object.fromEntries(nativeMediaClockEventNames.map((type) => [
        type, { trusted: 0, synthetic: 0 },
      ]));
      const listeners = new Map();
      for (const type of nativeMediaClockEventNames) {
        const listener = (event) => {
          const bucket = counts[type];
          bucket[event?.isTrusted === true ? "trusted" : "synthetic"] += 1;
        };
        media.addEventListener(type, listener, true);
        listeners.set(type, listener);
      }
      nativeMediaClockObserver.media = media;
      nativeMediaClockObserver.listeners = listeners;
      nativeMediaClockObserver.counts = counts;
    }
  }

  let playStatePosition = null;
  let reduxPosition = null;
  let playState = null;
  try {
    playState = getTidalPlayState();
    playStatePosition = parsePlaybackPosition(playState?.currentTime);
    reduxPosition = parsePlaybackPosition(playState?.playTime);
  } catch (_) {
    // A TidaLuna getter can be unavailable during app/plugin startup.
  }

  const counts = nativeMediaClockObserver.counts
    ? Object.fromEntries(Object.entries(nativeMediaClockObserver.counts).map(([type, value]) => [
      type, { ...value },
    ]))
    : null;
  if (resetCounts && nativeMediaClockObserver.counts) {
    for (const value of Object.values(nativeMediaClockObserver.counts)) {
      value.trusted = 0;
      value.synthetic = 0;
    }
  }

  const snapshot = {
    candidateCount: candidates.length,
    uniqueLoadedElement: Boolean(media),
    playStateCurrentTime: playStatePosition,
    reduxPlayTime: reduxPosition,
    playState: String(playState?.state ?? "").toUpperCase() || null,
    media: media ? {
      currentTime: parsePlaybackPosition(media.currentTime),
      paused: typeof media.paused === "boolean" ? media.paused : null,
      seeking: typeof media.seeking === "boolean" ? media.seeking : null,
      readyState: Number.isFinite(Number(media.readyState)) ? Number(media.readyState) : null,
      events: counts,
    } : null,
  };
  return snapshot;
}

function restoreNativeMediaPlayback() {
  detachNativeMediaClockObserver();
  for (const [media, handlers] of mediaEventHandlers) {
    media.removeEventListener("play", handlers.blockUnexpectedPlay, true);
    if (handlers.ownPlayDescriptor) Object.defineProperty(media, "play", handlers.ownPlayDescriptor);
    else delete media.play;
    delete media.dataset.omniphonyHooked;
  }
  mediaEventHandlers.clear();
}

function handleNativePlayerPlay(onPlaybackChange) {
  stalledPlayAuthorization = null;
  const queueTrackId = getQueuedTrackId();
  tracePlaybackEvent("native-player-play-request", {
    queuedTrackId: queueTrackId,
    armedPausedSelection: prepareNextSelectionPaused,
    pendingPausedSelection: pausedSelectionPending?.trackId ?? null,
  });

  if (prepareNextSelectionPaused) {
    prepareNextSelectionPaused = false;
    pausedSelectionConfirmedTrackId = null;
    pausedSelectionUserPlayAuthorization = null;
    if (!queueTrackId) {
      pausedSelectionPending = null;
      setTidalPlaybackIntent(false);
      updatePausedSelectionUi("TIDAL queue identity unavailable. Native playback was blocked; no stream was prepared.");
    } else {
      pausedSelectionPending = {
        trackId: queueTrackId,
        expiresAt: Date.now() + 5000,
        expired: false,
      };
      desiredPlaybackIntent = false;
      setTidalPlaybackIntent(false);
      updatePausedSelectionUi("TIDAL selected the track. Native playback is blocked while OmniFusion prepares it paused.");
    }
    onPlaybackChange();
  } else if (pausedSelectionPending) {
    setTidalPlaybackIntent(false);
    onPlaybackChange();
  } else if (pausedSelectionConfirmedTrackId !== null) {
    const confirmedTrackId = pausedSelectionConfirmedTrackId;
    const queueMatches = String(queueTrackId ?? "") === confirmedTrackId;
    const diagnosticsReady = queueMatches && isPausedSelectionDiagnosticsReady(confirmedTrackId);
    if (!queueMatches) pausedSelectionUserPlayAuthorization = null;
    const authorized = diagnosticsReady && hasPausedSelectionUserPlayAuthorization(confirmedTrackId);
    if (!authorized) {
      setTidalPlaybackIntent(false);
      updatePausedSelectionUi(!queueMatches
        ? "TIDAL queue changed after selection verification. Playback remains paused until the selected track is verified again."
        : diagnosticsReady
          ? "Stream diagnostics are ready. Use TIDAL's Play control to start this track."
          : "The selected stream diagnostics are still loading. Playback remains paused.");
      onPlaybackChange();
      return;
    }
    pausedSelectionConfirmedTrackId = null;
    pausedSelectionUserPlayAuthorization = null;
    updatePausedSelectionUi();
    setTidalPlaybackIntent(true);
    onPlaybackChange();
  } else {
    // The native media.play request is intercepted before TIDAL updates its
    // PlayState, so this request itself is the authoritative user play intent.
    pausedSelectionConfirmedTrackId = null;
    pausedSelectionUserPlayAuthorization = null;
    updatePausedSelectionUi();
    setTidalPlaybackIntent(true);
    const snapshot = getTidalPlaybackSnapshot();
    const requestedTrackId = Number(queueTrackId ?? snapshot?.trackId);
    if (!Number.isSafeInteger(requestedTrackId) || requestedTrackId <= 0) {
      tracePlaybackEvent("native-player-play-missing-track", { queuedTrackId: queueTrackId });
      setTidalPlaybackIntent(false);
      updatePausedSelectionUi("TIDAL queue identity is unavailable. Playback could not be started.");
      onPlaybackChange();
      return;
    }
    const engineOwnsTrack = Number(currentStatusData?.trackId) === requestedTrackId
      && Boolean(currentStatusData?.requestId)
      && ["prepared", "playing", "paused"].includes(String(currentStatusData?.playbackState || "").toLowerCase());
    if (!engineOwnsTrack) {
      const playState = getTidalPlayState();
      const position = getTidalPosition(playState) ?? snapshot?.position ?? 0;
      void triggerEnginePlay(requestedTrackId, position, {
        title: snapshot?.title || "",
        artist: snapshot?.artist || "",
      });
    }
    onPlaybackChange();
  }
}

async function interceptNativePlayerCommands(onPlaybackChange) {
  const ipc = window.__ipcRenderer;
  if (typeof ipc?.invoke !== "function" || typeof ipc?.on !== "function") {
    nativePlayerCommandHookDiagnostic = "TidaLuna native-module IPC is unavailable.";
    console.warn(`[Omniphony EX] Native player guard unavailable: ${nativePlayerCommandHookDiagnostic}`);
    return false;
  }

  const token = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let channel = null;
  let removeListener = null;
  try {
    channel = await ipc.invoke("__Luna.registerNative", NATIVE_PLAYER_GUARD_MODULE_ID, NATIVE_PLAYER_GUARD_MODULE_CODE);
    if (typeof channel !== "string" || !channel.startsWith("__LunaNative.")) {
      throw new Error("TidaLuna did not return the native-module IPC channel");
    }
    removeListener = ipc.on("omniphony:native-player-play", () => handleNativePlayerPlay(onPlaybackChange));
    const result = await ipc.invoke(channel, "install", token);
    if (result?.installed !== true) throw new Error("TIDAL native-player guard did not confirm installation");
    if (pluginDisposed) {
      await ipc.invoke(channel, "uninstall", token);
      removeListener?.();
      nativePlayerCommandHookDiagnostic = "Plugin unloaded before native guard installation completed.";
      return false;
    }

    nativePlayerCommandHookInstalled = true;
    nativePlayerCommandHookDiagnostic = `${result.target}; ${result.strategy}.`;
    unloads.add(() => {
      removeListener?.();
      nativePlayerCommandHookInstalled = false;
      nativePlayerCommandHookDiagnostic = "not installed";
      void ipc.invoke(channel, "uninstall", token).catch((error) => {
        console.warn("[Omniphony EX] Could not restore TIDAL native-player handler:", error);
      });
    });
    console.info(`[Omniphony EX] Native TIDAL play guard installed: ${nativePlayerCommandHookDiagnostic}`);
    return true;
  } catch (error) {
    removeListener?.();
    nativePlayerCommandHookInstalled = false;
    nativePlayerCommandHookDiagnostic = `${error?.name || "Error"}: ${error?.message || String(error)}`;
    console.warn(`[Omniphony EX] Native player guard unavailable: ${nativePlayerCommandHookDiagnostic}`);
    return false;
  }
}

function interceptMediaElements() {
  if (mediaInterceptTimer) clearInterval(mediaInterceptTimer);
  mediaInterceptTimer = setInterval(() => {
    const elements = document.querySelectorAll("audio");
    elements.forEach((media) => {
      if (media.dataset.omniphonyHooked) return;
      media.dataset.omniphonyHooked = "true";

      preventNativeMediaPlayback(media);
    });
    readNativeMediaClockDiagnostic();
  }, 800);
}

function getTidalPlayState() {
  try {
    return window.luna?.lib?.PlayState || null;
  } catch (error) {
    return null;
  }
}

let radiantReduxInitialGetState = null;
let radiantSyntheticLyricsSnapshot = null;
const radiantLyricsRecoveryActivations = new Set();
let radiantLyricsActivation = { trackId: null, generation: 0 };
const radiantLyricsFetchObserver = {
  originalDescriptor: null,
  originalFetch: null,
  wrapper: null,
  cleanupRegistered: false,
  generation: null,
};
const radiantLyricsOverlay = {
  store: null,
  originalDescriptor: null,
  originalGetState: null,
  wrapper: null,
  deactivate: null,
  cleanupRegistered: false,
};

function captureRadiantSyntheticLyricsSnapshot(state, trackId) {
  const id = String(trackId ?? "");
  const trackEntries = state?.entities?.tracks?.entities;
  const lyricEntities = state?.entities?.lyrics?.entities;
  if (!trackEntries || !lyricEntities) return null;
  const candidates = id ? [[id, trackEntries[id]]] : Object.entries(trackEntries);
  for (const [trackId, track] of candidates) {
    if (!track) continue;
    const relationship = track.relationships?.lyrics?.data;
    if (!Array.isArray(relationship)) continue;
    const expectedLyricsId = `radiant-lyrics-${trackId}`;
    const reference = relationship.find((item) => String(item?.id ?? "") === expectedLyricsId);
    const entity = lyricEntities[expectedLyricsId];
    if (!reference || !entity || entity.type !== "lyrics") continue;
    return {
      trackId: String(trackId),
      lyricsId: expectedLyricsId,
      relationship: [{ ...reference }],
      entity,
    };
  }
  return null;
}

function overlayCapturedRadiantLyricsState(state, snapshot) {
  if (!snapshot || String(getCurrentTrackId() ?? "") !== snapshot.trackId
      || !state?.entities) return state;
  const entities = state.entities;
  const tracks = entities.tracks;
  const lyrics = entities.lyrics;
  const currentTrack = tracks?.entities?.[snapshot.trackId];
  if (!currentTrack || !tracks?.entities || !lyrics?.entities) return state;
  const currentRelationship = currentTrack.relationships?.lyrics?.data;
  const currentHasNativeLyrics = Array.isArray(currentRelationship)
    && currentRelationship.some((item) => String(item?.id ?? "") !== snapshot.lyricsId);
  if (currentHasNativeLyrics) return state;
  const relationship = Array.isArray(currentRelationship) && currentRelationship.length > 0
    ? currentRelationship
    : snapshot.relationship;
  const updatedTrack = {
    ...currentTrack,
    relationships: {
      ...currentTrack.relationships,
      lyrics: {
        ...currentTrack.relationships?.lyrics,
        data: relationship,
      },
    },
  };
  return {
    ...state,
    entities: {
      ...entities,
      tracks: {
        ...tracks,
        entities: { ...tracks.entities, [snapshot.trackId]: updatedTrack },
      },
      lyrics: {
        ...lyrics,
        ids: Array.isArray(lyrics.ids) && lyrics.ids.includes(snapshot.lyricsId)
          ? lyrics.ids
          : [...(Array.isArray(lyrics.ids) ? lyrics.ids : []), snapshot.lyricsId],
        entities: { ...lyrics.entities, [snapshot.lyricsId]: snapshot.entity },
      },
    },
  };
}

function getRadiantUnwrappedReduxState(store) {
  const getter = store === radiantLyricsOverlay.store
      && store?.getState === radiantLyricsOverlay.wrapper
    ? radiantLyricsOverlay.originalGetState
    : store?.getState;
  if (typeof getter !== "function") return null;
  try {
    return Reflect.apply(getter, store, []);
  } catch {
    return null;
  }
}

function getRadiantLyricsActivation(trackId) {
  const id = String(trackId ?? "");
  if (!id) return null;
  if (radiantLyricsActivation.trackId !== id) {
    radiantLyricsActivation = {
      trackId: id,
      generation: radiantLyricsActivation.generation + 1,
    };
  }
  return `${id}:${radiantLyricsActivation.generation}`;
}

function parseRadiantLyricsRequest(input, init = {}) {
  const rawUrl = typeof input === "string"
    ? input
    : input instanceof URL ? input.href : input?.url;
  if (typeof rawUrl !== "string") return null;
  const method = String(init?.method ?? input?.method ?? "GET").toUpperCase();
  if (method !== "GET") return null;
  let url;
  try {
    url = new URL(rawUrl, globalThis.location?.href ?? "https://desktop.tidal.com/");
  } catch {
    return null;
  }
  const allowed = url.protocol === "https:"
    && ((url.hostname === "api.radiant-lyrics.org" && url.pathname === "/rl-api")
    || (url.hostname === "rl-api.kineticsand.net" && url.pathname === "/lyrics"));
  if (!allowed) return null;
  const title = url.searchParams.get("title")?.trim() ?? "";
  const artist = url.searchParams.get("artist")?.trim() ?? "";
  if (!title || !artist) return null;
  return { title, artist };
}

function normalizeLyricsMatchText(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function normalizeRadiantLyricsComparisonTitle(value) {
  // TIDAL's Atmos catalog title may add edition labels that Radiant omits.
  // Strip only these known trailing catalog/format suffixes; keep mix/remix
  // names intact so a different recording cannot match by a loose prefix.
  return normalizeLyricsMatchText(value).replace(/(?:dolbyatmos|albumversion)+$/g, "");
}

function radiantLyricsTitlesMatch(requestedTitle, currentTitle) {
  const requested = normalizeLyricsMatchText(requestedTitle);
  const current = normalizeLyricsMatchText(currentTitle);
  if (!requested || !current) return false;
  if (requested === current) return true;
  const requestedBase = normalizeRadiantLyricsComparisonTitle(requestedTitle);
  const currentBase = normalizeRadiantLyricsComparisonTitle(currentTitle);
  return Boolean(requestedBase && currentBase) && requestedBase === currentBase;
}

function matchesCurrentRadiantLyricsRequest(request) {
  const footer = getPlaybackFooter();
  const meta = typeof getCurrentTrackMeta === "function" ? getCurrentTrackMeta() : {};
  const footerTitle = footer?.querySelector?.('a[href*="/track/"]')?.textContent?.trim() ?? "";
  const footerArtist = footer?.querySelector?.('a[href*="/artist/"]')?.textContent?.trim() ?? "";
  const title = footerTitle || String(meta?.title ?? "").trim();
  const artist = footerArtist || String(meta?.artist ?? "").trim();
  const requestedArtist = normalizeLyricsMatchText(request?.artist);
  const currentArtist = normalizeLyricsMatchText(artist);
  const titleMatches = radiantLyricsTitlesMatch(request?.title, title);
  const artistMatches = Boolean(currentArtist)
    && (requestedArtist === currentArtist
      || requestedArtist.includes(currentArtist)
      || currentArtist.includes(requestedArtist));
  const trackId = String(getCurrentTrackId() ?? "");
  return titleMatches && artistMatches && /^\d+$/.test(trackId) ? trackId : null;
}

function formatRadiantLyricsTimestamp(seconds) {
  const totalMs = Math.round(Number(seconds) * 1000);
  const minutes = Math.floor(totalMs / 60000);
  const wholeSeconds = Math.floor((totalMs % 60000) / 1000);
  const hundredths = Math.floor((totalMs % 1000) / 10);
  return `[${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}.${String(hundredths).padStart(2, "0")}]`;
}

function buildRadiantLyricsSnapshot(trackId, response) {
  const id = String(trackId ?? "");
  if (!/^\d+$/.test(id) || !response || !["Line", "Word"].includes(response.type)
      || !Array.isArray(response.data) || response.data.length === 0 || response.data.length > 2000) {
    return null;
  }
  const lines = [];
  for (const line of response.data) {
    const startTime = Number(line?.startTime);
    const text = typeof line?.romanized === "string" && line.romanized.trim()
      ? line.romanized.trim()
      : typeof line?.text === "string" ? line.text.trim() : "";
    if (!Number.isFinite(startTime) || startTime < 0 || !text || text.length > 4000) continue;
    lines.push({ startTime, text });
  }
  if (lines.length === 0 || lines.length < Math.ceil(response.data.length / 2)) return null;

  const lyricsId = `radiant-lyrics-${id}`;
  const text = lines.map((line) => line.text).join("\n");
  const lrcText = lines.map((line) => `${formatRadiantLyricsTimestamp(line.startTime)}${line.text}`).join("\n");
  return {
    source: "radiant-fetch-observer",
    trackId: id,
    lyricsId,
    relationship: [{ id: lyricsId, type: "lyrics" }],
    entity: {
      id: lyricsId,
      type: "lyrics",
      attributes: {
        text,
        lrcText,
        technicalStatus: "OK",
        provider: {
          source: "THIRD_PARTY",
          name: response.type === "Word" && response._synthesized ? "Radiant Lyrics (AI)" : "Radiant Lyrics",
          commonTrackId: "",
          lyricsId: "",
        },
        direction: "LEFT_TO_RIGHT",
      },
      relationships: {
        owners: { links: { self: `/lyrics/${lyricsId}/relationships/owners` } },
        track: { links: { self: `/lyrics/${lyricsId}/relationships/track` } },
      },
    },
  };
}

function notifyNativeRadiantLyricsChanged(trackId) {
  const store = window.luna?.core?.modules?.["@luna/core"]?.reduxStore
    ?? window.luna?.lib?.redux?.store
    ?? null;
  const state = getRadiantUnwrappedReduxState(store);
  if (String(getCurrentTrackId() ?? "") !== String(trackId)
      || state?.settings?.nowPlayingActiveView !== "lyrics") return false;
  const setActiveView = window.luna?.lib?.redux?.actions?.["settings/SET_NOW_PLAYING_ACTIVE_VIEW"];
  if (typeof setActiveView !== "function") return false;
  try {
    setActiveView("credits");
    schedulePluginTimeout(() => {
      if (pluginDisposed || String(getCurrentTrackId() ?? "") !== String(trackId)) return;
      const latestState = getRadiantUnwrappedReduxState(store);
      if (latestState?.settings?.nowPlayingActiveView === "credits") setActiveView("lyrics");
    }, 0);
    return true;
  } catch {
    return false;
  }
}

function projectRadiantApiLyrics(trackId, response) {
  const snapshot = buildRadiantLyricsSnapshot(trackId, response);
  if (!snapshot) return false;
  const store = window.luna?.core?.modules?.["@luna/core"]?.reduxStore
    ?? window.luna?.lib?.redux?.store
    ?? null;
  const state = getRadiantUnwrappedReduxState(store);
  const track = state?.entities?.tracks?.entities?.[snapshot.trackId];
  const relationship = track?.relationships?.lyrics?.data;
  if (!track || (Array.isArray(relationship)
      && relationship.some((item) => String(item?.id ?? "") !== snapshot.lyricsId))) return false;
  if (String(getCurrentTrackId() ?? "") !== snapshot.trackId) return false;

  radiantSyntheticLyricsSnapshot = snapshot;
  if (!installRadiantLyricsPersistenceOverlay(store)) {
    radiantSyntheticLyricsSnapshot = null;
    return false;
  }
  notifyNativeRadiantLyricsChanged(snapshot.trackId);
  console.info("[Omniphony EX] Projected validated Radiant timed lyrics into TIDAL's native lyrics state.");
  return true;
}

function installRadiantLyricsFetchObserver() {
  if (radiantLyricsFetchObserver.wrapper && globalThis.fetch === radiantLyricsFetchObserver.wrapper) return true;
  const originalFetch = globalThis.fetch;
  if (typeof originalFetch !== "function") return false;
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "fetch") ?? null;
  const generation = advanceRadiantLyricsObserverGeneration();
  if (generation === null) return false;
  const wrapper = function (...args) {
    let request = null;
    let requestTrackId = null;
    try {
      request = parseRadiantLyricsRequest(args[0], args[1]);
      requestTrackId = request ? matchesCurrentRadiantLyricsRequest(request) : null;
    } catch {}
    const result = Reflect.apply(originalFetch, this, args);
    if (!request || !requestTrackId || !isRadiantLyricsObserverGenerationCurrent(generation)) return result;
    return Promise.resolve(result).then((response) => {
      if (isRadiantLyricsObserverGenerationCurrent(generation)
          && response?.ok && typeof response.clone === "function") {
        try {
          void response.clone().json().then((payload) => {
            if (!isRadiantLyricsObserverGenerationCurrent(generation)
                || String(getCurrentTrackId() ?? "") !== requestTrackId) return;
            schedulePluginTimeout(() => {
              if (isRadiantLyricsObserverGenerationCurrent(generation)
                  && !pluginDisposed && String(getCurrentTrackId() ?? "") === requestTrackId) {
                projectRadiantApiLyrics(requestTrackId, payload);
              }
            }, 100);
          }).catch(() => {});
        } catch {}
      }
      return response;
    });
  };
  try {
    if (!Reflect.set(globalThis, "fetch", wrapper, globalThis) || globalThis.fetch !== wrapper) return false;
  } catch {
    return false;
  }
  radiantLyricsFetchObserver.originalDescriptor = originalDescriptor;
  radiantLyricsFetchObserver.originalFetch = originalFetch;
  radiantLyricsFetchObserver.wrapper = wrapper;
  radiantLyricsFetchObserver.generation = generation;
  if (!radiantLyricsFetchObserver.cleanupRegistered) {
    radiantLyricsFetchObserver.cleanupRegistered = true;
    unloads.add(() => {
      if (globalThis.fetch === radiantLyricsFetchObserver.wrapper) {
        if (radiantLyricsFetchObserver.originalDescriptor) {
          Object.defineProperty(globalThis, "fetch", radiantLyricsFetchObserver.originalDescriptor);
        } else {
          Reflect.deleteProperty(globalThis, "fetch");
        }
      }
      advanceRadiantLyricsObserverGeneration(generation);
      radiantLyricsFetchObserver.originalDescriptor = null;
      radiantLyricsFetchObserver.originalFetch = null;
      radiantLyricsFetchObserver.wrapper = null;
      radiantLyricsFetchObserver.cleanupRegistered = false;
      radiantLyricsFetchObserver.generation = null;
    });
  }
  return true;
}

function scheduleRadiantLyricsRecovery(trackId, state, hasSyntheticEntity, hasNativeLyrics) {
  const id = String(trackId ?? "");
  const snapshot = radiantSyntheticLyricsSnapshot;
  const relationship = state?.entities?.tracks?.entities?.[id]?.relationships?.lyrics?.data;
  const hasCompleteSyntheticRegistration = Boolean(hasSyntheticEntity
    && Array.isArray(relationship)
    && relationship.some((item) => String(item?.id ?? "") === `radiant-lyrics-${id}`));
  if (!id || !snapshot || snapshot.trackId !== id || hasCompleteSyntheticRegistration || hasNativeLyrics
      || snapshot.source === "radiant-fetch-observer") return false;
  if (state?.settings?.nowPlayingActiveView !== "lyrics") return false;
  const activation = getRadiantLyricsActivation(id);
  if (!activation || radiantLyricsRecoveryActivations.has(activation)) return false;

  const refreshLyrics = window.updateLyricsStyle;
  if (typeof refreshLyrics !== "function") return false;
  radiantLyricsRecoveryActivations.add(activation);
  if (radiantLyricsRecoveryActivations.size > 100) {
    radiantLyricsRecoveryActivations.delete(radiantLyricsRecoveryActivations.values().next().value);
  }

  schedulePluginTimeout(() => {
    if (pluginDisposed || String(getCurrentTrackId() ?? "") !== id) return;
    const store = window.luna?.core?.modules?.["@luna/core"]?.reduxStore
      ?? window.luna?.lib?.redux?.store
      ?? null;
    const latestState = getRadiantUnwrappedReduxState(store);
    if (latestState?.settings?.nowPlayingActiveView !== "lyrics") return;
    const latestTrack = latestState?.entities?.tracks?.entities?.[id];
    const latestRelationship = latestTrack?.relationships?.lyrics?.data;
    const latestSyntheticPresent = Boolean(
      latestState?.entities?.lyrics?.entities?.[`radiant-lyrics-${id}`]
      && Array.isArray(latestRelationship)
      && latestRelationship.some((item) => String(item?.id ?? "") === `radiant-lyrics-${id}`)
    );
    const latestNativeLyricsPresent = Array.isArray(latestRelationship)
      && latestRelationship.some((item) => String(item?.id ?? "") !== `radiant-lyrics-${id}`);
    const latestCompleteSyntheticRegistration = Boolean(latestSyntheticPresent
      && Array.isArray(latestRelationship)
      && latestRelationship.some((item) => String(item?.id ?? "") === `radiant-lyrics-${id}`));
    if (latestCompleteSyntheticRegistration || latestNativeLyricsPresent
        || window.updateLyricsStyle !== refreshLyrics) return;
    try {
      refreshLyrics();
      console.info("[Omniphony EX] Requested one Radiant Lyrics refresh after its captured synthetic entity disappeared.");
    } catch (error) {
      console.warn("[Omniphony EX] Radiant Lyrics refresh hook failed:", error?.name || "Error");
    }
  }, 50);
  return true;
}

unloads.add(() => {
  radiantLyricsRecoveryActivations.clear();
  radiantLyricsActivation = { trackId: null, generation: 0 };
});

function installRadiantLyricsPersistenceOverlay(store) {
  if (!store || typeof store.getState !== "function") return false;
  if (radiantLyricsOverlay.store === store && store.getState === radiantLyricsOverlay.wrapper) return true;
  if (radiantLyricsOverlay.wrapper) {
    radiantLyricsOverlay.deactivate?.();
    if (radiantLyricsOverlay.store?.getState === radiantLyricsOverlay.wrapper) {
      if (radiantLyricsOverlay.originalDescriptor) {
        Object.defineProperty(radiantLyricsOverlay.store, "getState", radiantLyricsOverlay.originalDescriptor);
      } else {
        Reflect.deleteProperty(radiantLyricsOverlay.store, "getState");
      }
    }
  }
  const originalDescriptor = Object.getOwnPropertyDescriptor(store, "getState") ?? null;
  if (originalDescriptor && (!Object.prototype.hasOwnProperty.call(originalDescriptor, "value")
      || originalDescriptor.writable !== true)) return false;
  const originalGetState = store.getState;
  let lastInputState = null;
  let lastSnapshot = null;
  let lastActiveTrackId = null;
  let lastOutputState = null;
  let reading = false;
  let active = true;
  const deactivate = () => { active = false; };
  const wrapper = function (...args) {
    // Track-ID fallback and other subscribers can read this same store while
    // this wrapper is projecting a snapshot. Return the raw Redux state for
    // that nested read instead of recursively re-entering the projection.
    if (!active || reading) return Reflect.apply(originalGetState, this, args);
    reading = true;
    try {
      const state = Reflect.apply(originalGetState, this, args);
      const activeTrackId = String(getCurrentTrackId() ?? "");
      const captured = captureRadiantSyntheticLyricsSnapshot(state, activeTrackId);
      if (captured) {
        const previous = radiantSyntheticLyricsSnapshot;
        const previousReference = previous?.relationship?.[0];
        const capturedReference = captured.relationship?.[0];
        const sameReference = previousReference && capturedReference
          && Object.keys(previousReference).length === Object.keys(capturedReference).length
          && Object.keys(previousReference).every((key) => Object.is(
            previousReference[key], capturedReference[key],
          ));
        if (!previous || previous.trackId !== captured.trackId
            || previous.lyricsId !== captured.lyricsId
            || previous.entity !== captured.entity || !sameReference) {
          radiantSyntheticLyricsSnapshot = captured;
        }
      }
      const snapshot = radiantSyntheticLyricsSnapshot;
      // Redux getState is also used as an external-store snapshot by React. Keep
      // the overlaid object referentially stable until either Redux or the
      // captured lyrics snapshot or active track actually changes; rebuilding it
      // on every read can trigger React's maximum-update-depth error (#185).
      if (state === lastInputState && snapshot === lastSnapshot
          && activeTrackId === lastActiveTrackId) return lastOutputState;
      lastInputState = state;
      lastSnapshot = snapshot;
      lastActiveTrackId = activeTrackId;
      lastOutputState = overlayCapturedRadiantLyricsState(state, snapshot);
      return lastOutputState;
    } finally {
      reading = false;
    }
  };
  try {
    if (originalDescriptor) {
      Object.defineProperty(store, "getState", { ...originalDescriptor, value: wrapper });
    } else if (!Reflect.set(store, "getState", wrapper, store)) {
      return false;
    }
  } catch {
    return false;
  }
  if (store.getState !== wrapper) {
    deactivate();
    return false;
  }
  radiantLyricsOverlay.store = store;
  radiantLyricsOverlay.originalDescriptor = originalDescriptor;
  radiantLyricsOverlay.originalGetState = originalGetState;
  radiantLyricsOverlay.wrapper = wrapper;
  radiantLyricsOverlay.deactivate = deactivate;
  if (!radiantLyricsOverlay.cleanupRegistered) {
    radiantLyricsOverlay.cleanupRegistered = true;
    unloads.add(() => {
      radiantLyricsOverlay.deactivate?.();
      if (radiantLyricsOverlay.store?.getState === radiantLyricsOverlay.wrapper) {
        if (radiantLyricsOverlay.originalDescriptor) {
          Object.defineProperty(radiantLyricsOverlay.store, "getState", radiantLyricsOverlay.originalDescriptor);
        } else {
          Reflect.deleteProperty(radiantLyricsOverlay.store, "getState");
        }
      }
      radiantLyricsOverlay.store = null;
      radiantLyricsOverlay.originalDescriptor = null;
      radiantLyricsOverlay.originalGetState = null;
      radiantLyricsOverlay.wrapper = null;
      radiantLyricsOverlay.deactivate = null;
      radiantLyricsOverlay.cleanupRegistered = false;
      radiantSyntheticLyricsSnapshot = null;
    });
  }
  return true;
}

// Diagnostic for Radiant Lyrics' synthetic native-lyrics registration.
// It inspects both the public TidaLuna store and the exact @luna/core export
// Radiant imports. The getter capability check is synchronous and reversible.
// It never dispatches actions or exposes track metadata/lyrics payloads.
function inspectRadiantLyricsReduxState(trackId) {
  const id = String(trackId ?? "");
  const exposedStore = window.luna?.lib?.redux?.store ?? null;
  const radiantCore = window.luna?.core?.modules?.["@luna/core"] ?? null;
  const radiantStore = radiantCore?.reduxStore ?? null;
  const radiantGetStateNow = radiantStore?.getState;
  if (radiantReduxInitialGetState === null && typeof radiantGetStateNow === "function") {
    radiantReduxInitialGetState = radiantGetStateNow;
  }

  function inspectStore(store) {
    const getState = store?.getState;
    const descriptor = store
      ? Object.getOwnPropertyDescriptor(store, "getState")
      : null;
    const snapshot = {
      storeAvailable: Boolean(store),
      getStateAvailable: typeof getState === "function",
      getStateOwnProperty: Boolean(descriptor),
      getStateWritable: descriptor && "writable" in descriptor
        ? Boolean(descriptor.writable)
        : null,
      getStateAssignmentWorks: null,
      getStateAssignmentRestored: null,
      storeExtensible: null,
      trackEntityPresent: null,
      trackLyricsCount: null,
      syntheticLyricsEntityPresent: null,
      errorType: null,
    };
    if (!id || typeof getState !== "function") return snapshot;
    getRadiantLyricsActivation(id);
    try {
      snapshot.storeExtensible = Object.isExtensible(store);
      const ownGetState = Object.getOwnPropertyDescriptor(store, "getState");
      const probeWrapper = (...args) => getState.apply(store, args);
      try {
        snapshot.getStateAssignmentWorks = Reflect.set(store, "getState", probeWrapper, store)
          && store.getState === probeWrapper;
      } finally {
        if (ownGetState) {
          Object.defineProperty(store, "getState", ownGetState);
        } else if (store.getState === probeWrapper) {
          Reflect.deleteProperty(store, "getState");
        }
        snapshot.getStateAssignmentRestored = store.getState === getState;
      }
      const state = getRadiantUnwrappedReduxState(store);
      const track = state?.entities?.tracks?.entities?.[id] ?? null;
      const relation = track?.relationships?.lyrics?.data;
      snapshot.trackEntityPresent = Boolean(track);
      snapshot.trackLyricsCount = Array.isArray(relation) ? relation.length : 0;
      snapshot.syntheticLyricsEntityPresent = Boolean(
        state?.entities?.lyrics?.entities?.[`radiant-lyrics-${id}`]
      );
      const capturedLyrics = captureRadiantSyntheticLyricsSnapshot(state, id);
      if (capturedLyrics) {
        radiantSyntheticLyricsSnapshot = capturedLyrics;
      } else if (radiantSyntheticLyricsSnapshot?.trackId !== id) {
        radiantSyntheticLyricsSnapshot = null;
      }
      const syntheticLyricsId = `radiant-lyrics-${id}`;
      const hasNativeLyrics = Array.isArray(relation)
        && relation.some((item) => String(item?.id ?? "") !== syntheticLyricsId);
      scheduleRadiantLyricsRecovery(
        id,
        state,
        snapshot.syntheticLyricsEntityPresent,
        hasNativeLyrics,
      );
    } catch (error) {
      snapshot.errorType = error?.name || "Error";
    }
    return snapshot;
  }

  const exposed = inspectStore(exposedStore);
  const sameStore = Boolean(radiantStore && radiantStore === exposedStore);
  const radiant = sameStore ? exposed : inspectStore(radiantStore);
  const radiantGetStateReferenceChanged = typeof radiantGetStateNow === "function"
    ? radiantReduxInitialGetState !== radiantGetStateNow
    : null;
  if (radiantGetStateReferenceChanged === true) {
    installRadiantLyricsPersistenceOverlay(radiantStore);
  }
  return {
    trackId: id || null,
    ...exposed,
    exposedStoreAvailable: Boolean(exposedStore),
    radiantCoreModuleAvailable: Boolean(radiantCore),
    radiantStoreAvailable: Boolean(radiantStore),
    radiantStoreSameAsExposed: radiantStore ? sameStore : null,
    radiantGetStateOwnProperty: radiantStore
      ? Boolean(Object.getOwnPropertyDescriptor(radiantStore, "getState"))
      : null,
    radiantGetStateReferenceChanged,
    radiantGetStateAvailable: radiant.getStateAvailable,
    radiantGetStateWritable: radiant.getStateWritable,
    radiantGetStateAssignmentWorks: radiant.getStateAssignmentWorks,
    radiantGetStateAssignmentRestored: radiant.getStateAssignmentRestored,
    radiantTrackEntityPresent: radiant.trackEntityPresent,
    radiantTrackLyricsCount: radiant.trackLyricsCount,
    radiantSyntheticLyricsEntityPresent: radiant.syntheticLyricsEntityPresent,
    radiantStoreErrorType: radiant.errorType,
    radiantLyricsSnapshotCaptured: radiantSyntheticLyricsSnapshot?.trackId === id,
    radiantLyricsPersistenceOverlayInstalled: Boolean(
      radiantLyricsOverlay.store && radiantLyricsOverlay.store.getState === radiantLyricsOverlay.wrapper
    ),
    radiantRetryHookAvailable: typeof window.updateLyricsStyle === "function",
  };
}

function parsePlaybackPosition(value) {
  if (value === undefined || value === null || value === "") return null;
  const position = Number(value);
  return Number.isFinite(position) && position >= 0 ? position : null;
}

function parseNonNegativeFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value : null;
}

function getMonotonicNow() {
  const now = globalThis.performance?.now?.();
  return Number.isFinite(Number(now)) ? Number(now) : Date.now();
}

function getTidalPosition(playState) {
  // TIDAL currently exposes `currentTime` as a stale zero in some PlayState
  // snapshots while `playTime` contains the advancing playback clock. Prefer
  // the live clock and fall back only when it is absent or invalid.
  for (const candidate of [playState?.playTime, playState?.currentTime]) {
    const position = parsePlaybackPosition(candidate);
    if (position !== null) return position;
  }
  return null;
}

// Temporary, privacy-bounded trace for the startup position reset. Deliberately
// excludes request IDs, titles, URLs, and credentials.
function tracePlaybackEvent(event, detail = {}) {
  try {
    const playState = getTidalPlayState();
    const engineStatus = currentStatusData || {};
    console.info(`[Omniphony EX][playback-trace] ${JSON.stringify({
      wallTime: new Date().toISOString(),
      monotonicMs: Math.round(performance.now()),
      event,
      trackId: getCurrentTrackId() ?? null,
      tidalState: String(playState?.state || "").toUpperCase() || null,
      tidalDesired: String(playState?.desiredState || "").toUpperCase() || null,
      tidalPosition: getTidalPosition(playState),
      engineTrackId: engineStatus.trackId ?? null,
      enginePosition: parsePlaybackPosition(engineStatus.timePos),
      enginePaused: engineStatus.paused ?? null,
      engineGeneration: engineStatus.playbackGeneration ?? null,
      ...detail
    })}`);
  } catch (error) {
    console.debug("[Omniphony EX][playback-trace] trace emission failed");
  }
}

function dispatchTidalAction(type, payload) {
  try {
    const action = window.luna?.lib?.redux?.actions?.[type];
    if (typeof action !== "function") return false;
    const dispatchedAtMs = getMonotonicNow();
    action(payload);
    if (type === "playbackControls/TIME_UPDATE") {
      const numericPosition = parsePlaybackPosition(payload);
      const activePlayer = window.luna?.lib?.PlayState?.activePlayer;
      if (numericPosition !== null && activePlayer && typeof activePlayer === "object") {
        try {
          activePlayer.currentTime = numericPosition;
        } catch (_) {}
      }
      nativeTimelineDispatchHistory.push({
        sequence: ++nativeTimelineDispatchSequence,
        position: numericPosition,
        atMs: dispatchedAtMs,
      });
      if (nativeTimelineDispatchHistory.length > 256) nativeTimelineDispatchHistory.shift();
    }
    return true;
  } catch (error) {
    console.warn(`[Omniphony EX] TidaLuna ${type} action unavailable:`, error);
    return false;
  }
}

function readTidalPlaybackDuration(trackId) {
  try {
    const controls = getTidalPlayState()?.playbackControls;
    const playbackContext = controls?.playbackContext;
    const activeTrackId = controls?.mediaProduct?.productId
      ?? playbackContext?.actualProductId
      ?? null;
    const duration = parsePlaybackPosition(playbackContext?.actualDuration);
    return {
      trackId: activeTrackId === null ? null : String(activeTrackId),
      trackMatches: activeTrackId !== null && String(activeTrackId) === String(trackId ?? ""),
      duration,
    };
  } catch (_) {
    return { trackId: null, trackMatches: false, duration: null };
  }
}

function mirrorEngineDurationToTidal(duration, trackId = getCurrentTrackId()) {
  const seconds = parsePlaybackPosition(duration);
  const selectedTrackId = String(trackId ?? "");
  const engineTrackId = String(currentStatusData?.trackId ?? "");
  if (!selectedTrackId || selectedTrackId !== engineTrackId
      || seconds === null || seconds <= 0) return false;

  const durationKey = `${selectedTrackId}:${seconds.toFixed(3)}`;
  const before = readTidalPlaybackDuration(selectedTrackId);
  if (!before.trackMatches) {
    lastTidalDurationMirrorDiagnostic = {
      trackId: selectedTrackId,
      actualTrackId: before.trackId,
      expectedSeconds: seconds,
      actualSeconds: before.duration,
      dispatchAttempted: false,
      verified: false,
      reason: "tidal-track-mismatch-or-unavailable",
    };
    return false;
  }
  if (before.duration !== null && Math.abs(before.duration - seconds) <= 0.001) {
    lastMirroredUiDurationKey = durationKey;
    lastTidalDurationMirrorDiagnostic = {
      trackId: selectedTrackId,
      actualTrackId: before.trackId,
      expectedSeconds: seconds,
      actualSeconds: before.duration,
      dispatchAttempted: false,
      verified: true,
      reason: "redux-readback-already-matched",
    };
    return true;
  }
  if (durationKey === lastMirroredUiDurationAttemptKey
      && getMonotonicNow() - lastMirroredUiDurationAttemptAt < 250) return false;

  lastMirroredUiDurationAttemptKey = durationKey;
  lastMirroredUiDurationAttemptAt = getMonotonicNow();
  const dispatched = dispatchTidalAction("playbackControls/SET_DURATION", seconds);
  const after = readTidalPlaybackDuration(selectedTrackId);
  const verified = dispatched && after.trackMatches
    && after.duration !== null && Math.abs(after.duration - seconds) <= 0.001;
  lastTidalDurationMirrorDiagnostic = {
    trackId: selectedTrackId,
    actualTrackId: after.trackId,
    expectedSeconds: seconds,
    actualSeconds: after.duration,
    dispatchAttempted: true,
    verified,
    reason: verified ? "redux-readback-matched" : "redux-readback-did-not-match",
  };
  if (!verified) return false;
  lastMirroredUiDurationKey = durationKey;
  return true;
}

function recordNativeTimelineDiagnostic(kind, details = {}, now = getMonotonicNow()) {
  const sample = nativeTimelineDiagnosticWindow;
  const at = Number(now);
  if (!Number.isFinite(at)) return;
  if (sample.startedAt === null) sample.startedAt = at;

  if (kind === "frame") {
    const isAnimationFrame = details.projectionSource === "raf";
    const frameIntervalMs = !isAnimationFrame || sample.previousFrameAt === null
      ? null : Math.max(0, at - sample.previousFrameAt);
    const sampleAgeMs = details.sampleAgeMs;
    const sampleBracketHalfWidthMs = details.sampleBracketHalfWidthMs;
    const preWriteTargetDifferenceMs = details.preWriteTargetDifferenceMs;
    const postWriteReadbackDifferenceMs = details.postWriteReadbackDifferenceMs;
    const customSliderAriaTargetDifferenceMs = details.customSliderAriaTargetDifferenceMs;
    if (isAnimationFrame) sample.frames += 1;
    else sample.syncWrites += 1;
    if (frameIntervalMs !== null) {
      sample.maxFrameIntervalMs = Math.max(sample.maxFrameIntervalMs, frameIntervalMs);
    }
    if (typeof sampleAgeMs === "number" && Number.isFinite(sampleAgeMs)) {
      sample.sampleAgeCount += 1;
      sample.maxSampleAgeMs = Math.max(sample.maxSampleAgeMs, sampleAgeMs);
    }
    if (typeof sampleBracketHalfWidthMs === "number"
        && Number.isFinite(sampleBracketHalfWidthMs)
        && sampleBracketHalfWidthMs >= 0) {
      sample.sampleBracketCount += 1;
      sample.maxSampleBracketHalfWidthMs = Math.max(
        sample.maxSampleBracketHalfWidthMs,
        sampleBracketHalfWidthMs,
      );
    }
    if (typeof preWriteTargetDifferenceMs === "number"
        && Number.isFinite(preWriteTargetDifferenceMs)) {
      sample.preWriteTargetDifferenceCount += 1;
      sample.maxPreWriteTargetDifferenceMs = Math.max(
        sample.maxPreWriteTargetDifferenceMs,
        Math.abs(preWriteTargetDifferenceMs),
      );
    }
    if (typeof postWriteReadbackDifferenceMs === "number"
        && Number.isFinite(postWriteReadbackDifferenceMs)) {
      sample.postWriteReadbackDifferenceCount += 1;
      sample.maxPostWriteReadbackDifferenceMs = Math.max(
        sample.maxPostWriteReadbackDifferenceMs,
        Math.abs(postWriteReadbackDifferenceMs),
      );
    }
    if (typeof customSliderAriaTargetDifferenceMs === "number"
        && Number.isFinite(customSliderAriaTargetDifferenceMs)) {
      sample.customSliderAriaValueSampleCount += 1;
      sample.maxCustomSliderAriaTargetDifferenceMs = Math.max(
        sample.maxCustomSliderAriaTargetDifferenceMs,
        Math.abs(customSliderAriaTargetDifferenceMs),
      );
    }
    if (isAnimationFrame) sample.previousFrameAt = at;
    sample.last = { ...details, frameAtMs: at, frameIntervalMs };
  } else if (kind === "dom-commit") {
    sample.domCommitCount += 1;
    if (typeof details.dispatchToCommitDelayMs === "number"
        && Number.isFinite(details.dispatchToCommitDelayMs)
        && details.dispatchToCommitDelayMs >= 0) {
      sample.maxDomCommitDelayMs = Math.max(
        sample.maxDomCommitDelayMs,
        details.dispatchToCommitDelayMs,
      );
    }
    if (typeof details.sliderTargetDifferenceMs === "number"
        && Number.isFinite(details.sliderTargetDifferenceMs)) {
      sample.maxDomSliderTargetDifferenceMs = Math.max(
        sample.maxDomSliderTargetDifferenceMs,
        Math.abs(details.sliderTargetDifferenceMs),
      );
    }
    sample.lastDomCommit = { ...details, atMs: at };
  } else if (kind === "native-fill") {
    sample.nativeFillProjectionCount += 1;
    if (typeof details.preCorrectionErrorMs === "number"
        && Number.isFinite(details.preCorrectionErrorMs)) {
      sample.maxNativeFillPreCorrectionErrorMs = Math.max(
        sample.maxNativeFillPreCorrectionErrorMs,
        Math.abs(details.preCorrectionErrorMs),
      );
    }
    if (typeof details.postCorrectionErrorMs === "number"
        && Number.isFinite(details.postCorrectionErrorMs)) {
      sample.maxNativeFillPostCorrectionErrorMs = Math.max(
        sample.maxNativeFillPostCorrectionErrorMs,
        Math.abs(details.postCorrectionErrorMs),
      );
    }
    sample.lastNativeFillProjection = { ...details, atMs: at };
  } else if (kind === "redux") {
    sample.reduxUpdates += 1;
    sample.lastRedux = { ...details, atMs: at };
  } else if (kind === "skip") {
    const reason = String(details.reason || "unknown");
    sample.skipped[reason] = (sample.skipped[reason] || 0) + 1;
    sample.lastSkip = { reason, atMs: at, sequence: details.sequence ?? null };
  }

  if (at - sample.startedAt < NATIVE_TIMELINE_DIAGNOSTIC_INTERVAL_MS) return;
  const nativeMediaClock = readNativeMediaClockDiagnostic({ resetCounts: true });
  console.info(`[Omniphony EX][timeline-diag] ${JSON.stringify({
    windowMs: at - sample.startedAt,
    frames: sample.frames,
    syncWrites: sample.syncWrites,
    reduxUpdates: sample.reduxUpdates,
    sampleAgeCount: sample.sampleAgeCount,
    sampleBracketCount: sample.sampleBracketCount,
    preWriteTargetDifferenceCount: sample.preWriteTargetDifferenceCount,
    postWriteReadbackDifferenceCount: sample.postWriteReadbackDifferenceCount,
    customSliderAriaValueSampleCount: sample.customSliderAriaValueSampleCount,
    skipped: sample.skipped,
    maxSampleAgeMs: sample.sampleAgeCount ? Number(sample.maxSampleAgeMs.toFixed(3)) : null,
    maxFrameIntervalMs: sample.frames > 1 ? Number(sample.maxFrameIntervalMs.toFixed(3)) : null,
    maxSampleBracketHalfWidthMs: sample.sampleBracketCount
      ? Number(sample.maxSampleBracketHalfWidthMs.toFixed(3)) : null,
    maxPreWriteTargetDifferenceMs: sample.preWriteTargetDifferenceCount
      ? Number(sample.maxPreWriteTargetDifferenceMs.toFixed(3)) : null,
    // This confirms only that the range input accepted the assigned value; it
    // is deliberately not an independent playback synchronization metric.
    maxPostWriteReadbackDifferenceMs: sample.postWriteReadbackDifferenceCount
      ? Number(sample.maxPostWriteReadbackDifferenceMs.toFixed(3)) : null,
    maxCustomSliderAriaTargetDifferenceMs: sample.customSliderAriaValueSampleCount
      ? Number(sample.maxCustomSliderAriaTargetDifferenceMs.toFixed(3)) : null,
    domCommitCount: sample.domCommitCount,
    maxDomCommitDelayMs: sample.domCommitCount
      ? Number(sample.maxDomCommitDelayMs.toFixed(3)) : null,
    maxDomSliderTargetDifferenceMs: sample.domCommitCount
      ? Number(sample.maxDomSliderTargetDifferenceMs.toFixed(3)) : null,
    nativeFillProjectionCount: sample.nativeFillProjectionCount,
    maxNativeFillPreCorrectionErrorMs: sample.nativeFillProjectionCount
      ? Number(sample.maxNativeFillPreCorrectionErrorMs.toFixed(3)) : null,
    maxNativeFillPostCorrectionErrorMs: sample.nativeFillProjectionCount
      ? Number(sample.maxNativeFillPostCorrectionErrorMs.toFixed(6)) : null,
    nativeMediaClock,
    last: sample.last,
    lastDomCommit: sample.lastDomCommit,
    lastNativeFillProjection: sample.lastNativeFillProjection,
    lastRedux: sample.lastRedux ?? null,
    lastSkip: sample.lastSkip ?? null,
  })}`);
  nativeTimelineDiagnosticWindow = {
    startedAt: at,
    previousFrameAt: sample.previousFrameAt,
    frames: 0,
    syncWrites: 0,
    reduxUpdates: 0,
    sampleAgeCount: 0,
    sampleBracketCount: 0,
    preWriteTargetDifferenceCount: 0,
    postWriteReadbackDifferenceCount: 0,
    customSliderAriaValueSampleCount: 0,
    skipped: {},
    maxSampleAgeMs: 0,
    maxFrameIntervalMs: 0,
    maxPreWriteTargetDifferenceMs: 0,
    maxPostWriteReadbackDifferenceMs: 0,
    maxCustomSliderAriaTargetDifferenceMs: 0,
    domCommitCount: 0,
    maxDomCommitDelayMs: 0,
    maxDomSliderTargetDifferenceMs: 0,
    nativeFillProjectionCount: 0,
    maxNativeFillPreCorrectionErrorMs: 0,
    maxNativeFillPostCorrectionErrorMs: 0,
    lastNativeFillProjection: null,
    maxSampleBracketHalfWidthMs: 0,
    last: null,
    lastDomCommit: null,
  };
}

function setTidalUiPlaybackState(state) {
  const playState = getTidalPlayState();
  const expected = String(state || "").toUpperCase();
  if (expected !== "PLAYING" && expected !== "PAUSED") return false;
  mirroringTidalPlaybackState = true;
  try {
    if (String(playState?.desiredState || "").toUpperCase() !== expected) {
      dispatchTidalAction("playbackControls/SET_DESIRED_PAUSE_STATE", expected !== "PLAYING");
    }
    if (String(playState?.state || "").toUpperCase() !== expected) {
      dispatchTidalAction("playbackControls/SET_PLAYBACK_STATE", expected);
    }
  } finally {
    mirroringTidalPlaybackState = false;
  }
  return String(playState?.desiredState || "").toUpperCase() === expected
    && String(playState?.state || "").toUpperCase() === expected;
}

function setTidalPlaybackIntent(playing) {
  const nextIntent = Boolean(playing);
  transportIntentRevision += 1;
  if (nextIntent) {
    playIntentChangedAt = Date.now();
    const trackId = String(getCurrentTrackId() ?? "");
    pendingExplicitPlayIntent = trackId
      ? { trackId, startedAt: playIntentChangedAt }
      : null;
  } else {
    pendingExplicitPlayIntent = null;
  }
  desiredPlaybackIntent = nextIntent;
  const state = desiredPlaybackIntent ? "PLAYING" : "PAUSED";
  setTidalUiPlaybackState(state);
}

function hasRecentExplicitPlayIntent(trackId) {
  return desiredPlaybackIntent === true
    && enginePlayAllowPaused !== true
    && playIntentChangedAt > 0
    && Date.now() - playIntentChangedAt <= PLAY_INTENT_RECONCILIATION_WINDOW_MS
    && String(trackId ?? "") === String(getCurrentTrackId() ?? "");
}

function hasPendingExplicitPlayIntent(trackId, status) {
  const pending = pendingExplicitPlayIntent;
  const key = String(trackId ?? "");
  if (!pending) return false;
  if (desiredPlaybackIntent !== true
      || enginePlayAllowPaused === true
      || !key
      || pending.trackId !== key
      || String(getCurrentTrackId() ?? "") !== key) {
    pendingExplicitPlayIntent = null;
    return false;
  }
  if (Date.now() - pending.startedAt > EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS) {
    pendingExplicitPlayIntent = null;
    tracePlaybackEvent("explicit-play-handoff-expired", { trackId: key });
    return false;
  }
  if (Number(status?.trackId) === Number(key)
      && status?.playbackState === "playing"
      && status?.paused === false) {
    pendingExplicitPlayIntent = null;
    return false;
  }
  if (Number(status?.trackId) === Number(key)
      && ["ended", "failed", "stopped"].includes(String(status?.playbackState || "").toLowerCase())) {
    pendingExplicitPlayIntent = null;
    return false;
  }
  return true;
}

function consumePausedSelectionPreparation() {
  if (!prepareNextSelectionPaused) return false;
  prepareNextSelectionPaused = false;
  setTidalPlaybackIntent(false);
  return true;
}

function getQueuedTrackId(playState = getTidalPlayState()) {
  const queue = playState?.playQueue;
  const index = Number(queue?.currentIndex);
  if (!Number.isSafeInteger(index) || index < 0) return null;
  const id = queue?.elements?.[index]?.mediaItemId;
  return /^\d+$/.test(String(id ?? "")) ? String(id) : null;
}

function holdSyncForPausedSelection(trackId, now = Date.now()) {
  const pending = pausedSelectionPending;
  if (!pending) return false;
  if (String(trackId ?? "") === pending.trackId) {
    pausedSelectionConfirmedTrackId = pending.trackId;
    pausedSelectionUserPlayAuthorization = null;
    pausedSelectionPending = null;
    updatePausedSelectionUi("TIDAL confirmed the selected track. Preparing its stream paused.");
    return false;
  }
  if (!pending.expired && now >= pending.expiresAt) {
    pending.expired = true;
    updatePausedSelectionUi("TIDAL has not confirmed the requested track. Playback remains paused; no stream was prepared.");
  }
  return true;
}

function recordPausedSelectionUserPlayAuthorization(event) {
  if (event?.isTrusted !== true || pausedSelectionConfirmedTrackId === null) return false;
  const trackId = pausedSelectionConfirmedTrackId;
  const control = event.target?.closest?.("button, [role='button']");
  if (!control || String(getQueuedTrackId() ?? "") !== trackId
      || !isPausedSelectionDiagnosticsReady(trackId)) return false;
  const labels = [
    control.getAttribute?.("aria-label"),
    control.getAttribute?.("title"),
    control.getAttribute?.("data-testid"),
    control.getAttribute?.("data-test-id"),
    control.title,
    control.textContent,
  ].map((value) => String(value ?? "").trim().toLowerCase());
  if (!labels.some((value) => /^(play|resume|play button|play-button)$/.test(value))) return false;
  pausedSelectionUserPlayAuthorization = { trackId, expiresAt: Date.now() + 1500 };
  return true;
}

function getPlayControlTrackId(control, trackRow) {
  const candidates = [control, trackRow].filter(Boolean);
  for (const element of candidates) {
    for (const attribute of ["data-track-id", "data-media-item-id", "data-item-id"]) {
      const value = String(element.getAttribute?.(attribute) || "").trim();
      if (/^\d+$/.test(value)) return value;
    }
  }
  const link = trackRow?.querySelector?.('a[href*="/track/"]');
  const href = String(link?.getAttribute?.("href") || link?.href || "");
  return href.match(/\/track\/(\d+)(?:[/?#]|$)/)?.[1] || null;
}

function recordStalledPlayAuthorization(event) {
  if (event?.isTrusted !== true || prepareNextSelectionPaused || pausedSelectionPending) return false;
  const control = event.target?.closest?.("button, [role='button']");
  if (!control) return false;
  const labels = [
    control.getAttribute?.("aria-label"), control.getAttribute?.("title"),
    control.getAttribute?.("data-testid"), control.getAttribute?.("data-test-id"),
    control.title, control.textContent,
  ].map((value) => String(value ?? "").trim().toLowerCase());
  if (!labels.some((value) => /^(play|resume|play button|play-button)$/.test(value))) return false;

  const trackRow = control.closest?.(
    "tr, [role='row'], [data-test*='track-row'], [data-testid*='track-row'], [class*='trackRow']",
  ) || null;
  const selectedTrackId = String(getQueuedTrackId() ?? getCurrentTrackId() ?? "");
  stalledPlayAuthorization = {
    trackId: getPlayControlTrackId(control, trackRow),
    allowSelectionChange: Boolean(trackRow),
    priorTrackId: selectedTrackId,
    rowText: String(trackRow?.innerText || trackRow?.textContent || "")
      .replace(/\s+/g, " ").trim().toLowerCase(),
    expiresAt: Date.now() + 2000,
  };
  tracePlaybackEvent("stalled-play-gesture-captured", {
    rowControl: Boolean(trackRow),
    trackBound: Boolean(stalledPlayAuthorization.trackId),
  });
  return true;
}

function hasStalledPlayAuthorization(trackId, now = Date.now(), trackMeta = null) {
  const authorization = stalledPlayAuthorization;
  if (!authorization) return false;
  if (now > authorization.expiresAt) {
    stalledPlayAuthorization = null;
    return false;
  }
  const key = String(trackId ?? "");
  if (authorization.trackId) return authorization.trackId === key;
  if (!authorization.allowSelectionChange) return authorization.priorTrackId === key;
  if (authorization.priorTrackId === key) return true;
  const title = String(trackMeta?.title || "").trim().toLowerCase();
  const artist = String(trackMeta?.artist || "").trim().toLowerCase();
  return Boolean(title && artist && authorization.rowText.includes(title)
    && authorization.rowText.includes(artist));
}

function consumeStalledPlayAuthorization(trackId, now = Date.now(), trackMeta = null) {
  if (!hasStalledPlayAuthorization(trackId, now, trackMeta)) return false;
  stalledPlayAuthorization = null;
  return true;
}

function hasPausedSelectionUserPlayAuthorization(trackId, now = Date.now()) {
  const authorization = pausedSelectionUserPlayAuthorization;
  if (!authorization) return false;
  if (now > authorization.expiresAt || authorization.trackId !== String(trackId ?? "")) {
    pausedSelectionUserPlayAuthorization = null;
    return false;
  }
  return true;
}

function isPausedSelectionDiagnosticsReady(trackId) {
  const status = currentStatusData;
  if (!status
      || String(status.trackId ?? "") !== String(trackId ?? "")
      || status.paused !== true
      || !supportedFormats.includes(status.format)
      || !status.sourceCodec
      || !status.activeDecoder
      || !Number.isFinite(Number(status.sourceBitrate))
      || !Number.isFinite(Number(status.sourceSampleRate))
      || !Number.isFinite(Number(status.sourceChannels))
      || Number(status.sourceBitrate) <= 0
      || Number(status.sourceSampleRate) <= 0
      || Number(status.sourceChannels) <= 0
      || Number(status.streamProxyFailures ?? 0) !== 0
      || status.lastFailure) return false;
  if (status.format === "EAC3_JOC" && status.omniphonyRoute?.ready !== true) return false;
  return true;
}

function mirrorEngineStateToTidal(status, tidalTrackId, tidalPosition, suppressClock = false) {
  if (!status || !Number.isSafeInteger(Number(tidalTrackId))
      || Number(status.trackId) !== Number(tidalTrackId)) return false;
  mirrorEnginePlaybackStateToTidal(status);
  if (!suppressClock) mirrorEngineClockToTidal(status, tidalTrackId, tidalPosition);
  return true;
}

function isEnginePlayStartupPending(trackId) {
  const key = String(trackId ?? "");
  if (!key || desiredPlaybackIntent !== true || enginePlayAllowPaused === true) return false;
  return (String(enginePlayPendingTrackId ?? "") === key && Boolean(enginePlayPending))
    || (String(enginePlayRequestTrackId ?? "") === key && Boolean(enginePlayRequestId));
}

function mirrorEnginePlaybackStateToTidal(status) {
  if (!status) return false;
  // /play deliberately prepares libmpv paused. Keep TIDAL's native Play intent
  // visible until triggerEnginePlay confirms that the prepared stream resumed.
  if (isEnginePlayStartupPending(status.trackId)) {
    setTidalUiPlaybackState("PLAYING");
    return true;
  }
  if (status.paused === true && hasPendingExplicitPlayIntent(status.trackId, status)) {
    setTidalUiPlaybackState("PLAYING");
    return true;
  }
  // A paused diagnostic preparation is resumed directly by the sync loop,
  // without an in-flight /play request to satisfy the startup guard above.
  // Do not mirror that still-paused MPV snapshot back into TIDAL after the
  // operator has just pressed Play; doing so cancels the very intent that
  // should cause /resume. Keep the native UI on Play briefly while the
  // already-prepared engine accepts its resume command.
  const recentExplicitPlay = status.paused === true
    && hasRecentExplicitPlayIntent(status.trackId);
  if (recentExplicitPlay) {
    setTidalUiPlaybackState("PLAYING");
    return true;
  }
  const enginePlaying = status.playbackState === "playing" && status.paused === false;
  const uiState = desiredPlaybackIntent === false
    ? "PAUSED"
    : enginePlaying ? "PLAYING" : "PAUSED";
  setTidalUiPlaybackState(uiState);
  return true;
}

function mirrorEngineClockToTidal(status, tidalTrackId, tidalPosition) {
  if (!status || !Number.isSafeInteger(Number(tidalTrackId))
      || Number(status.trackId) !== Number(tidalTrackId)) return false;
  const position = getEnginePositionAtCurrentTime(status);
  const observedTidalPosition = parsePlaybackPosition(tidalPosition);
  // Publish moving-clock corrections at millisecond resolution. The active
  // animation-frame projection below keeps TIDAL's native timeline and lyrics
  // current between these service samples.
  const clockThreshold = 0.001;
  const tidalClockDiverged = observedTidalPosition !== null
    && position !== null
    && Math.abs(position - observedTidalPosition) >= clockThreshold;
  const positionChanged = lastMirroredUiTime === null
    || (position !== null && Math.abs(position - lastMirroredUiTime) >= clockThreshold);
  if (position !== null
      && (tidalClockDiverged || positionChanged)) {
    if (positionChanged) {
      tracePlaybackEvent("engine-to-tidal-time-update", {
        fromPosition: observedTidalPosition,
        toPosition: position
      });
    }
    if (dispatchTidalAction("playbackControls/TIME_UPDATE", position)) {
      lastMirroredUiTime = position;
      recordNativeTimelineDiagnostic("redux", {
        action: "playbackControls/TIME_UPDATE",
        position,
        thresholdMs: clockThreshold * 1000,
      });
    }
  }
  return true;
}

function summarizeNativePresentationElement(element, path) {
  if (!element) return null;
  const round = (value) => Number.isFinite(Number(value))
    ? Number(Number(value).toFixed(3)) : null;
  let rect = null;
  let style = {};
  try {
    const bounds = element.getBoundingClientRect?.();
    if (bounds) {
      rect = {
        left: round(bounds.left),
        top: round(bounds.top),
        right: round(bounds.right),
        bottom: round(bounds.bottom),
        width: round(bounds.width),
        height: round(bounds.height),
      };
    }
  } catch (_) {}
  try {
    const computed = window.getComputedStyle?.(element) ?? {};
    style = {
      display: String(computed.display ?? ""),
      visibility: String(computed.visibility ?? ""),
      position: String(computed.position ?? ""),
      width: String(computed.width ?? ""),
      height: String(computed.height ?? ""),
      left: String(computed.left ?? ""),
      transform: String(computed.transform ?? ""),
      inlineTransform: String(element.style?.getPropertyValue?.("transform")
        ?? element.style?.transform ?? ""),
      backgroundColor: String(computed.backgroundColor ?? ""),
      color: String(computed.color ?? ""),
      textShadow: String(computed.textShadow ?? ""),
      fontWeight: String(computed.fontWeight ?? ""),
      opacity: String(computed.opacity ?? ""),
      transitionProperty: String(computed.transitionProperty ?? ""),
      transitionDuration: String(computed.transitionDuration ?? ""),
      animationName: String(computed.animationName ?? ""),
      animationDuration: String(computed.animationDuration ?? ""),
    };
  } catch (_) {}
  const classValue = typeof element.className === "string"
    ? element.className : String(element.getAttribute?.("class") ?? "");
  const ariaCurrent = String(element.getAttribute?.("aria-current") ?? "");
  const ariaSelected = String(element.getAttribute?.("aria-selected") ?? "");
  const dataState = String(element.getAttribute?.("data-state") ?? "");
  const dataCurrent = String(element.getAttribute?.("data-current") ?? "");
  return {
    path: String(path ?? ""),
    tag: String(element.tagName ?? "").toLowerCase(),
    id: String(element.id ?? ""),
    className: classValue.slice(0, 240),
    role: String(element.getAttribute?.("role") ?? ""),
    dataTest: String(element.getAttribute?.("data-test") ?? ""),
    dataTestId: String(element.getAttribute?.("data-testid") ?? ""),
    ariaValueNow: String(element.getAttribute?.("aria-valuenow") ?? ""),
    ariaValueMin: String(element.getAttribute?.("aria-valuemin") ?? ""),
    ariaValueMax: String(element.getAttribute?.("aria-valuemax") ?? ""),
    ariaCurrent,
    ariaSelected,
    dataState,
    dataCurrent,
    activeHint: /active|current|highlight|selected/i.test(classValue)
      || ariaCurrent === "true" || ariaSelected === "true" || dataState === "active" || dataCurrent === "true",
    rect,
    style,
  };
}

function parseNativeTransformMatrix(value) {
  const transform = String(value ?? "none").trim();
  if (!transform || transform === "none") {
    return { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
  }
  const match = transform.match(
    /^matrix\(\s*([-+\deE.]+)\s*,\s*([-+\deE.]+)\s*,\s*([-+\deE.]+)\s*,\s*([-+\deE.]+)\s*,\s*([-+\deE.]+)\s*,\s*([-+\deE.]+)\s*\)$/,
  );
  if (!match) return null;
  const values = match.slice(1).map(Number);
  if (!values.every(Number.isFinite)) return null;
  const [a, b, c, d, e, f] = values;
  const tolerance = 1e-4;
  if (Math.abs(a - 1) > tolerance
      || Math.abs(b) > tolerance
      || Math.abs(c) > tolerance
      || Math.abs(d - 1) > tolerance
      || Math.abs(f) > tolerance) {
    return null;
  }
  return { a, b, c, d, e, f };
}

function readInlineTransform(indicator) {
  const style = indicator?.style;
  try {
    return {
      value: String(style?.getPropertyValue?.("transform") ?? style?.transform ?? ""),
      priority: String(style?.getPropertyPriority?.("transform") ?? ""),
    };
  } catch (_) {
    return { value: "", priority: "" };
  }
}

let nativeSliderInteractionActive = false;
let nativeSliderInteractionLastAt = -Infinity;
let nativeSliderInteractionObservedFooter = null;
let nativeSliderInteractionCleanup = null;
const NATIVE_SLIDER_INTERACTION_HOLD_MS = 250;

function disconnectNativeSliderInteractionGuards() {
  if (nativeSliderInteractionCleanup) {
    nativeSliderInteractionCleanup();
    unloads.delete(nativeSliderInteractionCleanup);
  }
  nativeSliderInteractionCleanup = null;
  nativeSliderInteractionObservedFooter = null;
  nativeSliderInteractionActive = false;
  nativeSliderInteractionLastAt = -Infinity;
}

function observeNativeSliderInteractionGuards(footer) {
  if (!footer || nativeSliderInteractionObservedFooter === footer) return;
  disconnectNativeSliderInteractionGuards();
  if (typeof footer.addEventListener !== "function") return;
  const doc = globalThis.document ?? window.document ?? null;
  const isSliderEvent = (event) => {
    const target = event?.target;
    if (!target) return false;
    const slider = target.id === "progressBar"
      ? target
      : target.closest?.('#progressBar, [role="slider"]');
    return Boolean(slider && (footer === slider || footer.contains?.(slider)));
  };
  const onPointerDown = (event) => {
    if (!isSliderEvent(event)) return;
    nativeSliderInteractionActive = true;
    nativeSliderInteractionLastAt = getMonotonicNow();
  };
  const onPointerEnd = () => {
    if (!nativeSliderInteractionActive) return;
    nativeSliderInteractionActive = false;
    nativeSliderInteractionLastAt = getMonotonicNow();
  };
  const onKeyDown = (event) => {
    if (!isSliderEvent(event)) return;
    const key = String(event?.key ?? "");
    if (/^(ArrowLeft|ArrowRight|ArrowUp|ArrowDown|Home|End|PageUp|PageDown)$/.test(key)) {
      nativeSliderInteractionLastAt = getMonotonicNow();
    }
  };
  footer.addEventListener("pointerdown", onPointerDown, true);
  footer.addEventListener("lostpointercapture", onPointerEnd, true);
  footer.addEventListener("keydown", onKeyDown, true);
  doc?.addEventListener?.("pointerup", onPointerEnd, true);
  doc?.addEventListener?.("pointercancel", onPointerEnd, true);
  nativeSliderInteractionObservedFooter = footer;
  nativeSliderInteractionCleanup = () => {
    footer.removeEventListener?.("pointerdown", onPointerDown, true);
    footer.removeEventListener?.("lostpointercapture", onPointerEnd, true);
    footer.removeEventListener?.("keydown", onKeyDown, true);
    doc?.removeEventListener?.("pointerup", onPointerEnd, true);
    doc?.removeEventListener?.("pointercancel", onPointerEnd, true);
    nativeSliderInteractionActive = false;
  };
  unloads.add(nativeSliderInteractionCleanup);
}

function isNativeSliderScrubbingOrSeeking(slider, nowMonotonic = getMonotonicNow()) {
  if (slider?.matches?.(":active")) return true;
  if (nativeSliderInteractionActive) return true;
  if (Number.isFinite(nowMonotonic)
      && nowMonotonic - nativeSliderInteractionLastAt <= NATIVE_SLIDER_INTERACTION_HOLD_MS) {
    return true;
  }
  const trackId = typeof getCurrentTrackId === "function" ? getCurrentTrackId() : null;
  if (trackId !== null
      && typeof shouldHoldEngineClockAfterTidalSeek === "function"
      && shouldHoldEngineClockAfterTidalSeek(
        lastTidalSeekIntent, trackId, currentStatusData, Date.now(),
      )) {
    return true;
  }
  return false;
}

function restoreNativeProgressFillProjection() {
  const state = nativeProgressFillProjectionState;
  if (!state) return false;
  const currentTransform = readInlineTransform(state.indicator);
  if (state.lastWrittenTransform !== null
      && currentTransform.value === state.lastWrittenTransform) {
    try {
      if (state.originalTransform.value) {
        state.indicator.style?.setProperty?.(
          "transform", state.originalTransform.value, state.originalTransform.priority,
        );
      } else {
        state.indicator.style?.removeProperty?.("transform");
      }
    } catch (_) {}
  }
  nativeProgressFillProjectionState = null;
  unloads.delete(restoreNativeProgressFillProjection);
  return true;
}

function applyNativeProgressFillProjection(slider, position, duration) {
  if (!slider || slider.isConnected === false
      || String(slider.tagName ?? "").toUpperCase() !== "DIV"
      || slider.getAttribute?.("role") !== "slider"
      || isNativeSliderScrubbingOrSeeking(slider)) {
    return { applied: false, reason: "unsupported-or-active-slider" };
  }
  const engineDuration = parsePlaybackPosition(duration);
  const enginePosition = parsePlaybackPosition(position);
  if (engineDuration === null || engineDuration <= 0 || enginePosition === null) {
    return { applied: false, reason: "invalid-timeline" };
  }
  const indicator = slider.querySelector?.('[data-test="progress-indicator"]');
  if (!indicator || indicator.isConnected === false || !indicator.style) {
    return { applied: false, reason: "progress-indicator-missing" };
  }
  const sliderBounds = slider.getBoundingClientRect?.();
  const sliderWidth = Number(sliderBounds?.width);
  const indicatorBounds = indicator.getBoundingClientRect?.();
  const indicatorWidth = Number(indicatorBounds?.width);
  const width = Number.isFinite(indicatorWidth) && indicatorWidth > 0 ? indicatorWidth : sliderWidth;
  if (!Number.isFinite(width) || width <= 0
      || (Number.isFinite(sliderWidth) && sliderWidth > 0 && Math.abs(width - sliderWidth) > 2)) {
    return { applied: false, reason: "invalid-slider-width" };
  }
  let direction = "ltr";
  try {
    direction = String(window.getComputedStyle?.(slider)?.direction ?? "ltr").toLowerCase();
  } catch (_) {}
  if (direction && direction !== "ltr") {
    return { applied: false, reason: "unsupported-direction" };
  }
  let computedTransform = "none";
  try {
    computedTransform = String(window.getComputedStyle?.(indicator)?.transform
      ?? indicator.style.transform ?? "none");
  } catch (_) {
    computedTransform = String(indicator.style.transform ?? "none");
  }
  const matrix = parseNativeTransformMatrix(computedTransform);
  if (!matrix) return { applied: false, reason: "unsupported-transform" };
  const translateTolerancePx = 2;
  if (matrix.e < -width - translateTolerancePx || matrix.e > translateTolerancePx) {
    return { applied: false, reason: "unsupported-transform-range" };
  }

  if (nativeProgressFillProjectionState
      && (nativeProgressFillProjectionState.slider !== slider
        || nativeProgressFillProjectionState.indicator !== indicator)) {
    restoreNativeProgressFillProjection();
  }
  let state = nativeProgressFillProjectionState;
  const currentInlineTransform = readInlineTransform(indicator);
  const currentAriaMax = slider.getAttribute?.("aria-valuemax");
  if (!state) {
    state = {
      slider,
      indicator,
      originalTransform: currentInlineTransform,
      lastWrittenTransform: null,
    };
    nativeProgressFillProjectionState = state;
    unloads.add(restoreNativeProgressFillProjection);
  } else {
    if (state.lastWrittenTransform !== null
        && currentInlineTransform.value !== state.lastWrittenTransform) {
      state.originalTransform = currentInlineTransform;
    }
  }

  const boundedPosition = Math.min(engineDuration, Math.max(0, enginePosition));
  const beforePosition = engineDuration * (1 + matrix.e / width);
  const targetTranslateX = Number((-width * (1 - boundedPosition / engineDuration)).toFixed(6));
  const targetTransform = `matrix(${matrix.a}, ${matrix.b}, ${matrix.c}, ${matrix.d}, ${targetTranslateX}, ${matrix.f})`;
  const writePriority = state.originalTransform.priority || "important";
  try {
    if (currentInlineTransform.value !== targetTransform
        || currentInlineTransform.priority !== writePriority) {
      if (typeof indicator.style.setProperty === "function") {
        indicator.style.setProperty("transform", targetTransform, writePriority);
      } else {
        indicator.style.transform = targetTransform;
      }
    }
  } catch (_) {
    return { applied: false, reason: "style-write-failed" };
  }
  state.lastWrittenTransform = readInlineTransform(indicator).value;
  let afterMatrix = null;
  try {
    afterMatrix = parseNativeTransformMatrix(
      window.getComputedStyle?.(indicator)?.transform ?? indicator.style.transform,
    );
  } catch (_) {
    afterMatrix = parseNativeTransformMatrix(indicator.style.transform);
  }
  const afterPosition = afterMatrix ? engineDuration * (1 + afterMatrix.e / width) : null;
  return {
    applied: true,
    width,
    engineDurationSeconds: engineDuration,
    enginePositionSeconds: boundedPosition,
    nativeAriaMaxSeconds: currentAriaMax === null || currentAriaMax === undefined
      ? null : Number(currentAriaMax),
    preCorrectionErrorMs: Number.isFinite(beforePosition)
      ? (beforePosition - boundedPosition) * 1000 : null,
    postCorrectionErrorMs: afterPosition === null ? null : (afterPosition - boundedPosition) * 1000,
    transform: state.lastWrittenTransform,
  };
}

function recordNativePresentationTopology(footer, slider, position, duration, now) {
  const at = Number(now);
  if (!Number.isFinite(at)
      || at - lastNativePresentationDiagnosticAt < NATIVE_PRESENTATION_DIAGNOSTIC_INTERVAL_MS) {
    return false;
  }
  lastNativePresentationDiagnosticAt = at;
  const documentRef = globalThis.document ?? window.document ?? null;
  if (!documentRef) return false;

  const progressNodes = [slider];
  try {
    const descendants = Array.from(slider.querySelectorAll?.("*") ?? []);
    for (const node of descendants.slice(0, 20)) progressNodes.push(node);
  } catch (_) {}

  const lyricNodes = new Set();
  const isVisibleLyricCandidate = (element) => {
    if (!element || element.isConnected === false) return false;
    if (hasVisibleLayout(element)) return true;
    for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
      const computed = typeof getComputedStyle === "function"
        ? getComputedStyle(ancestor)
        : ancestor.style || {};
      if (computed.display === "none" || computed.visibility === "hidden"
          || computed.visibility === "collapse" || Number(computed.opacity ?? 1) <= 0) {
        return false;
      }
    }
    const rect = element.getBoundingClientRect?.();
    const left = Number(rect?.left ?? rect?.x);
    const top = Number(rect?.top ?? rect?.y);
    const width = Number(rect?.width);
    const height = Number(rect?.height);
    if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
      return false;
    }
    const right = Number(rect?.right ?? left + width);
    const bottom = Number(rect?.bottom ?? top + height);
    const viewportWidth = Number((typeof window !== "undefined" && window.innerWidth)
      || document.documentElement?.clientWidth);
    const viewportHeight = Number((typeof window !== "undefined" && window.innerHeight)
      || document.documentElement?.clientHeight);
    if (viewportWidth > 0 && viewportHeight > 0) {
      if (right <= 0 || bottom <= 0 || left >= viewportWidth || top >= viewportHeight) return false;
    }
    return true;
  };
  for (const selector of [
    '.rl-wbw-line[data-current="true"]',
    '.rl-wbw-line-active',
    '.rl-wbw-active',
    '.rl-syl-active',
    'span[data-test="lyrics-line"][class*="_current_"]',
    '.rl-wbw-container',
    '.rl-wbw-line',
    '[class*="lyric" i]',
    '[data-test*="lyric" i]',
    '[data-testid*="lyric" i]',
    '[aria-current="true"]',
    '[aria-selected="true"]',
  ]) {
    try {
      for (const node of Array.from(documentRef.querySelectorAll?.(selector) ?? [])) {
        if (!isVisibleLyricCandidate(node)) continue;
        lyricNodes.add(node);
        if (lyricNodes.size >= 24) break;
      }
    } catch (_) {}
    if (lyricNodes.size >= 24) break;
  }

  const projection = nativeTimelineProjection;
  const report = {
    trackId: String(getCurrentTrackId() ?? "") || null,
    playbackGeneration: projection?.playbackGeneration ?? null,
    sampleSequence: projection?.sampleSequence ?? null,
    observationMonotonicMs: at,
    enginePositionSeconds: Number.isFinite(Number(position)) ? Number(position) : null,
    durationSeconds: Number.isFinite(Number(duration)) ? Number(duration) : null,
    footerVisible: Boolean(footer && hasVisibleLayout(footer)),
    progressBar: progressNodes.map((node, index) => summarizeNativePresentationElement(
      node, index === 0 ? "slider" : `slider-descendant-${index}`,
    )),
    lyricCandidates: Array.from(lyricNodes).map((node, index) => summarizeNativePresentationElement(
      node, `lyric-candidate-${index}`,
    )),
  };
  console.info(`[Omniphony EX][presentation-topology] ${JSON.stringify(report)}`);
  return true;
}

function applyNativeTimelinePosition(
  position,
  duration,
  frameTime = getMonotonicNow(),
  projectionSource = "sync",
) {
  const footer = getPlaybackFooter();
  if (!footer) {
    disconnectNativeTimelineRangeObserver();
    reportNativeTimelineProjection("no-footer");
    recordNativeTimelineDiagnostic("skip", { reason: "no-footer" }, frameTime);
    return false;
  }
  const candidates = Array.from(footer?.querySelectorAll?.("#progressBar") || []);
  if (!candidates.length) {
    const fallback = footer?.querySelector?.("#progressBar");
    if (fallback) candidates.push(fallback);
  }
  const slider = candidates.find((candidate) => candidate
    && candidate.isConnected !== false
    && footer?.contains?.(candidate)
    && hasVisibleLayout(candidate));
  if (!slider) {
    disconnectNativeTimelineRangeObserver();
    reportNativeTimelineProjection(candidates.length ? "no-visible-slider" : "no-slider");
    recordNativeTimelineDiagnostic("skip", {
      reason: candidates.length ? "no-visible-slider" : "no-slider",
    }, frameTime);
    return false;
  }
  recordNativePresentationTopology(footer, slider, position, duration, frameTime);
  observeNativeTimelineRange(footer);
  observeNativeSliderInteractionGuards(footer);
  if (isNativeSliderScrubbingOrSeeking(slider, frameTime)) {
    reportNativeTimelineProjection("native-scrub-active");
    recordNativeTimelineDiagnostic("skip", { reason: "native-scrub-active" }, frameTime);
    return false;
  }
  const isNativeRangeInput = String(slider.tagName || "").toUpperCase() === "INPUT"
    && String(slider.type || "").toLowerCase() === "range";
  const isTidalDivSlider = String(slider.tagName || "").toUpperCase() === "DIV"
    && slider.getAttribute?.("role") === "slider";
  if (isTidalDivSlider) {
    // Older plugin builds left these non-standard properties on TIDAL's DIV.
    // They are not part of TIDAL's state and do not affect its rendered slider.
    for (const property of ["value", "max"]) {
      if (Object.prototype.hasOwnProperty.call(slider, property)) delete slider[property];
    }
  }
  const engineDuration = parsePlaybackPosition(duration);
  const nativeMax = parsePlaybackPosition(slider.getAttribute?.("aria-valuemax"))
    ?? (isNativeRangeInput ? parsePlaybackPosition(slider.max) : null);
  if (engineDuration !== null && engineDuration > 0
      && isNativeRangeInput
      && (nativeMax === null || Math.abs(nativeMax - engineDuration) > 0.0005)) {
    // TIDAL's catalog duration can be a few milliseconds shorter than the
    // decoded stream. Keep its native range input, but use libmpv's confirmed
    // duration so the final native thumb position is not clamped early.
    slider.max = String(engineDuration);
  }
  const max = engineDuration !== null && engineDuration > 0 ? engineDuration : nativeMax;
  const min = parsePlaybackPosition(slider.getAttribute?.("aria-valuemin"))
    ?? (isNativeRangeInput ? parsePlaybackPosition(slider.min) : null)
    ?? 0;
  const bounded = Math.min(max, Math.max(min, Number(position)));
  if (!Number.isFinite(bounded)) {
    recordNativeTimelineDiagnostic("skip", { reason: "invalid-position" }, frameTime);
    return false;
  }
  const timelineDurationDispatchSucceeded = mirrorEngineDurationToTidal(
    engineDuration, getCurrentTrackId(),
  );
  const projection = nativeTimelineProjection;
  const customSliderAriaValueBefore = isTidalDivSlider
    ? parsePlaybackPosition(slider.getAttribute?.("aria-valuenow")) : null;
  const tidalReduxPosition = parsePlaybackPosition(
    getTidalPosition(getTidalPlayState()),
  );
  const tidalReduxPositionDiffers = tidalReduxPosition !== null
    && Math.abs(bounded - tidalReduxPosition) >= 0.001;
  const lastMirroredUiPosition = parsePlaybackPosition(lastMirroredUiTime);
  const lastMirroredUiPositionDiffers = lastMirroredUiPosition === null
    || Math.abs(bounded - lastMirroredUiPosition) >= 0.001;
  const timelineDispatchEligible = Boolean(projection)
    && (lastMirroredUiPositionDiffers || tidalReduxPositionDiffers);
  const timelineActionAvailable = typeof window.luna?.lib?.redux?.actions?.[
    "playbackControls/TIME_UPDATE"
  ] === "function";
  let timelineDispatchAttempted = false;
  let timelineDispatchSucceeded = false;

  // TIDAL's footer timeline is a custom slider whose rendered position comes
  // from its Redux TIME_UPDATE action. Assigning `.value` below is only useful
  // for native range implementations; it is an expando on TIDAL's div slider.
  // Mirror corrected paused samples and every changed playback frame through
  // TIDAL's own action so the custom native slider and synced lyrics follow
  // the authoritative engine position, including after an app restart.
  if (timelineDispatchEligible) {
    timelineDispatchAttempted = true;
    timelineDispatchSucceeded = dispatchTidalAction("playbackControls/TIME_UPDATE", bounded);
    if (timelineDispatchSucceeded) {
      lastMirroredUiTime = bounded;
      recordNativeTimelineDiagnostic("redux", {
        action: "playbackControls/TIME_UPDATE",
        position: bounded,
        thresholdMs: 1,
        source: projectionSource,
      }, frameTime);
    }
  }
  const tidalReduxPositionAfterDispatch = parsePlaybackPosition(
    getTidalPosition(getTidalPlayState()),
  );
  const timelineDispatchReadbackDifferenceMs = timelineDispatchAttempted
      && tidalReduxPositionAfterDispatch !== null
    ? (tidalReduxPositionAfterDispatch - bounded) * 1000
    : null;
  const timelineDispatchOutcome = !projection ? "projection-missing"
    : !timelineDispatchEligible ? "threshold-not-met"
      : !timelineActionAvailable ? "action-missing"
        : !timelineDispatchSucceeded ? "action-failed"
          : tidalReduxPositionAfterDispatch === null ? "playstate-readback-unavailable"
            : Math.abs(timelineDispatchReadbackDifferenceMs) < 1 ? "playstate-converged"
              : "playstate-not-converged";
  const customSliderAriaValueAfter = isTidalDivSlider
    ? parsePlaybackPosition(slider.getAttribute?.("aria-valuenow")) : null;
  let nativeFillProjectionResult = null;
  if (isTidalDivSlider && engineDuration !== null && engineDuration > 0) {
    const fillBasisPosition = customSliderAriaValueAfter ?? bounded;
    nativeFillProjectionResult = applyNativeProgressFillProjection(
      slider, fillBasisPosition, engineDuration,
    );
    if (nativeFillProjectionResult.applied) {
      recordNativeTimelineDiagnostic("native-fill", {
        source: projectionSource,
        basis: customSliderAriaValueAfter !== null ? "committed-aria" : "projected-engine",
        ...nativeFillProjectionResult,
      }, frameTime);
    }
  }

  // TIDAL's custom role=slider DIV is Redux controlled. Only mirror `.value`
  // when the host actually provides a native range input.
  const sliderBefore = isNativeRangeInput ? parsePlaybackPosition(slider.value) : null;
  if (isNativeRangeInput) slider.value = String(bounded);
  const sliderAfter = isNativeRangeInput ? parsePlaybackPosition(slider.value) : null;
  const sampleAgeMs = projection?.playing === true
    ? Number(frameTime) - Number(projection.sampledAt)
    : 0;
  recordNativeTimelineDiagnostic("frame", {
    projectionSource,
    sampleSequence: projection?.sampleSequence ?? null,
    projectionAtUnixMs: projection?.sampledAtUnixMs ?? null,
    sampleAgeMs,
    sampleBracketHalfWidthMs: projection?.sampleBracketHalfWidthMs ?? null,
    timePosTimingSource: projection?.timePosTimingSource ?? null,
    targetPosition: bounded,
    sliderIsNativeRange: isNativeRangeInput,
    sliderBefore,
    sliderAfter,
    customSliderAriaValueBefore,
    customSliderAriaValueAfter,
    timelineDispatchEligible,
    timelineActionAvailable,
    timelineDispatchAttempted,
    timelineDispatchSucceeded,
    timelineDurationDispatchSucceeded,
    durationMirrorDiagnostic: lastTidalDurationMirrorDiagnostic,
    timelineDispatchOutcome,
    tidalReduxPositionBefore: tidalReduxPosition,
    tidalReduxPositionAfterDispatch,
    timelineDispatchReadbackDifferenceMs,
    lastMirroredUiPosition,
    customSliderAriaTargetDifferenceMs: customSliderAriaValueBefore === null
      ? null : (customSliderAriaValueBefore - bounded) * 1000,
    preWriteTargetDifferenceMs: sliderBefore === null ? null : (sliderBefore - bounded) * 1000,
    postWriteReadbackDifferenceMs: sliderAfter === null ? null : (sliderAfter - bounded) * 1000,
  }, frameTime);
  reportNativeTimelineProjection("active");
  return true;
}

function disconnectNativeTimelineRangeObserver() {
  disconnectNativeSliderInteractionGuards();
  if (nativeTimelineRangeObserverCleanup) {
    nativeTimelineRangeObserverCleanup();
    unloads.delete(nativeTimelineRangeObserverCleanup);
  }
  nativeTimelineRangeObserver = null;
  nativeTimelineObservedFooter = null;
  nativeTimelineRangeObserverCleanup = null;
}

function observeNativeTimelineRange(footer) {
  if (!footer || nativeTimelineObservedFooter === footer) return;
  disconnectNativeTimelineRangeObserver();
  const Observer = window.MutationObserver || globalThis.MutationObserver;
  if (typeof Observer !== "function") return;
  const observer = new Observer((records = []) => {
    if (pluginDisposed) return;
    const sample = nativeTimelineProjection;
    if (!sample || String(getCurrentTrackId() ?? "") !== sample.trackId) return;
    const now = getMonotonicNow();
    let needsRangeCorrection = false;
    for (const record of records) {
      if (record?.type === "attributes" && record.attributeName === "aria-valuenow") {
        const slider = record.target;
        if (slider?.id !== "progressBar"
            || slider.getAttribute?.("role") !== "slider"
            || !footer.contains?.(slider)) continue;
        const committedAriaValue = parsePlaybackPosition(
          slider.getAttribute?.("aria-valuenow"),
        );
        const projectedEnginePosition = getFreshNativeTimelinePosition(sample, now);
        const recentDispatches = nativeTimelineDispatchHistory.filter((dispatch) =>
          dispatch.atMs <= now && now - dispatch.atMs <= 1500,
        );
        const matchingDispatch = recentDispatches
          .filter((dispatch) => committedAriaValue !== null
            && dispatch.position !== null
            && Math.abs(dispatch.position - committedAriaValue) <= 0.005)
          .at(-1) ?? null;
        const latestDispatch = recentDispatches.at(-1) ?? null;
        recordNativeTimelineDiagnostic("dom-commit", {
          attributeName: "aria-valuenow",
          dispatchSequence: matchingDispatch?.sequence ?? null,
          dispatchToCommitDelayMs: matchingDispatch
            ? now - matchingDispatch.atMs : null,
          committedAriaValue,
          latestDispatchedPosition: latestDispatch?.position ?? null,
          latestDispatchSequence: latestDispatch?.sequence ?? null,
          projectedEnginePosition,
          sliderTargetDifferenceMs: committedAriaValue === null
              || projectedEnginePosition === null
            ? null : (committedAriaValue - projectedEnginePosition) * 1000,
        }, now);
        if (committedAriaValue !== null
            && Number.isFinite(sample.duration)
            && sample.duration > 0) {
          const fillResult = applyNativeProgressFillProjection(
            slider, committedAriaValue, sample.duration,
          );
          if (fillResult.applied) {
            recordNativeTimelineDiagnostic("native-fill", {
              source: "dom-commit",
              basis: "committed-aria",
              ...fillResult,
            }, now);
          }
        }
      } else if (record?.type === "childList"
          || (record?.type === "attributes"
            && (record.attributeName === "max" || record.attributeName === "value"))) {
        needsRangeCorrection = true;
      }
    }
    if (!needsRangeCorrection) return;
    // React may restore the catalog max while committing TIME_UPDATE. Correct
    // the same native slider after that DOM mutation has reached the footer.
    scheduleNativeTimelineFrame();
  });
  observer.observe(footer, {
    attributes: true,
    attributeFilter: ["max", "value", "aria-valuenow"],
    childList: true,
    subtree: true,
  });
  nativeTimelineRangeObserver = observer;
  nativeTimelineObservedFooter = footer;
  nativeTimelineRangeObserverCleanup = () => observer.disconnect();
  unloads.add(nativeTimelineRangeObserverCleanup);
}

function reportNativeTimelineProjection(state) {
  if (lastNativeTimelineProjectionDiagnostic === state) return;
  lastNativeTimelineProjectionDiagnostic = state;
  console.info(`[Omniphony EX] Native TIDAL timeline projection: ${state}.`);
}

function clearNativeTimelineProjection() {
  nativeTimelineProjection = null;
  lastMirroredUiDurationKey = null;
  lastMirroredUiDurationAttemptKey = null;
  lastMirroredUiDurationAttemptAt = -Infinity;
  lastTidalDurationMirrorDiagnostic = null;
  cancelNativeTimelineFrame();
  restoreNativeProgressFillProjection();
}

async function ensureEngineClockAuthority(sample) {
  const serviceInstanceId = String(sample?.serviceInstanceId ?? "");
  const mpvInstanceId = String(sample?.mpvInstanceId ?? "");
  if (!serviceInstanceId || !mpvInstanceId) return false;
  if (serviceInstanceId === activeClockServiceInstanceId
      && mpvInstanceId === activeClockMpvInstanceId) return true;
  try {
    const response = await requestWithTimeout(
      `${API_BASE}/health`, { cache: "no-store" }, 1000,
    );
    if (!response.ok) return false;
    const health = await response.json();
    if (health?.version !== REQUIRED_SERVICE_VERSION
        || String(health.serviceInstanceId ?? "") !== serviceInstanceId
        || String(health.mpvInstanceId ?? "") !== mpvInstanceId) return false;
    activeClockServiceInstanceId = serviceInstanceId;
    activeClockMpvInstanceId = mpvInstanceId;
    clockOrderingInstanceId = serviceInstanceId;
    clockOrderingMpvInstanceId = mpvInstanceId;
    clockOrderingSequence = -1;
    lastFastClockSample = null;
    return true;
  } catch (_) {
    return false;
  }
}

function acceptEngineClockSampleOrdering(sample) {
  const instanceId = String(sample?.serviceInstanceId ?? "");
  const mpvInstanceId = String(sample?.mpvInstanceId ?? "");
  const sequence = Number(sample?.sampleSequence ?? sample?.timePosSampleSequence);
  if (!instanceId || !mpvInstanceId
      || instanceId !== activeClockServiceInstanceId
      || mpvInstanceId !== activeClockMpvInstanceId
      || !Number.isSafeInteger(sequence) || sequence < 0) return false;
  if (instanceId !== clockOrderingInstanceId
      || mpvInstanceId !== clockOrderingMpvInstanceId) return false;
  return sequence > clockOrderingSequence;
}

function commitEngineClockSampleOrdering(sample) {
  if (!acceptEngineClockSampleOrdering(sample)) return false;
  clockOrderingSequence = Number(sample.sampleSequence ?? sample.timePosSampleSequence);
  return true;
}

function isCurrentEngineClockSample(sample) {
  const sequence = Number(sample?.sampleSequence ?? sample?.timePosSampleSequence);
  const serviceInstanceId = String(sample?.serviceInstanceId ?? "");
  const mpvInstanceId = String(sample?.mpvInstanceId ?? "");
  return Number.isSafeInteger(sequence)
    && sequence === clockOrderingSequence
    && serviceInstanceId === activeClockServiceInstanceId
    && mpvInstanceId === activeClockMpvInstanceId
    && serviceInstanceId === clockOrderingInstanceId
    && mpvInstanceId === clockOrderingMpvInstanceId
    && String(sample?.trackId ?? "") === String(getCurrentTrackId() ?? "");
}

function isSyncObservationStillCurrent(status, acceptedSequence) {
  const serviceInstanceId = String(status?.serviceInstanceId ?? "");
  const mpvInstanceId = String(status?.mpvInstanceId ?? "");
  return !pluginDisposed
    && Number.isSafeInteger(Number(acceptedSequence))
    && clockOrderingSequence === Number(acceptedSequence)
    && serviceInstanceId === activeClockServiceInstanceId
    && mpvInstanceId === activeClockMpvInstanceId
    && serviceInstanceId === clockOrderingInstanceId
    && mpvInstanceId === clockOrderingMpvInstanceId
    && String(status?.trackId ?? "") === String(getCurrentTrackId() ?? "");
}

function mergeStatusWithLatestFastClockSample(status, sample, receivedAtMonotonic) {
  if (!status || !sample) return null;
  const statusSequence = Number(status.timePosSampleSequence);
  const sampleSequence = Number(sample.sampleSequence);
  const sampleAgeMs = Number(receivedAtMonotonic) - Number(sample.sampledAt);
  const samePlaybackIdentity = String(status.trackId ?? "") === String(sample.trackId ?? "")
    && Number(status.playbackGeneration) === Number(sample.playbackGeneration)
    && String(status.requestId ?? "") === String(sample.requestId ?? "")
    && String(status.serviceInstanceId ?? "") === String(sample.serviceInstanceId ?? "")
    && String(status.mpvInstanceId ?? "") === String(sample.mpvInstanceId ?? "");
  const timePos = parsePlaybackPosition(sample.timePos);
  if (!samePlaybackIdentity
      || !Number.isSafeInteger(statusSequence)
      || !Number.isSafeInteger(sampleSequence)
      || sampleSequence <= statusSequence
      || !Number.isFinite(sampleAgeMs)
      || sampleAgeMs < 0
      || sampleAgeMs > MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS
      || timePos === null
      || typeof sample.paused !== "boolean") return null;

  let playbackState = String(status.playbackState ?? "").toLowerCase();
  if (sample.paused === true) {
    if (!["ended", "failed", "stopped"].includes(playbackState)) playbackState = "paused";
  } else if (sample.engineClockAdvancing === true) {
    playbackState = "playing";
  }
  return {
    ...status,
    timePos,
    timePosSampledAtUnixMs: sample.timePosSampledAtUnixMs,
    timePosSampleSequence: sampleSequence,
    paused: sample.paused,
    playbackState,
    engineClockAdvancing: sample.engineClockAdvancing === true,
  };
}

function cancelNativeTimelineFrame() {
  if (nativeTimelineFrameId === null) return;
  const cancelFrame = window.cancelAnimationFrame || globalThis.cancelAnimationFrame;
  if (typeof cancelFrame === "function") cancelFrame.call(window, nativeTimelineFrameId);
  nativeTimelineFrameId = null;
}

const MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS = 250;

function resolveEngineSampleAnchor(status, receivedAt, receivedAtUnixMs) {
  const sampleUnixMs = status?.timePosSampledAtUnixMs;
  if (sampleUnixMs === undefined || sampleUnixMs === null) {
    return { valid: true, sampledAt: Number(receivedAt), reason: null };
  }
  const parsedSampleUnixMs = Number(sampleUnixMs);
  const sampleAgeMs = Number(receivedAtUnixMs) - parsedSampleUnixMs;
  if (!Number.isFinite(parsedSampleUnixMs)
      || parsedSampleUnixMs <= 0
      || !Number.isFinite(sampleAgeMs)
      || sampleAgeMs < -5) {
    return { valid: false, sampledAt: null, reason: "invalid-engine-sample-timestamp" };
  }
  if (sampleAgeMs > MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS) {
    return { valid: false, sampledAt: null, reason: "stale-engine-sample" };
  }
  return {
    valid: true,
    sampledAt: Number(receivedAt) - Math.max(0, sampleAgeMs),
    reason: null,
  };
}

function getEnginePositionAtCurrentTime(
  status,
  nowUnixMs = Date.now(),
  nowMonotonic = getMonotonicNow(),
) {
  const timePos = parsePlaybackPosition(status?.timePos);
  if (timePos === null) return null;
  if (status.paused !== false
      || status.playbackState !== "playing"
      || status.engineClockAdvancing !== true) return timePos;

  const duration = parsePlaybackPosition(status.duration);
  if (duration === null || duration <= 0) return null;
  const sampleAnchor = resolveEngineSampleAnchor(status, nowMonotonic, nowUnixMs);
  if (!sampleAnchor.valid) return null;
  return getFreshNativeTimelinePosition({
    playing: true,
    timePos: Math.min(timePos, duration),
    duration,
    sampledAt: sampleAnchor.sampledAt,
  }, Number(nowMonotonic));
}

function getFreshNativeTimelinePosition(sample, frameTime) {
  if (!sample || typeof sample.playing !== "boolean") return null;
  const elapsedMs = Number(frameTime) - Number(sample.sampledAt);
  const timePos = Number(sample.timePos);
  const duration = Number(sample.duration);
  if (!Number.isFinite(elapsedMs)
      || (sample.playing === true && elapsedMs > MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS)
      || !Number.isFinite(timePos)
      || !Number.isFinite(duration)
      || duration <= 0) return null;
  const projectedElapsed = sample.playing ? Math.max(0, elapsedMs) / 1000 : 0;
  return Math.min(duration, Math.max(0, timePos + projectedElapsed));
}

function scheduleNativeTimelineFrame() {
  if (nativeTimelineFrameId !== null) return;
  const requestFrame = window.requestAnimationFrame || globalThis.requestAnimationFrame;
  if (typeof requestFrame !== "function") {
    reportNativeTimelineProjection("animation-frame-unavailable");
    return;
  }
  nativeTimelineFrameId = requestFrame.call(window, (frameTime) => {
    nativeTimelineFrameId = null;
    const sample = nativeTimelineProjection;
    if (!sample) return;
    const position = getFreshNativeTimelinePosition(sample, frameTime);
    if (position === null) {
      if (nativeTimelineProjection === sample) {
        clearNativeTimelineProjection();
        reportNativeTimelineProjection("stale-engine-sample");
        recordNativeTimelineDiagnostic("skip", {
          reason: "stale-engine-sample",
          sequence: sample.sampleSequence ?? null,
          sampleAgeMs: Number(frameTime) - Number(sample.sampledAt),
        }, frameTime);
      }
      return;
    }
    const applied = applyNativeTimelinePosition(position, sample.duration, frameTime, "raf");
    if (sample.playing === true
        && applied
        && position < sample.duration
        && nativeTimelineProjection === sample) {
      scheduleNativeTimelineFrame();
    }
  });
}

function projectEngineClockToNativeTimeline(
  status,
  tidalTrackId,
  receivedAt = getMonotonicNow(),
  receivedAtUnixMs = Date.now(),
) {
  if (!status
      || !Number.isSafeInteger(Number(tidalTrackId))
      || Number(status.trackId) !== Number(tidalTrackId)) {
    clearNativeTimelineProjection();
    reportNativeTimelineProjection("track-mismatch");
    recordNativeTimelineDiagnostic("skip", { reason: "track-mismatch" }, receivedAt);
    return false;
  }
  const timePos = parsePlaybackPosition(status.timePos);
  const duration = parsePlaybackPosition(status.duration);
  if (timePos === null || duration === null || duration <= 0) {
    clearNativeTimelineProjection();
    reportNativeTimelineProjection("invalid-engine-clock");
    recordNativeTimelineDiagnostic("skip", { reason: "invalid-engine-clock" }, receivedAt);
    return false;
  }

  const sampleAnchor = resolveEngineSampleAnchor(status, receivedAt, receivedAtUnixMs);
  if (!sampleAnchor.valid) {
    clearNativeTimelineProjection();
    reportNativeTimelineProjection(sampleAnchor.reason);
    recordNativeTimelineDiagnostic("skip", { reason: sampleAnchor.reason }, receivedAt);
    return false;
  }
  const sampledAt = sampleAnchor.sampledAt;
  if (!Number.isFinite(sampledAt)) {
    clearNativeTimelineProjection();
    reportNativeTimelineProjection("invalid-monotonic-sample");
    recordNativeTimelineDiagnostic("skip", { reason: "invalid-monotonic-sample" }, receivedAt);
    return false;
  }

  const playing = status.paused === false
    && status.playbackState === "playing"
    && status.engineClockAdvancing === true;
  const sample = {
    trackId: String(tidalTrackId),
    timePos: Math.min(timePos, duration),
    duration,
    sampledAt: Number(sampledAt),
    sampledAtUnixMs: Number(status.timePosSampledAtUnixMs) || null,
    sampleSequence: Number.isSafeInteger(Number(status.sampleSequence ?? status.timePosSampleSequence))
      ? Number(status.sampleSequence ?? status.timePosSampleSequence) : null,
    sampleBracketHalfWidthMs: parseNonNegativeFiniteNumber(
      status.timePosSampleBracketHalfWidthMs,
    ),
    timePosTimingSource: String(status.timePosTimingSource ?? "") || null,
    playing,
  };
  nativeTimelineProjection = sample;
  const initialPosition = getFreshNativeTimelinePosition(sample, Number(receivedAt));
  if (initialPosition === null) {
    clearNativeTimelineProjection();
    reportNativeTimelineProjection("stale-engine-sample");
    recordNativeTimelineDiagnostic("skip", {
      reason: "stale-engine-sample",
      sequence: sample.sampleSequence,
      sampleAgeMs: Number(receivedAt) - Number(sampledAt),
    }, receivedAt);
    return false;
  }
  const applied = applyNativeTimelinePosition(initialPosition, sample.duration, receivedAt);
  // Project once on the next frame even while paused. TIME_UPDATE dispatches
  // can make React restore the catalog range during this sync pass; the frame
  // runs after that render and reapplies the confirmed engine timeline.
  if (applied) scheduleNativeTimelineFrame();
  else cancelNativeTimelineFrame();
  return true;
}

function hasRecentTidalSeekIntent(trackId, position, now = Date.now()) {
  return lastTidalSeekIntent !== null
    && String(lastTidalSeekIntent.trackId) === String(trackId)
    && parsePlaybackPosition(position) !== null
    && Math.abs(lastTidalSeekIntent.position - Number(position)) <= 0.05
    && now >= lastTidalSeekIntent.at
    && now - lastTidalSeekIntent.at <= 2000;
}

function shouldHoldEngineClockAfterTidalSeek(intent, trackId, status, now = Date.now()) {
  if (!intent || String(intent.trackId) !== String(trackId)) return false;
  if (intent.pending === true) return true;
  const settledAt = Number(intent.settledAt ?? intent.at);
  const elapsedMs = now - settledAt;
  if (!Number.isFinite(settledAt) || elapsedMs < 0 || elapsedMs > SEEK_RECONCILIATION_WINDOW_MS) {
    return false;
  }
  const enginePosition = parsePlaybackPosition(status?.timePos);
  if (enginePosition === null) return true;
  const expectedPosition = Number(intent.position)
    + (status?.paused === false ? elapsedMs / 1000 : 0);
  return Number.isFinite(expectedPosition)
    && Math.abs(enginePosition - expectedPosition) > SEEK_RECONCILIATION_TOLERANCE_SECONDS;
}

function forwardTidalVolume(payload) {
  const raw = typeof payload === "number" ? payload : Number(payload?.volume);
  const volume = Number(raw);
  const tidalTrackId = Number(getCurrentTrackId());
  const status = currentStatusData || {};
  if (!Number.isFinite(volume) || volume < 0 || volume > 100
      || !Number.isSafeInteger(tidalTrackId) || Number(status.trackId) !== tidalTrackId) return false;
  return sendEngineCommand("volume", {
    volume,
    trackId: tidalTrackId,
    playbackGeneration: status.playbackGeneration,
    requestId: status.requestId,
  });
}

function getNativeTimelinePosition(eventTarget) {
  const slider = eventTarget?.closest?.('input[type="range"], [role="slider"]')
    || (eventTarget?.matches?.('input[type="range"], [role="slider"]') ? eventTarget : null);
  const footer = getPlaybackFooter();
  if (!slider || !footer || (footer !== slider && !footer.contains?.(slider))) return null;
  const label = [
    slider.getAttribute?.("aria-label"),
    slider.getAttribute?.("data-test"),
    slider.title,
    slider.id
  ].filter(Boolean).join(" ");
  if (!/progress|seek|timeline/i.test(label)) return null;
  const value = slider.value ?? slider.getAttribute?.("aria-valuenow");
  return parsePlaybackPosition(value);
}

// Native & Radiant Lyrics Click-to-Seek Listener
function installLyricsClickListener() {
  const onLyricsClick = (event) => {
    // 1. Check for standard TIDAL lyrics line
    const lineElement = event.target.closest('[class*="lyrics-line"], [class*="lyricsLine"], .rl-wbw-line, [data-test="lyrics-line"], [class*="_line_"]');
    if (!lineElement) return;
    
    // Check if line carries timestamp in data attribute
    let targetTime = null;
    if (lineElement.dataset.time) targetTime = parseFloat(lineElement.dataset.time);
    else if (lineElement.dataset.timestamp) targetTime = parseFloat(lineElement.dataset.timestamp);
    else if (lineElement.dataset.startTime) targetTime = parseFloat(lineElement.dataset.startTime);
    
    // If not in dataset, check Radiant or TIDAL internal React props / fiber
    if (targetTime === null || isNaN(targetTime)) {
      for (const key in lineElement) {
        if (key.startsWith('__reactFiber') || key.startsWith('__reactInternalInstance')) {
          const fiber = lineElement[key];
          const memoized = fiber?.memoizedProps;
          const lineObj = memoized?.line || memoized?.item || memoized?.data;
          if (lineObj && typeof lineObj.startTime === 'number') {
            targetTime = lineObj.startTime;
            break;
          }
        }
      }
    }
    
    if (targetTime !== null && !isNaN(targetTime) && targetTime >= 0) {
      console.info(`[Omniphony EX] Lyrics line clicked -> seeking to ${targetTime}s`);
      handleImmediateTidalSeek(targetTime, 'lyrics-click');
      scheduleImmediateSync();
    }
  };
  
  document.addEventListener('click', onLyricsClick, true);
  unloads.add(() => document.removeEventListener('click', onLyricsClick, true));
}

function installPlaybackObservers(onPlaybackChange, onSeek) {
  const library = window.luna?.lib;
  nativePlayerCommandHookInstalled = false;
  nativePlayerCommandHookDiagnostic = "installing TidaLuna native-module guard";
  void interceptNativePlayerCommands(onPlaybackChange).then((installed) => {
    nativePlayerCommandHookInstalled = installed;
    updatePausedSelectionUi();
  });
  if (typeof document?.addEventListener === "function") {
    const onTrustedPlayClick = (event) => { recordPausedSelectionUserPlayAuthorization(event); };
  const onTrustedStalledPlayClick = (event) => { recordStalledPlayAuthorization(event); };
    const onTrustedPlayKeydown = (event) => {
      if (event?.key === "Enter" || event?.key === " " || event?.code === "Space") {
        recordPausedSelectionUserPlayAuthorization(event);
      recordStalledPlayAuthorization(event);
      }
    };
    document.addEventListener("click", onTrustedPlayClick, true);
  document.addEventListener("click", onTrustedStalledPlayClick, true);
    document.addEventListener("keydown", onTrustedPlayKeydown, true);
    unloads.add(() => {
      document.removeEventListener("click", onTrustedPlayClick, true);
    document.removeEventListener("click", onTrustedStalledPlayClick, true);
      document.removeEventListener("keydown", onTrustedPlayKeydown, true);
    });
  }
  if (typeof document?.addEventListener === "function" && typeof onSeek === "function") {
    let nativeTimelinePointerTarget = null;
    const forwardNativeTimelineSeek = (target, source = "native-ui") => {
      const position = getNativeTimelinePosition(target);
      if (position === null) return;
      tracePlaybackEvent("native-timeline-seek", { requestedPosition: position, source });
      onSeek(position, source);
    };
    const onNativeTimelineInput = (event) => forwardNativeTimelineSeek(event.target);
    const onNativeTimelinePointerDown = (event) => {
      if (getNativeTimelinePosition(event.target) !== null) {
        nativeTimelinePointerTarget = event.target;
      }
    };
    const onNativeTimelinePointerUp = (event) => {
      const target = getNativeTimelinePosition(event.target) !== null
        ? event.target : nativeTimelinePointerTarget;
      nativeTimelinePointerTarget = null;
      if (!target) return;
      // TIDAL's current custom range commits aria-valuenow on pointer release
      // without reliably emitting input/change or Redux SEEK. Read it after
      // React's pointer handlers commit, then forward the final target once.
      queueMicrotask(() => forwardNativeTimelineSeek(target, "native-ui"));
    };
    const onNativeTimelinePointerCancel = () => { nativeTimelinePointerTarget = null; };
    const onNativeTimelineKeyUp = (event) => {
      if (!/^(ArrowLeft|ArrowRight|ArrowUp|ArrowDown|Home|End|PageUp|PageDown)$/.test(String(event?.key ?? ""))) return;
      const target = event.target;
      if (getNativeTimelinePosition(target) === null) return;
      queueMicrotask(() => forwardNativeTimelineSeek(target, "native-ui"));
    };
    document.addEventListener("input", onNativeTimelineInput, true);
    document.addEventListener("change", onNativeTimelineInput, true);
    document.addEventListener("pointerdown", onNativeTimelinePointerDown, true);
    document.addEventListener("pointerup", onNativeTimelinePointerUp, true);
    document.addEventListener("pointercancel", onNativeTimelinePointerCancel, true);
    document.addEventListener("keyup", onNativeTimelineKeyUp, true);
    unloads.add(() => {
      document.removeEventListener("input", onNativeTimelineInput, true);
      document.removeEventListener("change", onNativeTimelineInput, true);
      document.removeEventListener("pointerdown", onNativeTimelinePointerDown, true);
      document.removeEventListener("pointerup", onNativeTimelinePointerUp, true);
      document.removeEventListener("pointercancel", onNativeTimelinePointerCancel, true);
      document.removeEventListener("keyup", onNativeTimelineKeyUp, true);
    });
  }
  if (typeof library?.PlayState?.onState === "function") {
    try {
      library.PlayState.onState(unloads, onPlaybackChange);
    } catch (error) {
      console.warn("[Omniphony EX] TidaLuna state observer unavailable:", error);
    }
  }
  if (typeof library?.MediaItem?.onMediaTransition === "function") {
    try {
      library.MediaItem.onMediaTransition(unloads, onPlaybackChange);
    } catch (error) {
      console.warn("[Omniphony EX] TidaLuna track observer unavailable:", error);
    }
  }
  if (typeof library?.redux?.intercept === "function") {
    try {
      if (desiredPlaybackIntent === null) {
        const initialDesiredState = String(library.PlayState?.desiredState || "").toUpperCase();
        const initialActualState = String(library.PlayState?.state || "").toUpperCase();
        desiredPlaybackIntent = initialDesiredState === "PLAYING"
          ? true
          : initialDesiredState === "PAUSED" || initialDesiredState === "NOT_PLAYING"
            ? false
            : initialActualState === "PLAYING";
      }
      // TIDAL's native streaming privilege is withheld while this add-on owns
      // audio. The Redux controls remain the user-facing transport; their
      // intent is mirrored to libmpv and the engine clock is published back.
      library.redux.intercept("player/ENSURE_PLAYBACK_PRIVILEGES", unloads, () => true);
      library.redux.intercept("playbackControls/PLAY", unloads, () => {
        tracePlaybackEvent("tidal-control-play");
        if (consumePausedSelectionPreparation()) {
          pausedSelectionConfirmedTrackId = null;
        pausedSelectionUserPlayAuthorization = null;
          const trackId = getQueuedTrackId();
          if (!trackId) {
            pausedSelectionPending = null;
            setTidalPlaybackIntent(false);
            updatePausedSelectionUi("TIDAL queue identity unavailable. Play was blocked; playback remains paused.");
            onPlaybackChange();
            return true;
          }
          pausedSelectionPending = {
            trackId,
            expiresAt: Date.now() + 5000,
            expired: false,
          };
          // TidaLuna's build-action interceptor suppresses the original Redux
          // action when a callback returns true. Let PLAY dispatch so TIDAL can
          // commit its already-updated queue selection, but pin our intent to
          // paused and reassert PAUSED in the first microtask after dispatch.
          desiredPlaybackIntent = false;
          tracePlaybackEvent("tidal-selection-commit-requested", { selectedTrackId: trackId });
          queueMicrotask(() => {
            if (pausedSelectionPending?.trackId !== trackId) return;
            setTidalUiPlaybackState("PAUSED");
            updatePausedSelectionUi("Selection committed. Holding TIDAL and libmpv paused while the stream loads.");
            onPlaybackChange();
          });
          onPlaybackChange();
          return false;
        }
        if (pausedSelectionPending) {
          tracePlaybackEvent("tidal-play-blocked-paused-selection", {
            pendingTrackId: pausedSelectionPending.trackId,
            queuedTrackId: getQueuedTrackId(),
          });
          setTidalPlaybackIntent(false);
          updatePausedSelectionUi("Selection is still being prepared. Playback remains paused.");
          onPlaybackChange();
          return true;
        }
        const queueTrackId = getQueuedTrackId();
        if (pausedSelectionConfirmedTrackId !== null) {
          const confirmedTrackId = pausedSelectionConfirmedTrackId;
          const queueMatches = String(queueTrackId ?? "") === confirmedTrackId;
          const diagnosticsReady = queueMatches && isPausedSelectionDiagnosticsReady(confirmedTrackId);
          if (!queueMatches) pausedSelectionUserPlayAuthorization = null;
          const authorized = diagnosticsReady && hasPausedSelectionUserPlayAuthorization(confirmedTrackId);
          if (!authorized) {
            tracePlaybackEvent("tidal-play-blocked-until-paused-diagnostics-ready", {
              confirmedTrackId,
              queuedTrackId: queueTrackId,
              queueMatches,
            });
            setTidalPlaybackIntent(false);
            updatePausedSelectionUi(!queueMatches
              ? "TIDAL queue changed after selection verification. Playback remains paused until the selected track is verified again."
              : diagnosticsReady
                ? "Stream diagnostics are ready. Use TIDAL's Play control to start this track."
                : "The selected stream diagnostics are still loading. Playback remains paused.");
            onPlaybackChange();
            return true;
          }
          pausedSelectionConfirmedTrackId = null;
          pausedSelectionUserPlayAuthorization = null;
          updatePausedSelectionUi();
        }
        pausedSelectionPending = null;
        pausedSelectionConfirmedTrackId = null;
        pausedSelectionUserPlayAuthorization = null;
        setTidalPlaybackIntent(true);
        const currentTrack = getCurrentTrackId();
        if (currentStatusData && String(currentStatusData.trackId) === String(currentTrack)
            && currentStatusData.paused === true) {
          const completedInstance = isAtNaturalEnd(
            true, currentStatusData, currentTrack, currentStatusData.trackId,
          ) || String(currentStatusData.playbackState || "").toLowerCase() === "ended";
          if (completedInstance) {
            const replay = getTidalPlaybackSnapshot();
            tracePlaybackEvent("explicit-replay-requested-at-end", {
              trackId: String(currentTrack),
              playbackGeneration: currentStatusData.playbackGeneration ?? null,
            });
            // A completed DASH instance must be reopened with a fresh request
            // identity. /resume would otherwise rewind or reattach the stale
            // ended stream instead of creating a new queue playback instance.
            void triggerEnginePlay(currentTrack, 0, {
              title: replay?.title || "",
              artist: replay?.artist || "",
            });
          } else {
            void sendEngineCommand("resume", {
              trackId: Number(currentTrack),
              playbackGeneration: currentStatusData.playbackGeneration,
              requestId: currentStatusData.requestId,
            });
          }
        }
        onPlaybackChange();
        return true;
      });
      library.redux.intercept("playbackControls/PAUSE", unloads, () => {
        tracePlaybackEvent("tidal-control-pause", {
          dispatchStack: new Error("TIDAL PAUSE dispatch").stack || null
        });
        stalledPlayAuthorization = null;
        setTidalPlaybackIntent(false);
        onPlaybackChange();
        return true;
      });
      library.redux.intercept("playbackControls/SET_DESIRED_PAUSE_STATE", unloads, (paused) => {
        if (mirroringTidalPlaybackState || typeof paused !== "boolean") return false;
        tracePlaybackEvent("tidal-control-desired-pause", { paused });
        setTidalPlaybackIntent(!paused);
        onPlaybackChange();
        return true;
      });
      library.redux.intercept("playbackControls/TOGGLE_PLAYBACK", unloads, () => {
        tracePlaybackEvent("tidal-control-toggle");
        setTidalPlaybackIntent(!(desiredPlaybackIntent ?? false));
        onPlaybackChange();
        return true;
      });
      library.redux.intercept("playbackControls/STOP", unloads, () => {
        tracePlaybackEvent("tidal-control-stop");
        setTidalPlaybackIntent(false);
        onPlaybackChange();
        return true;
      });
      library.redux.intercept("playbackControls/SKIP_NEXT", unloads, () => {
        library.PlayState?.next?.();
        return true;
      });
      library.redux.intercept("playbackControls/SKIP_PREVIOUS", unloads, () => {
        const snapshot = getTidalPlaybackSnapshot();
        if (snapshot && snapshot.position > 3) {
          onSeek(0);
          dispatchTidalAction("playbackControls/TIME_UPDATE", 0);
        } else {
          library.PlayState?.previous?.();
        }
        return true;
      });
      library.redux.intercept("playbackControls/SEEK", unloads, (position) => {
        tracePlaybackEvent("tidal-seek-action", { source: "SEEK", requestedPosition: Number(position) });
        onSeek(position, "redux");
        return true;
      });
      library.redux.intercept("playbackControls/SEEK_FORWARDS", unloads, (seconds) => {
        const step = Number.isFinite(Number(seconds)) ? Number(seconds) : 10;
        onSeek(Math.max(0, Number(currentStatusData?.timePos || 0) + step));
        return true;
      });
      library.redux.intercept("playbackControls/SEEK_BACKWARDS", unloads, (seconds) => {
        const step = Number.isFinite(Number(seconds)) ? Number(seconds) : 10;
        onSeek(Math.max(0, Number(currentStatusData?.timePos || 0) - step));
        return true;
      });
      library.redux.intercept("playbackControls/SET_VOLUME", unloads, (volume) => {
        void forwardTidalVolume(volume);
      });
    } catch (error) {
      console.warn("[Omniphony EX] TidaLuna seek observer unavailable:", error);
    }
  }
}

async function handleImmediateTidalSeek(timePos, source = "redux") {
  const targetTime = parsePlaybackPosition(timePos);
  const tidal = getTidalPlaybackSnapshot();
  const status = currentStatusData || {};
  const trackId = Number(tidal?.trackId);
  const seekPlaybackGeneration = status.playbackGeneration;
  const seekRequestId = status.requestId;
  tracePlaybackEvent("tidal-seek-callback", { requestedPosition: targetTime });
  if (targetTime === null
      || !Number.isSafeInteger(trackId) || trackId <= 0
      || Number(status.trackId) !== trackId) return false;

  const now = Date.now();
  if (hasRecentTidalSeekIntent(trackId, targetTime, now)
      && now - lastTidalSeekIntent.at <= 100) return true;
  const nativeUiIntent = source === "native-ui";
  const preparedPosition = parsePlaybackPosition(status.timePos);
  const resumeClockReset = !nativeUiIntent
    && targetTime <= 0.05
    && desiredPlaybackIntent === true
    && playIntentChangedAt > 0
    && now - playIntentChangedAt <= PLAY_INTENT_RECONCILIATION_WINDOW_MS
    && preparedPosition !== null
    && preparedPosition > 1;
  if (resumeClockReset) {
    tracePlaybackEvent("tidal-zero-seek-guard-fired", { requestedPosition: targetTime, preservedPosition: preparedPosition });
    console.info("[Omniphony EX] Ignored TIDAL's zero seek during Play startup; preserving the prepared libmpv position.");
    dispatchTidalAction("playbackControls/TIME_UPDATE", preparedPosition);
    return true;
  }

  const seekIntent = { trackId, position: targetTime, at: now, source, pending: true };
  lastTidalSeekIntent = seekIntent;
  const sent = await sendEngineCommand("seek", {
    timePos: targetTime,
    trackId,
    playbackGeneration: status.playbackGeneration,
    requestId: status.requestId,
    expectedPaused: status.paused === true
  });
  tracePlaybackEvent("tidal-seek-forwarded", { requestedPosition: targetTime, accepted: sent });
  if (lastTidalSeekIntent !== seekIntent) return sent;
  const latestStatus = currentStatusData || {};
  const samePlaybackIdentity = String(latestStatus.trackId ?? "") === String(trackId)
    && String(latestStatus.playbackGeneration ?? "") === String(seekPlaybackGeneration ?? "")
    && String(latestStatus.requestId ?? "") === String(seekRequestId ?? "");
  if (!samePlaybackIdentity) {
    lastTidalSeekIntent = null;
    tracePlaybackEvent("tidal-seek-ack-stale", {
      requestedPosition: targetTime,
      requestedGeneration: seekPlaybackGeneration ?? null,
      currentGeneration: latestStatus.playbackGeneration ?? null
    });
    return false;
  }
  if (!sent) {
    lastTidalSeekIntent = null;
    return false;
  }
  seekIntent.pending = false;
  seekIntent.settledAt = Date.now();
  const latestDuration = parsePlaybackPosition(latestStatus.duration);
  const newSeekIdentity = getPlaybackInstanceIdentity(latestStatus, trackId);
  if (newSeekIdentity && latestDuration !== null && latestDuration > 0
      && targetTime > 0 && latestDuration - targetTime <= 1) {
    seekToEndPlaybackIdentity = newSeekIdentity;
    seekToEndTargetPosition = targetTime;
    seekToEndProgressObserved = false;
  } else if (seekToEndPlaybackIdentity === newSeekIdentity) {
    seekToEndPlaybackIdentity = null;
    seekToEndTargetPosition = null;
    seekToEndProgressObserved = false;
  }
  if (sent && !pluginDisposed && String(getCurrentTrackId()) === String(trackId)) {
    // TidaLuna's intercepted SEEK action does not update PlayState itself,
    // and native range input can leave its clock stale while libmpv seeks.
    // Publish the accepted target immediately so the footer timeline and the
    // engine share the same position, including while playback is paused.
    if (dispatchTidalAction("playbackControls/TIME_UPDATE", targetTime)) {
      lastMirroredUiTime = targetTime;
      tracePlaybackEvent("tidal-seek-position-mirrored", {
        requestedPosition: targetTime,
        source
      });
    }
    if (previousEngineSample?.trackId === trackId) {
      previousEngineSample = {
        ...previousEngineSample,
        at: now,
        tidalPosition: targetTime,
        enginePosition: targetTime,
      };
    }
  }
  return sent;
}

function getCurrentTrackId() {
  const playState = getTidalPlayState();
  // playbackContext.actualProductId is not consistently the queued track ID
  // in current TIDAL builds (it may identify the resolved playback context).
  // mediaProduct.productId matches the selected track and queue item.
  const mediaProductId = playState?.playbackControls?.mediaProduct?.productId;
  if (mediaProductId !== undefined && mediaProductId !== null) return mediaProductId;

  const contextTrackId = playState?.playbackContext?.actualProductId;
  if (contextTrackId !== undefined && contextTrackId !== null) return contextTrackId;

  try {
    const state = window.neptune?.store?.getState?.();
    const id = state?.playbackControls?.mediaProduct?.productId
      ?? state?.playback?.currentTrack?.id;
    if (id !== undefined && id !== null) return id;
  } catch (error) {}

  // TidaLuna can temporarily clear mediaProduct while a paused selection is
  // still shown in TIDAL's own now-playing footer. That footer is distinct from
  // the page URL and queue links, so use only its current-track anchor.
  const footer = getPlaybackFooter();
  const footerTrack = footer?.querySelector?.('a[href*="/track/"]');
  const footerHref = footerTrack?.getAttribute?.("href") || footerTrack?.href || "";
  const footerMatch = String(footerHref).match(/\/track\/(\d+)(?:[/?#]|$)/);
  if (footerMatch) return footerMatch[1];

  // Navigation URLs and visible playlist links do not identify the playing
  // item. Refuse to start a stream unless playback state supplies its ID.
  return null;
}

function getTidalPlaybackSnapshot() {
  const playState = getTidalPlayState();
  if (!playState) return null;
  const desired = String(playState.desiredState || "").toUpperCase();
  const actual = String(playState.state || "").toUpperCase();
  const trackId = getCurrentTrackId();
  const rawEnginePosition = currentStatusData?.timePos;
  const enginePosition = rawEnginePosition === undefined
      || rawEnginePosition === null
      || rawEnginePosition === ""
    ? NaN
    : Number(rawEnginePosition);
  const engineOwnsTrack = trackId !== null && trackId !== undefined
    && String(currentStatusData?.trackId) === String(trackId);
  const tidalPosition = getTidalPosition(playState);
  // Once libmpv has loaded the selected track, it is the clock authority. The
  // TIDAL UI is driven from TIME_UPDATE, but some PlayState builds keep both
  // currentTime and playTime at zero; reading that stale mirror back as a seek
  // would rewind a valid paused position on the next sync pass.
  const position = engineOwnsTrack && Number.isFinite(enginePosition) && enginePosition >= 0
    ? enginePosition
    : (tidalPosition ?? 0);
  return {
    trackId,
    // TIDAL's observed PlayState is authoritative. The local intent can remain
    // true after natural EOF, when TIDAL settles to PAUSED/NOT_PLAYING; using
    // that stale intent here repeatedly resumes an already-ended engine.
    playing: actual === "PLAYING" && desiredPlaybackIntent !== false,
    desired,
    actual,
    position,
    tidalPosition,
    title: getCurrentTrackMeta().title,
    artist: getCurrentTrackMeta().artist
  };
}

function isUninterceptedStalledPlayIntent(snapshot) {
  return String(snapshot?.actual || "").toUpperCase() === "STALLED"
    && String(snapshot?.desired || "").toUpperCase() === "PLAYING";
}

function getStalledPlayRecoveryMode(snapshot, engineTrackId, {
  pausedPreparationArmed = false,
  pausedSelectionPending = false,
  pausedSelectionConfirmed = false,
  pausedSelectionDiagnosticsReady = false,
  explicitPlayAuthorized = false,
} = {}) {
  const selectedTrackId = Number(snapshot?.trackId);
  if (!isUninterceptedStalledPlayIntent(snapshot)
      || !Number.isSafeInteger(selectedTrackId)
      || selectedTrackId <= 0
      || selectedTrackId === Number(engineTrackId)
      || pausedSelectionPending) return null;
  if (pausedPreparationArmed) return "prepare-paused";
  if (pausedSelectionConfirmed
      && (!pausedSelectionDiagnosticsReady || !explicitPlayAuthorized)) {
    return "block-confirmed-selection";
  }
  return explicitPlayAuthorized ? "start-play" : null;
}

function getCurrentTrackMeta() {
  let title = "";
  let artist = "";
  const playState = getTidalPlayState();
  const current = playState?.playbackControls?.mediaProduct;
  if (current) {
    title = current.title || current.name || "";
    artist = current.artist?.name || current.artists?.[0]?.name || "";
  }

  if (!title || !artist) {
    try {
      const state = window.neptune?.store?.getState?.();
      const fallback = state?.playback?.currentTrack || state?.playbackControls?.mediaProduct;
      title ||= fallback?.title || fallback?.name || "";
      artist ||= fallback?.artist?.name || fallback?.artists?.[0]?.name || "";
    } catch (error) {}
  }

  if (!title) title = document.querySelector('[data-test="footer-track-title"]')?.innerText || "";
  if (!artist) artist = document.querySelector('[data-test="footer-track-artist"]')?.innerText || "";
  return { title, artist };
}

function getCurrentDownloadTrackMeta() {
  const trackId = getCurrentTrackId();
  if (trackId === null || trackId === undefined || String(trackId).trim() === "") return null;

  let current = null;
  try {
    const playState = getTidalPlayState();
    const state = window.neptune?.store?.getState?.();
    current = playState?.playbackControls?.mediaProduct
      || state?.playbackControls?.mediaProduct
      || state?.playback?.currentTrack
      || null;
  } catch (_) {}

  const fallback = getCurrentTrackMeta();
  return {
    id: trackId,
    title: current?.title || current?.name || fallback.title,
    artist: current?.artist || current?.artists?.[0] || fallback.artist,
    album: current?.album || current?.albumTitle || "",
    albumArtist: current?.albumArtist || current?.album?.artist?.name || "",
    year: current?.year || current?.releaseDate || "",
    trackNumber: current?.trackNumber || current?.track_number || "",
    discNumber: current?.discNumber || current?.disc_number || current?.volumeNumber || "",
    isrc: current?.isrc || "",
    artworkURL: current?.artworkURL || current?.cover || "",
    lyrics: current?.lyrics || "",
    syncedLyrics: current?.syncedLyrics || current?.subtitles || ""
  };
}

async function triggerEnginePlay(trackId, timePos = 0, meta = {}, options = {}) {
  const key = String(trackId);
  const allowPaused = options.allowPaused === true;
  if (enginePlayPending && enginePlayPendingTrackId === key) return enginePlayPending;

  tracePlaybackEvent("engine-play-request", {
    requestedTrackId: key,
    requestedPosition: Number(timePos) || 0,
    allowPaused
  });

  const previous = currentStatusData || {};
  if (previous.trackId !== null && previous.trackId !== undefined
      && String(previous.trackId) !== key && previous.paused === false) {
    void sendEngineCommand("pause", {
      trackId: Number(previous.trackId),
      playbackGeneration: previous.playbackGeneration,
      requestId: previous.requestId
    });
  }
  if (enginePlayRequestId) cancelPendingEnginePlay();
  const generation = ++enginePlayGeneration;
  enginePlayAbortController?.abort();
  const abortController = new AbortController();
  enginePlayAbortController = abortController;
  enginePlayPendingTrackId = key;
  let requestId = null;
  const task = (async () => {
    if (pluginDisposed || generation !== enginePlayGeneration) return false;
    console.log(`[Omniphony EX] Requesting prioritized playback for Track ${key}...`);
    try {
      const readiness = await checkPlaybackReadiness();
      if (pluginDisposed || generation !== enginePlayGeneration || abortController.signal.aborted) {
        return false;
      }
      if (!readiness.ready) {
        reportPlaybackReadinessBlock(readiness.reason);
        throw new Error(readiness.reason);
      }
      // Readiness can take long enough for a pause or track transition to
      // arrive. Revalidate TidaLuna's authoritative state at the dispatch edge.
      const latestPreflightSnapshot = getTidalPlaybackSnapshot();
      const preflightExplicitPlayPending = !allowPaused
        && (hasPendingExplicitPlayIntent(key, currentStatusData)
          || shouldPreserveExplicitPlayIntent(
            currentStatusData, key, currentStatusData?.trackId, desiredPlaybackIntent
          ));
      if (
        pluginDisposed
        || generation !== enginePlayGeneration
        || abortController.signal.aborted
        || String(latestPreflightSnapshot?.trackId ?? "") !== key
        || (latestPreflightSnapshot?.playing !== true && !preflightExplicitPlayPending) !== allowPaused
      ) {
        return false;
      }
      currentConfig = { ...currentConfig, ...readiness.config };
      requestId = crypto.randomUUID();
      enginePlayRequestId = requestId;
      enginePlayRequestTrackId = key;
      enginePlayAllowPaused = allowPaused;
      const res = await requestWithTimeout(`${API_BASE}/play`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trackId: Number(trackId),
          timePos: Number(timePos),
          title: meta.title || "",
          artist: meta.artist || "",
          requestId,
          // Keep libmpv silent while the service resolves/converts the stream.
          // TIDAL remains the playback authority and must confirm this track is
          // still playing before the engine is explicitly resumed.
          startPaused: true
        }),
        signal: abortController.signal
      }, 240000);
      if (!res.ok) {
        // Keep useful service diagnostics in DevTools without ever exposing
        // signed media URLs or bearer material that might appear in a body.
        let detail = "";
        try {
          const payload = await res.json();
          const raw = typeof payload?.error === "string" ? payload.error : "";
          const safe = raw
            .replace(/https?:\/\/\S+/gi, "[url]")
            .replace(/(access|refresh)[_-]?token\s*[:=]\s*\S+/gi, "$1=[redacted]")
            .replace(/[\r\n\t]+/g, " ")
            .slice(0, 240);
          const d = payload?.diagnostics;
          const loadEvent = Array.isArray(d?.recentMpvLoadEvents)
            ? [...d.recentMpvLoadEvents].reverse().find((event) => event?.event === "end-file")
            : null;
          const mpvError = typeof loadEvent?.error === "string"
            ? ` mpv=${loadEvent.error.replace(/https?:\/\/\S+/gi, "[url]").slice(0, 160)}`
            : "";
          const proxy = d?.dashProxy;
          const proxySummary = proxy && typeof proxy === "object"
            ? ` proxy(req=${Number(proxy.segmentRequests) || 0}, bytes=${Number(proxy.bytesServed) || 0}, failed=${Number(proxy.failedSegments) || 0})`
            : "";
          const summary = d && typeof d === "object"
            ? ` load(file=${Boolean(d.fileLoaded)}, path=${Boolean(d.pathMatched)}, audio=${Boolean(d.audioParamsAvailable)}, rate=${Number(d.sampleRate) || 0}, ch=${Number(d.channelCount) || 0}, target=${String(d.expectedTargetKind || "unknown")}, active=${String(d.activePathKind || "unknown")})${mpvError}${proxySummary}`
            : "";
          detail = `${safe}${summary}`;
        } catch (_) {}
        throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
      }
      const data = await res.json();
      if (pluginDisposed || generation !== enginePlayGeneration) return false;
      if (data.requestId !== requestId) {
        throw new Error("OmniFusion returned a mismatched playback request identity");
      }
      if (Number.isSafeInteger(Number(data.generation))) {
        currentStatusData.playbackGeneration = Number(data.generation);
      }
      tracePlaybackEvent("engine-play-accepted", {
        requestedTrackId: key,
        requestedPosition: Number(timePos) || 0,
        acceptedGeneration: Number(data.generation) || null,
        allowPaused
      });
      lastPlayAttempt = { trackId: key, at: Date.now() };
      currentFormatLabel = data.label || data.format || "Atmos";
      const latestTidal = getTidalPlaybackSnapshot();
      const sameSelectedTrack = String(latestTidal?.trackId ?? "") === key;
      const explicitPlayStillPending = !allowPaused
        && (hasPendingExplicitPlayIntent(key, currentStatusData)
          || shouldPreserveExplicitPlayIntent(
            currentStatusData, key, currentStatusData?.trackId, desiredPlaybackIntent
          ));
      if (!allowPaused && sameSelectedTrack
          && (latestTidal.playing === true || explicitPlayStillPending)) {
        const resumed = await sendEngineCommand("resume", {
          trackId: Number(key),
          playbackGeneration: data.generation,
          requestId,
        });
        if (!resumed) throw new Error("libmpv did not accept the TIDAL playback resume");

        // Close the small state-change window between the pre-resume snapshot
        // and the HTTP control reaching the service.
        const confirmedTidal = getTidalPlaybackSnapshot();
        const explicitPlayStillCurrent = !allowPaused
          && (hasPendingExplicitPlayIntent(key, currentStatusData)
            || shouldPreserveExplicitPlayIntent(
              currentStatusData, key, currentStatusData?.trackId, desiredPlaybackIntent
            ));
        if (String(confirmedTidal?.trackId ?? "") === key
            && (confirmedTidal.playing === true || explicitPlayStillCurrent)) {
          lastMirroredPlaybackState = "playing";
        } else {
          await sendEngineCommand("pause", {
            trackId: Number(key),
            playbackGeneration: data.generation,
            requestId,
          });
          lastMirroredPlaybackState = "paused";
          cancelPendingEnginePlay();
          return false;
        }
        enginePlayRequestId = null;
        enginePlayRequestTrackId = null;
        enginePlayAllowPaused = false;
      } else if (sameSelectedTrack && allowPaused) {
        // A deliberate paused selection is prepared for diagnostics without
        // ever resuming libmpv, even if TIDAL races into PLAYING while the
        // service resolves. Reassert the paused UI and keep the prepared
        // identity so a later verified Play can resume this exact stream.
        setTidalPlaybackIntent(false);
        lastMirroredPlaybackState = "paused";
        enginePlayRequestId = null;
        enginePlayRequestTrackId = null;
        enginePlayAllowPaused = false;
      } else {
        // A track that was paused or superseded during resolution stays silent
        // and its prepared server state is invalidated immediately.
        cancelPendingEnginePlay();
        lastMirroredPlaybackState = "paused";
        return false;
      }
      updateBadgeUI(currentFormatLabel);
      return true;
    } catch (e) {
      if (pluginDisposed || generation !== enginePlayGeneration || e?.name === "AbortError") return false;
      console.error("[Omniphony EX] Engine play dispatch failed:", e);
      if (allowPaused) {
        // A failed diagnostics-only preparation must not immediately retry on
        // every status poll. Require an explicit re-arm or a new track.
        pausedSelectionConfirmedTrackId = null;
        pausedSelectionUserPlayAuthorization = null;
        setTidalPlaybackIntent(false);
        const statusCode = /\bHTTP\s+(\d{3})\b/i.exec(String(e?.message || ""))?.[1];
        updatePausedSelectionUi(
          `Paused stream preparation failed${statusCode ? ` (HTTP ${statusCode})` : ""}. Playback remains paused. Press Prepare to retry.`
        );
      }
      cancelPendingEnginePlay();
      return false;
    }
  })().finally(() => {
    if (generation === enginePlayGeneration) {
      enginePlayPending = null;
      enginePlayPendingTrackId = null;
      enginePlayAbortController = null;
    }
  });
  enginePlayPending = task;
  return task;
}

function cancelPendingEnginePlay() {
  const requestId = enginePlayRequestId;
  if (!requestId) return false;
  const requestTrackId = enginePlayRequestTrackId;
  enginePlayGeneration += 1;
  enginePlayAbortController?.abort();
  enginePlayAbortController = null;
  enginePlayPending = null;
  enginePlayPendingTrackId = null;
  enginePlayRequestId = null;
  enginePlayRequestTrackId = null;
  enginePlayAllowPaused = false;
  if (lastPlayAttempt.trackId === requestTrackId) {
    lastPlayAttempt = { trackId: null, at: 0 };
  }
  void requestWithTimeout(`${API_BASE}/cancel-play`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestId })
  }, 3500).catch((error) => {
    console.warn("[Omniphony EX] Playback cancellation was not acknowledged:", error);
  });
  return true;
}

function queueTransportCommand(endpoint, body, revision, dispatch, queues, inFlight) {
  const transportKey = JSON.stringify([
    body?.trackId ?? "",
    body?.playbackGeneration ?? "",
    body?.requestId ?? "",
  ]);
  const requestKey = JSON.stringify([transportKey, endpoint, revision]);
  const existing = inFlight.get(requestKey);
  if (existing) return existing;

  const previous = queues.get(transportKey) || Promise.resolve(true);
  const scheduled = Promise.resolve(previous)
    .catch(() => false)
    .then(() => dispatch());
  queues.set(transportKey, scheduled);
  inFlight.set(requestKey, scheduled);
  const cleanup = () => {
    if (inFlight.get(requestKey) === scheduled) inFlight.delete(requestKey);
    if (queues.get(transportKey) === scheduled) queues.delete(transportKey);
  };
  void scheduled.then(cleanup, cleanup);
  return scheduled;
}

function sendEngineCommand(endpoint, body = {}) {
  if (endpoint !== "pause" && endpoint !== "resume") {
    return sendEngineCommandDirect(endpoint, body);
  }
  const revision = transportIntentRevision;
  return queueTransportCommand(
    endpoint,
    body,
    revision,
    () => {
      if (revision !== transportIntentRevision) {
        tracePlaybackEvent("engine-command-stale-intent", {
          endpoint,
          commandRevision: revision,
          currentRevision: transportIntentRevision
        });
        return false;
      }
      return sendEngineCommandDirect(endpoint, body);
    },
    transportCommandQueues,
    transportCommandsInFlight,
  );
}

async function sendEngineCommandDirect(endpoint, body = {}) {
  if (pluginDisposed) return false;
  const traceable = endpoint === "seek" || endpoint === "pause" || endpoint === "resume";
  if (traceable) tracePlaybackEvent("engine-command-sent", {
    endpoint,
    requestedPosition: endpoint === "seek" ? Number(body.timePos) : undefined,
    requestedTrackId: body.trackId ?? null
  });
  try {
    const res = await requestWithTimeout(`${API_BASE}/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }, 3500);
    if (!res.ok) {
      let seekDiagnostic = null;
      if (endpoint === "seek") {
        try {
          const payload = await res.json();
          const raw = payload?.seekDiagnostic;
          if (raw && typeof raw === "object") {
            const finiteOrNull = (value) => (
              typeof value === "number" && Number.isFinite(value) ? value : null
            );
            const booleanOrNull = (value) => (
              typeof value === "boolean" ? value : null
            );
            seekDiagnostic = {
              targetTimePos: finiteOrNull(raw.targetTimePos),
              ...(raw.seekMode === "absolute" || raw.seekMode === "absolute+exact"
                ? { seekMode: raw.seekMode } : {}),
              preSeekTimePos: finiteOrNull(raw.preSeekTimePos),
              preSeekInProgress: booleanOrNull(raw.preSeekInProgress),
              settlementTimePos: finiteOrNull(raw.settlementTimePos),
              postSeekTimePos: finiteOrNull(raw.postSeekTimePos),
              postSeekInProgress: booleanOrNull(raw.postSeekInProgress),
              paused: booleanOrNull(raw.paused),
              settlementSamples: Array.isArray(raw.settlementSamples)
                ? raw.settlementSamples.slice(-64).filter((sample) => (
                  sample && typeof sample === "object" && !Array.isArray(sample)
                )).map((sample) => ({
                  elapsedMs: finiteOrNull(sample.elapsedMs),
                  timePos: finiteOrNull(sample.timePos),
                  seeking: booleanOrNull(sample.seeking),
                  paused: booleanOrNull(sample.paused),
                  inTolerance: booleanOrNull(sample.inTolerance),
                  consecutive: Number.isInteger(sample.consecutive) && sample.consecutive >= 0
                    ? sample.consecutive : null,
                  stableMs: finiteOrNull(sample.stableMs),
                  ...(typeof sample.failure === "string" && /^[a-z-]{1,40}$/.test(sample.failure)
                    ? { failure: sample.failure } : {})
                }))
                : []
            };
          }
        } catch (error) {}
      }
      if (traceable) tracePlaybackEvent("engine-command-rejected", {
        endpoint,
        httpStatus: res.status,
        ...(seekDiagnostic ? { seekDiagnostic } : {})
      });
      console.warn(`[Omniphony EX] Engine ${endpoint} returned HTTP ${res.status}`);
      return false;
    }
    if (traceable) tracePlaybackEvent("engine-command-accepted", { endpoint, httpStatus: res.status });
    return true;
  } catch (e) {
    if (traceable) tracePlaybackEvent("engine-command-failed", { endpoint, errorName: e?.name || "Error" });
    console.warn(`[Omniphony EX] Engine ${endpoint} request failed:`, e);
    return false;
  }
}

function refreshSettingsTelemetry(status) {
  const tidalTrackId = getCurrentTrackId();
  const sameTrack = tidalTrackId !== null && status?.trackId !== null
    && status?.trackId !== undefined
    && String(tidalTrackId) === String(status.trackId);
  updateSettingsPanelStatus(status || {}, tidalTrackId, sameTrack, getCurrentTrackMeta());
}

function shouldPreserveExplicitPlayIntent(status, tidalTrackId, engineTrackId, explicitIntent) {
  const selectedTrackId = Number(tidalTrackId);
  return explicitIntent === true
    && Number.isSafeInteger(selectedTrackId)
    && selectedTrackId > 0
    && selectedTrackId === Number(engineTrackId)
    && status?.paused === false
    && status?.playbackState === "playing";
}

function shouldResumeEngine(
  tidalIsPlaying, status, tidalTrackId, engineTrackId, preparedRequestPending = false
) {
  if (!tidalIsPlaying || status?.paused !== true) return false;
  if (status?.playbackState === "prepared") {
    // A completed preparation can be committed later if TIDAL resumes the
    // same selected track. The current sync pass has just read PlayState.
    return !preparedRequestPending
      && String(tidalTrackId ?? "") === String(engineTrackId ?? "");
  }
  return true;
}

function isAtNaturalEnd(tidalIsPlaying, status, tidalTrackId, engineTrackId) {
  if (!tidalIsPlaying) return false;
  const selectedTrackId = Number(tidalTrackId);
  const activeTrackId = Number(engineTrackId);
  const reportedTrackId = Number(status?.trackId);
  const duration = Number(status?.duration);
  const position = Number(status?.timePos);
  if (!Number.isSafeInteger(selectedTrackId) || selectedTrackId <= 0
      || selectedTrackId !== activeTrackId || selectedTrackId !== reportedTrackId
      || !Number.isFinite(duration) || duration <= 0
      || !Number.isFinite(position) || position <= 0
      || position > duration + 0.6) return false;
  return duration - position <= 0.6;
}

function getPlaybackInstanceIdentity(status, trackId) {
  const trackKey = String(Number(trackId));
  const reportedTrackId = Number(status?.trackId);
  const playbackGeneration = Number(status?.playbackGeneration);
  const requestId = String(status?.requestId ?? "");
  const serviceInstanceId = String(status?.serviceInstanceId ?? "");
  const mpvInstanceId = String(status?.mpvInstanceId ?? "");
  if (!Number.isSafeInteger(Number(trackKey)) || Number(trackKey) <= 0
      || reportedTrackId !== Number(trackKey)
      || !Number.isSafeInteger(playbackGeneration) || playbackGeneration <= 0
      || !requestId || !serviceInstanceId || !mpvInstanceId) return null;
  return JSON.stringify([
    serviceInstanceId,
    mpvInstanceId,
    trackKey,
    playbackGeneration,
    requestId,
  ]);
}

function getStableNaturalEndIdentity(identity, previousObservation, status, now, intentRevision) {
  const sequence = Number(status?.timePosSampleSequence);
  const position = Number(status?.timePos);
  const duration = Number(status?.duration);
  if (!identity || !Number.isSafeInteger(sequence) || sequence < 0
      || !Number.isFinite(position) || !Number.isFinite(duration) || duration <= 0
      || position <= 0 || position > duration + 0.6) {
    return { identity: null, observation: null };
  }

  const matchingObservation = previousObservation?.identity === identity
    && previousObservation?.intentRevision === intentRevision;
  const sequenceAdvanced = matchingObservation
    && Number.isSafeInteger(previousObservation.sequence)
    && sequence > previousObservation.sequence;
  const positionStable = matchingObservation
    && Number.isFinite(previousObservation.position)
    && Math.abs(position - previousObservation.position) <= 0.6;
  if (!sequenceAdvanced || !positionStable) {
    return {
      identity: null,
      observation: { identity, intentRevision, startedAt: now, sequence, position },
    };
  }

  const observation = { ...previousObservation, sequence, position };
  return {
    identity: now - previousObservation.startedAt >= 750 ? identity : null,
    observation,
  };
}

function getNaturalEndPlaybackIdentity(
  tidalIsPlaying,
  status,
  tidalTrackId,
  engineTrackId,
  previousSample,
  now,
  explicitPause,
  trackReplacementPending,
  seekPending,
  clockSampleCurrent,
) {
  if (explicitPause || trackReplacementPending || seekPending || !clockSampleCurrent
      || !isAtNaturalEnd(tidalIsPlaying, status, tidalTrackId, engineTrackId)) return null;
  if (!new Set(["playing", "paused"]).has(String(status?.playbackState || "").toLowerCase())
      || typeof status?.paused !== "boolean") return null;

  const identity = getPlaybackInstanceIdentity(status, tidalTrackId);
  const previousIdentity = getPlaybackInstanceIdentity(previousSample, tidalTrackId);
  const previousPosition = Number(previousSample?.enginePosition);
  const previousAt = Number(previousSample?.at);
  const currentPosition = Number(status?.timePos);
  const elapsedSeconds = (Number(now) - previousAt) / 1000;
  const advancedSeconds = currentPosition - previousPosition;
  if (previousIdentity !== identity
      || !Number.isFinite(previousPosition)
      || !Number.isFinite(previousAt)
      || !Number.isFinite(elapsedSeconds)
      || elapsedSeconds < 0 || elapsedSeconds > 2.5
      || !Number.isFinite(advancedSeconds)
      || advancedSeconds <= 0.01
      || advancedSeconds > elapsedSeconds + 1.25) return null;

  // Generation/request identity permits a replay of the same catalog track
  // to advance again, while repeated polls of one EOF remain deduped.
  return identity;
}

function getNaturalEndHandoffAction(handoff, trackId, queueIndex, desiredPlaybackIntent, now = Date.now()) {
  if (!handoff) return { action: "none" };
  if (desiredPlaybackIntent === false) return { action: "cancel", reason: "explicit-pause" };
  if (!Number.isFinite(Number(handoff.expiresAt)) || now >= Number(handoff.expiresAt)) {
    return { action: "cancel", reason: "timeout" };
  }
  const selectedTrackId = String(trackId ?? "");
  const selectedQueueIndex = Number(queueIndex);
  const queueAdvanced = Number.isSafeInteger(selectedQueueIndex)
    && selectedQueueIndex !== handoff.sourceQueueIndex;
  if (!selectedTrackId
      || (selectedTrackId === handoff.sourceTrackId && !queueAdvanced)) {
    return { action: "wait" };
  }
  return {
    action: "start",
    trackId: selectedTrackId,
    queueIndex: Number.isSafeInteger(selectedQueueIndex) ? selectedQueueIndex : null,
  };
}

function hasTidalClockDiscontinuity(
  previousSample, currentTrackId, currentPosition, elapsedSeconds, currentTidalIsPlaying
) {
  // This identifies an unexpected sampled clock jump for diagnostics and to
  // suppress one drift correction. It never instructs libmpv to seek:
  // deliberate seeks arrive through explicit Redux/native input handlers.
  if (previousSample?.tidalPlaying !== true || currentTidalIsPlaying !== true) return false;
  if (!previousSample || String(previousSample.trackId) !== String(currentTrackId)
      || !Number.isFinite(currentPosition) || !Number.isFinite(elapsedSeconds)
      || elapsedSeconds <= 0 || !Number.isFinite(previousSample.tidalPosition)) return false;
  const sourceJump = currentPosition - previousSample.tidalPosition;
  return sourceJump < -0.75 || sourceJump > elapsedSeconds + 1.25;
}

function shouldSuppressStaleZeroSeek(currentPosition, status, trackId, now = Date.now()) {
  const enginePosition = parsePlaybackPosition(status?.timePos);
  return Number.isFinite(currentPosition)
    && currentPosition <= 0.05
    && enginePosition !== null
    && enginePosition > 1
    && !hasRecentTidalSeekIntent(trackId, currentPosition, now);
}

function invalidateEngineStatus(message = "OmniFusion status unavailable") {
  clearNativeTimelineProjection();
  currentStatusData = {
    trackId: null,
    playbackGeneration: null,
    playbackState: "unavailable",
    format: null,
    engineClockAdvancing: false,
    paused: true,
    title: "",
    audioParams: null,
    audioCodec: "",
    audioBitrate: 0,
    device: "",
    statusMessage: message
  };
  previousEngineSample = null;
  lastMirroredPlaybackState = null;
  const formatEl = document.getElementById("omniphony-panel-format");
  if (formatEl) formatEl.innerText = "Engine unavailable";
  const subEl = document.getElementById("omniphony-panel-sub");
  if (subEl) subEl.innerText = message;
  updateBadgeUI(currentFormatLabel);
}

/**
 * Reconcile TidaLuna playback intent and libmpv. State, track, and seek events
 * trigger immediate control sync; the 100 ms poll mirrors the authoritative
 * libmpv clock into TIDAL and records playback telemetry.
 */
function startSyncLoop() {
  if (pollTimer) clearInterval(pollTimer);
  if (clockPollTimer) clearInterval(clockPollTimer);
  let syncBusy = false;
  let syncRequested = false;
  let syncScheduled = false;
  let clockPollBusy = false;
  let naturalEndObservation = null;
  let naturalEndHandoff = null;
  let lastSyncDiagnosticKey = null;
  let lastLyricsReduxDiagnosticKey = null;
  let lastSyncError = null;
  let lastClockPollError = null;
  const scheduleImmediateSync = () => {
    if (pluginDisposed) return;
    if (syncBusy) {
      syncRequested = true;
      return;
    }
    if (syncScheduled) return;
    syncScheduled = true;
    queueMicrotask(() => {
      syncScheduled = false;
      if (syncBusy) {
        syncRequested = true;
        return;
      }
      void runSync();
    });
  };
  const runSync = async () => {
    if (syncBusy || pluginDisposed) return;
    syncBusy = true;
    try {
      const res = await requestWithTimeout(`${API_BASE}/status`, { cache: "no-store" }, 2500);
      if (pluginDisposed) return;
      if (!res.ok) {
        invalidateEngineStatus(`OmniFusion service unavailable · HTTP ${res.status}`);
        return;
      }
      let status = await res.json();
      if (!await ensureEngineClockAuthority(status)) return;
      const statusObservedAt = Date.now();
      const statusReceivedMonotonicAt = getMonotonicNow();
      let engineSampleAnchor = resolveEngineSampleAnchor(
        status, statusReceivedMonotonicAt, statusObservedAt,
      );
      const statusPosition = parsePlaybackPosition(status.timePos);
      const statusDuration = parsePlaybackPosition(status.duration);
      const statusClockUsable = engineSampleAnchor.valid
        && statusPosition !== null
        && statusDuration !== null
        && statusDuration > 0
        && typeof status.paused === "boolean";
      const clockSampleAccepted = statusClockUsable
        && acceptEngineClockSampleOrdering(status);
      const statusSequence = Number(status.timePosSampleSequence);
      const staleStatusSample = statusClockUsable
        && !clockSampleAccepted
        && String(status.serviceInstanceId ?? "") === activeClockServiceInstanceId
        && String(status.mpvInstanceId ?? "") === activeClockMpvInstanceId
        && Number.isSafeInteger(statusSequence)
        && statusSequence <= clockOrderingSequence;
      let clockSampleCurrent = clockSampleAccepted;
      if (staleStatusSample) {
        const mergedStatus = mergeStatusWithLatestFastClockSample(
          status, lastFastClockSample, statusReceivedMonotonicAt,
        );
        if (!mergedStatus) {
          tracePlaybackEvent("stale-status-sample-ignored", {
            sampleSequence: statusSequence,
            acceptedSequence: clockOrderingSequence,
            trackId: status.trackId,
            playbackGeneration: status.playbackGeneration,
          });
          return;
        }
        status = mergedStatus;
        // The merged status now carries the newest fast-clock sample already
        // accepted by the ordering gate. The original /status sample remains
        // stale, but its merged replacement is current for EOF confirmation.
        clockSampleCurrent = true;
        engineSampleAnchor = resolveEngineSampleAnchor(
          status, statusReceivedMonotonicAt, statusObservedAt,
        );
        tracePlaybackEvent("stale-status-control-merged", {
          sampleSequence: statusSequence,
          acceptedSequence: clockOrderingSequence,
          playbackGeneration: status.playbackGeneration,
        });
      }
      if (clockSampleAccepted) commitEngineClockSampleOrdering(status);
      currentStatusData = status;

      const tidal = getTidalPlaybackSnapshot();
      const mediaTrackId = Number(tidal?.trackId);
      const engineTrackId = Number(status.trackId);
      const tidalIsPlaying = Boolean(tidal?.playing && mediaTrackId > 0);
      const enginePlayStartupPending = isEnginePlayStartupPending(mediaTrackId);

      // Album-row playback can leave TIDAL at STALLED/PLAYING without reaching
      // either the native media.play hook or Redux PLAY interceptor. Recover
      // only when TIDAL's explicit desired state is PLAYING and the engine is
      // still on a different track. Unarmed recovery additionally requires a
      // fresh trusted Play-button gesture; a buffering state alone is not intent.
      const stalledPlayRecoveryMode = naturalEndHandoff ? null
        : getStalledPlayRecoveryMode(tidal, engineTrackId, {
          pausedPreparationArmed: prepareNextSelectionPaused,
          pausedSelectionPending: Boolean(pausedSelectionPending),
          pausedSelectionConfirmed: pausedSelectionConfirmedTrackId !== null,
          pausedSelectionDiagnosticsReady: pausedSelectionConfirmedTrackId !== null
            && isPausedSelectionDiagnosticsReady(pausedSelectionConfirmedTrackId),
          explicitPlayAuthorized: hasStalledPlayAuthorization(mediaTrackId, Date.now(), tidal)
            && (pausedSelectionConfirmedTrackId === null
              || hasPausedSelectionUserPlayAuthorization(mediaTrackId)),
        });
      if (stalledPlayRecoveryMode) {
        if (stalledPlayRecoveryMode === "prepare-paused") {
          prepareNextSelectionPaused = false;
          pausedSelectionPending = {
            trackId: String(mediaTrackId),
            expiresAt: Date.now() + 5000,
            expired: false,
          };
          pausedSelectionConfirmedTrackId = null;
          pausedSelectionUserPlayAuthorization = null;
          stalledPlayAuthorization = null;
          pendingExplicitPlayIntent = null;
          desiredPlaybackIntent = false;
          tracePlaybackEvent("stalled-play-selection-held-paused", {
            selectedTrackId: mediaTrackId,
            engineTrackId,
          });
          setTidalPlaybackIntent(false);
          updatePausedSelectionUi("TIDAL selected the track without delivering its native Play event. Holding it paused while OmniFusion prepares diagnostics.");
          return;
        }

        if (stalledPlayRecoveryMode === "block-confirmed-selection") {
          const confirmedTrackId = pausedSelectionConfirmedTrackId;
          const queueMatches = String(getQueuedTrackId() ?? "") === confirmedTrackId
            && confirmedTrackId === String(mediaTrackId);
          const diagnosticsReady = queueMatches && isPausedSelectionDiagnosticsReady(confirmedTrackId);
          setTidalPlaybackIntent(false);
          updatePausedSelectionUi(!queueMatches
            ? "TIDAL queue changed after selection verification. Playback remains paused until the selected track is verified again."
            : diagnosticsReady
              ? "Stream diagnostics are ready. Use TIDAL's Play control to start this track."
              : "The selected stream diagnostics are still loading. Playback remains paused.");
          return;
        }

        const stalledTrackKey = String(mediaTrackId);
        const now = Date.now();
        if (!consumeStalledPlayAuthorization(stalledTrackKey, now, tidal)) return;
        pausedSelectionConfirmedTrackId = null;
        pausedSelectionUserPlayAuthorization = null;
        desiredPlaybackIntent = true;
        playIntentChangedAt = now;
        pendingExplicitPlayIntent = { trackId: stalledTrackKey, startedAt: now };
        tracePlaybackEvent("stalled-play-intent-recovered", {
          selectedTrackId: mediaTrackId,
          engineTrackId,
        });
        if ((!enginePlayPending || enginePlayPendingTrackId !== stalledTrackKey)
            && (lastPlayAttempt.trackId !== stalledTrackKey
              || now - lastPlayAttempt.at > 5000)) {
          lastPlayAttempt = { trackId: stalledTrackKey, at: now };
          void triggerEnginePlay(mediaTrackId, tidal.position, {
            title: tidal.title,
            artist: tidal.artist,
          });
        }
        return;
      }
      if (holdSyncForPausedSelection(mediaTrackId, statusObservedAt)) {
        // This path intentionally skips transport reconciliation, but the
        // diagnostics-first state must still refresh the footer and overlay.
        currentFormatLabel = status.label || "Omniphony EX";
        updateBadgeUI(currentFormatLabel);
        refreshSettingsTelemetry(status);
        if (clockSampleAccepted) {
          projectEngineClockToNativeTimeline(
            status, mediaTrackId, statusReceivedMonotonicAt, statusObservedAt,
          );
        }
        return;
      }
      if (enginePlayRequestId
          && !enginePlayStartupPending
          && ((!enginePlayAllowPaused && !tidalIsPlaying)
            || String(mediaTrackId) !== enginePlayRequestTrackId)) {
        cancelPendingEnginePlay();
      }
      if (pausedSelectionConfirmedTrackId !== null
          && pausedSelectionConfirmedTrackId !== String(mediaTrackId)) {
        pausedSelectionConfirmedTrackId = null;
      }
      if (pausedSelectionConfirmedTrackId !== null) updatePausedSelectionUi();

      currentFormatLabel = status.label || "Omniphony EX";
      updateBadgeUI(currentFormatLabel);

      refreshSettingsTelemetry(status);

      const diagnosticKey = [
        mediaTrackId || "none",
        engineTrackId || "none",
        tidalIsPlaying ? "playing" : (tidal?.actual || "unknown"),
        status.paused ? "engine-paused" : "engine-running"
      ].join("|");
      if (diagnosticKey !== lastSyncDiagnosticKey) {
        lastSyncDiagnosticKey = diagnosticKey;
        console.info(`[Omniphony EX] Sync state: tidalTrack=${mediaTrackId || "none"}, tidal=${tidal?.actual || "unknown"}/${tidal?.desired || "unknown"}, engineTrack=${engineTrackId || "none"}, engine=${status.paused ? "paused" : "running"}.`);
      }
      // Do not reconcile the intentionally paused preparation state back into
      // TIDAL or issue transport commands while the matching Play request owns
      // startup. triggerEnginePlay performs the guarded resume after /play
      // returns and rechecks the native TIDAL intent at that dispatch edge.
      if (enginePlayStartupPending) {
        setTidalUiPlaybackState("PLAYING");
        return;
      }

      // TIDAL's queue can select the next item while its native player remains
      // NOT_PLAYING because our native audio guard intercepted media.play().
      // Carry the natural-end autoplay intent across that one queue transition
      // and start the matching libmpv stream. A deliberate Pause cancels it;
      // the deadline prevents a stale handoff from starting a later selection.
      if (naturalEndHandoff) {
        const queueIndex = Number(getTidalPlayState()?.playQueue?.currentIndex);
        const handoffAction = getNaturalEndHandoffAction(
          naturalEndHandoff, mediaTrackId, queueIndex, desiredPlaybackIntent, Date.now(),
        );
        if (handoffAction.action === "cancel") {
          tracePlaybackEvent("natural-end-handoff-cancelled", {
            sourceTrackId: naturalEndHandoff.sourceTrackId,
            selectedTrackId: mediaTrackId,
            reason: handoffAction.reason,
          });
          naturalEndHandoff = null;
        } else if (handoffAction.action === "start" && mediaTrackId > 0) {
            const selectionChanged = naturalEndHandoff.targetTrackId !== handoffAction.trackId
              || naturalEndHandoff.targetQueueIndex !== handoffAction.queueIndex;
            if (selectionChanged) {
              naturalEndHandoff.targetTrackId = handoffAction.trackId;
              naturalEndHandoff.targetQueueIndex = handoffAction.queueIndex;
              naturalEndHandoff.intentGranted = false;
              tracePlaybackEvent("natural-end-handoff-selected", {
                sourceTrackId: naturalEndHandoff.sourceTrackId,
                targetTrackId: mediaTrackId,
                queueIndex: Number.isSafeInteger(queueIndex) ? queueIndex : null,
              });
            }
            if (engineTrackId === Number(handoffAction.trackId) && status.paused === false) {
              naturalEndHandoff = null;
            } else if (!naturalEndHandoff.intentGranted) {
              setTidalPlaybackIntent(true);
              naturalEndHandoff.intentGranted = true;
            }
            if (naturalEndHandoff
                && engineTrackId !== Number(handoffAction.trackId)
                && (!enginePlayPending || enginePlayPendingTrackId !== String(mediaTrackId))
                && (lastPlayAttempt.trackId !== String(mediaTrackId)
                  || Date.now() - lastPlayAttempt.at > 5000)) {
              lastPlayAttempt = { trackId: String(mediaTrackId), at: Date.now() };
              tracePlaybackEvent("natural-end-handoff-play-request", {
                sourceTrackId: naturalEndHandoff.sourceTrackId,
                targetTrackId: mediaTrackId,
                tidalIsPlaying,
              });
              // The queue has moved to a new track, but TIDAL can still expose
              // the completed track's terminal position during this handoff.
              // Always begin the newly selected stream at zero; a stale offset
              // makes the service seek the new MPD to the prior track's EOF.
              void triggerEnginePlay(mediaTrackId, 0, {
                title: tidal.title,
                artist: tidal.artist,
              });
              return;
            }
        }
      }

      // TidaLuna PlayState is the playback authority. HTML media elements are
      // not required: current TIDAL builds can keep their player outside DOM.
      // Retry a failed request even when it is the same selected track.
      if (tidalIsPlaying && engineTrackId !== mediaTrackId
          && (!enginePlayPending || enginePlayPendingTrackId !== String(mediaTrackId))
          && (lastPlayAttempt.trackId !== String(mediaTrackId)
            || Date.now() - lastPlayAttempt.at > 5000)) {
        lastPlayAttempt = { trackId: String(mediaTrackId), at: Date.now() };
        console.info(`[Omniphony EX] TIDAL PlayState selected ${mediaTrackId}; requesting the matching libmpv stream.`);
        void triggerEnginePlay(mediaTrackId, tidal.position, { title: tidal.title, artist: tidal.artist });
        return;
      }

      // A selected paused track can be resolved and held in MPV for the
      // footer diagnostics and route preflight. This never resumes playback.
      if (!tidalIsPlaying && mediaTrackId > 0
          && pausedSelectionConfirmedTrackId === String(mediaTrackId)
          && engineTrackId !== mediaTrackId
          && (!enginePlayPending || enginePlayPendingTrackId !== String(mediaTrackId))
          && (lastPlayAttempt.trackId !== String(mediaTrackId)
            || Date.now() - lastPlayAttempt.at > 5000)) {
        lastPlayAttempt = { trackId: String(mediaTrackId), at: Date.now() };
        console.info(`[Omniphony EX] Preparing paused TIDAL selection ${mediaTrackId} for route and stream diagnostics.`);
        void triggerEnginePlay(
          mediaTrackId,
          tidal.position,
          { title: tidal.title, artist: tidal.artist },
          { allowPaused: true }
        );
        return;
      }

      if (mediaTrackId > 0 && engineTrackId === mediaTrackId) {
        const holdSeekClock = shouldHoldEngineClockAfterTidalSeek(
          lastTidalSeekIntent, mediaTrackId, status, Date.now()
        );
        const recentSeekToCurrentPosition = hasRecentTidalSeekIntent(
          mediaTrackId, tidal.tidalPosition, Date.now()
        );
        const currentPlaybackIdentity = getPlaybackInstanceIdentity(status, mediaTrackId);
        if (seekToEndPlaybackIdentity !== null
            && seekToEndPlaybackIdentity !== currentPlaybackIdentity) {
          seekToEndPlaybackIdentity = null;
          seekToEndTargetPosition = null;
          seekToEndProgressObserved = false;
        } else if (seekToEndPlaybackIdentity === currentPlaybackIdentity
            && Number.isFinite(seekToEndTargetPosition)
            && clockSampleAccepted && isCurrentEngineClockSample(status)
            && Number(status.timePos) > seekToEndTargetPosition + 0.01) {
          seekToEndProgressObserved = true;
        }
        const atNaturalEnd = isAtNaturalEnd(
          tidalIsPlaying, status, mediaTrackId, engineTrackId
        );
        let naturalEndIdentity = atNaturalEnd
          ? getNaturalEndPlaybackIdentity(
            tidalIsPlaying,
            status,
            mediaTrackId,
            engineTrackId,
            previousEngineSample,
            Date.now(),
            desiredPlaybackIntent === false,
            Boolean(enginePlayPending),
            holdSeekClock || recentSeekToCurrentPosition,
            clockSampleCurrent && isCurrentEngineClockSample(status),
          )
          : null;
        if (naturalEndIdentity !== null || !atNaturalEnd) {
          naturalEndObservation = null;
        } else if (desiredPlaybackIntent === false || enginePlayPending
            || holdSeekClock || recentSeekToCurrentPosition
            || (seekToEndPlaybackIdentity === currentPlaybackIdentity
              && !seekToEndProgressObserved)
            || !clockSampleCurrent || !isCurrentEngineClockSample(status)) {
          naturalEndObservation = null;
        } else {
          const stableEnd = getStableNaturalEndIdentity(
            currentPlaybackIdentity,
            naturalEndObservation,
            status,
            Date.now(),
            transportIntentRevision,
          );
          naturalEndObservation = stableEnd.observation;
          naturalEndIdentity = stableEnd.identity;
          if (naturalEndIdentity !== null) {
            tracePlaybackEvent("natural-end-confirmed-by-stable-eof", {
              trackId: mediaTrackId,
              playbackGeneration: status.playbackGeneration,
            });
          }
        }
        if (naturalEndIdentity !== null) {
          // Never reconcile a completed engine back to /resume while TIDAL's
          // queue transition is pending (including when auto-advance is off).
          if (!autoAdvanceEnabled || naturalEndIdentity === lastAutoAdvancedPlaybackIdentity) {
            return;
          }
          const playState = window.luna?.lib?.PlayState;
          if (typeof playState?.next !== "function") {
            if (lastAutoAdvanceUnavailableIdentity !== naturalEndIdentity) {
              console.warn("[Omniphony EX] Natural-end auto-advance waiting for TIDAL PlayState.next.");
              lastAutoAdvanceUnavailableIdentity = naturalEndIdentity;
            }
            return;
          }
          lastAutoAdvanceUnavailableIdentity = null;
          lastAutoAdvancedPlaybackIdentity = naturalEndIdentity;
          naturalEndObservation = null;
          const sourceQueueIndex = Number(playState.playQueue?.currentIndex);
          naturalEndHandoff = {
            sourceTrackId: String(mediaTrackId),
            sourceQueueIndex: Number.isSafeInteger(sourceQueueIndex) ? sourceQueueIndex : null,
            targetTrackId: null,
            targetQueueIndex: null,
            intentGranted: false,
            expiresAt: Date.now() + EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS,
          };
          const remainingSec = Number(status.duration) - Number(status.timePos);
          console.info(`[Omniphony EX] Track ${mediaTrackId} reached natural end (remaining: ${remainingSec.toFixed(2)}s). Advancing TIDAL queue.`);
          try {
            const advanceResult = playState.next();
            if (advanceResult && typeof advanceResult.then === "function") {
              void Promise.resolve(advanceResult).catch((err) => {
                naturalEndHandoff = null;
                tracePlaybackEvent("natural-end-next-rejected", {
                  trackId: mediaTrackId,
                  playbackGeneration: status.playbackGeneration,
                  message: String(err?.message || err),
                });
                console.warn("[Omniphony EX] TIDAL queue advance rejected:", err);
              });
            }
          } catch (err) {
            naturalEndHandoff = null;
            console.warn("[Omniphony EX] Error triggering next track on natural end:", err);
          }
          if (!pluginDisposed) scheduleImmediateSync();
          return;
        }
        if (atNaturalEnd) return;
        if (holdSeekClock && !lastTidalSeekIntent.holdLogged) {
          lastTidalSeekIntent.holdLogged = true;
          tracePlaybackEvent("seek-clock-reconciliation-hold", {
            requestedPosition: lastTidalSeekIntent.position,
            enginePosition: parsePlaybackPosition(status.timePos)
          });
        }
        // Keep transport controls responsive, but defer clock projection until
        // this poll has interpreted TIDAL's independent timeline as seek intent.
        mirrorEnginePlaybackStateToTidal(status);
        // TIDAL's native decoder can report NOT_PLAYING after the operator's
        // Play action even though the matching libmpv stream has just resumed.
        // Honor the fresh, track-matched Play intent through that handoff so
        // this same reconciliation pass cannot undo it with /pause.
        const pendingExplicitPlay = hasPendingExplicitPlayIntent(mediaTrackId, status);
        const transportPlaying = tidalIsPlaying
          || pendingExplicitPlay
          || hasRecentExplicitPlayIntent(mediaTrackId)
          // The UI mirror above may update PlayState after tidalIsPlaying was
          // sampled. If this same track is already running and the operator's
          // explicit Play intent is still active, don't issue a stale /pause.
          // This remains bounded by the engine's confirmed playing state, so
          // a paused or ended engine is not resumed from stale local intent.
          || shouldPreserveExplicitPlayIntent(
            status, mediaTrackId, engineTrackId, desiredPlaybackIntent
          );
        const desiredState = transportPlaying ? "playing" : "paused";
        const needsResume = shouldResumeEngine(
          transportPlaying,
          status,
          mediaTrackId,
          engineTrackId,
          enginePlayPending && enginePlayPendingTrackId === String(mediaTrackId)
        );
        const needsPause = !transportPlaying && status.paused === false;
        const stateMismatch = needsResume || needsPause;
        const shouldReconcile = stateMismatch
          && (desiredState !== lastMirroredPlaybackState
            || Date.now() - lastEngineStateCommandAt >= 1000);
        if (shouldReconcile) {
          const endpoint = needsResume ? "resume" : "pause";
          const sequenceBeforeCommand = clockOrderingSequence;
          lastEngineStateCommandAt = Date.now();
          const commandSucceeded = await sendEngineCommand(endpoint, {
            trackId: mediaTrackId,
            playbackGeneration: status.playbackGeneration,
            requestId: status.requestId
          });
          if (commandSucceeded) lastMirroredPlaybackState = desiredState;
          if (!isSyncObservationStillCurrent(status, sequenceBeforeCommand)) {
            tracePlaybackEvent("post-command-sync-invalidated", {
              endpoint,
              oldSequence: sequenceBeforeCommand,
              latestSequence: clockOrderingSequence,
              trackId: mediaTrackId,
            });
          }
          // The command can change MPV's transport state even when no faster
          // clock sample arrived during its await. Re-read status before any
          // further clock, Redux, or previous-sample writes from this pass.
          if (!pluginDisposed) scheduleImmediateSync();
          return;
        } else if (!stateMismatch) {
          lastMirroredPlaybackState = desiredState;
        }

        if (!engineSampleAnchor.valid) {
          currentStatusData.engineClockAdvancing = false;
          previousEngineSample = null;
          clearNativeTimelineProjection();
          reportNativeTimelineProjection(engineSampleAnchor.reason);
          updateBadgeUI(currentFormatLabel);
          return;
        }

        // Explicit Redux/native seek handlers forward user intent. A sampled
        // clock jump is diagnostic only and must never reposition libmpv.
        const now = Date.now();
        const enginePosition = parsePlaybackPosition(status.timePos);
        const nativeTidalPosition = tidal.tidalPosition;
        const staleZeroReset = shouldSuppressStaleZeroSeek(
          nativeTidalPosition, status, mediaTrackId, now
        );
        const trustedTidalPosition = staleZeroReset ? null : nativeTidalPosition;
        if (staleZeroReset) {
          tracePlaybackEvent("stale-zero-seek-suppressed", {
            previousTidalPosition: previousEngineSample?.tidalPosition ?? null,
            requestedPosition: nativeTidalPosition,
            enginePosition
          });
        }
        if (previousEngineSample?.trackId === mediaTrackId) {
          const elapsed = (now - previousEngineSample.at) / 1000;
          if (enginePosition !== null
              && Number.isFinite(previousEngineSample.enginePosition)
              && enginePosition < previousEngineSample.enginePosition - 1) {
            tracePlaybackEvent("engine-clock-backward-jump", {
              previousPosition: previousEngineSample.enginePosition,
              currentPosition: enginePosition
            });
          }
          const explicitSeekInFlight = hasRecentTidalSeekIntent(
            mediaTrackId, nativeTidalPosition, now
          );
          if (!explicitSeekInFlight && hasTidalClockDiscontinuity(
            previousEngineSample, mediaTrackId, trustedTidalPosition, elapsed, tidalIsPlaying
          )) {
            tracePlaybackEvent("tidal-clock-discontinuity-ignored", {
              source: "sync-sample",
              previousPosition: previousEngineSample.tidalPosition,
              sampledPosition: trustedTidalPosition,
              enginePosition
            });
            // TIDAL may publish a delayed or corrected clock sample. The
            // libmpv clock remains authoritative; only an explicit seek action
            // may reposition it.
          }
        }
        const advancing = previousEngineSample?.trackId === engineTrackId
          && enginePosition !== null
          && Number.isFinite(previousEngineSample.enginePosition)
          && enginePosition > previousEngineSample.enginePosition + 0.01
          && !status.paused
          && status.playbackState === "playing";
        currentStatusData.engineClockAdvancing = Boolean(advancing);
        previousEngineSample = {
          trackId: mediaTrackId,
          playbackGeneration: status.playbackGeneration,
          requestId: status.requestId,
          serviceInstanceId: status.serviceInstanceId,
          mpvInstanceId: status.mpvInstanceId,
          at: now,
          tidalPlaying: tidalIsPlaying,
          tidalPosition: trustedTidalPosition === null
            ? (previousEngineSample?.trackId === mediaTrackId ? previousEngineSample.tidalPosition : null)
            : trustedTidalPosition,
          enginePosition
        };
        if (holdSeekClock) {
          reportNativeTimelineProjection("projection-held-for-seek");
          clearNativeTimelineProjection();
        }
        // runSync may yield while a transport reconciliation is in flight.
        // A faster /clock poll can commit a newer sequence during that await;
        // never let the older /status response move TIDAL's timeline backward
        // or paint it onto a track selected while the command was pending.
        if (!holdSeekClock
            && clockSampleAccepted
            && isCurrentEngineClockSample(status)) {
          mirrorEngineClockToTidal(status, mediaTrackId, tidal.tidalPosition);
          // The Redux TIME_UPDATE above can rerender TIDAL's controlled range
          // and restore its shorter catalog duration. Project afterward; the
          // native timeline helper also schedules a one-frame post-render fix.
          projectEngineClockToNativeTimeline(
            status, mediaTrackId, statusReceivedMonotonicAt, statusObservedAt,
          );
        }
        // Clock advancement is plugin-local evidence, not hardware-output proof.
        // Refresh only after comparing consecutive samples of the same track.
        updateBadgeUI(currentFormatLabel);
      } else {
        currentStatusData.engineClockAdvancing = false;
        previousEngineSample = null;
        lastMirroredPlaybackState = null;
      }
    } catch (e) {
      invalidateEngineStatus(`OmniFusion service unavailable · ${localRequestFailureSummary("GET /status", e)} · output unverified`);
      const message = String(e?.message || e);
      if (message !== lastSyncError) {
        lastSyncError = message;
        console.warn("[Omniphony EX] Playback sync poll failed:", message);
      }
    }
    finally {
      syncBusy = false;
      if (syncRequested && !pluginDisposed) {
        syncRequested = false;
        scheduleImmediateSync();
      }
    }
  };
  const runClockPoll = async () => {
    if (clockPollBusy || pluginDisposed) return;
    const baseStatus = currentStatusData;
    const tidal = getTidalPlaybackSnapshot();
    const trackId = Number(tidal?.trackId);
    if (!baseStatus
        || !Number.isSafeInteger(trackId)
        || Number(baseStatus.trackId) !== trackId
        || baseStatus.playbackState === "unavailable") return;
    clockPollBusy = true;
    try {
      const res = await requestWithTimeout(
        `${API_BASE}/clock`, { cache: "no-store" }, 1000,
      );
      if (!res.ok || pluginDisposed) return;
      const sample = await res.json();
      const latestStatus = currentStatusData;
      const samplePosition = parsePlaybackPosition(sample?.timePos);
      const sampleDuration = parsePlaybackPosition(latestStatus?.duration);
      const sampleAnchor = resolveEngineSampleAnchor(
        sample, getMonotonicNow(), Date.now(),
      );
      if (!latestStatus
          || String(sample.trackId ?? "") !== String(trackId)
          || String(latestStatus.trackId ?? "") !== String(trackId)
          || Number(sample.playbackGeneration) !== Number(latestStatus.playbackGeneration)
          || String(sample.requestId ?? "") !== String(latestStatus.requestId ?? "")
          || String(sample.serviceInstanceId ?? "") !== String(latestStatus.serviceInstanceId ?? "")
          || String(sample.mpvInstanceId ?? "") !== String(latestStatus.mpvInstanceId ?? "")
          || String(sample.serviceInstanceId ?? "") !== activeClockServiceInstanceId
          || String(sample.mpvInstanceId ?? "") !== activeClockMpvInstanceId
          || samplePosition === null
          || sampleDuration === null
          || sampleDuration <= 0
          || typeof sample.paused !== "boolean"
          || !sampleAnchor.valid
          || String(getCurrentTrackId() ?? "") !== String(trackId)
          || shouldHoldEngineClockAfterTidalSeek(
            lastTidalSeekIntent, trackId, latestStatus, Date.now()
          )) return;
      if (!acceptEngineClockSampleOrdering(sample)) return;
      const sampleNowMonotonic = getMonotonicNow();
      const previousFastSample = lastFastClockSample;
      const sameFastIdentity = previousFastSample
        && previousFastSample.trackId === String(trackId)
        && previousFastSample.playbackGeneration === Number(sample.playbackGeneration)
        && previousFastSample.requestId === String(sample.requestId)
        && previousFastSample.serviceInstanceId === String(sample.serviceInstanceId)
        && previousFastSample.mpvInstanceId === String(sample.mpvInstanceId);
      const fastElapsedMs = sameFastIdentity
        ? sampleNowMonotonic - previousFastSample.sampledAt
        : Infinity;
      const engineClockAdvancing = sample.paused === false
        && sameFastIdentity
        && previousFastSample.paused === false
        && fastElapsedMs > 0
        && fastElapsedMs <= MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS
        && samplePosition > previousFastSample.timePos + 0.0005;
      lastFastClockSample = {
        trackId: String(trackId),
        playbackGeneration: Number(sample.playbackGeneration),
        requestId: String(sample.requestId),
        serviceInstanceId: String(sample.serviceInstanceId),
        mpvInstanceId: String(sample.mpvInstanceId),
        sampleSequence: Number(sample.sampleSequence),
        timePosSampledAtUnixMs: sample.timePosSampledAtUnixMs,
        timePosSampleBracketHalfWidthMs: sample.timePosSampleBracketHalfWidthMs,
        timePosTimingSource: sample.timePosTimingSource,
        timePos: samplePosition,
        sampledAt: sampleNowMonotonic,
        paused: sample.paused,
        engineClockAdvancing,
      };
      const projectedStatus = {
        ...latestStatus,
        timePos: samplePosition,
        timePosSampledAtUnixMs: sample.timePosSampledAtUnixMs,
        paused: sample.paused === true,
        playbackState: sample.paused === true
          ? "paused"
          : engineClockAdvancing ? "playing" : latestStatus.playbackState,
        engineClockAdvancing,
      };
      projectEngineClockToNativeTimeline(
        projectedStatus, trackId, sampleNowMonotonic, Date.now(),
      );
      if (engineClockAdvancing) {
        mirrorEngineClockToTidal(projectedStatus, trackId, tidal.tidalPosition);
      }
      commitEngineClockSampleOrdering(sample);
    } catch (e) {
      // The 100 ms /status control loop remains authoritative. A missed
      // read-only clock tick must not invalidate playback or send a command.
      const message = String(e?.message || e);
      if (message !== lastClockPollError) {
        lastClockPollError = message;
        console.debug("[Omniphony EX] Read-only clock sample skipped:", message);
      }
    } finally {
      clockPollBusy = false;
    }
  };
  pollTimer = setInterval(() => runSync(), SYNC_POLL_INTERVAL_MS);
  clockPollTimer = setInterval(() => { void runClockPoll(); }, CLOCK_POLL_INTERVAL_MS);
  const onPlaybackChange = () => {
    const tidal = getTidalPlaybackSnapshot();
    if (enginePlayRequestId
        && ((!enginePlayAllowPaused && tidal?.playing !== true)
          || String(tidal?.trackId ?? "") !== enginePlayRequestTrackId)) {
      cancelPendingEnginePlay();
    }
    scheduleImmediateSync();
  };
  installPlaybackObservers(
    onPlaybackChange,
    (timePos, source = "redux") => {
      void handleImmediateTidalSeek(timePos, source);
      scheduleImmediateSync();
    }
  );
}

// ---------------------------------------------------------------------------
// Native In-TIDAL UI Elements (Atmos Badge & Acrylic Settings Panel)
// ---------------------------------------------------------------------------

function getPlaybackFooter() {
  // Current TIDAL builds expose the visible footer as #footerPlayer. Prefer
  // that canonical node over generic data-test selectors, which can match a
  // hidden/transitioning playback-controls subtree during Electron re-renders.
  const nativeFooter = document.getElementById?.("footerPlayer");
  if (nativeFooter && nativeFooter.isConnected !== false) return nativeFooter;
  return document.querySelector('[data-test="playback-controls"]')
    || document.querySelector('[data-test="footer-player"]')
    || document.querySelector('footer')
    || document.querySelector('[class*="playbackControls"]');
}

function hasVisibleLayout(element) {
  if (!element || element.isConnected === false) return false;
  for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
    const computed = typeof getComputedStyle === "function"
      ? getComputedStyle(ancestor)
      : ancestor.style || {};
    if (computed.display === "none" || computed.visibility === "hidden"
        || computed.visibility === "collapse" || Number(computed.opacity ?? 1) <= 0) {
      return false;
    }
  }

  const rect = element.getBoundingClientRect?.();
  const left = Number(rect?.left ?? rect?.x);
  const top = Number(rect?.top ?? rect?.y);
  const width = Number(rect?.width);
  const height = Number(rect?.height);
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
    return false;
  }

  const viewportWidth = Number((typeof window !== "undefined" && window.innerWidth)
    || document.documentElement?.clientWidth);
  const viewportHeight = Number((typeof window !== "undefined" && window.innerHeight)
    || document.documentElement?.clientHeight);
  if (viewportWidth > 0 && viewportHeight > 0) {
    const right = Number(rect?.right ?? left + width);
    const bottom = Number(rect?.bottom ?? top + height);
    if (left < 0 || top < 0 || right > viewportWidth || bottom > viewportHeight) return false;
  }
  return true;
}

function findNativeQualityPill() {
  const footer = getPlaybackFooter();
  if (!footer) return null;
  // The original pill is hidden after the first mount, so its bounding box no
  // longer qualifies for a fresh search. Reuse it while it still belongs to
  // the current footer rather than moving the addon into a fallback overlay.
  let ancestor = activeNativeQualityPill?.parentElement || null;
  while (ancestor && ancestor !== footer) ancestor = ancestor.parentElement;
  if (ancestor === footer && activeNativeQualityPill?.isConnected
      && hasVisibleLayout(activeNativeQualityPill.parentElement)) {
    return activeNativeQualityPill;
  }
  const pattern = /\b\d{2,3}\s*[-‑]?\s*bit\b.*\b\d{2,3}(?:\.\d+)?\s*kHz\b/i;
  const candidates = Array.from(footer.querySelectorAll('button,[role="button"],[data-test]'))
    .filter((element) => element.id !== "omniphony-badge")
    .filter((element) => pattern.test(String(element.innerText || element.textContent || "").trim()))
    .filter((element) => hasVisibleLayout(element) && hasVisibleLayout(element.parentElement));
  return candidates.sort((a, b) =>
    String(a.innerText || a.textContent || "").length
      - String(b.innerText || b.textContent || "").length
  )[0] || null;
}

function findNativeQualityBadgeSlot(footer) {
  if (!footer?.querySelectorAll) return null;
  return Array.from(footer.querySelectorAll("[class]"))
    .find((element) => {
      if (!String(element.className || "").includes("qualityBadgeContainer")) return false;
      return hasVisibleLayout(element) && hasVisibleLayout(element.parentElement);
    }) || null;
}

function findNativeAudioOutputButton(footer) {
  if (!footer?.querySelectorAll) return null;
  return Array.from(footer.querySelectorAll('button,[role="button"]'))
    .find((element) => {
      const label = [
        element.getAttribute?.("aria-label"),
        element.getAttribute?.("title"),
        element.getAttribute?.("data-test"),
        element.innerText,
        element.textContent
      ].filter(Boolean).join(" ");
      return /\b(sound|audio)\s+output\b/i.test(label)
        && hasVisibleLayout(element) && hasVisibleLayout(element.parentElement);
    }) || null;
}

function hideNativeQualityPill(pill) {
  if (!pill) return;
  if (!nativeQualityPillStates.has(pill)) {
    nativeQualityPillStates.set(pill, {
      display: pill.style.display,
      visibility: pill.style.visibility,
      ariaHidden: pill.getAttribute("aria-hidden")
    });
  }
  pill.style.display = "none";
  pill.setAttribute("aria-hidden", "true");
}

function restoreNativeQualityPills() {
  for (const [pill, original] of nativeQualityPillStates) {
    pill.style.display = original.display;
    pill.style.visibility = original.visibility;
    if (original.ariaHidden === null) pill.removeAttribute("aria-hidden");
    else pill.setAttribute("aria-hidden", original.ariaHidden);
  }
  nativeQualityPillStates.clear();
  activeNativeQualityPill = null;
}

function reportBadgePlacement(badge, slot) {
  try {
    const rect = badge.getBoundingClientRect?.();
    const footer = getPlaybackFooter();
    const computed = typeof getComputedStyle === "function" ? getComputedStyle(badge) : badge.style || {};
    const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
    const left = number(rect?.left ?? rect?.x);
    const top = number(rect?.top ?? rect?.y);
    const width = number(rect?.width);
    const height = number(rect?.height);
    const right = number(rect?.right) || left + width;
    const bottom = number(rect?.bottom) || top + height;
    const viewportWidth = number(window.innerWidth || document.documentElement?.clientWidth);
    const viewportHeight = number(window.innerHeight || document.documentElement?.clientHeight);
    const display = String(computed.display || "");
    const visibility = String(computed.visibility || "");
    const opacity = number(computed.opacity || 1);
    const withinViewport = viewportWidth > 0 && viewportHeight > 0
      && width > 0 && height > 0 && left >= 0 && top >= 0
      && right <= viewportWidth && bottom <= viewportHeight;
    const report = {
      slot,
      connected: badge.isConnected !== false,
      footerId: String(footer?.id || ""),
      footerTest: String(footer?.getAttribute?.("data-test") || ""),
      badgeInsideFooter: Boolean(footer && (footer === badge || footer.contains?.(badge))),
      badgeParentId: String(badge.parentElement?.id || ""),
      visible: badge.isConnected !== false && display !== "none"
        && visibility !== "hidden" && opacity > 0 && withinViewport
        && hasVisibleLayout(badge),
      rect: { left, top, right, bottom, width, height },
      viewport: { width: viewportWidth, height: viewportHeight },
      style: { display, visibility, opacity, zIndex: String(computed.zIndex || "") },
      parentTag: String(badge.parentElement?.tagName || "").toLowerCase()
    };
    const signature = JSON.stringify(report);
    if (signature !== lastBadgeMountSignature) {
      lastBadgeMountSignature = signature;
      console.info(`[Omniphony EX] Footer addon mount ${JSON.stringify(report)}`);
    }
  } catch (error) {
    console.warn(`[Omniphony EX] Footer addon mount diagnostic failed: ${error?.name || "Error"}`);
  }
}

function attachPlaybackBadge() {
  let badge = document.getElementById("omniphony-badge");
  if (!badge) {
    badge = document.createElement("button");
    badge.id = "omniphony-badge";
    badge.type = "button";
    badge.title = "Omniphony EX playback status and settings";
    badge.setAttribute("aria-label", "Omniphony EX playback status and settings");
  }
  // TidaLuna can retain this DOM node across a plugin hot reload. Rebind the
  // handler on every attach so a stale closure from the previous module cannot
  // leave the visible footer badge inert and keep the playback gate unverified.
  badge.onclick = (event) => {
    event.stopPropagation();
    toggleSettingsPanel();
  };

  // Avoid TIDAL hashed quality-pill classes: late rules can impose fixed sizing
  // and !important padding on this two-line telemetry control.
  badge.className = "omniphony-native-quality-badge";

  let mountSlot = "fixed-fallback";
  const pill = findNativeQualityPill();
  if (pill) {
    mountSlot = "native-quality-pill";
    if (activeNativeQualityPill && activeNativeQualityPill !== pill
        && activeNativeQualityPill.isConnected) {
      const old = nativeQualityPillStates.get(activeNativeQualityPill);
      if (old) {
        activeNativeQualityPill.style.display = old.display;
        activeNativeQualityPill.style.visibility = old.visibility;
        if (old.ariaHidden === null) activeNativeQualityPill.removeAttribute("aria-hidden");
        else activeNativeQualityPill.setAttribute("aria-hidden", old.ariaHidden);
        nativeQualityPillStates.delete(activeNativeQualityPill);
      }
    }
    activeNativeQualityPill = pill;
    hideNativeQualityPill(pill);
    // Keep the add-on control exactly where TIDAL places its native quality pill.
    pill.parentElement.insertBefore(badge, pill.nextSibling);
    badge.style.position = "static";
    badge.style.right = "auto";
    badge.style.bottom = "auto";
    badge.style.zIndex = "auto";
    Object.assign(badge.style, {
      display: "inline-flex", flexDirection: "column", justifyContent: "center",
      alignItems: "center", whiteSpace: "pre-line", textAlign: "center",
      boxSizing: "border-box", minHeight: "32px", minWidth: "0", maxWidth: "184px", width: "184px",
      flex: "0 0 184px", flexShrink: "0", padding: "4px 6px", margin: "0",
      borderRadius: "14px", border: "1px solid rgba(255,255,255,.12)",
      background: "#151515", color: "#f5c400", fontFamily: "inherit", fontSize: "10px", fontWeight: "600", lineHeight: "1.2",
      cursor: "pointer", overflow: "hidden", textOverflow: "clip", overflowWrap: "anywhere"
    });
  } else {
    const footer = getPlaybackFooter();
    const qualitySlot = footer ? findNativeQualityBadgeSlot(footer) : null;
    if (qualitySlot) {
      mountSlot = "native-quality-slot";
      if (badge.parentElement !== qualitySlot) qualitySlot.appendChild(badge);
      badge.style.position = "static";
      badge.style.right = "auto";
      badge.style.bottom = "auto";
      badge.style.zIndex = "auto";
      Object.assign(badge.style, {
        display: "inline-flex", flexDirection: "column", justifyContent: "center",
        alignItems: "center", whiteSpace: "pre-line", textAlign: "center",
        boxSizing: "border-box", minHeight: "32px", minWidth: "0", maxWidth: "184px", width: "184px",
        flex: "0 0 184px", flexShrink: "0", padding: "4px 6px", margin: "0",
        borderRadius: "14px", border: "1px solid rgba(255,255,255,.12)",
        background: "#151515", color: "#f5c400", fontFamily: "inherit", fontSize: "10px",
        fontWeight: "600", lineHeight: "1.2", cursor: "pointer", overflow: "hidden",
        textOverflow: "clip", overflowWrap: "anywhere"
      });
    } else {
      const outputButton = footer ? findNativeAudioOutputButton(footer) : null;
      if (outputButton?.parentElement) {
        mountSlot = "footer-audio-control";
        outputButton.parentElement.insertBefore(badge, outputButton.nextSibling);
        Object.assign(badge.style, {
          position: "static", right: "auto", bottom: "auto", zIndex: "auto",
          display: "inline-flex", flexDirection: "column", justifyContent: "center",
          alignItems: "center", whiteSpace: "pre-line", textAlign: "center",
          boxSizing: "border-box", minHeight: "32px", minWidth: "0", maxWidth: "168px",
          width: "144px", flex: "0 1 144px", flexShrink: "1", padding: "4px 6px",
          margin: "0", borderRadius: "14px", border: "1px solid rgba(255,255,255,.12)",
          background: "#151515", color: "#f5c400", fontFamily: "inherit",
          fontSize: "10px", fontWeight: "600", lineHeight: "1.2", cursor: "pointer",
          overflow: "hidden", textOverflow: "clip", overflowWrap: "anywhere"
        });
      } else if (footer) {
        // Keep the control inside TIDAL's actual player footer even when its
        // quality pill markup changes; never fall back to a floating overlay.
        mountSlot = "footer-inline";
        if (badge.parentElement !== footer) footer.appendChild(badge);
        Object.assign(badge.style, {
          position: "static", right: "auto", bottom: "auto", zIndex: "auto",
          display: "inline-flex", flexDirection: "column", justifyContent: "center",
          alignItems: "center", whiteSpace: "pre-line", textAlign: "center",
          boxSizing: "border-box", minHeight: "32px", minWidth: "0", maxWidth: "168px",
          width: "144px", flex: "0 1 144px", flexShrink: "1", padding: "4px 6px",
          margin: "0 8px", borderRadius: "14px", border: "1px solid rgba(255,255,255,.12)",
          background: "#151515", color: "#f5c400", fontFamily: "inherit",
          fontSize: "10px", fontWeight: "600", lineHeight: "1.2", cursor: "pointer",
          overflow: "hidden", textOverflow: "clip", overflowWrap: "anywhere"
        });
      } else {
        // Keep a visible settings entry point during initial TIDAL startup,
        // before the native footer has been created.
        if (badge.parentElement !== document.body) document.body.appendChild(badge);
        Object.assign(badge.style, {
          position: "fixed", right: "24px", bottom: "24px", zIndex: "99999",
          display: "inline-flex", flexDirection: "column", whiteSpace: "pre-line",
          padding: "7px 12px", borderRadius: "999px", background: "#151515",
          color: "#f5c400", border: "1px solid rgba(255,255,255,.12)",
          fontFamily: "inherit", fontSize: "11px", fontWeight: "600", lineHeight: "1.25", cursor: "pointer"
        });
      }
    }
  }

  const oldInfo = document.getElementById("omniphony-stream-info");
  if (oldInfo) oldInfo.remove();
  reportBadgePlacement(badge, mountSlot);
}

function injectUI() {
  attachPlaybackBadge();
  updateBadgeUI(currentFormatLabel);
  if (settingsButtonTimer) clearInterval(settingsButtonTimer);
  settingsButtonTimer = setInterval(attachPlaybackBadge, 750);
}

function formatBitrate(bitsPerSecond) {
  const value = Number(bitsPerSecond);
  if (!Number.isFinite(value) || value <= 0) return "";
  const kbps = String(Number((value / 1000).toFixed(3)));
  return `${kbps} kbps`;
}

function getAudioParam(params, ...keys) {
  for (const key of keys) {
    const value = Number(params?.[key]);
    if (Number.isFinite(value) && value > 0) return value;
  }
  return 0;
}

function formatSampleRate(sampleRate) {
  const value = Number(sampleRate);
  if (!Number.isFinite(value) || value <= 0) return "";
  return value >= 1000
    ? `${Number((value / 1000).toFixed(3))} kHz`
    : `${value} Hz`;
}

function formatChannelCount(channelCount) {
  const value = Number(channelCount);
  if (!Number.isFinite(value) || value <= 0) return "";
  return value === 1 ? "Mono" : value === 2 ? "Stereo" : `${value} ch`;
}

function getPcmOutputBitrate(status) {
  const params = status?.audioOutputParams;
  if (!params || typeof params !== "object" || Array.isArray(params)) return 0;
  const sampleRate = getAudioParam(params, "samplerate", "sample-rate");
  const channelCount = getAudioParam(params, "channel-count", "channelcount");
  const sampleFormat = String(params.format || "").toLowerCase();
  const bitsPerSample = ({
    u8: 8, u8p: 8, s8: 8, s8p: 8,
    s16: 16, s16p: 16, s24: 24, s24p: 24, s32: 32, s32p: 32,
    flt: 32, fltp: 32, float: 32, floatp: 32,
    dbl: 64, dblp: 64, double: 64
  })[sampleFormat];
  if (sampleRate > 0 && channelCount > 0 && bitsPerSample) {
    return sampleRate * channelCount * bitsPerSample;
  }
  return 0;
}

function isVerifiedOmniphonyJocRoute(status) {
  if (status?.format !== "EAC3_JOC") return false;
  const route = status.omniphonyRoute;
  const checks = route?.checks || {};
  return route?.ready === true
    && status.activeDecoder === "orender"
    && checks.studio === true
    && checks.mpv === true
    && checks.orenderModuleLoaded === true
    && checks.studioRendererConnected === true;
}

function getSourceBitrate(status) {
  const explicit = Number(status?.sourceBitrate);
  return Number.isFinite(explicit) && explicit > 0 ? explicit : 0;
}

function getSourceBitrateKind(status) {
  const basis = String(status?.sourceBitrateBasis || "").toUpperCase();
  if (basis.includes("MP4")) return "average";
  if (basis.includes("DASH") && basis.includes("BANDWIDTH")) return "declared";
  if (basis.includes("ESTIMAT")) return "estimated";
  return "reported";
}

function formatSourceBitrate(status, bitrate = getSourceBitrate(status)) {
  if (!Number.isFinite(Number(bitrate)) || Number(bitrate) <= 0) return "bitrate pending";
  const kind = getSourceBitrateKind(status);
  const value = formatBitrate(bitrate);
  if (kind === "average") return `${value} avg`;
  if (kind === "declared") return `${value} MPD`;
  if (kind === "estimated") return `~${value} est`;
  return `${value} reported`;
}

function getSourcePlaybackSummary(status, format) {
  const provider = status?.sourceProvider || (String(status?.streamTransport || '').includes('amazon') || String(status?.streamTransport || '').includes('amzm') ? 'AMAZON' : 'TIDAL');
  const sampleRate = getAudioParam({ value: status?.sourceSampleRate }, "value");
  const channels = getAudioParam({ value: status?.sourceChannels }, "value");
  const bitrate = getSourceBitrate(status);
  const compactBitrate = formatSourceBitrate(status, bitrate);
  const compactRate = sampleRate > 0 ? formatSampleRate(sampleRate) : "rate n/a";
  const compactChannels = channels > 0 ? `${channels} ch` : "ch n/a";
  const bitDepth = Number(status?.sourceBitDepth);
  const compactBitDepth = Number.isInteger(bitDepth) && bitDepth > 0 ? ` · ${bitDepth}-bit` : "";
  return `[${provider}] ${format} · ${compactBitrate}\n${compactRate} · ${compactChannels}${compactBitDepth}`;
}

function getEngineOutputDetails(status) {
  const decodedParams = status?.audioParams && typeof status.audioParams === "object" && !Array.isArray(status.audioParams)
    ? status.audioParams : {};
  const outputParams = status?.audioOutputParams && typeof status.audioOutputParams === "object" && !Array.isArray(status.audioOutputParams)
    ? status.audioOutputParams : {};
  const decodedRate = getAudioParam({ value: status?.decodedSampleRate }, "value")
    || getAudioParam(decodedParams, "samplerate", "sample-rate");
  const decodedChannels = getAudioParam({ value: status?.decodedChannels }, "value")
    || getAudioParam(decodedParams, "channel-count", "channelcount");
  const decodedFormat = String(decodedParams.format || "").toUpperCase();
  const outputRate = getAudioParam(outputParams, "samplerate", "sample-rate");
  const outputChannels = getAudioParam(outputParams, "channel-count", "channelcount");
  const outputFormat = String(outputParams.format || "").toUpperCase();
  const parts = [];
  // Only sourceCodec is provenance for the encoded input. The generic codec
  // field may describe the selected playback path (for example eac3_joc).
  const sourceCodec = String(status?.sourceCodec || "").toUpperCase();
  const sourceBitrate = getSourceBitrate(status);
  const outputBitrate = getPcmOutputBitrate(status);

  const sourceParts = [];
  sourceParts.push(sourceCodec || "codec unavailable");
  const sourceRate = getAudioParam({ value: status?.sourceSampleRate }, "value");
  const sourceChannels = getAudioParam({ value: status?.sourceChannels }, "value");
  sourceParts.push(sourceRate > 0 ? formatSampleRate(sourceRate) : "source rate unavailable");
  sourceParts.push(sourceChannels > 0 ? `${sourceChannels} source ch` : "source channels unavailable");
  if (sourceBitrate) sourceParts.push(`${formatSourceBitrate(status, sourceBitrate)} source`);
  else sourceParts.push("source bitrate unavailable");
  if (status?.sourceBitrateBasis) {
    sourceParts.push(String(status.sourceBitrateBasis).toUpperCase().includes("MP4")
      ? "MP4 packet average"
      : status.sourceBitrateBasis);
  }
  const sourceBitDepth = Number(status?.sourceBitDepth);
  if (Number.isInteger(sourceBitDepth) && sourceBitDepth > 0) sourceParts.push(`${sourceBitDepth}-bit`);
  parts.push(`Source ${sourceParts.join(" · ")}`);

  const decodedParts = [];
  if (decodedRate > 0) decodedParts.push(formatSampleRate(decodedRate));
  if (decodedChannels > 0) decodedParts.push(formatChannelCount(decodedChannels));
  if (decodedFormat) decodedParts.push(`PCM ${decodedFormat}`);
  parts.push(`Decoded ${decodedParts.length ? decodedParts.join(" · ") : "unavailable"}`);

  const outputParts = [];
  if (outputRate > 0) outputParts.push(formatSampleRate(outputRate));
  if (outputChannels > 0) outputParts.push(formatChannelCount(outputChannels));
  if (outputFormat) outputParts.push(`PCM ${outputFormat}`);
  if (outputBitrate) outputParts.push(`${formatBitrate(outputBitrate)} PCM payload`);
  parts.push(`Output ${outputParts.length ? outputParts.join(" · ") : "unavailable"}`);

  const actualDevice = String(status?.device || "");
  const actualInfo = availableDevices.find((device) => device.name === actualDevice);
  const deviceName = actualInfo?.description
    || (actualDevice === REQUIRED_AUDIO_DEVICE ? "Voicemeeter Input" : actualDevice.replace(/^wasapi\//i, ""));
  if (deviceName && deviceName !== "auto") parts.push(`${deviceName} · WASAPI`);
  if (status?.format === "EAC3_JOC") {
    parts.push(isVerifiedOmniphonyJocRoute(status)
      ? "Omniphony Studio ↔ orender connected · decoder confirmed"
      : "Omniphony JOC route unverified · playback must remain blocked");
  }
  return parts.join(" · ");
}

function getEngineOutputSummary(status) {
  const params = status?.audioOutputParams;
  if (!params || typeof params !== "object" || Array.isArray(params)) return "Output unavailable";
  const sampleRate = getAudioParam(params, "samplerate", "sample-rate");
  const channelCount = getAudioParam(params, "channel-count", "channelcount");
  const parts = [];
  if (sampleRate > 0) parts.push(formatSampleRate(sampleRate));
  if (channelCount > 0) parts.push(formatChannelCount(channelCount));
  return parts.length ? parts.join(" · ") : "Output unavailable";
}

function updateSettingsPanelStatus(status, tidalTrackId, sameTrack, meta) {
  const formatElement = document.getElementById("omniphony-panel-format");
  const detailElement = document.getElementById("omniphony-panel-sub");
  const dot = document.getElementById("omniphony-panel-status-dot");
  if (!formatElement || !detailElement || !dot) return;

  const active = sameTrack && Boolean(status?.format);
  const jocRouteVerified = isVerifiedOmniphonyJocRoute(status);
  const clockAdvancing = active
    && status.paused === false
    && status.engineClockAdvancing === true
    && status.playbackState === "playing"
    && (status.format !== "EAC3_JOC" || jocRouteVerified);
  const paused = active && Boolean(status.paused);
  const unavailable = status?.playbackState === "unavailable";
  const color = clockAdvancing ? "#00e18b" : active || unavailable ? "#ffc34d" : "#8b919b";
  dot.style.background = color;
  dot.style.boxShadow = `0 0 8px ${color}`;

  if (active) {
    const labels = { AC4_IMS: "AC-4 IMS", EAC3_JOC: "E-AC-3 JOC", HI_RES_LOSSLESS: "Hi-Res FLAC", LOSSLESS: "Lossless FLAC" };
    const stateLabel = status.format === "EAC3_JOC" && !jocRouteVerified
      ? "Omniphony route unverified"
      : paused ? "Paused" : clockAdvancing ? "Clock advancing" : "Playback clock not confirmed";
    formatElement.innerText = `${labels[status.format] || status.format} · ${stateLabel}`;
    detailElement.innerText = getEngineOutputDetails(status);
    return;
  }

  if (unavailable) {
    formatElement.innerText = "Engine unavailable";
    detailElement.innerText = status.statusMessage || "Playback telemetry is unavailable";
    return;
  }

  const selected = meta?.title || (tidalTrackId ? `Track ${tidalTrackId}` : "No TIDAL track selected");
  const configuredDevice = availableDevices.find((device) => device.name === REQUIRED_AUDIO_DEVICE)?.description
    || "Voicemeeter Input · WASAPI configured";
  formatElement.innerText = "No active stream match";
  detailElement.innerText = `${selected} · Engine idle or on another track · ${configuredDevice} · bitrate appears when a matched stream is loaded`;
}

function updateBadgeUI(label) {
  const badge = document.getElementById("omniphony-badge");
  const tidalTrackId = getCurrentTrackId();
  const status = currentStatusData || {};
  const sameTrack = tidalTrackId !== null && status.trackId !== null
    && status.trackId !== undefined
    && String(tidalTrackId) === String(status.trackId);
  const hasEngineStream = sameTrack && Boolean(status.format);
  const meta = getCurrentTrackMeta();
  updateSettingsPanelStatus(status, tidalTrackId, sameTrack, meta);
  if (!badge) return;

  if (hasEngineStream) {
    const format = status.format === "AC4_IMS" ? "AC-4 IMS"
      : status.format === "EAC3_JOC" ? "E-AC-3 JOC"
      : status.format === "HI_RES_LOSSLESS" ? "Hi-Res FLAC"
      : status.format === "LOSSLESS" ? "Lossless FLAC"
      : String(label || status.format);
    const jocRouteVerified = isVerifiedOmniphonyJocRoute(status);
    const clockAdvancing = !status.paused
      && status.engineClockAdvancing === true
      && status.playbackState === "playing";
    const playbackState = status.format === "EAC3_JOC" && !jocRouteVerified
      ? "Omniphony route unverified"
      : status.paused ? "Paused"
      : clockAdvancing ? "Engine clock advancing" : "Playback clock not confirmed";
    const sourceBitrate = getSourceBitrate(status);
    const bitrateText = formatSourceBitrate(status, sourceBitrate);
    badge.innerText = getSourcePlaybackSummary(status, format);
    badge.dataset.omniphonyState = status.format === "EAC3_JOC" && !jocRouteVerified ? "route-unverified"
      : status.paused ? "paused"
      : clockAdvancing ? "clock-advancing" : "playback-unconfirmed";
    badge.title = `${meta.title ? `${meta.title} · ` : ""}${format} · ${playbackState} via OmniFusion · ${getEngineOutputDetails(status)}`;
    badge.setAttribute("aria-label", `Omniphony EX: ${meta.title ? `${meta.title}, ` : ""}${format}, ${bitrateText}; ${getEngineOutputDetails(status)}. Click for playback details and settings.`);
    badge.style.color = status.paused ? "#d6d6d6" : "#f5c400";
    badge.style.borderColor = "rgba(255,255,255,.16)";
    return;
  }

  const hasEngineTrack = status.trackId !== null && status.trackId !== undefined;
  badge.innerText = hasEngineTrack ? "Omniphony EX · Not matched" : "Omniphony EX · Idle";
  badge.dataset.omniphonyState = "not-driving";
  badge.title = `${meta.title || "No TIDAL track selected"} · ${status.title ? `engine last track ${status.title}` : "engine idle"}. No active stream match; bitrate and format are intentionally withheld.`;
  badge.setAttribute("aria-label", `${badge.title} Click for playback details and settings.`);
  badge.style.color = "#ffcc66";
  badge.style.borderColor = "rgba(255,204,102,.35)";
}

function toggleSettingsPanel() {
  let panel = document.getElementById("omniphony-settings-panel");
  if (panel) {
    if (panel._omniphonyResizeHandler) {
      window.removeEventListener("resize", panel._omniphonyResizeHandler);
      delete panel._omniphonyResizeHandler;
    }
    panel.remove();
    configLoadGeneration += 1;
    return;
  }

  panel = document.createElement("div");
  panel.id = "omniphony-settings-panel";
  Object.assign(panel.style, {
    position: "fixed",
    bottom: "85px",
    right: "30px",
    width: "380px",
    maxWidth: "calc(100vw - 24px)",
    maxHeight: "calc(100vh - 105px)",
    overflowY: "scroll",
    scrollbarWidth: "thin",
    scrollbarColor: "#00d2c4 rgba(255,255,255,0.06)",
    backgroundColor: "rgba(18, 18, 24, 0.96)",
    backdropFilter: "blur(18px)",
    border: "1px solid rgba(255, 255, 255, 0.12)",
    borderRadius: "12px",
    padding: "18px",
    boxShadow: "0 16px 40px rgba(0, 0, 0, 0.65)",
    zIndex: "99999",
    color: "#fff",
    fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    fontSize: "13px"
  });

  // Keep the panel clear of TIDAL's footer controls as the window changes size.
  const positionPanelAboveFooter = () => {
    const footer = getPlaybackFooter();
    const footerTop = footer?.getBoundingClientRect?.().top;
    const gap = 10;
    const bottom = Number.isFinite(footerTop)
      ? Math.max(12, window.innerHeight - footerTop + gap)
      : 85;
    panel.style.bottom = `${bottom}px`;
  };
  panel._omniphonyResizeHandler = positionPanelAboveFooter;
  window.addEventListener("resize", positionPanelAboveFooter);
  positionPanelAboveFooter();

  // Header
  const header = document.createElement("div");
  header.style.display = "flex";
  header.style.justifyContent = "space-between";
  header.style.alignItems = "center";
  header.style.marginBottom = "14px";
  header.innerHTML = `
    <div style="font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 8px;">
      <span id="omniphony-panel-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#8b919b; box-shadow:0 0 8px #8b919b;"></span>
      Omniphony EX Engine
    </div>
    <button id="omniphony-close-btn" style="background:none; border:none; color:#888; cursor:pointer; font-size:16px;">✕</button>
  `;
  panel.appendChild(header);

  // Active Stream Status Box
  const statusBox = document.createElement("div");
  statusBox.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
  statusBox.style.padding = "10px";
  statusBox.style.borderRadius = "8px";
  statusBox.style.marginBottom = "14px";
  statusBox.style.fontSize = "12px";
  statusBox.innerHTML = `
    <div style="color:#aaa; margin-bottom:4px;">Live playback</div>
    <div style="font-weight:600; color:#31d8ff;" id="omniphony-panel-format">Checking stream match…</div>
    <div style="color:#bbb; font-size:11px; line-height:1.45; margin-top:6px; overflow-wrap:anywhere;" id="omniphony-panel-sub">Waiting for TIDAL and engine status</div>
  `;
  panel.appendChild(statusBox);

  const preparePausedButton = document.createElement("button");
  preparePausedButton.id = "omniphony-prepare-paused";
  preparePausedButton.type = "button";
  preparePausedButton.setAttribute("aria-label", "Prepare the next TIDAL track selection without starting playback");
  preparePausedButton.style.cssText = "width:100%;background:#20242b;color:#fff;border:1px solid #46515d;border-radius:7px;padding:9px 10px;margin:0 0 5px;cursor:pointer;text-align:left;font:inherit;";
  preparePausedButton.onclick = () => {
    if (!nativePlayerCommandHookInstalled) {
      updatePausedSelectionUi(`TIDAL's synchronous native-player guard is unavailable (${nativePlayerCommandHookDiagnostic}). Selection remains unarmed.`);
      return;
    }
    if (prepareNextSelectionPaused || pausedSelectionPending) {
      prepareNextSelectionPaused = false;
      pausedSelectionPending = null;
      pausedSelectionConfirmedTrackId = null;
      setTidalPlaybackIntent(false);
      updatePausedSelectionUi("Paused preparation canceled. TIDAL remains paused.");
    } else {
      pausedSelectionConfirmedTrackId = null;
      pausedSelectionUserPlayAuthorization = null;
      prepareNextSelectionPaused = true;
      updatePausedSelectionUi();
    }
  };
  panel.appendChild(preparePausedButton);
  const preparePausedStatus = document.createElement("div");
  preparePausedStatus.id = "omniphony-prepare-paused-status";
  preparePausedStatus.setAttribute("role", "status");
  preparePausedStatus.style.cssText = "min-height:16px;color:#aaa;font-size:11px;line-height:1.4;margin:0 0 14px;";
  panel.appendChild(preparePausedStatus);

  // RealMax Quality Priority Setting
  const priorityLabel = document.createElement("label");
  priorityLabel.innerText = "RealMax Quality Priority:";
  priorityLabel.style.fontWeight = "600";
  priorityLabel.style.display = "block";
  priorityLabel.style.marginBottom = "6px";
  panel.appendChild(priorityLabel);

  const prioritySelect = document.createElement("select");
  Object.assign(prioritySelect.style, {
    width: "100%",
    backgroundColor: "#222",
    color: "#fff",
    border: "1px solid #444",
    borderRadius: "6px",
    padding: "8px",
    marginBottom: "14px",
    outline: "none"
  });

  prioritySelect.id = "omniphony-priority-select";
  prioritySelect.setAttribute("aria-label", "RealMax format priority");
  const updatePriorityTitle = () => {
    const selected = priorityPresets.find((preset) => preset.value.join(",") === prioritySelect.value);
    prioritySelect.title = selected?.fullLabel || "Select RealMax format priority";
  };
  prioritySelect.setAttribute("aria-describedby", "omniphony-priority-help");
  priorityPresets.forEach((opt) => {
    const el = document.createElement("option");
    el.value = opt.value.join(",");
    el.innerText = opt.label;
    el.title = opt.fullLabel;
    if (currentConfig.priority.join(",") === el.value) el.selected = true;
    prioritySelect.appendChild(el);
  });
  updatePriorityTitle();

  const priorityHelp = document.createElement("div");
  priorityHelp.id = "omniphony-priority-help";
  priorityHelp.innerText = "AC-4 = AC-4 IMS · JOC = E-AC-3 JOC · Hi-Res and Lossless use FLAC";
  priorityHelp.style.cssText = "color:#888;font-size:10px;line-height:1.4;margin:-9px 0 12px;";

  const prioritySaveStatus = document.createElement("div");
  prioritySaveStatus.id = "omniphony-priority-save-status";
  prioritySaveStatus.setAttribute("role", "status");
  prioritySaveStatus.style.cssText = "min-height:16px;color:#aaa;font-size:11px;margin:-8px 0 12px;";
  prioritySaveStatus.innerText = "Loading saved priority…";
  prioritySaveStatus.style.color = "#aaa";
  prioritySelect.disabled = true;

  prioritySelect.onchange = async () => {
    const chosen = prioritySelect.value.split(",");
    if (chosen.length !== supportedFormats.length || !supportedFormats.every((format) => chosen.includes(format))) {
      prioritySaveStatus.innerText = "That priority order is invalid.";
      prioritySaveStatus.style.color = "#ff7272";
      return;
    }
    const previous = currentConfig.priority;
    prioritySelect.disabled = true;
    prioritySaveStatus.innerText = "Saving…";
    prioritySaveStatus.style.color = "#aaa";
    try {
      const response = await requestWithTimeout(`${API_BASE}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ priority: chosen })
      });
      const result = await response.json();
      if (!response.ok || result.status !== "saved") throw new Error(result.error || `HTTP ${response.status}`);
      if (!Array.isArray(result.config?.priority)
          || result.config.priority.join(",") !== chosen.join(",")) {
        throw new Error("Service did not confirm the selected priority");
      }
      configLoadGeneration += 1;
      currentConfig = { ...currentConfig, ...result.config };
      prioritySelect.value = currentConfig.priority.join(",");
      updatePriorityTitle();
      prioritySaveStatus.innerText = "Saved. This order applies to the next track.";
      prioritySaveStatus.style.color = "#74e6a2";
      console.log("[Omniphony EX] Saved RealMax priority:", currentConfig.priority);
    } catch (error) {
      currentConfig.priority = previous;
      prioritySelect.value = previous.join(",");
      updatePriorityTitle();
      prioritySaveStatus.innerText = `Save failed: ${error.message}`;
      prioritySaveStatus.style.color = "#ff7272";
      console.error("[Omniphony EX] Could not save RealMax priority:", error);
    } finally {
      prioritySelect.disabled = false;
    }
  };
  panel.appendChild(prioritySelect);
  panel.appendChild(priorityHelp);
  panel.appendChild(prioritySaveStatus);

  // RealMAX Source Provider Selector (Both / TIDAL Only / Amazon Music Only)
  const sourceLabel = document.createElement("label");
  sourceLabel.innerText = "RealMAX Source Provider:";
  sourceLabel.style.fontWeight = "600";
  sourceLabel.style.display = "block";
  sourceLabel.style.marginBottom = "6px";
  panel.appendChild(sourceLabel);

  const sourceSelect = document.createElement("select");
  Object.assign(sourceSelect.style, {
    width: "100%",
    backgroundColor: "#222",
    color: "#fff",
    border: "1px solid #444",
    borderRadius: "6px",
    padding: "8px",
    marginBottom: "14px",
    outline: "none"
  });
  sourceSelect.id = "omniphony-source-select";
  sourceSelect.setAttribute("aria-label", "RealMAX source provider mode");
  [
    { value: "auto", label: "Both (TIDAL + Amazon Music / Eclipse)" },
    { value: "tidal_only", label: "TIDAL Only (Atmos AC-4/JOC + Hi-Res)" },
    { value: "amazon_only", label: "Amazon Music Only (No AC-4 IMS)" },
  ].forEach((opt) => {
    const el = document.createElement("option");
    el.value = opt.value;
    el.innerText = opt.label;
    sourceSelect.appendChild(el);
  });
  sourceSelect.onchange = async () => {
    const mode = sourceSelect.value;
    try {
      await fetch(`${API_BASE}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ realmax_source: mode })
      });
      console.log("[Omniphony EX] Saved RealMAX source mode:", mode);
    } catch (err) {
      console.warn("[Omniphony EX] Failed to save source mode:", err);
    }
  };
  panel.appendChild(sourceSelect);


  // Audio Output Device Selector
  const deviceLabel = document.createElement("label");
  deviceLabel.innerText = "Audio Output Device:";
  deviceLabel.style.fontWeight = "600";
  deviceLabel.style.display = "block";
  deviceLabel.style.marginBottom = "6px";
  panel.appendChild(deviceLabel);

  const deviceSelect = document.createElement("select");
  Object.assign(deviceSelect.style, {
    width: "100%",
    backgroundColor: "#222",
    color: "#fff",
    border: "1px solid #444",
    borderRadius: "6px",
    padding: "8px",
    marginBottom: "8px",
    outline: "none"
  });

  deviceSelect.id = "omniphony-device-select";
  deviceSelect.setAttribute("aria-label", "Audio output device");
  populateDeviceSelector(deviceSelect);

  deviceSelect.onchange = async () => {
    const chosenDevice = deviceSelect.value;
    deviceSelect.dataset.userChanged = "true";
    currentConfig.audio_device = chosenDevice;
    await fetch(`${API_BASE}/device`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: chosenDevice })
    });
    console.log("[Omniphony EX] Switched active audio device to:", chosenDevice);
  };
  panel.appendChild(deviceSelect);

  const deviceLoadStatus = document.createElement("div");
  deviceLoadStatus.id = "omniphony-device-load-status";
  deviceLoadStatus.setAttribute("role", "status");
  deviceLoadStatus.style.cssText = "min-height:16px;color:#aaa;font-size:11px;line-height:1.4;margin:0 0 8px;";
  deviceLoadStatus.innerText = "Loading local WASAPI device list…";
  panel.appendChild(deviceLoadStatus);

  // TIDAL Account Authorization Section
  const authDivider = document.createElement("div");
  authDivider.style.cssText = "height:1px;background:rgba(255,255,255,0.1);margin:14px 0 12px;";
  panel.appendChild(authDivider);

  const authLabel = document.createElement("label");
  authLabel.innerText = "TIDAL Account Link:";
  authLabel.style.fontWeight = "600";
  authLabel.style.display = "block";
  authLabel.style.marginBottom = "6px";
  panel.appendChild(authLabel);

  const authBtn = document.createElement("button");
  authBtn.id = "omniphony-auth-btn";
  authBtn.type = "button";
  authBtn.innerText = "Link / Authenticate TIDAL Account";
  authBtn.style.cssText = "width:100%;background:#00d2c4;color:#000;font-weight:700;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font:inherit;margin-bottom:6px;transition:opacity 0.2s;";
  panel.appendChild(authBtn);

  const authStatus = document.createElement("div");
  authStatus.id = "omniphony-auth-status";
  authStatus.style.cssText = "min-height:16px;color:#aaa;font-size:11px;line-height:1.4;margin:0 0 10px;overflow-wrap:anywhere;";
  authStatus.innerText = "Ready to link TIDAL OAuth session.";
  panel.appendChild(authStatus);

  // Engine & Studio Binary Paths Section
  
  // Omniphony EX RealMAX Downloader Section
  const dlDivider = document.createElement("div");
  dlDivider.style.cssText = "height:1px;background:rgba(255,255,255,0.1);margin:14px 0 12px;";
  panel.appendChild(dlDivider);

  const dlHeader = document.createElement("label");
  dlHeader.innerText = "RealMAX Downloader (Tracks & Albums):";
  dlHeader.style.fontWeight = "600";
  dlHeader.style.display = "block";
  dlHeader.style.marginBottom = "6px";
  panel.appendChild(dlHeader);

  const dlFolderLabel = document.createElement("div");
  dlFolderLabel.innerText = "Download Save Folder:";
  dlFolderLabel.style.cssText = "color:#bbb;font-size:11px;margin-bottom:3px;";
  panel.appendChild(dlFolderLabel);

  const dlFolderInput = document.createElement("input");
  dlFolderInput.id = "omniphony-dl-folder-input";
  dlFolderInput.type = "text";
  dlFolderInput.value = (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_download_folder") : null) || "";
  dlFolderInput.style.cssText = "width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:6px 8px;margin-bottom:6px;font:inherit;font-size:11px;";
  dlFolderInput.onchange = () => {
    if (typeof localStorage !== "undefined") localStorage.setItem("omniphony_download_folder", dlFolderInput.value.trim());
  };
  panel.appendChild(dlFolderInput);

  const browseFolderBtn = document.createElement("button");
  browseFolderBtn.type = "button";
  browseFolderBtn.innerText = "📁 Select Download Directory...";
  browseFolderBtn.style.cssText = "width:100%;background:#20242b;color:#fff;border:1px solid #46515d;border-radius:6px;padding:6px 8px;cursor:pointer;font:inherit;font-size:11px;margin-bottom:10px;";
  browseFolderBtn.onclick = async () => {
    const chosen = await selectDownloadFolderNative();
    if (chosen) {
      dlFolderInput.value = chosen;
      if (typeof localStorage !== "undefined") localStorage.setItem("omniphony_download_folder", chosen);
    }
  };
  panel.appendChild(browseFolderBtn);

  const ac4FmtLabel = document.createElement("div");
  ac4FmtLabel.innerText = "AC-4 IMS Download Format:";
  ac4FmtLabel.style.cssText = "color:#bbb;font-size:11px;margin-bottom:3px;";
  panel.appendChild(ac4FmtLabel);

  const ac4FmtSelect = document.createElement("select");
  ac4FmtSelect.style.cssText = "width:100%;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:6px 8px;margin-bottom:10px;font:inherit;font-size:11px;";
  [
    { value: "flac", label: "FLAC (Binaural 24-bit/48kHz - Compact & Tagged)" },
    { value: "wav", label: "WAV (Binaural 24-bit/48kHz - Bit-Perfect Uncompressed)" },
  ].forEach((opt) => {
    const el = document.createElement("option");
    el.value = opt.value;
    el.innerText = opt.label;
    if (((typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_download_ac4_format") : null) || "flac") === opt.value) el.selected = true;
    ac4FmtSelect.appendChild(el);
  });
  ac4FmtSelect.onchange = () => {
    if (typeof localStorage !== "undefined") localStorage.setItem("omniphony_download_ac4_format", ac4FmtSelect.value);
  };
  panel.appendChild(ac4FmtSelect);

  const dlNowBtn = document.createElement("button");
  dlNowBtn.type = "button";
  dlNowBtn.innerText = "⬇ Download Current Playing Track (RealMAX)";
  dlNowBtn.style.cssText = "width:100%;background:#00d2c4;color:#000;font-weight:700;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font:inherit;font-size:12px;margin-bottom:6px;";
  dlNowBtn.onclick = async () => {
    const meta = getCurrentDownloadTrackMeta();
    if (!meta) {
      alert("No active track is loaded or playing.");
      return;
    }
    dlNowBtn.disabled = true;
    dlNowBtn.innerText = "Queueing Download...";
    try {
      const q = await queueTrackDownload({
        ...meta,
        title: meta.title,
        artist: meta.artist,
        album: meta.album,
      });
      alert(`Download queued! Job ID: ${q.jobId}`);
    } catch (e) {
      alert("Download error: " + e.message);
    } finally {
      dlNowBtn.disabled = false;
      dlNowBtn.innerText = "⬇ Download Current Playing Track (RealMAX)";
    }
  };
  panel.appendChild(dlNowBtn);

    // RealMAX Multi-Source Unified Search Section
  const searchDivider = document.createElement("div");
  searchDivider.style.cssText = "height:1px;background:rgba(255,255,255,0.1);margin:14px 0 12px;";
  panel.appendChild(searchDivider);

  const searchHeader = document.createElement("label");
  searchHeader.innerText = "RealMAX Multi-Source Unified Search:";
  searchHeader.style.fontWeight = "600";
  searchHeader.style.display = "block";
  searchHeader.style.marginBottom = "6px";
  panel.appendChild(searchHeader);

  const searchInput = document.createElement("input");
  searchInput.id = "omniphony-multi-search-input";
  searchInput.type = "text";
  searchInput.placeholder = "Search tracks, Atmos editions, exclusives across TIDAL & Amazon...";
  searchInput.style.cssText = "width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:8px 10px;margin-bottom:8px;font:inherit;font-size:12px;";
  panel.appendChild(searchInput);

  const searchResultsContainer = document.createElement("div");
  searchResultsContainer.id = "omniphony-search-results";
  searchResultsContainer.style.cssText = "max-height:220px;overflow-y:auto;background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:6px;margin-bottom:12px;font-size:11px;display:none;";
  panel.appendChild(searchResultsContainer);

  let searchDebounceTimer = null;
  let searchGeneration = 0;
  const setSearchMessage = (message, color = "#888") => {
    const messageElement = document.createElement("div");
    messageElement.style.cssText = `color:${color};padding:6px;`;
    messageElement.textContent = String(message ?? "");
    searchResultsContainer.replaceChildren(messageElement);
  };

  const createTextBadge = (label, cssText, iconMarkup = "") => {
    const badge = document.createElement("span");
    badge.style.cssText = cssText;
    if (iconMarkup) {
      const svgDocument = new DOMParser().parseFromString(iconMarkup, "image/svg+xml");
      if (svgDocument.documentElement?.localName === "svg") {
        badge.appendChild(document.importNode(svgDocument.documentElement, true));
      }
    }
    const labelElement = document.createElement("span");
    labelElement.textContent = String(label ?? "");
    badge.appendChild(labelElement);
    return badge;
  };

  searchInput.oninput = () => {
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
    const generation = ++searchGeneration;
    const q = searchInput.value.trim();
    if (!q) {
      searchResultsContainer.style.display = "none";
      searchResultsContainer.replaceChildren();
      return;
    }
    searchResultsContainer.style.display = "block";
    setSearchMessage("Searching across TIDAL & Amazon Music...");

    searchDebounceTimer = setTimeout(async () => {
      try {
        const sRes = await fetch(`${API_BASE}/search/multi?q=${encodeURIComponent(q)}&limit=10`);
        if (generation !== searchGeneration || !searchInput.isConnected) return;
        if (!sRes.ok) throw new Error(`Search failed: ${sRes.status}`);
        const data = await sRes.json();
        if (generation !== searchGeneration || !searchInput.isConnected) return;
        const tracks = data?.tracks || [];
        if (!tracks.length) {
          setSearchMessage("No tracks found across providers.");
          return;
        }

        searchResultsContainer.replaceChildren();
        tracks.forEach((t) => {
          const row = document.createElement("div");
          row.style.cssText = "display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,0.05);gap:8px;";

          const coverUrl = (() => {
            try {
              const parsed = new URL(String(t.cover || ""));
              return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
            } catch (_) {
              return "";
            }
          })();
          if (coverUrl) {
            const coverEl = document.createElement("img");
            coverEl.src = coverUrl;
            coverEl.style.cssText = "width:34px;height:34px;border-radius:4px;object-fit:cover;flex-shrink:0;";
            coverEl.onerror = () => { coverEl.style.display = "none"; };
            row.appendChild(coverEl);
          }

          const metaDiv = document.createElement("div");
          metaDiv.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";

          const amazonLogoSvg = `<svg style='width:11px;height:11px;fill:currentColor;flex-shrink:0;' viewBox='0 0 24 24'><path d='M.045 18.02c.072-.116.187-.124.348-.022 3.636 2.11 7.594 3.166 11.87 3.166 2.852 0 5.668-.533 8.447-1.595l.315-.14c.138-.06.234-.1.293-.13.226-.088.39-.046.525.13.12.174.09.336-.12.48-.256.19-.6.41-1.006.654-1.244.743-2.64 1.316-4.185 1.726a17.617 17.617 0 01-10.951-.577 17.88 17.88 0 01-5.43-3.35c-.1-.074-.151-.15-.151-.22 0-.047.021-.09.051-.13zm6.565-6.218c0-1.005.247-1.863.743-2.577.495-.71 1.17-1.25 2.04-1.615.796-.335 1.756-.575 2.912-.72.39-.046 1.033-.103 1.92-.174v-.37c0-.93-.105-1.558-.3-1.875-.302-.43-.78-.65-1.44-.65h-.182c-.48.046-.896.196-1.246.46-.35.27-.575.63-.675 1.096-.06.3-.206.465-.435.51l-2.52-.315c-.248-.06-.372-.18-.372-.39 0-.046.007-.09.022-.15.247-1.29.855-2.25 1.82-2.88.976-.616 2.1-.975 3.39-1.05h.54c1.65 0 2.957.434 3.888 1.29.135.15.27.3.405.48.12.165.224.314.283.45.075.134.15.33.195.57.06.254.105.42.135.51.03.104.062.3.076.615.01.313.02.493.02.553v5.28c0 .376.06.72.165 1.036.105.313.21.54.315.674l.51.674c.09.136.136.256.136.36 0 .12-.06.226-.18.314-1.2 1.05-1.86 1.62-1.963 1.71-.165.135-.375.15-.63.045a6.062 6.062 0 01-.526-.496l-.31-.347a9.391 9.391 0 01-.317-.42l-.3-.435c-.81.886-1.603 1.44-2.4 1.665-.494.15-1.093.227-1.83.227-1.11 0-2.04-.343-2.76-1.034-.72-.69-1.08-1.665-1.08-2.94l-.05-.076zm3.753-.438c0 .566.14 1.02.425 1.364.285.34.675.512 1.155.512.045 0 .106-.007.195-.02.09-.016.134-.023.166-.023.614-.16 1.08-.553 1.424-1.178.165-.28.285-.58.36-.91.09-.32.12-.59.135-.8.015-.195.015-.54.015-1.005v-.54c-.84 0-1.484.06-1.92.18-1.275.36-1.92 1.17-1.92 2.43l-.035-.02zm9.162 7.027c.03-.06.075-.11.132-.17.362-.243.714-.41 1.05-.5a8.094 8.094 0 011.612-.24c.14-.012.28 0 .41.03.65.06 1.05.168 1.172.33.063.09.099.228.099.39v.15c0 .51-.149 1.11-.424 1.8-.278.69-.664 1.248-1.156 1.68-.073.06-.14.09-.197.09-.03 0-.06 0-.09-.012-.09-.044-.107-.12-.064-.24.54-1.26.806-2.143.806-2.64 0-.15-.03-.27-.087-.344-.145-.166-.55-.257-1.224-.257-.243 0-.533.016-.87.046-.363.045-.7.09-1 .135-.09 0-.148-.014-.18-.044-.03-.03-.036-.047-.02-.077 0-.017.006-.03.02-.063v-.06z'/></svg>`;

          const tidalLogoSvg = `<svg style='width:11px;height:11px;fill:currentColor;flex-shrink:0;' viewBox='0 0 24 24'><path d='M12.012 3.992L8.008 7.996 4.004 3.992 0 7.996l4.004 4.004L0 16.004l4.004 4.004 4.004-4.004 4.004 4.004 4.004-4.004-4.004-4.004 4.004-4.004-4.004-4.004zm4.004 4.004l4.004-4.004L24 7.996l-3.984 4.004-4.004-4.004z'/></svg>`;

          const titleLine = document.createElement("div");
          const isAmazon = t.source === "AMAZON";
          titleLine.appendChild(createTextBadge(
            isAmazon ? "AMAZON MUSIC" : "TIDAL",
            `display:inline-flex;align-items:center;background:${isAmazon ? "#ff9900" : "#00d2c4"};color:#000;font-weight:700;font-size:9px;padding:2px 5px;border-radius:3px;margin-right:4px;vertical-align:middle;line-height:1;gap:3px;`,
            isAmazon ? amazonLogoSvg : tidalLogoSvg,
          ));
          if (t.upgrade && t.upgrade.provider === "AMAZON") {
            titleLine.appendChild(createTextBadge(
              "AMZ UPGRADE",
              "display:inline-flex;align-items:center;background:rgba(255,153,0,0.18);border:1px solid #ff9900;color:#ff9900;font-weight:700;font-size:9px;padding:1px 5px;border-radius:3px;margin-right:4px;vertical-align:middle;line-height:1;gap:3px;",
              amazonLogoSvg,
            ));
          }
          if (t.exclusive) {
            titleLine.appendChild(createTextBadge("EXCLUSIVE", "display:inline-flex;align-items:center;background:#7928ca;color:#fff;font-weight:700;font-size:9px;padding:2px 5px;border-radius:3px;margin-right:4px;vertical-align:middle;line-height:1;"));
          }
          if (t.isAtmos || t.upgrade) {
            titleLine.appendChild(createTextBadge("ATMOS", "display:inline-flex;align-items:center;background:#31d8ff;color:#000;font-weight:700;font-size:9px;padding:2px 5px;border-radius:3px;margin-right:4px;vertical-align:middle;line-height:1;"));
          }
          const title = document.createElement("strong");
          title.textContent = String(t.title ?? "Unknown title");
          titleLine.appendChild(title);

          const detailsLine = document.createElement("div");
          detailsLine.style.cssText = "color:#888;font-size:10px;";
          detailsLine.textContent = `${String(t.artist ?? "Unknown artist")} · ${String(t.album ?? "Unknown album")}`;
          metaDiv.append(titleLine, detailsLine);
          row.appendChild(metaDiv);

          const btnGroup = document.createElement("div");
          btnGroup.style.cssText = "display:flex;gap:4px;";

          const playBtn = document.createElement("button");
          playBtn.type = "button";
          playBtn.innerText = "▶";
          playBtn.title = "Stream with RealMAX";
          playBtn.style.cssText = "background:#222;color:#31d8ff;border:1px solid #444;border-radius:4px;padding:4px 7px;cursor:pointer;font-size:10px;";
          playBtn.onclick = async () => {
            playBtn.innerText = "⏳";
            try {
              await fetch(`${API_BASE}/play`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  trackId: t.id,
                  title: t.title,
                  artist: t.artist,
                  album: t.album,
                  timePos: 0,
                  startPaused: false
                })
              });
            } catch (err) {
              alert("Play error: " + err.message);
            } finally {
              playBtn.innerText = "▶";
            }
          };
          btnGroup.appendChild(playBtn);

          const dlBtn = document.createElement("button");
          dlBtn.type = "button";
          dlBtn.innerText = "⬇";
          dlBtn.title = "Download with RealMAX";
          dlBtn.style.cssText = "background:#222;color:#00ff88;border:1px solid #444;border-radius:4px;padding:4px 7px;cursor:pointer;font-size:10px;";
          dlBtn.onclick = async () => {
            dlBtn.innerText = "⏳";
            try {
              const qRes = await queueTrackDownload(t);
              alert(`Queued download for ${t.title}! (Job: ${qRes.jobId})`);
            } catch (err) {
              alert("Download error: " + err.message);
            } finally {
              dlBtn.innerText = "⬇";
            }
          };
          btnGroup.appendChild(dlBtn);

          row.appendChild(btnGroup);
          searchResultsContainer.appendChild(row);
        });
      } catch (err) {
        if (generation === searchGeneration && searchInput.isConnected) {
          setSearchMessage(`Error: ${err.message}`, "#ff7272");
        }
      }
    }, 300);
  };

  const pathsDivider = document.createElement("div");
  pathsDivider.style.cssText = "height:1px;background:rgba(255,255,255,0.1);margin:14px 0 12px;";
  panel.appendChild(pathsDivider);

  const pathsHeader = document.createElement("label");
  pathsHeader.innerText = "OmniFusion & Studio Directories:";
  pathsHeader.style.fontWeight = "600";
  pathsHeader.style.display = "block";
  pathsHeader.style.marginBottom = "6px";
  panel.appendChild(pathsHeader);

  // MPV / Engine Folder Path
  const mpvLabel = document.createElement("div");
  mpvLabel.innerText = "MPV Engine Folder:";
  mpvLabel.style.cssText = "color:#bbb;font-size:11px;margin-bottom:3px;";
  panel.appendChild(mpvLabel);

  const mpvInput = document.createElement("input");
  mpvInput.id = "omniphony-mpv-path-input";
  mpvInput.type = "text";
  mpvInput.value = (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_mpv_path") : null) || "";
  mpvInput.style.cssText = "width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:6px 8px;margin-bottom:8px;font:inherit;font-size:11px;";
  mpvInput.onchange = () => {
    if (typeof localStorage !== "undefined") localStorage.setItem("omniphony_mpv_path", mpvInput.value.trim());
  };
  panel.appendChild(mpvInput);

  // Omniphony Studio Path
  const studioLabel = document.createElement("div");
  studioLabel.innerText = "Omniphony Studio Executable / Folder:";
  studioLabel.style.cssText = "color:#bbb;font-size:11px;margin-bottom:3px;";
  panel.appendChild(studioLabel);

  const studioInput = document.createElement("input");
  studioInput.id = "omniphony-studio-path-input";
  studioInput.type = "text";
  studioInput.value = (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_studio_path") : null) || "";
  studioInput.style.cssText = "width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:6px 8px;margin-bottom:12px;font:inherit;font-size:11px;";
  studioInput.onchange = () => {
    if (typeof localStorage !== "undefined") localStorage.setItem("omniphony_studio_path", studioInput.value.trim());
  };
  panel.appendChild(studioInput);


  // External Eclipse Addon Providers
  const addonDivider = document.createElement("div");
  addonDivider.style.cssText = "height:1px;background:rgba(255,255,255,0.1);margin:14px 0 12px;";
  panel.appendChild(addonDivider);

  const addonLabel = document.createElement("label");
  addonLabel.innerText = "Eclipse Addon Providers (RealMAX Multi-Source):";
  addonLabel.style.fontWeight = "600";
  addonLabel.style.display = "block";
  addonLabel.style.marginBottom = "6px";
  panel.appendChild(addonLabel);

  const addonInput = document.createElement("input");
  addonInput.id = "omniphony-addon-input";
  addonInput.type = "url";
  addonInput.placeholder = "https://.../manifest.json";
  addonInput.style.cssText = "width:100%;box-sizing:border-box;background:#222;color:#fff;border:1px solid #444;border-radius:6px;padding:8px;margin-bottom:6px;font:inherit;font-size:11px;";
  panel.appendChild(addonInput);

  const addAddonBtn = document.createElement("button");
  addAddonBtn.id = "omniphony-add-addon-btn";
  addAddonBtn.type = "button";
  addAddonBtn.innerText = "+ Add Eclipse Manifest / Provider";
  addAddonBtn.style.cssText = "width:100%;background:#31d8ff;color:#000;font-weight:700;border:none;border-radius:6px;padding:7px 10px;cursor:pointer;font:inherit;margin-bottom:8px;";
  panel.appendChild(addAddonBtn);

  const addonStatus = document.createElement("div");
  addonStatus.id = "omniphony-addon-status";
  addonStatus.style.cssText = "min-height:16px;color:#aaa;font-size:11px;line-height:1.4;margin:0 0 10px;overflow-wrap:anywhere;";
  addonStatus.innerText = "Default: Amazon Music Eclipse Addon supported (ISRC / title matching).";
  panel.appendChild(addonStatus);
  const setAddonStatus = (message, color = "#aaa", emphasize = false) => {
    addonStatus.replaceChildren();
    const statusText = document.createElement(emphasize ? "strong" : "span");
    statusText.style.color = color;
    statusText.textContent = String(message ?? "");
    addonStatus.appendChild(statusText);
  };

  addAddonBtn.onclick = async () => {
    let url;
    try {
      url = parseEclipseManifestUrl(addonInput.value);
    } catch (err) {
      setAddonStatus(err.message || "Please enter a valid HTTPS manifest URL.", "#ff7272");
      return;
    }
    addonStatus.innerText = "Saving & probing Eclipse addon manifest...";
    try {
      const saveRes = await requestWithTimeout(`${API_BASE}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ eclipse_addons: [url] })
      }, 4000);
      if (!saveRes.ok) {
        throw new Error(`Service rejected manifest (${saveRes.status})`);
      }
      if (typeof localStorage !== "undefined") {
        localStorage.setItem("omniphony_eclipse_manifest", url);
      }
      currentConfig.eclipse_addons = [url];
      const r = await fetch(url).catch(() => null);
      if (r && r.ok) {
        const manifest = await r.json();
        setAddonStatus(`✓ Saved & Loaded ${String(manifest.name || "Addon")} v${String(manifest.version || "1.0")} (${String(manifest.description || "Active")})`, "#00ff88", true);
      } else {
        setAddonStatus(`✓ Saved Eclipse Manifest to RealMAX Engine (${url})`, "#00ff88", true);
      }
    } catch (err) {
      addonStatus.innerText = `Error: ${err.message}`;
    }
  };


  let authPollTimer = null;
  const updateAuthStatus = async () => {
    try {
      const res = await requestWithTimeout(`${API_BASE}/login/status`, {}, 2500);
      if (!res.ok) return;
      const data = await res.json();
      if (!panel.isConnected) {
        if (authPollTimer) clearInterval(authPollTimer);
        return;
      }
      if (data.status === "waiting_for_user" && data.userCode) {
        authStatus.replaceChildren(document.createTextNode("Open browser and verify code: "));
        const code = document.createElement("strong");
        code.style.cssText = "color:#00d2c4;font-size:13px;";
        code.textContent = String(data.userCode);
        authStatus.append(code, document.createTextNode(` (${String(data.verificationUrl || "")})`));
        authBtn.disabled = true;
        authBtn.style.opacity = "0.6";
      } else if (data.status === "success") {
        authStatus.replaceChildren();
        const success = document.createElement("strong");
        success.style.cssText = "color:#00ff88;font-weight:600;";
        success.textContent = "✓ TIDAL Account Linked Successfully!";
        authStatus.appendChild(success);
        authBtn.disabled = false;
        authBtn.style.opacity = "1";
        if (authPollTimer) {
          clearInterval(authPollTimer);
          authPollTimer = null;
        }
      } else if (data.status === "expired" || data.status === "failed") {
        authStatus.innerText = `Auth flow ${data.status}. Click to retry.`;
        authBtn.disabled = false;
        authBtn.style.opacity = "1";
        if (authPollTimer) {
          clearInterval(authPollTimer);
          authPollTimer = null;
        }
      }
    } catch (_) {}
  };

  authBtn.onclick = async () => {
    authStatus.innerText = "Initiating TIDAL device login…";
    authBtn.disabled = true;
    authBtn.style.opacity = "0.6";
    try {
      const postRes = await requestWithTimeout(`${API_BASE}/login/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }, 6000);
      if (postRes.ok) {
        if (authPollTimer) clearInterval(authPollTimer);
        authPollTimer = setInterval(updateAuthStatus, 2000);
        updateAuthStatus();
      } else {
        authStatus.innerText = "Failed to start login flow. Service might be busy or already authenticating.";
        authBtn.disabled = false;
        authBtn.style.opacity = "1";
      }
    } catch (err) {
      authStatus.innerText = `Auth request failed: ${err.message}`;
      authBtn.disabled = false;
      authBtn.style.opacity = "1";
    }
  };
  updateAuthStatus();

  document.body.appendChild(panel);
  updatePausedSelectionUi();

  const panelLoadToken = ++configLoadGeneration;
  Promise.all([fetchConfig(), fetchDevices()]).then(([loaded, devicesLoaded]) => {
    if (!panel.isConnected || configLoadGeneration !== panelLoadToken) return;
    if (!loaded) {
      prioritySaveStatus.innerText = `Could not load saved settings (${lastSettingsFetchFailure || "unknown failure"}). Retry by closing and reopening.`;
      prioritySaveStatus.style.color = "#ff7272";
      prioritySelect.disabled = true;
      settingsUiVerified = false;
    } else {
      currentConfig = { ...currentConfig, ...loaded };
      if (document.getElementById("omniphony-source-select")) document.getElementById("omniphony-source-select").value = loaded?.realmax_source || "auto";
      if (Array.isArray(loaded?.eclipse_addons) && loaded.eclipse_addons.length > 0) {
        if (addonInput && !addonInput.value) addonInput.value = loaded.eclipse_addons[0];
        if (addonStatus) {
          setAddonStatus(`✓ Active Eclipse Manifest: ${String(loaded.eclipse_addons[0])}`, "#00ff88", true);
        }
      }
      prioritySelect.value = loaded.priority.join(",");
      updatePriorityTitle();
      prioritySelect.disabled = false;
      prioritySaveStatus.innerText = "Saved priority loaded. Changes apply to the next track.";
      prioritySaveStatus.style.color = "#aaa";
    }
    if (deviceSelect.dataset.userChanged !== "true") {
      populateDeviceSelector(deviceSelect, loaded?.audio_device || currentConfig.audio_device);
    }
    const selectedDeviceDetected = availableDevices.some((item) => item.name === REQUIRED_AUDIO_DEVICE);
    deviceLoadStatus.innerText = !devicesLoaded
      ? `Could not load devices (${lastDeviceFetchFailure || "unknown failure"}).`
      : selectedDeviceDetected
        ? `Voicemeeter WASAPI endpoint detected (${availableDevices.length} device${availableDevices.length === 1 ? "" : "s"} enumerated).`
        : `The configured Voicemeeter WASAPI endpoint is absent from the ${availableDevices.length}-device list.`;
    deviceLoadStatus.style.color = devicesLoaded && selectedDeviceDetected ? "#aaa" : "#ff7272";
    settingsUiVerified = verifySettingsPanelControls(panel);
    if (loaded && !settingsUiVerified) {
      prioritySaveStatus.innerText = "Settings controls could not be fully verified; playback checks the saved priority and output device before starting.";
      prioritySaveStatus.style.color = "#ff7272";
    }
  });

  document.getElementById("omniphony-close-btn").onclick = () => {
    if (panel._omniphonyResizeHandler) {
      window.removeEventListener("resize", panel._omniphonyResizeHandler);
      delete panel._omniphonyResizeHandler;
    }
    panel.remove();
  };
}


// ==============================================================================
// Omniphony EX RealMAX Native Song & Album Downloader
// ==============================================================================

const downloadState = { active: false, cancel: false };
let downloadButtonRef = null;

function getDownloadSettings() {
  return {
    destinationFolder: (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_download_folder") : null) || "",
    pathPattern: (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_download_pattern") : null) || "{artist}/{album}/{trackNumber} - {title}",
    ac4Format: (typeof localStorage !== "undefined" ? localStorage.getItem("omniphony_download_ac4_format") : null) || "flac",
  };
}

async function selectDownloadFolderNative() {
  try {
    const showOpenDialog = window.luna?.native?.showOpenDialog || window.luna?.lib?.showOpenDialog;
    if (showOpenDialog) {
      const res = await showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
      if (!res?.canceled && res?.filePaths?.[0]) {
        return res.filePaths[0];
      }
    }
  } catch (_) {}
  return null;
}

async function queueTrackDownload(trackMeta) {
  const cfg = getDownloadSettings();
  const artistMeta = trackMeta.artist;
  const albumMeta = trackMeta.album;
  const artistName = typeof artistMeta === "string"
    ? artistMeta
    : artistMeta?.name || trackMeta.artistName ||
      (Array.isArray(trackMeta.artists) ? trackMeta.artists.map((item) => item?.name).filter(Boolean).join("; ") : "Unknown Artist");
  const albumName = typeof albumMeta === "string"
    ? albumMeta
    : albumMeta?.title || albumMeta?.name || trackMeta.albumTitle || "Unknown Album";
  const albumCover = typeof albumMeta?.cover === "string" ? albumMeta.cover : "";
  const albumArtUrl = trackMeta.artworkURL || trackMeta.cover || albumMeta?.artworkURL ||
    (albumCover.startsWith("http") ? albumCover : albumCover.includes("-")
      ? `https://resources.tidal.com/images/${albumCover.replace(/-/g, "/")}/1280x1280.jpg`
      : "");
  const lyricText = typeof trackMeta.lyrics === "string" ? trackMeta.lyrics : trackMeta.lyrics?.text || trackMeta.plainLyrics || "";
  const syncedLyrics = typeof trackMeta.syncedLyrics === "string" ? trackMeta.syncedLyrics : trackMeta.subtitles || trackMeta.lrc || "";
  const payload = {
    trackId: trackMeta.id || trackMeta.trackId,
    title: trackMeta.title || trackMeta.name || "Unknown Title",
    artist: artistName,
    album: albumName,
    albumArtist: trackMeta.albumArtist || albumMeta?.artist?.name || "",
    year: trackMeta.year || (trackMeta.releaseDate ? String(trackMeta.releaseDate).slice(0, 4) : ""),
    trackNumber: trackMeta.trackNumber || trackMeta.track_number || "",
    discNumber: trackMeta.discNumber || trackMeta.disc_number || trackMeta.volumeNumber || "",
    isrc: trackMeta.isrc || "",
    genre: Array.isArray(trackMeta.genre) ? trackMeta.genre.join("; ") : trackMeta.genre || "",
    composer: Array.isArray(trackMeta.composer) ? trackMeta.composer.map((item) => item?.name || item).join("; ") : trackMeta.composer || "",
    copyright: trackMeta.copyright || "",
    lyrics: lyricText,
    syncedLyrics,
    albumArtUrl,
    destinationFolder: cfg.destinationFolder,
    ac4Format: cfg.ac4Format,
    pathPattern: cfg.pathPattern,
  };

  const res = await requestWithTimeout(`${API_BASE}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }, 10000);

  if (!res.ok) {
    throw new Error(`Download queue failed: ${res.status}`);
  }
  return await res.json();
}

function installNativeDownloadContextMenu() {
  try {
    const ContextMenu = window.luna?.lib?.ContextMenu;
    if (!ContextMenu || typeof ContextMenu.onMediaItem !== "function") {
      return;
    }
    const btn = ContextMenu.addButton(unloads);
    downloadButtonRef = btn;

    ContextMenu.onMediaItem(unloads, async ({ mediaCollection, contextMenu }) => {
      let count = 0;
      try {
        count = await mediaCollection.count();
      } catch (_) {
        count = 1;
      }
      if (count === 0) return;

      const defaultText = (btn.text = count > 1 ? `Download ${count} tracks (RealMAX)` : `Download Track (RealMAX)`);

      btn.onClick(async () => {
        if (!btn.elem) return;
        if (downloadState.active) {
          downloadState.cancel = true;
          btn.text = "Stopping...";
          return;
        }

        downloadState.active = true;
        downloadState.cancel = false;
        try {
          let items = [];
          if (typeof mediaCollection.mediaItems === "function") {
            for await (const m of await mediaCollection.mediaItems()) {
              items.push(m);
            }
          }
          if (items.length === 0 && typeof mediaCollection.all === "function") {
            items = await mediaCollection.all();
          }

          let doneCount = 0;
          for (const item of items) {
            if (downloadState.cancel) break;
            btn.text = `Queueing ${++doneCount}/${items.length}...`;
            try {
              let tMeta = item;
              if (typeof item.flacTags === "function") {
                const { tags } = await item.flacTags();
                tMeta = { ...item, ...tags };
              }
              const qRes = await queueTrackDownload(tMeta);
              const jobId = qRes?.jobId;
              if (jobId) {
                // Poll progress briefly
                let finished = false;
                for (let poll = 0; poll < 60; poll++) {
                  await new Promise((r) => setTimeout(r, 800));
                  const pollRes = await requestWithTimeout(`${API_BASE}/download/status`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ jobId })
                  }, 4000);
                  if (pollRes.ok) {
                    const st = await pollRes.json();
                    btn.text = `${st.status === "rendering" ? "Rendering Binaural" : "Downloading"} ${st.progress || 0}%`;
                    if (st.status === "completed" || st.status === "failed") {
                      finished = true;
                      break;
                    }
                  }
                }
              }
            } catch (err) {
              console.warn("[Omniphony EX Downloader] Track error:", err);
            }
          }
        } finally {
          downloadState.active = false;
          downloadState.cancel = false;
          btn.text = defaultText;
        }
      });

      await btn.show(contextMenu);
    });
  } catch (err) {
    console.warn("[Omniphony EX Downloader] ContextMenu setup error:", err);
  }
}


function updatePausedSelectionUi(message = null) {
  const button = document.getElementById("omniphony-prepare-paused");
  const status = document.getElementById("omniphony-prepare-paused-status");
  const armed = prepareNextSelectionPaused || Boolean(pausedSelectionPending);
  if (button) {
    button.textContent = armed ? "Cancel paused preparation" : "Prepare next selection paused";
    button.setAttribute("aria-pressed", String(armed));
  }
  if (!status) return;
  if (message !== null) {
    status.textContent = message;
  } else if (!nativePlayerCommandHookInstalled) {
    status.textContent = `Native player guard unavailable: ${nativePlayerCommandHookDiagnostic}. Playback remains blocked.`;
  } else if (prepareNextSelectionPaused) {
    status.textContent = "Armed for one TIDAL track selection. The next Play action will be held paused.";
  } else if (pausedSelectionPending) {
    status.textContent = pausedSelectionPending.expired
      ? "TIDAL has not confirmed the requested track. Playback remains paused; no stream was prepared."
      : "Waiting for TIDAL to confirm the selected track. Playback remains paused.";
  } else if (typeof pausedSelectionConfirmedTrackId !== "undefined"
      && pausedSelectionConfirmedTrackId !== null) {
    const confirmedTrackId = String(pausedSelectionConfirmedTrackId);
    const queueMatches = typeof getQueuedTrackId === "function"
      && String(getQueuedTrackId() ?? "") === confirmedTrackId;
    status.textContent = !queueMatches
      ? "TIDAL queue changed after selection verification. Playback remains paused until the selected track is verified again."
      : typeof isPausedSelectionDiagnosticsReady === "function"
          && isPausedSelectionDiagnosticsReady(confirmedTrackId)
        ? "Stream diagnostics are ready. Use TIDAL's Play control to start this track."
        : "The selected stream diagnostics are still loading. Playback remains paused.";
  } else {
    status.textContent = "Native player guard verified. Arm this before selecting a track to prepare it without audible playback.";
  }
}

// Run only after every module-level observer and state object has initialized.
onLoad();
installNativeDownloadContextMenu();


// Native TidaLuna Settings Modal Component
export const Settings = () => {
  const React = window.luna?.core?.modules?.["react"] || window.React;
  const LunaUi = window.luna?.core?.modules?.["@luna/ui"];
  
  if (!React || !LunaUi) {
    return null;
  }
  
  const { LunaSettings, LunaSwitchSetting, LunaSelectSetting, LunaSelectItem, LunaTextSetting, LunaButtonSetting } = LunaUi;
  
  const [priority, setPriority] = React.useState(currentConfig.priority.join(","));
  const priorityTouched = React.useRef(false);
  const [device, setDevice] = React.useState(currentConfig.audio_device);
  const [sourceMode, setSourceMode] = React.useState(localStorage.getItem("omniphony_realmax_source") || "auto");
  const [mpvPath, setMpvPath] = React.useState(localStorage.getItem("omniphony_mpv_path") || "");
  const [studioPath, setStudioPath] = React.useState(localStorage.getItem("omniphony_studio_path") || "");
  const [addonUrl, setAddonUrl] = React.useState("");
  const [authStatus, setAuthStatus] = React.useState("Ready");
  const [dlFolder, setDlFolder] = React.useState(localStorage.getItem("omniphony_download_folder") || "");
  const [ac4Format, setAc4Format] = React.useState(localStorage.getItem("omniphony_download_ac4_format") || "flac");
  const [dlPattern, setDlPattern] = React.useState(localStorage.getItem("omniphony_download_pattern") || "{artist}/{album}/{trackNumber} - {title}");

  React.useEffect(() => {
    let active = true;
    requestWithTimeout(`${API_BASE}/config`, { cache: "no-store" }, 5000)
      .then(async (response) => {
        if (!response.ok) return;
        const config = await response.json();
        const loadedPriority = Array.isArray(config?.priority)
          ? config.priority.join(",")
          : "";
        const isSupportedPriority = priorityPresets.some(
          (preset) => preset.value.join(",") === loadedPriority
        );
        if (active && !priorityTouched.current && isSupportedPriority) {
          currentConfig = { ...currentConfig, ...config };
          setPriority(loadedPriority);
        }
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);
  
  return React.createElement(
    LunaSettings,
    { title: "Omniphony EX Engine & RealMAX" },
    React.createElement(
      LunaSelectSetting,
      {
        title: "RealMAX Quality Priority",
        desc: "Format hierarchy for discrete Atmos (AC-4 / E-AC-3 JOC) and Hi-Res Lossless.",
        value: priority,
        onChange: async (e) => {
          const val = e.target.value;
          priorityTouched.current = true;
          setPriority(val);
          try {
            await fetch(`${API_BASE}/config`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ priority: val.split(",") })
            });
          } catch (_) {}
        }
      },
      priorityPresets.map((opt) =>
        React.createElement(LunaSelectItem, { key: opt.value.join(","), value: opt.value.join(",") }, opt.label)
      )
    ),
    React.createElement(
      LunaSelectSetting,
      {
        title: "RealMAX Source Provider",
        desc: "Choose between TIDAL only, Amazon Music only, or Both (highest quality wins). Amazon Music has no AC-4 IMS.",
        value: sourceMode,
        onChange: async (e) => {
          const val = e.target.value;
          setSourceMode(val);
          localStorage.setItem("omniphony_realmax_source", val);
          try {
            await fetch(`${API_BASE}/config`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ realmax_source: val })
            });
          } catch (_) {}
        }
      },
      React.createElement(LunaSelectItem, { value: "auto" }, "Both (TIDAL + Amazon Music / Eclipse)"),
      React.createElement(LunaSelectItem, { value: "tidal_only" }, "TIDAL Only (Atmos AC-4/JOC + Hi-Res)"),
      React.createElement(LunaSelectItem, { value: "amazon_only" }, "Amazon Music Only (No AC-4 IMS)")
    ),
    React.createElement(LunaTextSetting, {
      title: "MPV Engine Directory",
      desc: "Directory containing mpv.exe and orender.dll.",
      value: mpvPath,
      onChange: (e) => {
        const val = e.target.value;
        setMpvPath(val);
        localStorage.setItem("omniphony_mpv_path", val);
      }
    }),
    React.createElement(LunaTextSetting, {
      title: "Omniphony Studio Executable",
      desc: "Path to omniphony-studio-egui.exe for spatial headphone monitoring.",
      value: studioPath,
      onChange: (e) => {
        const val = e.target.value;
        setStudioPath(val);
        localStorage.setItem("omniphony_studio_path", val);
      }
    }),
    React.createElement(LunaButtonSetting, {
      title: "Apply Engine Paths",
      desc: "Restart the plugin-owned API with the selected external MPV and Studio paths.",
      onClick: async () => {
        const ready = await ensureLocalOmniFusionService();
        if (ready) {
          alert("Omniphony API is ready with the selected engine paths.");
        } else {
          alert("Omniphony API could not start. Check that the bundled runtime is installed and both external paths are correct.");
        }
      }
    }, "Apply Paths / Restart API"),
    React.createElement(LunaTextSetting, {
      title: "Amazon Music Eclipse Manifest URL",
      desc: "Eclipse add-on manifest URL for multi-source Amazon Music resolution.",
      value: addonUrl,
      onChange: (e) => setAddonUrl(e.target.value)
    }),
    React.createElement(LunaButtonSetting, {
      title: "Add / Update Eclipse Manifest",
      desc: "Registers manifest with local RealMAX resolver.",
      onClick: async () => {
        if (!addonUrl.trim()) return;
        try {
          await fetch(`${API_BASE}/config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ eclipse_addons: [addonUrl.trim()] })
          });
          alert("Saved Eclipse Manifest!");
        } catch (err) {
          alert("Failed to save manifest: " + err.message);
        }
      }
    }, "Save Eclipse Manifest"),
    
    React.createElement(LunaTextSetting, {
      title: "RealMAX Download Folder",
      desc: "Directory where downloaded tracks, albums, and lyrics are saved.",
      value: dlFolder,
      onChange: (e) => {
        const val = e.target.value;
        setDlFolder(val);
        localStorage.setItem("omniphony_download_folder", val);
      }
    }),
    React.createElement(LunaButtonSetting, {
      title: "Select Download Directory",
      desc: `Current folder: ${dlFolder}`,
      onClick: async () => {
        const chosen = await selectDownloadFolderNative();
        if (chosen) {
          setDlFolder(chosen);
          localStorage.setItem("omniphony_download_folder", chosen);
        }
      }
    }, "Browse Folder..."),
    React.createElement(
      LunaSelectSetting,
      {
        title: "AC-4 IMS Download Format",
        desc: "Choose between 24-bit/48kHz Binaural FLAC (tagged & compact) or Bit-Perfect WAV.",
        value: ac4Format,
        onChange: (e) => {
          const val = e.target.value;
          setAc4Format(val);
          localStorage.setItem("omniphony_download_ac4_format", val);
        }
      },
      React.createElement(LunaSelectItem, { value: "flac" }, "FLAC (Binaural 24-bit/48kHz - Tagged)"),
      React.createElement(LunaSelectItem, { value: "wav" }, "WAV (Binaural 24-bit/48kHz - Uncompressed)")
    ),
    React.createElement(LunaTextSetting, {
      title: "Download Filename & Folder Template",
      desc: "Supports {artist}, {album}, {title}, {trackNumber}, {year}, and {isrc}.",
      value: dlPattern,
      onChange: (e) => {
        const val = e.target.value;
        setDlPattern(val);
        localStorage.setItem("omniphony_download_pattern", val);
      }
    }),
    React.createElement(LunaButtonSetting, {
      title: "TIDAL OAuth Account Authorization",
      desc: authStatus,
      onClick: async () => {
        setAuthStatus("Starting OAuth in browser...");
        try {
          const res = await fetch(`${API_BASE}/login/start`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
          if (res.ok) {
            const data = await res.json();
            setAuthStatus(`Verify Code in Browser: ${data.userCode}`);
          } else {
            setAuthStatus("Failed to start login");
          }
        } catch (err) {
          setAuthStatus("Error: " + err.message);
        }
      }
    }, "Link / Authenticate Account")
  );
};
