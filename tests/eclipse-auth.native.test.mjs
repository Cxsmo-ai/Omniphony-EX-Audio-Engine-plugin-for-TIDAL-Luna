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
        nativeRequests.push({ options, body: JSON.parse(body) });
        const response = new EventEmitter();
        response.statusCode = 200;
        queueMicrotask(() => {
          request.emit("response", response);
          response.emit("data", Buffer.from('{"connected":true}'));
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
    if (name === "crypto") return { randomBytes: () => Buffer.alloc(12, 7) };
    throw new Error(`unexpected module: ${name}`);
  },
  URL,
  Buffer,
  setImmediate,
  globalThis,
});

const handler = exported.exports;
const started = await handler.startLogin("http://127.0.0.1:8765");
assert.equal(started.started, true);
assert.equal(started.existing, false);
assert.equal(lastWindow.url, "https://eclipsemusic.app/sign-in");
assert.equal(lastWindow.options.webPreferences.nodeIntegration, false);
assert.match(lastWindow.options.webPreferences.partition, /^temp:omniphony-eclipse-auth-/);
const reused = await handler.startLogin("http://127.0.0.1:8765");
assert.equal(reused.started, true);
assert.equal(reused.existing, true);

let prevented = false;
lastWindow.webContents.emit("will-redirect", { preventDefault() { prevented = true; } },
  "https://eclipsemusic.app/sign-in?token=secret-test-eclipse-token");
assert.equal(prevented, true, "the first-party callback token is stopped before page navigation");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(nativeRequests.length, 1);
assert.equal(nativeRequests[0].options.hostname, "127.0.0.1");
assert.equal(nativeRequests[0].options.path, "/eclipse-auth/complete");
assert.equal(nativeRequests[0].options.headers.Origin, "http://127.0.0.1:8765");
assert.deepEqual(nativeRequests[0].body, { token: "secret-test-eclipse-token" });
assert.equal(lastWindow.destroyed, true, "the ephemeral login window closes after the token is verified");
assert.equal(lastWindow.webContents.sessionCleared, true, "the temporary browser session is cleared after login");

const rejectedEndpoint = await handler.startLogin("https://example.invalid");
assert.equal(rejectedEndpoint.started, false);
assert.equal(rejectedEndpoint.error, "service_must_be_loopback");
console.log("Eclipse Discord login uses an isolated browser session and sends only the callback token to the loopback verifier.");
