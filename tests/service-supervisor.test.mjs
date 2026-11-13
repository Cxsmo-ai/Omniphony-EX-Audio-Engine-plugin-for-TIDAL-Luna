import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const moduleSource = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const supervisorCode = moduleSource.match(
  /const NATIVE_SERVICE_SUPERVISOR_MODULE_CODE = String\.raw`([\s\S]*?)`;/,
)?.[1];
assert.ok(supervisorCode, "the native service supervisor is embedded in the plugin entrypoint");

function touch(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "test fixture");
}

function makeHarness({ healthResponses, mpvDir, studioExe, createRuntimeFiles = true }) {
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
      if (name === "child_process") return {
        spawn(...args) {
          spawned.push(args);
          const child = new EventEmitter();
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
    version: "2.1.27",
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
    cleanup: () => fs.rmSync(userData, { recursive: true, force: true }),
    ensure: () => module.exports.ensureService("2.1.27", {}),
  };
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
  const harness = makeHarness({ healthResponses: [] });
  const wrongPaths = {
    ...harness.health,
    mpvDirectory: path.join(os.tmpdir(), "old-mpv-path"),
    studioExecutable: path.join(os.tmpdir(), "old-studio.exe"),
  };
  harness.setHealthResponses([wrongPaths, null]);
  try {
    const result = await harness.ensure();
    assert.equal(result.ready, true, "a stale same-plugin service is replaced with the configured paths");
    assert.equal(harness.requests.length, 1);
    assert.equal(harness.requests[0].path, "/shutdown");
    assert.equal(harness.requests[0].headers.Origin, "http://127.0.0.1:8765");
    assert.equal(harness.spawned.length, 1);
  } finally {
    harness.cleanup();
  }
}

console.log("Runtime-path persistence, validation, concurrency, and restart checks passed.");
