import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const startupCode = source.match(
  /function getConfiguredRuntimePaths\(\) \{[\s\S]*?(?=\nfunction onLoad\(\) \{)/,
)?.[0];
const nativeCode = source.match(
  /const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
assert.ok(startupCode, "renderer startup reads and passes persisted runtime paths");
assert.ok(nativeCode, "renderer startup uses the TidaLuna native supervisor");

function makeHarness(result, { ipc = true, mpvDir = "", studioExe = "" } = {}) {
  const calls = [];
  const storage = new Map([
    ["omniphony_mpv_path", mpvDir],
    ["omniphony_studio_path", studioExe],
  ]);
  const sandbox = {
    SERVICE_STARTUP_STATE_KEY: Symbol.for("omniphony-ex.service-startup-state"),
    REQUIRED_SERVICE_VERSION: "2.1.27",
    NATIVE_SERVICE_SUPERVISOR_MODULE_ID: "OmniphonyEX/service-supervisor.native.js",
    NATIVE_SERVICE_SUPERVISOR_MODULE_CODE: nativeCode,
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
    },
    console: { info() {}, error() {} },
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
  return { calls, storage, run: () => sandbox.ensureLocalOmniFusionService() };
}

{
  const result = {
    ready: true,
    version: "2.1.27",
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

console.log("Renderer-to-native startup and settings synchronization checks passed.");
