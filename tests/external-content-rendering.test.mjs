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

const addonRendering = section("// External Eclipse Addon Providers", "let authPollTimer = null;");
assert.doesNotMatch(addonRendering, /\.innerHTML\s*=/, "untrusted Eclipse account fields are rendered as text");
assert.match(addonRendering, /row\.textContent\s*=/);
assert.match(addonRendering, /Connect Eclipse with Discord/);
assert.match(addonRendering, /eclipse-auth\/addons/);
assert.match(addonRendering, /omniphony-eclipse-inventory-track-id/);
assert.match(addonRendering, /omniphony-eclipse-inventory-title/);
assert.match(addonRendering, /omniphony-eclipse-inventory-artist/);
assert.match(addonRendering, /omniphony-eclipse-inventory-isrc/);
assert.match(addonRendering, /omniphony-eclipse-inventory-provider/);
assert.match(addonRendering, /cancelEclipseInventoryWithNativeSupervisor/);
assert.match(addonRendering, /Inspect connected Eclipse qualities/);
assert.match(addonRendering, /eclipseEnabledProviders = active/,
  "inventory enumerates every enabled connected addon, independent of RealMAX playback selection");
assert.match(addonRendering, /requestEclipseInventoryWithNativeSupervisor\(/,
  "inventory calls the guarded native supervisor instead of the protected service endpoint from renderer fetch");
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

const inventoryFormatter = source.match(/function formatEclipseInventorySummary\([\s\S]*?\n\}/)?.[0];
assert.ok(inventoryFormatter, "the connected addon panel has a bounded stream summary formatter");
const formatSandbox = {};
vm.createContext(formatSandbox);
vm.runInContext(`${inventoryFormatter}\nglobalThis.format = formatEclipseInventorySummary;`, formatSandbox);
const formattedInventory = formatSandbox.format("Gravity", {
  complete: true,
  attempts: [{ requestedQuality: "Atmos", status: "descriptor_returned" }],
  streams: [{
    actualQuality: "Atmos", codec: "E-AC-3", container: "m3u8", bitrate: 768000,
    protection: { status: "protected", systems: ["widevine"] },
    playbackCompatibility: "authorized playback required",
    url: "https://cdn.example/private.m3u8?signature=secret",
    streamRef: "local-secret-handle",
    headers: { Authorization: "Bearer private-token" },
    trackIdentityStatus: "verified",
    referenceExpiresAt: 1900000000,
    mediaVariantsInspectionComplete: false,
    mediaVariants: [{ id: "audio-1", codecs: "eac3", declaredBandwidth: 768000, bandwidthScope: "audio representation" }],
  }],
  completeness: { qualityRequestsComplete: true, streamResultsTruncated: true, platformBlockedProfiles: ["apple-fps-alac-v1"] },
});
assert.match(formattedInventory, /Atmos · E-AC-3 \/ m3u8 · protected/);
assert.match(formattedInventory, /DRM: widevine/);
assert.match(formattedInventory, /bandwidth \(audio representation\)/);
assert.match(formattedInventory, /Adaptive media representation inspection incomplete/);
assert.match(formattedInventory, /apple-fps-alac-v1/);
assert.match(formattedInventory, /Result truncated/);
assert.match(formattedInventory, /reference expires/);
assert.doesNotMatch(formattedInventory, /cdn\.example|secret|private-token|streamRef/,
  "the panel does not display media URLs, opaque handles, or authorization headers");
const oversizedInventory = formatSandbox.format("Big provider", {
  complete: false,
  attempts: Array.from({ length: 256 }, (_, index) => ({ requestedQuality: `tier-${index}`, reason: "x".repeat(120) })),
  streams: [],
});
assert.match(oversizedInventory, /Inventory display shortened at 24000 characters/);
assert.match(oversizedInventory, /256 attempts/);

const reportFormatter = source.match(/function formatEclipseInventoryReports\([\s\S]*?\n\}/)?.[0];
assert.ok(reportFormatter, "inventory output has a bounded report formatter");
const reportSandbox = {};
vm.createContext(reportSandbox);
vm.runInContext(`${reportFormatter}\nglobalThis.formatReports = formatEclipseInventoryReports;`, reportSandbox);
const reports = Array.from({ length: 8 }, (_, index) => `Provider-${index}: ${"x".repeat(5000)}`);
const boundedReports = reportSandbox.formatReports(reports, 4096);
assert.ok(boundedReports.length <= 4096);
assert.match(boundedReports, /Provider-0/);
for (let index = 0; index < reports.length; index++) assert.match(boundedReports, new RegExp(`Provider-${index}`));
assert.match(boundedReports, /Report shortened for display/);

assert.match(searchRendering, /let searchGeneration = 0/);
assert.match(searchRendering, /generation !== searchGeneration/);
assert.match(searchRendering, /generation === searchGeneration/);

console.log("External metadata uses safe DOM rendering; manifest URL validation and search-race guards pass.");
