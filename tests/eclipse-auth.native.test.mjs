import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const moduleCode = source.match(/const NATIVE_ECLIPSE_AUTH_MODULE_CODE = String\.raw`([\s\S]*?)`;/)?.[1];
assert.ok(moduleCode, "the native auth module is represented in the plugin source");
assert.equal(source.includes("const ECLIPSE_ACCOUNT_LINKING_SUPPORTED = true;"), true);
assert.doesNotMatch(moduleCode, /setAsDefaultProtocolClient|console\.(?:log|info|debug)|localStorage|BrowserView/,
  "the APK callback is handled in a private window without changing OS protocol ownership or logging credentials");

const requests = [];
const windows = [];
class FakeWindow extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.destroyed = false;
    this.webContents = new EventEmitter();
    this.webContents.getURL = () => this.url;
    this.webContents.setWindowOpenHandler = (handler) => { this.openHandler = handler; };
    windows.push(this);
  }
  loadURL(url) { this.url = url; return Promise.resolve(); }
  getURL() { return this.url; }
  close() { if (!this.destroyed) { this.destroyed = true; this.emit("closed"); } }
  isDestroyed() { return this.destroyed; }
}

const http = {
  request(options, callback) {
    const request = new EventEmitter();
    request.setTimeout = () => {};
    request.end = (buffer) => {
      const body = JSON.parse(buffer.toString("utf8"));
      requests.push({ path: options.path, body, headers: options.headers });
      const response = new EventEmitter();
      response.statusCode = 200;
      response.setEncoding = () => {};
      callback(response);
      queueMicrotask(() => {
        response.emit("data", JSON.stringify(options.path.endsWith("/flow-status")
          ? { recorded: true }
          : { connected: true, user: { id: "account-id" } }));
        response.emit("end");
      });
    };
    request.destroy = (error) => request.emit("error", error);
    return request;
  },
};
const electron = {
  BrowserWindow: FakeWindow,
  session: {
    fromPartition(partition, options) {
      assert.match(partition, /^temp:omniphony-eclipse-[a-f0-9]{32}$/);
      assert.equal(options.cache, false);
      return { clearStorageData: async () => {}, clearCache: async () => {} };
    },
  },
};
const sandbox = {
  module: { exports: {} },
  require(name) {
    if (name === "crypto") return awaitlessCrypto;
    if (name === "http") return http;
    if (name === "electron") return electron;
    throw new Error(`unexpected require: ${name}`);
  },
  Buffer,
  URL,
  setTimeout,
  clearTimeout,
};
const serviceIdentity = { shutdownToken: "a".repeat(64), serviceInstanceId: "service-instance-test" };
import crypto from "node:crypto";
const awaitlessCrypto = crypto;
vm.runInNewContext(moduleCode, sandbox, { codeGeneration: { strings: false, wasm: false } });
const handler = sandbox.module.exports;

const invalidOrigin = await handler.startLogin("http://example.com:8765");
assert.deepEqual(JSON.parse(JSON.stringify(invalidOrigin)), { started: false, error: "invalid_service_origin" });
assert.equal(requests.length, 0, "invalid local service origins do not receive credentials");

const unavailableIdentity = await handler.startLogin("http://127.0.0.1:8765", {
  shutdownToken: null,
  serviceInstanceId: "service-instance-test",
});
assert.deepEqual(JSON.parse(JSON.stringify(unavailableIdentity)), { started: false, error: "service_identity_unavailable" });
assert.equal(requests.length, 0, "auth credentials are never posted without the current service identity");
const start = await handler.startLogin("http://127.0.0.1:8765/", serviceIdentity);
assert.equal(start.started, true);
assert.equal(windows.length, 1);
assert.equal(windows[0].url, "https://api.eclipsemusic.app/api/auth/mobile/discord");
assert.equal(windows[0].options.webPreferences.nodeIntegration, false);
assert.equal(windows[0].options.webPreferences.contextIsolation, true);
assert.equal(windows[0].options.webPreferences.sandbox, true);
assert.equal(requests[0].path, "/eclipse-auth/flow-status");
assert.equal(requests[0].body.reason, "started");
assert.match(requests[0].body.attemptId, /^[a-f0-9]{32}$/);
assert.equal(requests[0].headers.Authorization, `Bearer ${"a".repeat(64)}`);
assert.equal(requests[0].headers["X-Omniphony-Service-Instance"], "service-instance-test");

const callback = "debridmusic://auth/callback?token=apk-flow-token&userId=account-id&email=user%40example.com";
windows[0].url = "https://discord.com/oauth2/authorize";
let subframePrevented = false;
windows[0].webContents.emit("will-navigate", { preventDefault() { subframePrevented = true; } }, callback, false, false);
assert.equal(subframePrevented, true, "a custom-scheme callback in a subframe is blocked");
assert.equal(requests.some((request) => request.path === "/eclipse-auth/complete"), false);
let prevented = false;
windows[0].webContents.emit("will-navigate", { preventDefault() { prevented = true; } }, callback, false, true);
assert.equal(prevented, true, "the APK callback is consumed before the OS protocol handler can receive it");
for (let i = 0; i < 20 && sandbox.__omniphonyEclipseLogin; i++) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

const completion = requests.find((request) => request.path === "/eclipse-auth/complete");
assert.ok(completion, "the callback token is sent only to the local verification service");
assert.equal(completion.body.token, "apk-flow-token");
assert.equal(completion.body.userId, "account-id");
assert.equal(completion.body.email, "user@example.com");
assert.equal(completion.body.attemptId, requests[0].body.attemptId);
assert.equal(completion.headers.Authorization, requests[0].headers.Authorization);
assert.equal(completion.headers["X-Omniphony-Service-Instance"], requests[0].headers["X-Omniphony-Service-Instance"]);
assert.equal(JSON.stringify(start).includes("apk-flow-token"), false, "the renderer never receives the callback token");
assert.equal(windows[0].destroyed, true);
assert.equal(await handler.closeLogin().then((result) => result.closed), true);

console.log("Eclipse APK Discord callback is intercepted, service-authenticated, locally verified and never exposed to the renderer.");
