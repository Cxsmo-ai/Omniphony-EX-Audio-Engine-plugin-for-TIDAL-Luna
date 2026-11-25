import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const moduleSource = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const legacyBootstrapSource = fs.readFileSync(path.join(root, "..", "index.js"), "utf8");
const pluginVersion = moduleSource.match(/^const PLUGIN_BUILD = "([^"]+)";$/m)?.[1];
const testServiceVersion = "2.1.36";
const runtimeBundleSha256 = moduleSource.match(/const bundleSha256 = "([A-F0-9]+)";/)?.[1];
const runtimeBundleVersion = moduleSource.match(
  /const bundledRuntimeRoot = path\.join\(pluginRoot, "runtime-bundles", "([^"]+)"\);/,
)?.[1];
const nativeAuthModuleCode = moduleSource.match(
  /const NATIVE_ECLIPSE_AUTH_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
const supervisorCodeTemplate = moduleSource.match(
  /const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
const supervisorCode = supervisorCodeTemplate?.replace(
  "${NATIVE_ECLIPSE_AUTH_MODULE_CODE}",
  () => nativeAuthModuleCode,
);
assert.ok(supervisorCode, "the native service supervisor is embedded in the plugin entrypoint");
assert.match(supervisorCode, /catch \(promotionError\)[\s\S]*?fs\.renameSync\(backup, bundledRuntimeRoot\)[\s\S]*?throw promotionError/,
  "a failed runtime promotion restores the previous bundle before reporting failure");
assert.doesNotMatch(supervisorCode, /new Function\s*\(/,
  "the supervisor contains no runtime code generation forbidden by TidaLuna's secure loader");
assert.match(supervisorCode, /async startEclipseLogin\(serviceBase, requiredVersion, runtimeConfig = \{\}\)[\s\S]*?module\.exports\.ensureService\(requiredVersion, runtimeConfig\)/,
  "Eclipse authentication ensures and uses the service identity in one native-module context");
assert.match(supervisorCode, /async requestEclipseStreamInventory\(serviceBase, requiredVersion, runtimeConfig = \{\}, input = \{\}\)[\s\S]*?Authorization: "Bearer " \+ shared\.shutdownToken[\s\S]*?X-Omniphony-Service-Instance/,
  "stream inventory authorization stays inside the native service supervisor");
assert.match(supervisorCode, /invalid_provider_key[\s\S]*?track_identity_required/,
  "the native inventory request validates provider and track identity before building its local request");
assert.match(moduleSource, /setEclipseInventoryStatus\(`Inspecting \$\{provider\.name\}[\s\S]*?let operationId = null;\s*try \{\s*operationId = createEclipseInventoryOperationId\(\);[\s\S]*?catch \(error\) \{\s*if \(operationId &&\s*eclipseInventoryOperationRef\.current === operationId\)/,
  "inventory failure handling keeps the operation ID in scope and preserves newer operation ownership");
assert.match(moduleSource, /const canceledGeneration = \+\+eclipseInventoryGenerationRef\.current;\s*if \(eclipseInventoryGenerationRef\.current === canceledGeneration\) setEclipseInventoryBusy\(false\);/,
  "manual cancellation releases the busy state for a subsequent inventory run");
assert.ok(pluginVersion, "the plugin build version is declared in the canonical entrypoint");
assert.ok(runtimeBundleVersion, "the separately versioned API runtime bundle is declared in the supervisor");
assert.match(moduleSource, /NATIVE_SERVICE_SUPERVISOR_MODULE_ID = `OmniphonyEX\/service-supervisor\.native\.js\.\$\{PLUGIN_BUILD\}`/,
  "each plugin build registers a fresh native supervisor module");
assert.match(moduleSource, /SERVICE_STARTUP_STATE_KEY = Symbol\.for\(`omniphony-ex\.service-startup-state\.\$\{PLUGIN_BUILD\}`\)/,
  "renderer reloads do not reuse a previous plugin build's supervisor channel");
assert.match(legacyBootstrapSource, /^\/\/ Compatibility entrypoint:[\s\S]*export \* from ["']\.\/omniphony\.mjs["'];?\s*$/,
  "the alternate entrypoint delegates all startup and shutdown behavior to the canonical module");
assert.doesNotMatch(legacyBootstrapSource, /spawn\(|child_process|ensureLocalServiceRunning|shutdown/,
  "the alternate entrypoint must not duplicate service lifecycle logic");
assert.doesNotMatch(
  legacyBootstrapSource,
  /path\.join\(path\.dirname\(studioExe\),\s*['"]orender\.exe['"]\)/,
  "in-process orender.dll playback must not require the standalone renderer executable",
);
assert.match(moduleSource, /async function startEclipseLoginWithNativeSupervisor\(\)[\s\S]*?ipc\.invoke\([\s\S]*?channel,[\s\S]*?"startEclipseLogin",[\s\S]*?API_BASE,[\s\S]*?REQUIRED_SERVICE_VERSION,[\s\S]*?getConfiguredRuntimePaths\(\)/,
  "Eclipse sign-in and service startup are routed through one native supervisor call");
assert.equal((moduleSource.match(/startEclipseLoginWithNativeSupervisor\(\)/g) || []).length, 3,
  "both Eclipse account controls use the shared native supervisor login path");
assert.doesNotMatch(moduleSource, /NATIVE_ECLIPSE_AUTH_MODULE_ID|"startLogin", API_BASE/,
  "no stale renderer-side auth module registration can bypass the service-owning supervisor");
assert.match(moduleSource, /getAttribute\(SERVICE_SUPERVISOR_CHANNEL_ATTRIBUTE\)/,
  "plugin contexts recover the service-owning native channel from their shared TIDAL document");
assert.match(moduleSource, /setAttribute\(SERVICE_SUPERVISOR_CHANNEL_ATTRIBUTE, channel\)/,
  "the service-owning native channel is published without exposing its authentication token");

function touch(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "test fixture");
}

function makeHarness({
  healthResponses,
  mpvDir,
  studioExe,
  createRuntimeFiles = true,
  localServiceVersion = testServiceVersion,
  verifiedBundle = false,
  processPid = 4321,
  browserPid = 4321,
  browserMetrics = null,
}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omniphony-supervisor-test-"));
  const pluginRoot = path.join(userData, "plugins", "omniphony-tidal-luna-plugin");
  const dataRoot = path.join(pluginRoot, "data");
  const apiRoot = path.join(pluginRoot, "api");
  const paths = {
    mpvDir: mpvDir || path.join(userData, "external", "mpv"),
    studioExe: studioExe || path.join(userData, "external", "studio", "omniphony-studio-egui.exe"),
  };
  fs.mkdirSync(path.join(pluginRoot, ".git"), { recursive: true });
  fs.mkdirSync(path.join(dataRoot, "config"), { recursive: true });
  fs.writeFileSync(
    path.join(dataRoot, "config", "runtime-paths.json"),
    JSON.stringify(paths),
  );
  if (createRuntimeFiles) {
    for (const file of [
      path.join(paths.mpvDir, "mpv.exe"),
      path.join(paths.mpvDir, "orender.dll"),
      paths.studioExe,
    ]) touch(file);
  }
  for (const file of [
    "runtime/python.exe",
    "omnifusion_service.py",
    "eclipse_streams.py",
    "vendor/tidalapi/__init__.py",
    "oxideav/adapter-v0.1.71/ac4-to-wav.exe",
    "oxideav/adapter-v0.1.71/omnifusion_ac4_adapter.dll",
    "oxideav/adapter-v0.1.71/LICENSE",
    "harletty/v0.9.0/harletty_dolby_bridge.dll",
    "harletty/v0.9.0/harletty_dts_bridge.dll",
    "harletty/v0.9.0/harletty_iamf_bridge.dll",
    "harletty/v0.9.0/LICENSE",
  ]) touch(path.join(apiRoot, file));
  fs.writeFileSync(
    path.join(apiRoot, "omnifusion_service.py"),
    `SERVICE_VERSION = "${localServiceVersion}"\n`,
  );
  if (verifiedBundle) {
    const bundleRoot = path.join(pluginRoot, "runtime-bundles", runtimeBundleVersion);
    const bundledApiRoot = path.join(bundleRoot, "api");
    for (const file of [
      "runtime/python.exe",
      "omnifusion_service.py",
      "eclipse_streams.py",
      "vendor/tidalapi/__init__.py",
      "oxideav/adapter-v0.1.71/ac4-to-wav.exe",
      "oxideav/adapter-v0.1.71/omnifusion_ac4_adapter.dll",
      "oxideav/adapter-v0.1.71/LICENSE",
      "harletty/v0.9.0/harletty_dolby_bridge.dll",
      "harletty/v0.9.0/harletty_dts_bridge.dll",
      "harletty/v0.9.0/harletty_iamf_bridge.dll",
      "harletty/v0.9.0/LICENSE",
    ]) touch(path.join(bundledApiRoot, file));
    fs.writeFileSync(path.join(bundledApiRoot, "omnifusion_service.py"), `SERVICE_VERSION = "${testServiceVersion}"\n`);
    const bundleSha256 = moduleSource.match(/const bundleSha256 = "([A-F0-9]+)";/)?.[1];
    assert.ok(bundleSha256, "the runtime archive digest is pinned in the plugin");
    fs.writeFileSync(path.join(bundleRoot, "bundle-version.json"), JSON.stringify({
      pluginVersion: runtimeBundleVersion,
      serviceVersion: testServiceVersion,
      sha256: bundleSha256,
    }));
  }

  const queuedHealth = healthResponses.slice();
  let defaultHealth = null;
  const requests = [];
  const getRequests = [];
  const spawned = [];
  const children = [];
  const syncCalls = [];
  let inventoryResponseMode = "complete";
  let releaseInventoryResponse = null;
  let cancelResponseMode = "complete";
  let releaseCancelResponse = null;
  const http = {
    get(_options, callback) {
      getRequests.push(_options);
      const request = new EventEmitter();
      request.setTimeout = () => {};
      request.destroy = (error) => queueMicrotask(() => request.emit("error", error));
      queueMicrotask(() => {
        const health = queuedHealth.length ? queuedHealth.shift() : defaultHealth;
        if (!health) {
          request.emit("error", new Error("connection refused"));
          return;
        }
        const response = new EventEmitter();
        response.statusCode = 200;
        response.complete = true;
        response.setEncoding = () => {};
        callback(response);
        queueMicrotask(() => {
          if (String(_options?.path || "").startsWith("/eclipse/streams") && inventoryResponseMode === "pending") {
            releaseInventoryResponse = () => {
              response.emit("data", JSON.stringify({ complete: false, streams: [], attempts: [] }));
              response.emit("end");
            };
            return;
          }
          if (String(_options?.path || "").startsWith("/eclipse/streams") && inventoryResponseMode === "aborted") {
            response.complete = false;
            response.emit("data", "{\"partial\":");
            response.emit("aborted");
            return;
          }
          const payload = String(_options?.path || "").startsWith("/eclipse/streams")
            ? { complete: false, reason: "upstream_rate_limited", streams: [], attempts: [] }
            : health;
          response.emit("data", JSON.stringify(payload));
          response.emit("end");
        });
      });
      return request;
    },
    request(options, callback) {
      requests.push(options);
      const request = new EventEmitter();
      request.setTimeout = () => {};
      request.end = () => queueMicrotask(() => {
        if (String(options?.path || "") === "/eclipse/streams/cancel" && cancelResponseMode === "delayed") {
          releaseCancelResponse = () => {
            const response = new EventEmitter();
            response.statusCode = 200;
            response.resume = () => {};
            response.setEncoding = () => {};
            callback(response);
            response.emit("data", JSON.stringify({ canceled: true }));
            response.emit("end");
          };
          return;
        }
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        response.setEncoding = () => {};
        callback(response);
        response.emit("data", JSON.stringify({ canceled: false }));
        response.emit("end");
      });
      return request;
    },
  };
  const module = { exports: {} };
  const luna = { modules: {} };
  const sandbox = {
    module,
    luna,
    process: { pid: processPid, env: {} },
    URL,
    setTimeout: (callback, delay = 0) => delay >= 900
      ? globalThis.setTimeout(callback, delay)
      : queueMicrotask(callback),
    clearTimeout: (timer) => globalThis.clearTimeout(timer),
    console: { info() {}, error() {} },
    require(name) {
      if (name === "fs") return fs;
      if (name === "path") return path;
      if (name === "http") return http;
      if (name === "crypto") return { randomBytes: (size) => Buffer.alloc(size, 0x5a) };
      if (name === "child_process") return {
        spawn(...args) {
          spawned.push(args);
          const child = new EventEmitter();
          child.pid = 5678;
          child.exitCode = null;
          child.signalCode = null;
          child.killed = false;
          child.unref = () => {};
          child.kill = () => {
            child.killed = true;
            child.signalCode = "SIGTERM";
            child.emit("exit", null, "SIGTERM");
            return true;
          };
          children.push(child);
          return child;
        },
        spawnSync(...args) {
          syncCalls.push(args);
          const targetPid = Number(args[1]?.[1]);
          const target = children.find((candidate) => candidate.pid === targetPid);
          if (args[0].endsWith("taskkill.exe") && target) {
            target.kill();
            return { status: 0, error: null, stdout: "SUCCESS", stderr: "" };
          }
          throw new Error("unexpected bundle extraction in offline test");
        },
      };
      if (name === "electron") return {
        app: {
          getPath: () => userData,
          getAppMetrics: () => browserMetrics ?? (browserPid === null ? [] : [{ type: "Browser", pid: browserPid }]),
        },
      };
      throw new Error(`unexpected module ${name}`);
    },
  };
  vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });
  vm.runInContext(supervisorCode, sandbox, { filename: "service-supervisor.native.js" });
  const health = {
    status: "ok",
    version: testServiceVersion,
    runtimeBundleSha256,
    servicePid: 5678,
    serviceInstanceId: "test-instance-1",
    tidalOwnerPid: browserPid ?? processPid,
    tidalOwnerAlive: true,
    pluginRoot,
    dataRoot,
    mpvDirectory: paths.mpvDir,
    studioExecutable: paths.studioExe,
  };
  defaultHealth = health;
  return {
    paths,
    health,
    healthResponses: queuedHealth,
    setHealthResponses: (responses) => queuedHealth.splice(0, queuedHealth.length, ...responses),
    setDefaultHealth: (value) => { defaultHealth = value; },
    setInventoryResponseMode: (value) => { inventoryResponseMode = value; },
    releaseInventoryResponse: () => releaseInventoryResponse?.(),
    setCancelResponseMode: (value) => { cancelResponseMode = value; },
    releaseCancelResponse: () => releaseCancelResponse?.(),
    ageInventoryOperationStart: (operationId, ageMs) => {
      const operation = luna["__omniphony-ex.native-service-supervisor"]?.activeInventoryRequests?.get(operationId);
      if (operation) operation.createdAt = Date.now() - ageMs;
    },
    ageInventoryCancellation: (operationId, ageMs) => {
      const operation = luna["__omniphony-ex.native-service-supervisor"]?.activeInventoryRequests?.get(operationId);
      if (operation?.canceled) operation.canceledAt = Date.now() - ageMs;
    },
    setEnsureService: (fn) => { module.exports.ensureService = fn; },
    requests,
    getRequests,
    spawned,
    children,
    syncCalls,
    loseSupervisorToken: () => {
      luna["__omniphony-ex.native-service-supervisor"].shutdownToken = null;
    },
    restoreSupervisorToken: () => {
      luna["__omniphony-ex.native-service-supervisor"].shutdownToken = "5a".repeat(32);
    },
    reRegisterSupervisor: () => {
      const reloadedModule = { exports: {} };
      const reloadedSandbox = { ...sandbox, module: reloadedModule };
      vm.createContext(reloadedSandbox);
      vm.runInContext(supervisorCode, reloadedSandbox, { filename: "service-supervisor.native.js" });
      return {
        ensure: () => reloadedModule.exports.ensureService(testServiceVersion, {}),
        startEclipseLogin: (serviceBase) => reloadedModule.exports.startEclipseLogin(serviceBase, testServiceVersion, {}),
        requestEclipseStreamInventory: (serviceBase, input) => reloadedModule.exports.requestEclipseStreamInventory(serviceBase, testServiceVersion, {}, input),
        cancelEclipseStreamInventory: (serviceBase, input) => reloadedModule.exports.cancelEclipseStreamInventory(serviceBase, testServiceVersion, {}, input),
      };
    },
    cleanup: () => fs.rmSync(userData, { recursive: true, force: true }),
    ensure: () => module.exports.ensureService(testServiceVersion, {}),
    startEclipseLogin: (serviceBase) => module.exports.startEclipseLogin(serviceBase, testServiceVersion, {}),
    requestEclipseStreamInventory: (serviceBase, input) => module.exports.requestEclipseStreamInventory(serviceBase, testServiceVersion, {}, input),
    cancelEclipseStreamInventory: (serviceBase, input) => module.exports.cancelEclipseStreamInventory(serviceBase, testServiceVersion, {}, input),
  };
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    const result = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "renderer-supplied-invalid-provider", trackId: "track-1",
    });
    assert.equal(result.ok, false);
    assert.equal(result.error, "invalid_provider_key");
    const badOrigin = await harness.requestEclipseStreamInventory("http://localhost:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", trackId: "track-1",
    });
    assert.equal(badOrigin.error, "invalid_service_endpoint");
    assert.equal(harness.spawned.length, 0, "invalid requests are rejected before service startup");
    const inventory = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: "a".repeat(32), title: "A Song & Story", artist: "An Artist",
    });
    assert.equal(inventory.ok, true, JSON.stringify(inventory));
    assert.equal(inventory.inventory.reason, "upstream_rate_limited");
    const nativeRequest = harness.getRequests.find((request) => String(request?.path || "").startsWith("/eclipse/streams"));
    assert.ok(nativeRequest, "the inventory route is called by the native process");
    assert.match(nativeRequest.path, /title=A\+Song\+%26\+Story/);
    assert.match(nativeRequest.path, /operationId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
    assert.equal(nativeRequest.headers.Authorization, "Bearer " + "5a".repeat(32));
    assert.equal(nativeRequest.headers["X-Omniphony-Service-Instance"], "test-instance-1");
    const reusedOperationId = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: "a".repeat(32), trackId: "track-2",
    });
    assert.equal(reusedOperationId.ok, true,
      "a completed operation releases its owner record instead of remaining stuck as in progress");
    const shortBudget = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: "b".repeat(32), trackId: "track-1", timeoutMs: 1000,
    });
    assert.equal(shortBudget.ok, true, `an immediate service readiness can use a one-second inventory budget: ${JSON.stringify(shortBudget)}`);
    const shortBudgetRequest = harness.getRequests.filter((request) =>
      String(request?.path || "").startsWith("/eclipse/streams")
      && request.path.includes("timeoutMs="),
    ).at(-1);
    assert.ok(shortBudgetRequest);
    const requestedTimeoutMs = Number(new URLSearchParams(shortBudgetRequest.path.split("?")[1]).get("timeoutMs"));
    assert.ok(requestedTimeoutMs > 0 && requestedTimeoutMs <= 1000,
      "the native request passes its remaining deadline without rounding it upward");
    harness.setInventoryResponseMode("aborted");
    const interrupted = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: "c".repeat(32), trackId: "track-1",
    });
    assert.equal(interrupted.ok, false);
    assert.equal(interrupted.error, "inventory_response_aborted",
      "a partial response abort settles the native promise instead of hanging");
    const canceled = await harness.cancelEclipseStreamInventory("http://127.0.0.1:8765", { operationId: "d".repeat(32) });
    assert.equal(canceled.ok, true);
    assert.equal(canceled.canceled, false, "completed inventory operations are not falsely reported as canceled");
    const cancelRequest = harness.requests.find((request) => request.path === "/eclipse/streams/cancel");
    assert.ok(cancelRequest, "cancellation is relayed through a separately authenticated native request");
    assert.equal(cancelRequest.headers.Authorization, "Bearer " + "5a".repeat(32));
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  harness.setInventoryResponseMode("pending");
  harness.setCancelResponseMode("delayed");
  try {
    const operationId = "9".repeat(32);
    const inventoryPromise = harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "track-1",
    });
    await new Promise((resolve) => setImmediate(resolve));
    const cancelPromise = harness.cancelEclipseStreamInventory("http://127.0.0.1:8765", { operationId });
    await new Promise((resolve) => setImmediate(resolve));
    harness.ageInventoryOperationStart(operationId, 180000);
    harness.ageInventoryCancellation(operationId, 180000);
    const reused = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "track-2",
    });
    assert.equal(reused.ok, false);
    assert.equal(reused.error, "inventory_canceled",
      "a pending cancellation transport keeps its tombstone even beyond its normal retention window");
    harness.loseSupervisorToken();
    const duplicateCancel = await harness.cancelEclipseStreamInventory("http://127.0.0.1:8765", { operationId });
    assert.equal(duplicateCancel.ok, true);
    assert.equal(duplicateCancel.canceled, true);
    harness.restoreSupervisorToken();
    const stillOwned = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "track-3",
    });
    assert.equal(stillOwned.ok, false);
    assert.equal(stillOwned.error, "inventory_canceled",
      "a duplicate cancel without credentials cannot release another call's pending transport ownership");
    harness.releaseCancelResponse();
    const canceled = await cancelPromise;
    assert.equal(canceled.ok, true);
    assert.equal(canceled.canceled, true);
    assert.equal((await inventoryPromise).error, "inventory_canceled");
    harness.setInventoryResponseMode("complete");
    const afterExpiry = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "track-after-expiry",
    });
    assert.equal(afterExpiry.ok, true, JSON.stringify(afterExpiry));
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  harness.setEnsureService(() => new Promise((resolve) => globalThis.setTimeout(
    () => resolve({ ready: true }), 1100,
  )));
  try {
    const startedAt = Date.now();
    const result = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: "e".repeat(32), trackId: "track-1", timeoutMs: 1000,
    });
    assert.equal(result.ok, false);
    assert.equal(result.error, "inventory_deadline_exceeded");
    assert.ok(Date.now() - startedAt < 2000, "service readiness is included in the absolute request deadline");
    assert.equal(harness.getRequests.filter((request) => String(request?.path || "").startsWith("/eclipse/streams")).length, 0);
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  let releaseStartup;
  try {
    await harness.ensure();
    harness.loseSupervisorToken();
    harness.setEnsureService(() => new Promise((resolve) => { releaseStartup = resolve; }));
    const operationId = "f".repeat(32);
    const inventoryPromise = harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "track-1",
    });
    await Promise.resolve();
    const canceled = await harness.cancelEclipseStreamInventory("http://127.0.0.1:8765", { operationId });
    assert.equal(canceled.ok, true, "a request canceled during ensureService needs no service credentials");
    assert.equal(canceled.canceled, true);
    const canceledInventory = await inventoryPromise;
    assert.equal(canceledInventory.ok, false);
    assert.equal(canceledInventory.error, "inventory_canceled");
    const reusedCanceledId = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId, trackId: "stale-track",
    });
    assert.equal(reusedCanceledId.ok, false);
    assert.equal(reusedCanceledId.error, "inventory_canceled",
      "a canceled startup operation ID remains reserved and cannot be revived immediately");
    releaseStartup({ ready: true });
    assert.equal(harness.getRequests.filter((request) =>
      String(request?.path || "").startsWith("/eclipse/streams"),
    ).length, 0, "a canceled startup never issues the stream inventory GET");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  let releaseFirstStartup;
  let startupCalls = 0;
  try {
    await harness.ensure();
    harness.setEnsureService(() => {
      startupCalls++;
      return startupCalls === 1
        ? new Promise((resolve) => { releaseFirstStartup = resolve; })
        : Promise.resolve({ ready: true });
    });
    const firstId = "1".repeat(32);
    const secondId = "2".repeat(32);
    const firstPromise = harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: firstId, trackId: "first-track",
    });
    await Promise.resolve();
    const second = await harness.requestEclipseStreamInventory("http://127.0.0.1:8765", {
      providerKey: "eclipse:0123456789abcdefabcd", operationId: secondId, trackId: "second-track",
    });
    assert.equal(second.ok, true, JSON.stringify(second));
    const canceledFirst = await harness.cancelEclipseStreamInventory("http://127.0.0.1:8765", { operationId: firstId });
    assert.equal(canceledFirst.ok, true, "canceling the stale startup affects only its own operation ID");
    assert.equal(canceledFirst.canceled, true);
    releaseFirstStartup({ ready: true });
    const canceledFirstInventory = await firstPromise;
    assert.equal(canceledFirstInventory.ok, false);
    assert.equal(canceledFirstInventory.error, "inventory_canceled");
    const inventoryPaths = harness.getRequests
      .map((request) => String(request?.path || ""))
      .filter((requestPath) => requestPath.startsWith("/eclipse/streams?"));
    assert.equal(inventoryPaths.length, 1, "only the restarted current operation reaches the service");
    assert.match(inventoryPaths[0], /operationId=22222222222222222222222222222222/);
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    const ready = await harness.ensure();
    assert.equal(ready.ready, true);
    const result = await harness.startEclipseLogin("https://127.0.0.1:8765/");
    assert.equal(result.error, "invalid_service_origin",
      "the supervisor passes its live service identity into the auth module before URL validation");
    const reloaded = harness.reRegisterSupervisor();
    assert.equal((await reloaded.ensure()).ready, true,
      "native module re-registration shares the live service owner state through Luna's process-wide object");
    assert.equal((await reloaded.startEclipseLogin("https://127.0.0.1:8765/")).error, "invalid_service_origin");
    assert.equal(harness.spawned.length, 1, "re-registration reuses the exact owned service rather than duplicating it");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    const result = await harness.startEclipseLogin("https://127.0.0.1:8765/");
    assert.equal(result.error, "invalid_service_origin");
    assert.equal(harness.spawned.length, 1,
      "the sign-in IPC call starts its own service before attempting authentication");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null], processPid: 4321, browserPid: null });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "tidal_owner_pid_unavailable");
    assert.equal(harness.spawned.length, 0, "no service starts without an unambiguous Electron Browser PID");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({
    healthResponses: [null],
    processPid: 1010,
    browserPid: null,
    browserMetrics: [
      { type: "Browser", pid: 4321 },
      { type: "Browser", pid: 4322 },
    ],
  });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "tidal_owner_pid_unavailable");
    assert.equal(harness.spawned.length, 0, "ambiguous Browser metrics are rejected");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  const unrelatedHealthyService = { ...harness.health, servicePid: 9999 };
  harness.setHealthResponses([null, unrelatedHealthyService]);
  harness.setDefaultHealth(null);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "spawned_service_identity_mismatch");
    assert.equal(result.cleanupSucceeded, true, "the supervisor reaps its exact child after rejecting unrelated health");
    assert.equal(harness.spawned.length, 1, "the supervisor rejects health from a different Python process");
    assert.equal(harness.requests.length, 0, "the unrelated healthy port owner is never sent shutdown");
    assert.deepEqual(
      Array.from(harness.syncCalls[0][1]),
      ["/PID", "5678", "/T", "/F"],
      "failed startup cleanup targets only the spawned child's process tree",
    );
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  harness.setDefaultHealth(null);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "health_timeout");
    assert.equal(result.cleanupSucceeded, true, "the service child tree is reaped when health never comes up");
    assert.equal(harness.children[0].killed, true);
    assert.deepEqual(Array.from(harness.syncCalls[0][1]), ["/PID", "5678", "/T", "/F"]);
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null], processPid: 1010, browserPid: 4321 });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true);
    assert.equal(harness.spawned[0][2].env.OMNIFUSION_TIDAL_PID, "4321");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    assert.equal((await harness.ensure()).ready, true);
    harness.loseSupervisorToken();
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "service_identity_unavailable");
    assert.equal(harness.requests.length, 0, "a lost token cannot be replaced with a guessed or new credential");
    assert.equal(harness.spawned.length, 1, "the live same-owner service is not duplicated");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    assert.equal((await harness.ensure()).ready, true);
    const oldService = { ...harness.health, version: "2.1.34" };
    harness.setHealthResponses([oldService, null, harness.health]);
    const result = await harness.ensure();
    assert.equal(result.ready, true, "an older service build is replaced by the required build");
    assert.equal(result.version, testServiceVersion);
    assert.equal(harness.requests.length, 1, "the old same-owner service receives one authenticated shutdown");
    assert.equal(harness.spawned.length, 2, "the old service is never reused as the new build");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    const results = await Promise.all([harness.ensure(), harness.ensure()]);
    assert.deepEqual(results.map((result) => result.ready), [true, true], JSON.stringify(results));
    assert.equal(harness.spawned.length, 1, "concurrent startup calls spawn one service");
    const options = harness.spawned[0][2];
    assert.equal(options.env.OMNIFUSION_MPV_DIR, harness.paths.mpvDir);
    assert.equal(options.env.OMNIFUSION_STUDIO_EXE, harness.paths.studioExe);
    assert.equal(options.env.OMNIFUSION_PLUGIN_DIR, harness.health.pluginRoot);
    assert.equal(options.env.OMNIFUSION_TIDAL_PID, "4321");
    assert.equal(options.env.OMNIFUSION_RUNTIME_SHA256, runtimeBundleSha256);
    assert.equal(options.env.OMNIFUSION_SHUTDOWN_TOKEN, "5a".repeat(32));
    assert.deepEqual(
      JSON.parse(fs.readFileSync(path.join(harness.health.dataRoot, "config", "runtime-paths.json"), "utf8")),
      harness.paths,
      "the selected paths remain persisted in plugin user data",
    );
    assert.equal(
      fs.existsSync(path.join(path.dirname(harness.paths.studioExe), "orender.exe")),
      false,
      "the in-process orender.dll route does not require a separate renderer executable",
    );
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  try {
    assert.equal((await harness.ensure()).ready, true);
    const staleRuntime = { ...harness.health, runtimeBundleSha256: "A".repeat(64) };
    harness.setHealthResponses([staleRuntime, null, harness.health]);
    const result = await harness.ensure();
    assert.equal(result.ready, true, "a same-version service from stale runtime bytes is replaced");
    assert.equal(harness.requests.length, 1, "the stale same-owner service receives authenticated shutdown");
    assert.equal(harness.spawned.length, 2, "the stale runtime is never reused");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [] });
  const previousOwner = { ...harness.health, tidalOwnerPid: 1234, tidalOwnerAlive: false };
  harness.setHealthResponses([previousOwner, null]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true, "a service owned by a previous TIDAL process is replaced");
    assert.equal(harness.requests.length, 0, "a different owner is never shut down by the new TIDAL process");
    assert.equal(harness.spawned.length, 1, "the new TIDAL owner starts one service");
    assert.equal(harness.spawned[0][2].env.OMNIFUSION_TIDAL_PID, "4321");
  } finally {
    harness.cleanup();
  }
}

{
  const missingMpv = path.join(os.tmpdir(), "omniphony-missing-mpv-test");
  const harness = makeHarness({ healthResponses: [null], mpvDir: missingMpv, createRuntimeFiles: false });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "runtime_paths_invalid");
    assert.equal(harness.spawned.length, 0, "invalid paths never launch a broken service");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({
    healthResponses: [null],
    localServiceVersion: "2.1.27",
    verifiedBundle: true,
  });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true, "a matching verified bundle replaces stale local development API files");
    assert.equal(
      harness.spawned[0][1][0],
      path.join(harness.health.pluginRoot, "runtime-bundles", runtimeBundleVersion, "api", "omnifusion_service.py"),
      "stale checked-out service code is never spawned over the required release bundle",
    );
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({
    healthResponses: [null],
    localServiceVersion: testServiceVersion,
    verifiedBundle: true,
  });
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true);
    assert.equal(
      harness.spawned[0][1][0],
      path.join(harness.health.pluginRoot, "runtime-bundles", runtimeBundleVersion, "api", "omnifusion_service.py"),
      "a verified packaged runtime takes priority over a same-version dirty development API checkout",
    );
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [null] });
  await harness.ensure();
  const wrongPaths = {
    ...harness.health,
    mpvDirectory: path.join(os.tmpdir(), "old-mpv-path"),
    studioExecutable: path.join(os.tmpdir(), "old-studio.exe"),
  };
  harness.setHealthResponses([wrongPaths, null, harness.health]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true, `a stale same-plugin service is replaced with the configured paths: ${JSON.stringify(result)}`);
    assert.equal(harness.requests.length, 1);
    assert.equal(harness.requests[0].path, "/shutdown");
    assert.equal(harness.requests[0].headers.Authorization, "Bearer " + "5a".repeat(32));
    assert.equal(harness.requests[0].headers["X-Omniphony-Service-Instance"], "test-instance-1");
    assert.equal(harness.spawned.length, 2, "the initial and replacement service are each spawned once");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [] });
  const anotherLiveOwner = { ...harness.health, tidalOwnerPid: 9999, tidalOwnerAlive: true };
  harness.setHealthResponses([anotherLiveOwner]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "service_owned_by_another_live_tidal");
    assert.equal(harness.requests.length, 0, "a live competing TIDAL owner is never stopped");
    assert.equal(harness.spawned.length, 0, "the single service port is not raced by a second instance");
  } finally {
    harness.cleanup();
  }
}

{
  const harness = makeHarness({ healthResponses: [] });
  const legacyService = { ...harness.health };
  delete legacyService.tidalOwnerPid;
  delete legacyService.tidalOwnerAlive;
  harness.setHealthResponses([legacyService]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "legacy_service_owner_unverifiable");
    assert.equal(harness.requests.length, 0, "legacy services never receive unauthenticated shutdown");
    assert.equal(harness.spawned.length, 0, "the fixed port is not raced against a legacy service");
  } finally {
    harness.cleanup();
  }
}

console.log("Runtime-path persistence, validation, concurrency, and restart checks passed.");
