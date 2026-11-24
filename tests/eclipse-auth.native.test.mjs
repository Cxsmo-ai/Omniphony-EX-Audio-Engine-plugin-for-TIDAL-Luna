import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");
const moduleCode = source.match(/const NATIVE_ECLIPSE_AUTH_MODULE_CODE = String\.raw`([\s\S]*?)`;/)?.[1];

assert.ok(moduleCode, "the native auth module remains explicitly represented");
assert.match(moduleCode, /unsupported_account_integration/);
assert.doesNotMatch(moduleCode, /BrowserWindow|net\.request|Authorization|token/i,
  "disabled account linking cannot open a login window or transmit a credential");

const sandbox = { module: { exports: {} } };
vm.runInNewContext(moduleCode, sandbox);
const handler = sandbox.module.exports;
const loginResult = await handler.startLogin("http://127.0.0.1:8765");
assert.equal(loginResult.started, false);
assert.equal(loginResult.error, "unsupported_account_integration");
assert.equal((await handler.closeLogin()).closed, true);
assert.equal(source.includes('const ECLIPSE_ACCOUNT_LINKING_SUPPORTED = false;'), true);
assert.match(source, /Eclipse has not documented a supported third-party account sign-in flow/);

console.log("Undocumented Eclipse account linking is disabled in the UI, native module, and service.");
