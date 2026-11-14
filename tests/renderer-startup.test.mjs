import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8").replace(/\r\n/g, "\n");
const startupCode = source.match(
  /function getConfiguredRuntimePaths\(\) \{[\s\S]*?(?=\nfunction onLoad\(\) \{)/,
)?.[0];
const nativeCode = source.match(
  /const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
const pollGateSource = source.match(
  /function canPollLocalServiceStatus\(startupPending\) \{[\s\S]*?\n\}/,
)?.[0];
const syncLoopStart = source.indexOf("function startSyncLoop() {");
const syncLoopEnd = source.indexOf(
  "\n// ---------------------------------------------------------------------------\n// Native In-TIDAL UI Elements",
  syncLoopStart,
);
const syncLoopSource = syncLoopStart >= 0 && syncLoopEnd > syncLoopStart
  ? source.slice(syncLoopStart, syncLoopEnd).trimEnd()
  : null;
const onLoadSource = source.match(
  /function onLoad\(\) \{[\s\S]*?(?=\nunloads\.add\()/,
)?.[0];
assert.ok(startupCode, "renderer startup reads and passes persisted runtime paths");
assert.ok(nativeCode, "renderer startup uses the TidaLuna native supervisor");
assert.ok(pollGateSource, "service status polling has an explicit startup gate");
assert.ok(syncLoopSource, "the actual sync loop can be exercised in a controlled harness");
assert.ok(onLoadSource, "the actual renderer startup lifecycle can be exercised");

const pollGateSandbox = {};
vm.createContext(pollGateSandbox);
vm.runInContext(
  `${pollGateSource}\nglobalThis.canPollLocalServiceStatus = canPollLocalServiceStatus;`,
  pollGateSandbox,
);
assert.equal(
  pollGateSandbox.canPollLocalServiceStatus(true),
  false,
  "status polling remains suppressed while native service startup is pending",
);
assert.equal(
  pollGateSandbox.canPollLocalServiceStatus(false),
  true,
  "status polling resumes after service startup has settled",
);

{
  let settleStartup;
  const startupSandbox = {
    pluginDisposed: true,
    localServiceStartupPending: false,
    PLUGIN_BUILD: "test",
    console: { log() {}, error() {} },
    ensureLocalOmniFusionService: () => new Promise((resolve) => { settleStartup = resolve; }),
    tracePlaybackEvent() {},
    window: {},
    installRadiantLyricsFetchObserver: () => true,
    interceptMediaElements() {},
    installLyricsClickListener() {},
    injectUI() {},
    startSyncLoop() {},
  };
  vm.createContext(startupSandbox);
  vm.runInContext(`${onLoadSource}\nglobalThis.onLoad = onLoad;`, startupSandbox);
  startupSandbox.onLoad();
  assert.equal(startupSandbox.localServiceStartupPending, true,
    "onLoad closes the status poll gate before launching the native supervisor");
  settleStartup(true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(startupSandbox.localServiceStartupPending, false,
    "the gate reopens after successful native service startup");
}

{
  const intervals = [];
  const requests = [];
  const warnings = [];
  const syncSandbox = {
    pollTimer: null,
    clockPollTimer: null,
    pluginDisposed: false,
    localServiceStartupPending: true,
    API_BASE: "http://127.0.0.1:8765",
    SYNC_POLL_INTERVAL_MS: 100,
    CLOCK_POLL_INTERVAL_MS: 50,
    canPollLocalServiceStatus: (pending) => pending !== true,
    setInterval(callback, delay) {
      intervals.push({ callback, delay });
      return intervals.length;
    },
    clearInterval() {},
    installPlaybackObservers() {},
    requestWithTimeout: async (...args) => {
      requests.push(args);
      throw new Error("connection refused");
    },
    invalidateEngineStatus() {},
    localRequestFailureSummary: () => "connection refused",
    console: { warn(...args) { warnings.push(args); }, debug() {}, info() {}, error() {} },
  };
  vm.createContext(syncSandbox);
  vm.runInContext(`${syncLoopSource}\nglobalThis.startSyncLoop = startSyncLoop;`, syncSandbox);
  syncSandbox.startSyncLoop();
  await intervals[0].callback();
  assert.equal(requests.length, 0, "pending startup sends no /status request and emits no fetch warning");
  assert.equal(warnings.length, 0);

  syncSandbox.localServiceStartupPending = false;
  await intervals[0].callback();
  assert.equal(requests.length, 1, "a settled startup resumes normal service health polling");
  assert.equal(warnings.length, 1, "real post-start connection failures remain visible");
}

function makeHarness(result, { ipc = true, mpvDir = "", studioExe = "" } = {}) {
  const calls = [];
  const errors = [];
  const storage = new Map([
    ["omniphony_mpv_path", mpvDir],
    ["omniphony_studio_path", studioExe],
  ]);
  const sandbox = {
    SERVICE_STARTUP_STATE_KEY: Symbol.for("omniphony-ex.service-startup-state"),
    REQUIRED_SERVICE_VERSION: "2.1.33",
    NATIVE_SERVICE_SUPERVISOR_MODULE_ID: "OmniphonyEX/service-supervisor.native.js",
    NATIVE_SERVICE_SUPERVISOR_MODULE_CODE: nativeCode,
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
    },
    console: { info() {}, error(...args) { errors.push(args); } },
    window: ipc ? {
      __ipcRenderer: {
        async invoke(...args) {
          calls.push(args);
          if (args[0] === "__Luna.registerNative") return "__LunaNative.service-supervisor";
          return result;
        },
      },
    } : {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${startupCode}\nglobalThis.ensureLocalOmniFusionService = ensureLocalOmniFusionService;`,
    sandbox,
  );
  return { calls, errors, storage, run: () => sandbox.ensureLocalOmniFusionService() };
}

{
  const result = {
    ready: true,
    version: "2.1.33",
    mpvDirectory: "C:\\OmniFusion-Standalone\\mpv",
    studioExecutable: "C:\\OmniFusion\\studio\\omniphony-studio-egui.exe",
  };
  const harness = makeHarness(result);
  const ready = await Promise.all([harness.run(), harness.run()]);
  assert.deepEqual(ready, [true, true], "parallel plugin startup calls share one native request");
  assert.equal(harness.calls.length, 2, "one module registration and one startup call are made");
  assert.equal(harness.calls[0][0], "__Luna.registerNative");
  assert.equal(harness.calls[1][1], "ensureService");
  assert.deepEqual(
    JSON.parse(JSON.stringify(harness.calls[1][3])),
    { mpvDir: "", studioExe: "" },
    "empty UI fields defer to the plugin's persisted runtime-path configuration",
  );
  assert.equal(harness.storage.get("omniphony_mpv_path"), result.mpvDirectory);
  assert.equal(harness.storage.get("omniphony_studio_path"), result.studioExecutable);
}

{
  const harness = makeHarness({ ready: false }, { ipc: false });
  assert.equal(await harness.run(), false, "missing native IPC reports startup failure cleanly");
}

{
  const failure = {
    ready: false,
    reason: "runtime_paths_invalid",
    missingFiles: ["C:\\\\OmniphonyEX\\\\mpv.exe"],
  };
  const harness = makeHarness(failure);
  assert.equal(await harness.run(), false, "native supervisor failures remain unavailable");
  assert.deepEqual(
    harness.errors[0],
    [
      "[Omniphony EX] Native service supervisor could not start OmniFusion:",
      JSON.stringify(failure),
    ],
    "structured native startup failures are serialized instead of becoming [object Object] in TIDAL logs",
  );
}

console.log("Renderer-to-native startup and settings synchronization checks passed.");
