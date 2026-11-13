import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, "..", "omniphony.mjs"), "utf8");

function section(start, end) {
  const startIndex = source.indexOf(start);
  assert.notEqual(startIndex, -1, `expected source marker: ${start}`);
  const endIndex = source.indexOf(end, startIndex + start.length);
  assert.notEqual(endIndex, -1, `expected source marker: ${end}`);
  return source.slice(startIndex, endIndex);
}

const searchRendering = section("let searchDebounceTimer = null;", "const pathsDivider = document.createElement(\"div\")");
assert.doesNotMatch(searchRendering, /\.innerHTML\s*=/, "provider metadata is never interpolated as HTML");
assert.match(searchRendering, /title\.textContent\s*=\s*String\(t\.title/);
assert.match(searchRendering, /detailsLine\.textContent\s*=/);
assert.match(searchRendering, /parsed\.protocol === \"https:\" \|\| parsed\.protocol === \"http:\"/);

const addonRendering = section("const setAddonStatus = (message, color", "let authPollTimer = null;");
assert.doesNotMatch(addonRendering, /\.innerHTML\s*=/, "untrusted Eclipse manifest fields and URLs are rendered as text");
assert.match(addonRendering, /statusText\.textContent\s*=\s*String\(message/);

const authRendering = section("const updateAuthStatus = async () => {", "authBtn.onclick = async () => {");
assert.doesNotMatch(authRendering, /\.innerHTML\s*=/, "device-login response values are rendered as text");
assert.match(authRendering, /code\.textContent\s*=\s*String\(data\.userCode\)/);

const manifestUrlCode = section("function parseEclipseManifestUrl(value) {", "const NATIVE_PLAYER_GUARD_MODULE_ID =");
const parseEclipseManifestUrl = vm.runInNewContext(`${manifestUrlCode}\nparseEclipseManifestUrl`, { URL });
assert.equal(parseEclipseManifestUrl("https://example.com/manifest.json"), "https://example.com/manifest.json");
assert.equal(parseEclipseManifestUrl("http://localhost:3000/manifest.json"), "http://localhost:3000/manifest.json");
for (const unsafeUrl of [
  "javascript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "http://example.com/manifest.json",
  "https://user:pass@example.com/manifest.json",
  "not a URL",
]) {
  assert.throws(() => parseEclipseManifestUrl(unsafeUrl), undefined, `rejects ${unsafeUrl}`);
}

assert.match(searchRendering, /let searchGeneration = 0/);
assert.match(searchRendering, /generation !== searchGeneration/);
assert.match(searchRendering, /generation === searchGeneration/);

console.log("External metadata uses safe DOM rendering; manifest URL validation and search-race guards pass.");
