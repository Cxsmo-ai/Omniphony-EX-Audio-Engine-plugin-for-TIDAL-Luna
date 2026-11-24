import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const moduleCode = source.match(/const NATIVE_ECLIPSE_AUTH_MODULE_CODE = String\.raw`([\s\S]*?)`;/)?.[1];
assert.ok(moduleCode, "the Discord OAuth browser flow is embedded in the plugin native module");
assert.doesNotMatch(moduleCode, /localStorage|cookies|console\.(?:log|info|warn|error)/i,
  "Eclipse sign-in must not scrape web storage or log account tokens");
assert.match(moduleCode, /contextIsolation:\s*true/);
assert.match(moduleCode, /nodeIntegration:\s*false/);
assert.match(moduleCode, /sandbox:\s*true/);

const nativeRequests = [];
let lastWindow;
let nextAuthResponse = { statusCode: 200, body: '{"connected":true}' };
class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.session = { clearStorageData: async () => { this.sessionCleared = true; } };
  }
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; }
}
class FakeBrowserWindow extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    lastWindow = this;
  }
  setMenuBarVisibility() {}
  async loadURL(url) { this.url = url; }
  show() { this.shown = true; }
  focus() { this.focused = true; }
  isDestroyed() { return this.destroyed; }
  destroy() { this.destroyed = true; this.emit("closed"); }
  setTitle(value) { this.title = value; }
}
const electron = {
  BrowserWindow: FakeBrowserWindow,
  net: {
    request(options) {
      const request = new EventEmitter();
      let body = "";
      request.write = (value) => { body += value; };
      request.end = () => {
        const parsedBody = JSON.parse(body);
        nativeRequests.push({ options, body: parsedBody });
        const response = new EventEmitter();
        response.statusCode = options.path === "/eclipse-auth/flow-status"
          ? 200 : nextAuthResponse.statusCode;
        const responseBody = options.path === "/eclipse-auth/flow-status"
          ? '{"recorded":true}' : nextAuthResponse.body;
        queueMicrotask(() => {
          request.emit("response", response);
          response.emit("data", Buffer.from(responseBody));
          response.emit("end");
        });
      };
      return request;
    },
  },
};
const exported = { exports: {} };
vm.runInNewContext(moduleCode, {
  module: exported,
  require(name) {
    if (name === "electron") return electron;
    if (name === "crypto") {
      let fill = 7;
      return { randomBytes: (size) => Buffer.alloc(size, fill++) };
    }
    throw new Error(`unexpected module: ${name}`);
  },
  URL,
  Buffer,
  setImmediate,
  setTimeout,
  clearTimeout,
  globalThis,
});

const handler = exported.exports;
const started = await handler.startLogin("http://127.0.0.1:8765");
assert.equal(started.started, true);
assert.equal(started.existing, false);
assert.equal(lastWindow.url, "https://eclipsemusic.app/sign-in");
assert.equal(lastWindow.options.webPreferences.nodeIntegration, false);
assert.match(lastWindow.options.webPreferences.partition, /^temp:omniphony-eclipse-auth-/);
assert.equal(nativeRequests.length, 1);
assert.equal(nativeRequests[0].options.path, "/eclipse-auth/flow-status");
assert.equal(nativeRequests[0].body.reason, "started");
assert.match(nativeRequests[0].body.attemptId, /^[a-f0-9]{32}$/);
const reused = await handler.startLogin("http://127.0.0.1:8765");
assert.equal(reused.started, true);
assert.equal(reused.existing, true);

let prevented = false;
lastWindow.webContents.emit("will-redirect", { preventDefault() { prevented = true; } },
  "https://eclipsemusic.app/sign-in?token=secret-test-eclipse-token");
assert.equal(prevented, true, "the first-party callback token is stopped before page navigation");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(nativeRequests.length, 2);
assert.equal(nativeRequests[1].options.hostname, "127.0.0.1");
assert.equal(nativeRequests[1].options.path, "/eclipse-auth/complete");
assert.equal(nativeRequests[1].options.headers.Origin, "http://127.0.0.1:8765");
assert.equal(nativeRequests[1].body.token, "secret-test-eclipse-token");
assert.equal(nativeRequests[1].body.attemptId, nativeRequests[0].body.attemptId);
assert.equal(lastWindow.destroyed, true, "the ephemeral login window closes after the token is verified");
assert.equal(lastWindow.webContents.sessionCleared, true, "the temporary browser session is cleared after login");

nextAuthResponse = { statusCode: 401, body: '{"connected":false,"error":"unauthorized"}' };
await handler.startLogin("http://127.0.0.1:8765");
const failedWindow = lastWindow;
failedWindow.webContents.emit("will-redirect", { preventDefault() {} },
  "https://eclipsemusic.app/sign-in?token=another-test-token");
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
assert.equal(failedWindow.destroyed, false, "the failed auth window remains available for diagnosis");
assert.equal(failedWindow.title, "Eclipse sign-in failed: unauthorized", "the safe server error is visible instead of being erased by a login reload");
assert.equal(failedWindow.url, "https://eclipsemusic.app/sign-in", "a failed verification does not silently reload the sign-in page");
assert.equal(nativeRequests[3].body.token, "another-test-token");
assert.match(nativeRequests[3].body.attemptId, /^[a-f0-9]{32}$/);
await handler.closeLogin();

await handler.startLogin("http://127.0.0.1:8765");
const callbackWindow = lastWindow;
let blockedHttpNavigation = false;
callbackWindow.webContents.emit("will-redirect", { preventDefault() { blockedHttpNavigation = true; } },
  "http://api.eclipsemusic.app/api/auth/mobile/discord/callback?state=web", false, true);
assert.equal(blockedHttpNavigation, true, "non-HTTPS Eclipse navigation remains blocked");
callbackWindow.webContents.emit("will-redirect", { preventDefault() {} },
  "https://api.eclipsemusic.app/api/auth/mobile/discord/callback?state=web", false, false);
callbackWindow.webContents.emit("did-navigate-in-page", {}, "https://eclipsemusic.app/sign-in", false);
assert.equal(nativeRequests.length, 5, "blocked and subframe navigations cannot change login status");
callbackWindow.webContents.emit("will-redirect", { preventDefault() {} },
  "https://api.eclipsemusic.app/api/auth/mobile/discord/callback?state=web&code=authorization-code");
callbackWindow.webContents.emit("did-navigate", {}, "https://eclipsemusic.app/sign-in");
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
assert.equal(nativeRequests.length, 6, "a callback that returns to sign-in without a token is reported to the service");
assert.equal(nativeRequests[5].options.path, "/eclipse-auth/flow-status");
assert.equal(nativeRequests[5].body.reason, "callback_missing");
assert.equal(nativeRequests[5].body.attemptId, nativeRequests[4].body.attemptId);
assert.equal(callbackWindow.title, "Eclipse returned without an account token");
assert.equal(callbackWindow.destroyed, false, "the login window remains available for diagnosis");
const firstAttemptId = nativeRequests[4].body.attemptId;
callbackWindow.webContents.emit("will-navigate", { preventDefault() {} },
  "https://api.eclipsemusic.app/configure/auth/discord");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(nativeRequests[6].body.reason, "started", "retry starts a new correlated auth attempt in the same window");
assert.notEqual(nativeRequests[6].body.attemptId, firstAttemptId);
callbackWindow.webContents.emit("will-redirect", { preventDefault() {} },
  "https://api.eclipsemusic.app/api/auth/mobile/discord/callback?state=web&code=retry-code");
callbackWindow.webContents.emit("did-navigate", {}, "https://eclipsemusic.app/sign-in");
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setImmediate(resolve));
assert.equal(nativeRequests[7].body.reason, "callback_missing");
assert.equal(nativeRequests[7].body.attemptId, nativeRequests[6].body.attemptId);
await handler.closeLogin();

const rejectedEndpoint = await handler.startLogin("https://example.invalid");
assert.equal(rejectedEndpoint.started, false);
assert.equal(rejectedEndpoint.error, "service_must_be_loopback");
console.log("Eclipse Discord login uses an isolated browser session, preserves safe verification errors, and never logs callback tokens.");
