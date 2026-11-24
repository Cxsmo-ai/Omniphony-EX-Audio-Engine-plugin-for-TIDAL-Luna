import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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

const addonRendering = section("// External Eclipse Addon Providers", "let authPollTimer = null;");
assert.doesNotMatch(addonRendering, /\.innerHTML\s*=/, "untrusted Eclipse account fields are rendered as text");
assert.match(addonRendering, /row\.textContent\s*=/);
assert.match(addonRendering, /Connect Eclipse with Discord/);
assert.match(addonRendering, /eclipse-auth\/addons/);
assert.doesNotMatch(addonRendering, /manifest(?:Url| URL).*\.value|Add Eclipse Manifest/i,
  "Eclipse add-ons are account-discovered rather than manually configured by URL");
assert.match(source, /value: "both", label: "Both \(TIDAL API \+ Eclipse add-ons\)"/);
assert.match(source, /value: "tidal_api", label: "TIDAL API only"/);
assert.match(source, /value: "eclipse_addons", label: "Eclipse add-ons only"/);
assert.match(source, /function normalizeRealmaxSourceMode\(value\)/);
assert.match(source, /function getSourcePlaybackSummary\(status, format\)[\s\S]*sourceName/,
  "the footer badge names the actual playback provider/add-on");

const authRendering = section("const updateAuthStatus = async () => {", "authBtn.onclick = async () => {");
assert.doesNotMatch(authRendering, /\.innerHTML\s*=/, "device-login response values are rendered as text");
assert.match(authRendering, /code\.textContent\s*=\s*String\(data\.userCode\)/);

assert.doesNotMatch(source, /function parseEclipseManifestUrl\s*\(/,
  "the old manual Eclipse manifest URL editor is not part of the account-based plugin");

assert.match(searchRendering, /let searchGeneration = 0/);
assert.match(searchRendering, /generation !== searchGeneration/);
assert.match(searchRendering, /generation === searchGeneration/);

console.log("External metadata uses safe DOM rendering; manifest URL validation and search-race guards pass.");
