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
const supervisorCode = moduleSource.match(
  /const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
assert.ok(supervisorCode, "the native service supervisor is embedded in the plugin entrypoint");
assert.doesNotMatch(
  legacyBootstrapSource,
  /path\.join\(path\.dirname\(studioExe\),\s*['"]orender\.exe['"]\)/,
  "in-process orender.dll playback must not require the standalone renderer executable",
);

function touch(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "test fixture");
}

function makeHarness({
  healthResponses,
  mpvDir,
  studioExe,
  createRuntimeFiles = true,
  localServiceVersion = "2.1.31",
  verifiedBundle = false,
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
    const bundleRoot = path.join(pluginRoot, "runtime-bundles", "2.1.45");
    const bundledApiRoot = path.join(bundleRoot, "api");
    for (const file of [
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
    ]) touch(path.join(bundledApiRoot, file));
    fs.writeFileSync(path.join(bundledApiRoot, "omnifusion_service.py"), 'SERVICE_VERSION = "2.1.31"\n');
    const bundleSha256 = moduleSource.match(/const bundleSha256 = "([A-F0-9]+)";/)?.[1];
    assert.ok(bundleSha256, "the runtime archive digest is pinned in the plugin");
    fs.writeFileSync(path.join(bundleRoot, "bundle-version.json"), JSON.stringify({
      pluginVersion: "2.1.45",
      serviceVersion: "2.1.31",
      sha256: bundleSha256,
    }));
  }

  const queuedHealth = healthResponses.slice();
  let defaultHealth = null;
  const requests = [];
  const spawned = [];
  const http = {
    get(_options, callback) {
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
        response.setEncoding = () => {};
        callback(response);
        queueMicrotask(() => {
          response.emit("data", JSON.stringify(health));
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
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        callback(response);
        response.emit("end");
      });
      return request;
    },
  };
  const module = { exports: {} };
  const sandbox = {
    module,
    process: { pid: 4321, env: {} },
    URL,
    setTimeout: (callback) => queueMicrotask(callback),
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
          return child;
        },
        spawnSync() { throw new Error("unexpected bundle extraction in offline test"); },
      };
      if (name === "electron") return { app: { getPath: () => userData } };
      throw new Error(`unexpected module ${name}`);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(supervisorCode, sandbox, { filename: "service-supervisor.native.js" });
  const health = {
    status: "ok",
    version: "2.1.31",
    servicePid: 5678,
    serviceInstanceId: "test-instance-1",
    tidalOwnerPid: 4321,
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
    requests,
    spawned,
    loseSupervisorToken: () => {
      vm.runInContext('globalThis[Symbol.for("omniphony-ex.native-service-supervisor")].shutdownToken = null', sandbox);
    },
    cleanup: () => fs.rmSync(userData, { recursive: true, force: true }),
    ensure: () => module.exports.ensureService("2.1.31", {}),
  };
}

{
  const harness = makeHarness({ healthResponses: [null] });
  const unrelatedHealthyService = { ...harness.health, servicePid: 9999 };
  harness.setHealthResponses([null, unrelatedHealthyService]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, false);
    assert.equal(result.reason, "spawned_service_identity_mismatch");
    assert.equal(harness.spawned.length, 1, "the supervisor rejects health from a different Python process");
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
    const results = await Promise.all([harness.ensure(), harness.ensure()]);
    assert.deepEqual(results.map((result) => result.ready), [true, true], JSON.stringify(results));
    assert.equal(harness.spawned.length, 1, "concurrent startup calls spawn one service");
    const options = harness.spawned[0][2];
    assert.equal(options.env.OMNIFUSION_MPV_DIR, harness.paths.mpvDir);
    assert.equal(options.env.OMNIFUSION_STUDIO_EXE, harness.paths.studioExe);
    assert.equal(options.env.OMNIFUSION_PLUGIN_DIR, harness.health.pluginRoot);
    assert.equal(options.env.OMNIFUSION_TIDAL_PID, "4321");
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
    assert.match(
      harness.spawned[0][1][0],
      /runtime-bundles[\\/]2\.1\.45[\\/]api[\\/]omnifusion_service\.py$/,
      "stale checked-out service code is never spawned over the required release bundle",
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
