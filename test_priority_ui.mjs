import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sourcePath = resolve(dirname(fileURLToPath(import.meta.url)), "omniphony.mjs");
const source = readFileSync(sourcePath, "utf8");
const manifestPath = resolve(dirname(sourcePath), "omniphony.json");
const pluginManifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const lunaManifest = JSON.parse(readFileSync(resolve(dirname(sourcePath), "manifest.json"), "utf8"));
assert.equal(lunaManifest.version, pluginManifest.version,
  "repository manifest version matches the development plugin manifest version");

const distPath = resolve(dirname(sourcePath), "dist");
const store = JSON.parse(readFileSync(resolve(distPath, "store.json"), "utf8"));
assert.ok(Array.isArray(store.plugins) && store.plugins.length > 0,
  "built TidaLuna store catalog lists at least one plugin");
const builtPluginEntries = store.plugins
  .filter((name) => typeof name === "string" && name.endsWith(".mjs") && path.basename(name) === name)
  .map((name) => {
    const modulePath = resolve(distPath, name);
    return {
      name,
      moduleBytes: readFileSync(modulePath),
      package: JSON.parse(readFileSync(modulePath.replace(/\.mjs$/, ".json"), "utf8")),
    };
  });
const builtPlugin = builtPluginEntries.find((entry) => entry.package.name === "Omniphony EX Audio Engine");
assert.ok(builtPlugin, "built TidaLuna catalog includes the Omniphony EX module");
const builtPackage = builtPlugin.package;
const builtModuleBytes = builtPlugin.moduleBytes;
const builtModuleSha256 = createHash("sha256").update(builtModuleBytes).digest("hex").toUpperCase();
assert.equal(pluginManifest.hash, builtModuleSha256,
  "development manifest SHA-256 matches the built module bytes");
assert.equal(lunaManifest.hash, builtModuleSha256,
  "repository manifest SHA-256 matches the built module bytes");
assert.equal(builtPackage.name, "Omniphony EX Audio Engine",
  "built module metadata identifies the expected Luna plugin");
assert.equal(builtPackage.version, store.version,
  "built plugin package version matches the TidaLuna store version");
assert.ok(typeof builtPackage.hash === "string" && builtPackage.hash.length > 0,
  "built Luna package contains the esbuild output hash");
assert.ok(builtModuleBytes.length > 0,
  "built Luna plugin module is nonempty");
const requiredServiceVersion = source.match(/^const REQUIRED_SERVICE_VERSION = "([^"]+)";$/m)?.[1];
assert.ok(requiredServiceVersion, "runtime declares a required service version");
assert.ok(pluginManifest.main.includes(requiredServiceVersion),
  "module cache key identifies the service version required by this plugin build");
assert.ok(pluginManifest.main.includes(pluginManifest.version),
  "module cache key identifies the plugin package version");
assert.match(source, new RegExp(`const PLUGIN_BUILD = "${pluginManifest.version}";`),
  "runtime diagnostics report the same build version as the served manifest");
const naturalEndHandoffDispatch = source.match(
  /tracePlaybackEvent\("natural-end-handoff-play-request"[\s\S]*?void triggerEnginePlay\(mediaTrackId,\s*([^,]+),/
);
assert.ok(naturalEndHandoffDispatch,
  "natural-end queue handoff dispatches the newly selected track");
assert.equal(naturalEndHandoffDispatch[1].trim(), "0",
  "natural-end handoff starts the next track at zero instead of reusing the completed track position");
const downloadSettingsFunction = source.match(/function getDownloadSettings\(\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(downloadSettingsFunction, "downloader settings are defined");
const downloadSettings = vm.runInNewContext(`${downloadSettingsFunction}; getDownloadSettings();`, {
  localStorage: { getItem: (key) => key === "omniphony_download_folder"
    ? "C:\\OmniFusion-Standalone\\downloads" : null },
});
assert.equal(downloadSettings.destinationFolder, "C:\\OmniFusion-Standalone\\downloads",
  "saved Windows download directory retains its path separators");
assert.doesNotMatch(source, /currentPlaybackMeta/,
  "the current-track downloader does not reference an undeclared metadata variable");
assert.match(source, /const meta = getCurrentDownloadTrackMeta\(\);/,
  "the current-track downloader derives its request from TIDAL's live playback state");
const downloadMetadataSandbox = {
  getCurrentTrackId: () => 97874146,
  getCurrentTrackMeta: () => ({ title: "Machine", artist: "Imagine Dragons" }),
  getTidalPlayState: () => ({ playbackControls: { mediaProduct: {
    title: "Machine", artist: { name: "Imagine Dragons" }, album: { name: "Origins" }, trackNumber: 3,
  } } }),
  window: { neptune: { store: { getState: () => ({}) } } },
};
const downloadMetadata = vm.runInNewContext(
  `${extractFunction("getCurrentDownloadTrackMeta")}; getCurrentDownloadTrackMeta();`,
  downloadMetadataSandbox,
);
assert.equal(downloadMetadata.id, 97874146, "downloader uses the currently selected TIDAL track id");
assert.equal(downloadMetadata.title, "Machine");
assert.equal(downloadMetadata.artist.name, "Imagine Dragons");
assert.equal(downloadMetadata.album.name, "Origins");
assert.equal(downloadMetadata.trackNumber, 3);
const lyricsObserverDeclaration = source.indexOf("const radiantLyricsFetchObserver = {");
const pluginLoadCall = source.lastIndexOf("onLoad();");
assert.ok(lyricsObserverDeclaration !== -1 && pluginLoadCall > lyricsObserverDeclaration,
  "plugin startup runs after the Radiant lyrics fetch observer is initialized");
assert.equal((source.match(/^onLoad\(\);$/gm) || []).length, 1,
  "the plugin entry point runs exactly once");
assert.match(source, /const SYNC_POLL_INTERVAL_MS = 100;/,
  "control reconciliation requests status at a 100 ms cadence");
assert.match(source, /const CLOCK_POLL_INTERVAL_MS = 50;/,
  "read-only timeline samples use a 50 ms cadence separate from control reconciliation");
const syncLoopStart = source.indexOf("function startSyncLoop()");
const nativeUiStart = source.indexOf("// Native In-TIDAL UI Elements", syncLoopStart);
assert.ok(syncLoopStart !== -1 && nativeUiStart > syncLoopStart,
  "playback sync loop is present");
const syncLoopSource = source.slice(syncLoopStart, nativeUiStart);
assert.match(syncLoopSource, /\$\{API_BASE\}\/clock/,
  "high-rate timeline samples use the dedicated read-only endpoint");
assert.match(syncLoopSource, /acceptEngineClockSampleOrdering\(status\)/,
  "full status samples share the ordering fence with fast clock samples");
assert.match(syncLoopSource, /clockPollBusy/,
  "clock requests cannot overlap when the service is slow");
assert.match(syncLoopSource,
  /if \(engineClockAdvancing\)\s*\{\s*mirrorEngineClockToTidal\(projectedStatus, trackId, tidal\.tidalPosition\);/,
  "the high-rate authoritative clock reaches TIDAL timing consumers for lyrics and the footer");
assert.doesNotMatch(syncLoopSource, /sendEngineCommand\(["']seek["']/,
  "sampled TIDAL clock and automatic drift reconciliation cannot seek libmpv");
assert.match(syncLoopSource, /tidal-clock-discontinuity-ignored/,
  "unexpected TIDAL clock jumps remain visible in diagnostics");
assert.match(syncLoopSource,
  /if \(holdSyncForPausedSelection\([\s\S]*?\)\) \{\s*\/\/ This path intentionally skips transport reconciliation[\s\S]*?currentFormatLabel = status\.label \|\| "Omniphony EX";\s*updateBadgeUI\(currentFormatLabel\);\s*refreshSettingsTelemetry\(status\);[\s\S]*?return;/,
  "paused preflight refreshes the footer and overlay before its early return");
assert.match(syncLoopSource,
  /if \(pausedSelectionConfirmedTrackId !== null\) updatePausedSelectionUi\(\);/,
  "successful status polling refreshes confirmed paused-selection readiness text");
assert.match(syncLoopSource, /getStalledPlayRecoveryMode\(tidal, engineTrackId/,
  "an explicit stalled TIDAL selection is reconciled when native Play interceptors do not fire");
assert.match(syncLoopSource, /stalled-play-selection-held-paused/,
  "an armed paused-preparation selection remains paused during stalled-play recovery");
assert.match(syncLoopSource, /stalled-play-intent-recovered/,
  "an unarmed stalled selection starts only after a trusted Play gesture was captured");
assert.match(source, /function recordStalledPlayAuthorization\(event\)[\s\S]*?event\?\.isTrusted !== true[\s\S]*?stalledPlayAuthorization = \{/,
  "fallback playback authorization originates from a trusted user Play control event");
assert.match(source, /if \(!allowPaused && sameSelectedTrack[\s\S]*?latestTidal\.playing === true/,
  "a diagnostics-only preparation cannot enter the asynchronous resume branch");
assert.match(source, /handleImmediateTidalSeek[\s\S]*?sendEngineCommand\(["']seek["']/,
  "explicit intercepted TIDAL seeks still reach libmpv");
const stalledRecoverySandbox = {};
vm.runInNewContext(extractFunction("isUninterceptedStalledPlayIntent"), stalledRecoverySandbox);
vm.runInNewContext(extractFunction("getStalledPlayRecoveryMode"), stalledRecoverySandbox);
stalledRecoverySandbox.stalledPlayAuthorization = null;
vm.runInNewContext(extractFunction("hasStalledPlayAuthorization"), stalledRecoverySandbox);
vm.runInNewContext(extractFunction("consumeStalledPlayAuthorization"), stalledRecoverySandbox);
stalledRecoverySandbox.prepareNextSelectionPaused = false;
stalledRecoverySandbox.pausedSelectionPending = null;
stalledRecoverySandbox.getQueuedTrackId = () => "97874144";
stalledRecoverySandbox.getCurrentTrackId = () => "97874144";
stalledRecoverySandbox.tracePlaybackEvent = () => {};
vm.runInNewContext(extractFunction("getPlayControlTrackId"), stalledRecoverySandbox);
vm.runInNewContext(extractFunction("recordStalledPlayAuthorization"), stalledRecoverySandbox);
const playRow = { innerText: "Natural Imagine Dragons 3:10", getAttribute: () => null, querySelector: () => null };
const playControl = {
  textContent: "",
  title: "",
  getAttribute: (name) => name === "aria-label" ? "Play" : null,
  closest: () => playRow,
};
const playEvent = { isTrusted: true, target: { closest: () => playControl } };
assert.equal(stalledRecoverySandbox.recordStalledPlayAuthorization({ ...playEvent, isTrusted: false }), false,
  "synthetic or untrusted clicks cannot create fallback Play authorization");
assert.equal(stalledRecoverySandbox.recordStalledPlayAuthorization(playEvent), true,
  "a trusted Play control inside a TIDAL track row creates a short-lived authorization");
assert.equal(stalledRecoverySandbox.stalledPlayAuthorization.allowSelectionChange, true);
assert.equal(stalledRecoverySandbox.stalledPlayAuthorization.rowText, "natural imagine dragons 3:10");
stalledRecoverySandbox.prepareNextSelectionPaused = true;
stalledRecoverySandbox.stalledPlayAuthorization = null;
assert.equal(stalledRecoverySandbox.recordStalledPlayAuthorization(playEvent), false,
  "an armed paused-preparation gate never creates an unpaused Play authorization");
const stalledNatural = { trackId: 227401768, actual: "STALLED", desired: "PLAYING" };
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144), null,
  "unarmed STALLED/PLAYING without a fresh trusted Play gesture cannot start audio");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  pausedPreparationArmed: true,
}), "prepare-paused", "an armed selection is prepared without starting audio");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  pausedSelectionPending: true,
}), null, "an existing paused preparation is left to its pending-selection state machine");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  pausedSelectionConfirmed: true,
}), "block-confirmed-selection", "a confirmed paused selection blocks stalled state without trusted Play authorization");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  pausedSelectionConfirmed: true,
  explicitPlayAuthorized: true,
}), "block-confirmed-selection", "a confirmed selection also waits until diagnostics are ready");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  pausedSelectionConfirmed: true,
  pausedSelectionDiagnosticsReady: true,
  explicitPlayAuthorized: true,
}), "start-play", "a confirmed selection starts only after diagnostics and trusted Play authorization");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 97874144, {
  explicitPlayAuthorized: true,
}), "start-play", "a fresh trusted Play gesture authorizes the matching unarmed selection");
stalledRecoverySandbox.stalledPlayAuthorization = { trackId: "227401768", expiresAt: 5000 };
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(227401768, 4999), true,
  "a current authorization matches its selected track before expiry");
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(97874144, 4999), false,
  "a trusted Play gesture cannot authorize a different track");
assert.equal(stalledRecoverySandbox.consumeStalledPlayAuthorization("227401768", 4999), true,
  "a matching stalled-play authorization is consumed exactly once");
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(227401768, 4999), false,
  "consumed stalled-play authorization cannot be reused");
stalledRecoverySandbox.stalledPlayAuthorization = { trackId: "227401768", expiresAt: 4999 };
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(227401768, 5000), false,
  "expired trusted Play authorization cannot restart a stalled selection");
stalledRecoverySandbox.stalledPlayAuthorization = {
  trackId: null, allowSelectionChange: true, priorTrackId: "97874144",
  rowText: "natural imagine dragons 3:10", expiresAt: 5000,
};
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(227401768, 4999, {
  title: "Natural", artist: "Imagine Dragons",
}), true, "a track-row gesture binds only when the selected title and artist match that row");
assert.equal(stalledRecoverySandbox.hasStalledPlayAuthorization(227401768, 4999, {
  title: "Bad Liar", artist: "Imagine Dragons",
}), false, "a track-row gesture cannot authorize a different selected title");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode(stalledNatural, 227401768), null,
  "a stalled state on the already-matched engine track does not trigger a duplicate stream");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode({ ...stalledNatural, desired: "NOT_PLAYING" }, 97874144), null,
  "a stalled TIDAL state without desired Play is not treated as user intent");
assert.equal(stalledRecoverySandbox.getStalledPlayRecoveryMode({ ...stalledNatural, actual: "BUFFERING" }, 97874144), null,
  "buffering alone does not trigger fallback playback");
const clockOrderingSandbox = {
  clockOrderingInstanceId: "svc-a",
  clockOrderingMpvInstanceId: "mpv-a",
  clockOrderingSequence: 4,
  activeClockServiceInstanceId: "svc-a",
  activeClockMpvInstanceId: "mpv-a",
};
vm.runInNewContext(extractFunction("acceptEngineClockSampleOrdering"), clockOrderingSandbox);
vm.runInNewContext(extractFunction("commitEngineClockSampleOrdering"), clockOrderingSandbox);
const clockSample = (service, mpv, sequence) =>
  `({serviceInstanceId:${JSON.stringify(service)},mpvInstanceId:${JSON.stringify(mpv)},sampleSequence:${sequence}})`;
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 5)})`,
  clockOrderingSandbox,
), true);
vm.runInNewContext(`commitEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 5)})`, clockOrderingSandbox);
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 5)})`,
  clockOrderingSandbox,
), false, "duplicate or reordered clock samples are rejected");
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 6)})`,
  clockOrderingSandbox,
), true, "a newer sample from the same service is accepted");
vm.runInNewContext(`commitEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 6)})`, clockOrderingSandbox);
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 102)})`,
  clockOrderingSandbox,
), true, "a candidate can be checked without advancing the committed watermark");
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 101)})`,
  clockOrderingSandbox,
), true, "an invalid higher candidate cannot poison a lower valid sample");
vm.runInNewContext(`commitEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 101)})`, clockOrderingSandbox);
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 102)})`,
  clockOrderingSandbox,
), true);
vm.runInNewContext(`commitEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 102)})`, clockOrderingSandbox);
clockOrderingSandbox.activeClockServiceInstanceId = "svc-b";
clockOrderingSandbox.activeClockMpvInstanceId = "mpv-b";
clockOrderingSandbox.clockOrderingInstanceId = "svc-b";
clockOrderingSandbox.clockOrderingMpvInstanceId = "mpv-b";
clockOrderingSandbox.clockOrderingSequence = -1;
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-b", "mpv-b", 0)})`,
  clockOrderingSandbox,
), true, "a validated service restart gets a new ordering epoch");
assert.equal(vm.runInNewContext(
  `acceptEngineClockSampleOrdering(${clockSample("svc-a", "mpv-a", 99)})`,
  clockOrderingSandbox,
), false, "a delayed old-service sample cannot reclaim clock authority");
const advancingAt = source.indexOf("currentStatusData.engineClockAdvancing = Boolean(advancing);");
assert.notEqual(advancingAt, -1, "clock advancement is calculated from consecutive engine telemetry");
assert.ok(source.indexOf("updateBadgeUI(currentFormatLabel);", advancingAt) > advancingAt,
  "native-slot playback badge refreshes after engine-clock evidence is calculated");
const settingsPanelMountAt = source.indexOf("document.body.appendChild(panel);", source.indexOf("function toggleSettingsPanel("));
const pausedUiAfterMountAt = source.indexOf("updatePausedSelectionUi();", settingsPanelMountAt);
assert.ok(settingsPanelMountAt !== -1 && pausedUiAfterMountAt > settingsPanelMountAt,
  "paused-selection labels are populated after the settings panel is attached to the document");

function extractFunction(name) {
  const marker = `function ${name}(`;
  let start = source.indexOf(marker);
  assert.notEqual(start, -1, `missing ${name}`);
  if (source.slice(start - 6, start) === "async ") start -= 6;
  const signatureEnd = source.indexOf(") {", start);
  assert.notEqual(signatureEnd, -1, `missing body for ${name}`);
  const open = signatureEnd + 2;
  let depth = 0;
  let quote = null;
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (lineComment) {
      if (ch === "\n") lineComment = false;
      continue;
    }
    if (blockComment) {
      if (ch === "*" && next === "/") { blockComment = false; i += 1; }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "/" && next === "/") { lineComment = true; i += 1; continue; }
    if (ch === "/" && next === "*") { blockComment = true; i += 1; continue; }
    if (ch === "'" || ch === '"' || ch === "`") { quote = ch; continue; }
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

const pausedUiButton = {
  textContent: "",
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
};
const pausedUiStatus = { textContent: "" };
const pausedUiSandbox = {
  document: {
    getElementById: (id) => id === "omniphony-prepare-paused" ? pausedUiButton
      : id === "omniphony-prepare-paused-status" ? pausedUiStatus : null,
  },
  prepareNextSelectionPaused: false,
  pausedSelectionPending: null,
  pausedSelectionConfirmedTrackId: null,
  pausedSelectionUserPlayAuthorization: null,
  desiredPlaybackIntent: false,
  supportedFormats: ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
  currentStatusData: {},
  queuedTrackId: null,
  getQueuedTrackId: null,
  nativePlayerCommandHookInstalled: true,
  nativePlayerCommandHookDiagnostic: "test guard installed",
};
pausedUiSandbox.getQueuedTrackId = () => pausedUiSandbox.queuedTrackId;
vm.createContext(pausedUiSandbox);
vm.runInContext(extractFunction("isPausedSelectionDiagnosticsReady"), pausedUiSandbox);
vm.runInContext(extractFunction("updatePausedSelectionUi"), pausedUiSandbox);
pausedUiSandbox.updatePausedSelectionUi();
assert.equal(pausedUiButton.textContent, "Prepare next selection paused",
  "paused-preparation control text is written through the standard DOM textContent API");
assert.match(pausedUiStatus.textContent, /Arm this before selecting a track/,
  "the paused-preparation status explanation is visible while unarmed");
pausedUiSandbox.prepareNextSelectionPaused = true;
pausedUiSandbox.updatePausedSelectionUi();
assert.equal(pausedUiButton.textContent, "Cancel paused preparation",
  "arming the one-shot control updates its visible label");
assert.equal(pausedUiButton.attributes["aria-pressed"], "true",
  "the one-shot control exposes its armed state accessibly");
pausedUiSandbox.prepareNextSelectionPaused = false;
pausedUiSandbox.pausedSelectionConfirmedTrackId = "9001";
pausedUiSandbox.queuedTrackId = "9001";
pausedUiSandbox.updatePausedSelectionUi();
assert.equal(pausedUiStatus.textContent,
  "The selected stream diagnostics are still loading. Playback remains paused.",
  "confirmed selection remains blocked until matching stream diagnostics arrive");
pausedUiSandbox.currentStatusData = {
  trackId: 9001, playbackState: "prepared", paused: true, format: "AC4_IMS",
  sourceCodec: "ac-4", activeDecoder: "pcm_s16le", sourceBitrate: 256022,
  sourceSampleRate: 48000, sourceChannels: 2, streamProxyFailures: 0,
  lastFailure: null, omniphonyRoute: null,
};
assert.equal(pausedUiSandbox.isPausedSelectionDiagnosticsReady("9001"), true,
  "prepared AC4_IMS diagnostics are ready without an Omniphony route");
pausedUiSandbox.updatePausedSelectionUi();
assert.equal(pausedUiStatus.textContent,
  "Stream diagnostics are ready. Use TIDAL's Play control to start this track.",
  "status refresh advances the panel from loading to ready without another PLAY");
assert.equal(pausedUiSandbox.desiredPlaybackIntent, false,
  "displaying readiness does not authorize playback");
assert.equal(pausedUiSandbox.pausedSelectionConfirmedTrackId, "9001",
  "displaying readiness keeps the track confirmation gate in place");
assert.equal(pausedUiSandbox.pausedSelectionUserPlayAuthorization, null,
  "displaying readiness does not create trusted Play authorization");

let pausedFooterTrackLink = null;
const stalePlaybackControls = { id: "hidden-playback-controls" };
const playbackStateFooter = {
  querySelector: (selector) => selector.includes("/track/") ? pausedFooterTrackLink : null,
};
const playbackStateSandbox = {
  desiredPlaybackIntent: null,
  playIntentChangedAt: 0,
  currentStatusData: {},
  window: {
    luna: { lib: { PlayState: {
      playbackContext: { actualProductId: 42 },
      playbackControls: { mediaProduct: { productId: 99, title: "Current track", artist: { name: "Current artist" } } },
      desiredState: "PLAYING", state: "PLAYING", currentTime: 0, playTime: 12.5,
    } } },
    location: { href: "https://desktop.tidal.com/track/999" },
  },
  document: {
    getElementById: (id) => id === "footerPlayer" ? playbackStateFooter : null,
    querySelector: (selector) => {
    if (selector === '[data-test="playback-controls"]') return stalePlaybackControls;
    if (selector === '[data-test="footer-player"]') return playbackStateFooter;
    return { getAttribute: () => "/track/999" };
  } },
};
vm.createContext(playbackStateSandbox);
vm.runInContext(`
  ${extractFunction("parsePlaybackPosition")}
  ${extractFunction("getTidalPlayState")}
  ${extractFunction("getTidalPosition")}
  ${extractFunction("getPlaybackFooter")}
  ${extractFunction("getCurrentTrackId")}
  ${extractFunction("getCurrentTrackMeta")}
  ${extractFunction("hasTidalClockDiscontinuity")}
  ${extractFunction("getTidalPlaybackSnapshot")}
  globalThis.playbackSnapshot = getTidalPlaybackSnapshot();
`, playbackStateSandbox);
assert.equal(playbackStateSandbox.parsePlaybackPosition(null), null,
  "a null engine timestamp is unavailable rather than position zero");
assert.equal(playbackStateSandbox.parsePlaybackPosition(undefined), null,
  "an absent engine timestamp is unavailable");
assert.equal(playbackStateSandbox.parsePlaybackPosition(""), null,
  "an empty engine timestamp is unavailable");
assert.equal(playbackStateSandbox.parsePlaybackPosition(Number.NaN), null,
  "a NaN engine timestamp is unavailable");
assert.equal(playbackStateSandbox.parsePlaybackPosition(0), 0,
  "a real numeric zero remains a valid position");
assert.equal(playbackStateSandbox.playbackSnapshot.trackId, 99,
  "selected media product ID wins over playback-context and unrelated URL IDs");
assert.equal(playbackStateSandbox.getPlaybackFooter(), playbackStateFooter,
  "the visible TIDAL #footerPlayer wins over a stale generic playback-controls match");
assert.equal(playbackStateSandbox.playbackSnapshot.position, 12.5,
  "snapshot uses playTime instead of a stale zero currentTime");
assert.equal(playbackStateSandbox.playbackSnapshot.tidalPosition, 12.5,
  "snapshot preserves the independent TIDAL clock for discontinuity diagnostics");
assert.equal(playbackStateSandbox.playbackSnapshot.playing, true,
  "snapshot follows TidaLuna playback state without a DOM media element");
playbackStateSandbox.currentStatusData = { trackId: 99, timePos: 161.85 };
vm.runInContext("globalThis.engineAuthoritativeSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.engineAuthoritativeSnapshot.position, 161.85,
  "the matching libmpv clock remains authoritative over stale TIDAL time fields");
assert.equal(playbackStateSandbox.engineAuthoritativeSnapshot.tidalPosition, 12.5,
  "engine clock authority does not hide the independent TIDAL timeline position");
playbackStateSandbox.window.luna.lib.PlayState.playTime = 160;
playbackStateSandbox.currentStatusData = { trackId: 99, timePos: 150 };
vm.runInContext("globalThis.nativeSeekSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.nativeSeekSnapshot.position, 150,
  "libmpv remains the public snapshot clock for the matching track");
assert.equal(playbackStateSandbox.nativeSeekSnapshot.tidalPosition, 160,
  "a native TIDAL seek remains observable independently of the engine clock");
assert.equal(playbackStateSandbox.hasTidalClockDiscontinuity(
  { trackId: 99, tidalPosition: 120, tidalPlaying: false }, 99,
  playbackStateSandbox.nativeSeekSnapshot.tidalPosition, 0.1, false
), false, "a paused clock change is not classified as a playback discontinuity");
assert.equal(playbackStateSandbox.hasTidalClockDiscontinuity(
  { trackId: 99, tidalPosition: 44.149, tidalPlaying: true }, 99,
  42.989, 0.1, true
), true, "a playing clock rewind is detected for diagnostics without authorizing a seek");
for (const missingPosition of [null, undefined, "", Number.NaN]) {
  playbackStateSandbox.currentStatusData = { trackId: 99, timePos: missingPosition };
  vm.runInContext("globalThis.missingEngineSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
  assert.equal(playbackStateSandbox.missingEngineSnapshot.position, 160,
    `missing engine position ${String(missingPosition)} falls back to TIDAL time`);
}
playbackStateSandbox.currentStatusData = { trackId: 99, timePos: 0 };
vm.runInContext("globalThis.zeroEngineSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.zeroEngineSnapshot.position, 0,
  "a real engine zero remains valid and is not treated as missing");
playbackStateSandbox.currentStatusData = { trackId: 99, timePos: 159.727885 };
vm.runInContext("globalThis.validEngineSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.validEngineSnapshot.position, 159.727885,
  "a valid nonzero engine position remains authoritative");
playbackStateSandbox.window.luna.lib.PlayState.playTime = 12.5;
playbackStateSandbox.currentStatusData = {};
playbackStateSandbox.window.luna.lib.PlayState.state = "NOT_PLAYING";
vm.runInContext("globalThis.pendingPlaySnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.pendingPlaySnapshot.playing, false,
  "an optimistic desired PLAYING state cannot dispatch before TIDAL actually starts");
playbackStateSandbox.desiredPlaybackIntent = true;
playbackStateSandbox.window.luna.lib.PlayState.desiredState = "NOT_PLAYING";
playbackStateSandbox.window.luna.lib.PlayState.state = "PAUSED";
vm.runInContext("globalThis.endedPlaybackSnapshot = getTidalPlaybackSnapshot();", playbackStateSandbox);
assert.equal(playbackStateSandbox.endedPlaybackSnapshot.playing, false,
  "a stale local PLAY intent cannot resume libmpv after TIDAL reaches paused at natural EOF");
playbackStateSandbox.window.luna.lib.PlayState.playbackContext.actualProductId = undefined;
playbackStateSandbox.window.luna.lib.PlayState.playbackControls.mediaProduct.productId = undefined;
vm.runInContext("globalThis.navigationOnlyTrackId = getCurrentTrackId();", playbackStateSandbox);
assert.equal(playbackStateSandbox.navigationOnlyTrackId, null,
  "a track route alone cannot be mistaken for the playing track");
pausedFooterTrackLink = {
  getAttribute: (name) => name === "href" ? "https://desktop.tidal.com/track/238551884" : null,
};
vm.runInContext("globalThis.pausedFooterTrackId = getCurrentTrackId();", playbackStateSandbox);
assert.equal(playbackStateSandbox.pausedFooterTrackId, "238551884",
  "the exact now-playing footer track recovers selection when PlayState temporarily clears mediaProduct");

// Playback readiness requires the mounted footer controls, current service, and saved output device.
const readinessSandbox = {
  document: {
    getElementById: (id) => id === "omniphony-badge" ? readinessBadge
      : id === "footerPlayer" ? readinessFooter : null,
    querySelector: () => staleReadinessFooter,
  },
  API_BASE: "http://127.0.0.1:8765",
  REQUIRED_SERVICE_VERSION: requiredServiceVersion,
  REQUIRED_SERVICE_CAPABILITY: "prepared-playback-v1",
  REQUIRED_AUDIO_DEVICE: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}",
  nativePlayerCommandHookInstalled: true,
  nativePlayerCommandHookDiagnostic: "test bridge unavailable",
  currentConfig: {},
  settingsUiVerified: false,
  window: { innerWidth: 1920, innerHeight: 985 },
  getComputedStyle: (element) => ({ display: element.style?.display || "", visibility: element.style?.visibility || "visible", opacity: element.style?.opacity || "1" }),
  requestWithTimeout: async (url) => ({
    ok: true, status: 200,
    json: async () => url.endsWith("/devices")
      ? { devices: [{ name: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}" }] }
      : { version: readinessVersion, capabilities: readinessCapabilities },
  }),
  fetchConfig: async () => readinessConfig,
};
const readinessControl = {
  isConnected: true, parentElement: null, style: {},
  getBoundingClientRect: () => ({ left: 10, top: 900, right: 30, bottom: 920, width: 20, height: 20 }),
};
const readinessFooter = { isConnected: true, parentElement: null, querySelectorAll: () => [readinessControl] };
readinessControl.parentElement = readinessFooter;
const staleReadinessFooter = { id: "hidden-playback-controls", querySelectorAll: () => [readinessControl] };
const readinessBadge = {
  isConnected: true, parentElement: readinessFooter, style: {},
  getBoundingClientRect: () => ({ left: 40, top: 900, right: 140, bottom: 940, width: 100, height: 40 }),
};
let readinessVersion = "2.1.17";
let readinessCapabilities = ["prepared-playback-v1"];
let readinessConfig = {
  priority: ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
  audio_device: readinessSandbox.REQUIRED_AUDIO_DEVICE,
};
vm.createContext(readinessSandbox);
vm.runInContext(`
  ${extractFunction("hasVisibleLayout")}
  ${extractFunction("getPlaybackFooter")}
  ${extractFunction("checkPlaybackReadiness")}
  ${extractFunction("reportPlaybackReadinessBlock")}
`, readinessSandbox);
const unverifiedUiReadiness = await readinessSandbox.checkPlaybackReadiness();
assert.equal(unverifiedUiReadiness.ready, false, "unverified addon controls block playback before service checks");
assert.match(
  unverifiedUiReadiness.reason,
  new RegExp(`${requiredServiceVersion.replaceAll(".", "\\.")} is required`),
);
readinessSandbox.settingsUiVerified = true;
readinessSandbox.nativePlayerCommandHookInstalled = false;
const missingNativeGuardReadiness = await readinessSandbox.checkPlaybackReadiness();
assert.equal(missingNativeGuardReadiness.ready, false,
  "native playback remains blocked when the synchronous TIDAL IPC guard is unavailable");
assert.match(missingNativeGuardReadiness.reason, /synchronous native-player guard/);
readinessSandbox.nativePlayerCommandHookInstalled = true;
const staleReadiness = await readinessSandbox.checkPlaybackReadiness();
assert.equal(staleReadiness.ready, false, "stale service build fails the playback preflight");
assert.match(staleReadiness.reason, new RegExp(`${requiredServiceVersion.replaceAll(".", "\\.")}.*2\\.1\\.17`));
readinessVersion = requiredServiceVersion;
assert.equal((await readinessSandbox.checkPlaybackReadiness()).ready, true,
  "the exact required service and WASAPI endpoint pass readiness");
readinessBadge.getBoundingClientRect = () => ({ left: 40, top: 900, right: 40, bottom: 900, width: 0, height: 0 });
const hiddenBadgeReadiness = await readinessSandbox.checkPlaybackReadiness();
assert.equal(hiddenBadgeReadiness.ready, false,
  "a connected footer badge with zero-size geometry fails closed before playback");
assert.match(hiddenBadgeReadiness.reason, /no visible footer layout/);
readinessBadge.getBoundingClientRect = () => ({ left: 40, top: 900, right: 140, bottom: 940, width: 100, height: 40 });
assert.deepEqual(readinessSandbox.currentConfig, {},
  "readiness inspection does not mutate shared config before track generation is validated");
readinessCapabilities = [];
const unsupportedPreparationReadiness = await readinessSandbox.checkPlaybackReadiness();
assert.equal(unsupportedPreparationReadiness.ready, false,
  "a version-matching service without the paused-preparation contract cannot receive playback");
assert.match(unsupportedPreparationReadiness.reason, /prepared-playback-v1/);
readinessCapabilities = ["prepared-playback-v1"];
readinessConfig = { ...readinessConfig, audio_device: "auto" };
assert.equal((await readinessSandbox.checkPlaybackReadiness()).ready, false,
  "an unexpected device blocks playback preflight");

// An obsolete preflight that completes last must not overwrite newer shared settings.
const deferredReadinessConfigs = [];
const readinessRacePlayRequests = [];
const readinessRaceCommands = [];
const readinessRaceSandbox = {
  readinessFooter,
  document: {
    getElementById: (id) => id === "omniphony-badge" ? readinessBadge : null,
    querySelector: () => readinessFooter,
  },
  API_BASE: "http://127.0.0.1:8765",
  REQUIRED_SERVICE_VERSION: requiredServiceVersion,
  REQUIRED_SERVICE_CAPABILITY: "prepared-playback-v1",
  REQUIRED_AUDIO_DEVICE: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}",
  settingsUiVerified: true,
  nativePlayerCommandHookInstalled: true,
  currentConfig: { priority: ["LOSSLESS", "AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS"], audio_device: "initial" },
  currentStatusData: {},
  pluginDisposed: false,
  crypto: globalThis.crypto,
  AbortController,
  console: { log() {}, warn() {}, error(...args) { console.error(...args); } },
  fetchConfig: () => new Promise((resolve) => deferredReadinessConfigs.push(resolve)),
  requestWithTimeout: async (url, options = {}) => {
    if (url.endsWith("/health")) {
      return { ok: true, status: 200, json: async () => ({
        version: requiredServiceVersion, capabilities: ["prepared-playback-v1", "playback-clock-v1"],
      }) };
    }
    if (url.endsWith("/devices")) {
      return { ok: true, json: async () => ({ devices: [{
        name: readinessRaceSandbox.REQUIRED_AUDIO_DEVICE,
      }] }) };
    }
    if (url.endsWith("/play")) {
      const payload = JSON.parse(options.body);
      readinessRacePlayRequests.push(payload);
      return { ok: true, json: async () => ({
        generation: 1, requestId: payload.requestId, format: "LOSSLESS", label: "Lossless FLAC",
      }) };
    }
    if (url.endsWith("/resume") || url.endsWith("/pause") || url.endsWith("/cancel-play")) {
      readinessRaceCommands.push({ endpoint: url.split("/").at(-1), body: JSON.parse(options.body) });
      return { ok: true };
    }
    throw new Error(`Unexpected readiness race request: ${url}`);
  },
};
vm.createContext(readinessRaceSandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let desiredPlaybackIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastMirroredPlaybackState = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  function getTidalPlaybackSnapshot() { return { trackId: "9", playing: true }; }
  function hasPendingExplicitPlayIntent() { return false; }
  function shouldPreserveExplicitPlayIntent() { return false; }
  let transportIntentRevision = 1;
  const transportCommandQueues = new Map();
  const transportCommandsInFlight = new Map();
  function updateBadgeUI() {}
  function reportPlaybackReadinessBlock() {}
  function getPlaybackFooter() { return globalThis.readinessFooter; }
  ${extractFunction("hasVisibleLayout")}
  ${extractFunction("checkPlaybackReadiness")}
  ${extractFunction("triggerEnginePlay")}
  ${extractFunction("queueTransportCommand")}
  ${extractFunction("sendEngineCommand")}
  ${extractFunction("sendEngineCommandDirect")}
`, readinessRaceSandbox);
const staleConfigPlay = readinessRaceSandbox.triggerEnginePlay(8, 0, {});
for (let attempt = 0; attempt < 12 && deferredReadinessConfigs.length < 1; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 0));
const newestConfigPlay = readinessRaceSandbox.triggerEnginePlay(9, 0, {});
for (let attempt = 0; attempt < 12 && deferredReadinessConfigs.length < 2; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(deferredReadinessConfigs.length, 2, "both track preflights fetched their independent saved config");
const newestConfig = {
  priority: ["AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS", "LOSSLESS"],
  audio_device: readinessRaceSandbox.REQUIRED_AUDIO_DEVICE,
};
deferredReadinessConfigs[1](newestConfig);
for (let attempt = 0; attempt < 12 && readinessRacePlayRequests.length < 1; attempt += 1) await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(readinessRacePlayRequests.length, 1, "newest track dispatches after receiving its config");
assert.equal(readinessRacePlayRequests[0].startPaused, true,
  "the service is asked to keep the loaded stream paused until TIDAL is revalidated");
assert.equal(await newestConfigPlay, true);
assert.deepEqual(readinessRaceCommands.map(({ endpoint }) => endpoint), ["resume"],
  "a still-playing TIDAL track resumes libmpv only after the paused load returns");
assert.deepEqual(readinessRaceSandbox.currentConfig.priority, newestConfig.priority);
deferredReadinessConfigs[0]({
  priority: ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"],
  audio_device: readinessRaceSandbox.REQUIRED_AUDIO_DEVICE,
});
assert.equal(await staleConfigPlay, false, "superseded track remains cancelled");
assert.deepEqual(readinessRaceSandbox.currentConfig.priority, newestConfig.priority,
  "late stale config cannot overwrite the newest playback config");

const observedTidalEvents = [];
const observerCallbacks = {};
const observerCleanups = new Set();
const observerDocumentListeners = new Map();
const observerTimelineSlider = {
  id: "progressBar",
  value: "33.5",
  title: "",
  closest() { return this; },
  matches() { return true; },
  getAttribute() { return null; },
};
const observerTimelineFooter = {
  contains(element) { return element === observerTimelineSlider; },
};
let observedTidalPosition = 0;
const observerSandbox = {
  Date: { now: () => 1000000 },
  desiredPlaybackIntent: null,
  mirroringTidalPlaybackState: false,
  enginePlayPending: null,
  enginePlayPendingTrackId: null,
  enginePlayRequestId: null,
  enginePlayRequestTrackId: null,
  enginePlayAllowPaused: false,
  prepareNextSelectionPaused: false,
  pausedSelectionPending: null,
  pausedSelectionConfirmedTrackId: null,
  pausedSelectionUserPlayAuthorization: null,
  currentStatusData: {},
  lastMirroredUiTime: null,
  observerTimelineFooter,
  document: {
    getElementById() { return null; },
    addEventListener(name, callback, capture) {
      assert.equal(capture, true, "native seek observer uses capture phase");
      observerDocumentListeners.set(name, callback);
    },
    removeEventListener(name, callback) {
      if (observerDocumentListeners.get(name) === callback) observerDocumentListeners.delete(name);
      observedTidalEvents.push("native-timeline-unsubscribed");
    },
  },
  window: { luna: { lib: {
    PlayState: { state: "PLAYING", desiredState: "NOT_PLAYING", playQueue: {
      currentIndex: 0, elements: [{ mediaItemId: "9001" }],
    }, onState(unloads, callback) {
      observerCallbacks.state = callback;
      const cleanup = () => { observedTidalEvents.push("state-unsubscribed"); };
      unloads.add(cleanup);
      return cleanup;
    } },
    MediaItem: { onMediaTransition(unloads, callback) {
      observerCallbacks.transition = callback;
      const cleanup = () => { observedTidalEvents.push("transition-unsubscribed"); };
      unloads.add(cleanup);
      return cleanup;
    } },
    redux: { intercept(action, unloads, callback) {
      observerCallbacks[action] = callback;
      const cleanup = () => { observedTidalEvents.push("seek-unsubscribed"); };
      unloads.add(cleanup);
      return cleanup;
    }, actions: {
      "playbackControls/SET_DESIRED_PAUSE_STATE": (paused) => {
        observerSandbox.window.luna.lib.PlayState.desiredState = paused ? "PAUSED" : "PLAYING";
        observedTidalEvents.push(["set-desired-pause", paused]);
      },
      "playbackControls/SET_PLAYBACK_STATE": (value) => {
        observerSandbox.window.luna.lib.PlayState.state = value;
        observedTidalEvents.push(["set-state", value]);
      },
      "playbackControls/TIME_UPDATE": (value) => {
        observedTidalPosition = value;
        observedTidalEvents.push(["time", value]);
      },
    } },
  } } },
  unloads: observerCleanups,
};
vm.createContext(observerSandbox);
vm.runInContext(`
  const PLAY_INTENT_RECONCILIATION_WINDOW_MS = 1000;
  const EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS = 15000;
  const supportedFormats = ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"];
  const NATIVE_TIMELINE_DIAGNOSTIC_INTERVAL_MS = 2000;
  globalThis.nativeTimelineDiagnosticWindow = {
    startedAt: null, previousFrameAt: null, frames: 0, syncWrites: 0,
    reduxUpdates: 0, sampleAgeCount: 0, sampleBracketCount: 0,
    preWriteTargetDifferenceCount: 0, postWriteReadbackDifferenceCount: 0, skipped: {},
    maxSampleAgeMs: 0, maxFrameIntervalMs: 0, maxSampleBracketHalfWidthMs: 0,
    maxPreWriteTargetDifferenceMs: 0, maxPostWriteReadbackDifferenceMs: 0, last: null,
    domCommitCount: 0, maxDomCommitDelayMs: 0, maxDomSliderTargetDifferenceMs: 0,
    lastDomCommit: null,
  };
  globalThis.nativeTimelineDispatchSequence = 0;
  globalThis.nativeTimelineDispatchHistory = [];
  function tracePlaybackEvent() {}
  function queueMicrotask(callback) { Promise.resolve().then(callback); }
  function getMonotonicNow() { return 8000; }
  const MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS = 250;
  let nativePlayerCommandHookInstalled = false;
  globalThis.desiredPlaybackIntent = null;
  globalThis.playIntentChangedAt = 0;
  let pendingExplicitPlayIntent = null;
  let transportIntentRevision = 0;
  function interceptNativePlayerCommands() { nativePlayerCommandHookInstalled = true; return Promise.resolve(true); }
  function readNativeMediaClockDiagnostic() { return { testStub: true }; }
  ${extractFunction("parsePlaybackPosition")}
  ${extractFunction("resolveEngineSampleAnchor")}
  ${extractFunction("getFreshNativeTimelinePosition")}
  ${extractFunction("getEnginePositionAtCurrentTime")}
  function getPlaybackFooter() { return globalThis.observerTimelineFooter; }
  ${extractFunction("getNativeTimelinePosition")}
  ${extractFunction("getTidalPlayState")}
  ${extractFunction("dispatchTidalAction")}
  ${extractFunction("recordNativeTimelineDiagnostic")}
  ${extractFunction("setTidalUiPlaybackState")}
  ${extractFunction("setTidalPlaybackIntent")}
  ${extractFunction("hasRecentExplicitPlayIntent")}
  ${extractFunction("hasPendingExplicitPlayIntent")}
  ${extractFunction("consumePausedSelectionPreparation")}
  ${extractFunction("getQueuedTrackId")}
  ${extractFunction("isPausedSelectionDiagnosticsReady")}
  ${extractFunction("hasPausedSelectionUserPlayAuthorization")}
  ${extractFunction("recordPausedSelectionUserPlayAuthorization")}
  ${extractFunction("isEnginePlayStartupPending")}
  ${extractFunction("mirrorEnginePlaybackStateToTidal")}
  ${extractFunction("mirrorEngineClockToTidal")}
  ${extractFunction("forwardTidalVolume")}
  ${extractFunction("installLyricsClickListener")}  ${extractFunction("installPlaybackObservers")}
  function updatePausedSelectionUi() {}
  let currentStatusData = globalThis.currentStatusData;
  globalThis.setCurrentStatusData = (value) => { currentStatusData = value; };
  const sentVolumeCommands = [];
  function getCurrentTrackId() { return 42; }
  function sendEngineCommand(...args) { sentVolumeCommands.push(args); return Promise.resolve(true); }
  globalThis.getSentVolumeCommands = () => sentVolumeCommands;
  globalThis.installPlaybackObservers = installPlaybackObservers;
`, observerSandbox);
observerSandbox.installPlaybackObservers(
  (...args) => observedTidalEvents.push(["playback-change", ...args]),
  (position, source) => observedTidalEvents.push(["seek", position, source]),
);
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "initial transport intent follows desiredState when actual state is stale");
observerCallbacks.state("PAUSED");
observerCallbacks.transition({ id: 42 });
assert.equal(observerCallbacks["player/ENSURE_PLAYBACK_PRIVILEGES"](), true,
  "native TIDAL streaming privileges are intercepted while the add-on owns audio");
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "the native footer Play control is intercepted instead of starting a second decoder");
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PLAYING",
  "intercepted Play keeps TIDAL's visible state synchronized with add-on intent");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PLAYING",
  "intercepted Play updates TIDAL's separate desired playback state");
assert.ok(observerSandbox.playIntentChangedAt > 0,
  "an explicit Play arms the startup zero-seek guard");
assert.equal(observerCallbacks["playbackControls/PAUSE"](), true,
  "the native footer Pause control is intercepted");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PAUSED",
  "intercepted Pause updates TIDAL's separate desired playback state");
assert.equal(observerCallbacks["playbackControls/SET_DESIRED_PAUSE_STATE"](true), true,
  "direct TIDAL desired-pause updates are intercepted even when PAUSE is bypassed");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "a direct desired-pause update immediately pauses the engine intent");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PAUSED",
  "a direct desired-pause update keeps TIDAL's UI paused");
assert.equal(observerCallbacks["playbackControls/SET_DESIRED_PAUSE_STATE"](false), true,
  "the native desired-unpause update is intercepted as an explicit Play intent");
assert.equal(observerSandbox.desiredPlaybackIntent, true,
  "an explicit desired-unpause update resumes the engine intent");
assert.equal(observerCallbacks["playbackControls/SET_DESIRED_PAUSE_STATE"](true), true,
  "a second direct pause is accepted after a manual resume");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "the second direct pause cannot be overwritten by the prior Play intent");
observerSandbox.setTidalPlaybackIntent(true);
observerSandbox.setTidalUiPlaybackState("PAUSED");
observerSandbox.mirrorEnginePlaybackStateToTidal({
  trackId: 42, playbackState: "prepared", paused: true, timePos: 12.75,
});
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PLAYING",
  "a just-issued Play intent survives a paused prepared-engine snapshot while /resume is dispatched");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PLAYING",
  "resume handoff restores both native PlayState values after a stale paused mirror");
observerSandbox.setTidalPlaybackIntent(false);
observerSandbox.mirrorEnginePlaybackStateToTidal({
  trackId: 42, playbackState: "prepared", paused: true, timePos: 12.75,
});
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "an explicit Pause still wins while the prepared engine is paused");
observerSandbox.setTidalPlaybackIntent(true);
observerSandbox.enginePlayPending = Promise.resolve(true);
observerSandbox.enginePlayPendingTrackId = "42";
observerSandbox.enginePlayRequestId = "startup-42";
observerSandbox.enginePlayRequestTrackId = "42";
observerSandbox.enginePlayAllowPaused = false;
observerSandbox.mirrorEnginePlaybackStateToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 12.75,
});
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PLAYING",
  "the prepared-paused engine state cannot overwrite TIDAL's Play intent during the matching startup request");
observerSandbox.enginePlayAllowPaused = true;
observerSandbox.mirrorEnginePlaybackStateToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 12.75,
});
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "deliberate diagnostics-only preparation remains paused instead of inheriting the startup guard");
observerSandbox.enginePlayPending = null;
observerSandbox.enginePlayPendingTrackId = null;
observerSandbox.enginePlayRequestId = null;
observerSandbox.enginePlayRequestTrackId = null;
observerSandbox.enginePlayAllowPaused = false;
observerSandbox.setTidalPlaybackIntent(false);
observerSandbox.mirroringTidalPlaybackState = true;
assert.equal(observerCallbacks["playbackControls/SET_DESIRED_PAUSE_STATE"](false), false,
  "plugin-originated state mirrors pass through without becoming user transport intent");
observerSandbox.mirroringTidalPlaybackState = false;
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "a plugin-originated PLAYING mirror cannot clear a user pause intent");
observerSandbox.prepareNextSelectionPaused = true;
assert.equal(observerCallbacks["playbackControls/PLAY"](), false,
  "one-shot paused selection permits TIDAL's original PLAY dispatch to commit the queued track");
assert.equal(observerSandbox.prepareNextSelectionPaused, false,
  "one-shot paused preparation is consumed by exactly one Play action");
assert.equal(observerSandbox.pausedSelectionPending.trackId, "9001",
  "the pending selection is pinned to the verified TIDAL queue identity");
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "armed track selection remains paused in TIDAL");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PAUSED",
  "armed track selection never sets TIDAL's desired state to PLAYING");
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a duplicate PLAY cannot resume while the armed selection is still pending");
assert.equal(observerSandbox.pausedSelectionPending.trackId, "9001",
  "the duplicate PLAY keeps the selected track pinned until sync confirms it");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "a duplicate PLAY during selection preparation preserves paused intent");
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "the duplicate PLAY cannot change TIDAL's visible state during preparation");
observerSandbox.pausedSelectionPending = null;
observerSandbox.pausedSelectionConfirmedTrackId = "9001";
assert.equal(vm.runInContext("pausedSelectionPending", observerSandbox), null,
  "the selection is confirmed before the duplicate-play readiness checks");
observerSandbox.setCurrentStatusData({ trackId: 9001, paused: true });
observerSandbox.window.luna.lib.PlayState.state = "PLAYING";
observerSandbox.window.luna.lib.PlayState.desiredState = "PLAYING";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a rapid duplicate PLAY remains blocked after TIDAL confirms the row but before matching diagnostics are ready");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "confirmation alone cannot release the paused selection latch");
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "an early duplicate PLAY keeps the native UI paused while stream diagnostics load");
observerSandbox.setCurrentStatusData({
  trackId: 9001, paused: true, format: "LOSSLESS", sourceCodec: "FLAC",
  activeDecoder: "flac", sourceBitrate: 1000000, sourceSampleRate: 44100,
  sourceChannels: 2, streamProxyFailures: 0,
});
assert.equal(vm.runInContext("isPausedSelectionDiagnosticsReady(9001)", observerSandbox), true,
  "complete paused status is recognized for the confirmed selected track");
observerSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "9002";
observerSandbox.window.luna.lib.PlayState.state = "PLAYING";
observerSandbox.window.luna.lib.PlayState.desiredState = "PLAYING";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a confirmed-track mismatch is intercepted even when TIDAL reports PLAYING");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "a queue mismatch cannot turn on playback intent");
assert.equal(observerSandbox.pausedSelectionConfirmedTrackId, "9001",
  "a mismatched PLAY does not discard the confirmed selection latch");
observerSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "9001";
observerSandbox.window.luna.lib.PlayState.state = "PLAYING";
observerSandbox.window.luna.lib.PlayState.desiredState = "PLAYING";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a delayed PLAY without a fresh user action stays blocked even after diagnostics are ready");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "diagnostics readiness alone never resumes the stream");
assert.equal(vm.runInContext(`recordPausedSelectionUserPlayAuthorization({
  isTrusted: true,
  target: { closest() { return {
    getAttribute(name) { return name === "aria-label" ? "Play" : null; },
    textContent: "",
  }; } },
})`, observerSandbox), true,
"a trusted TIDAL Play control click authorizes the exact ready selection");
observerSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "9002";
observerSandbox.window.luna.lib.PlayState.state = "PLAYING";
observerSandbox.window.luna.lib.PlayState.desiredState = "PLAYING";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a queue change inside the short Play authorization window remains blocked");
observerSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "9001";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "returning to the confirmed track cannot reuse authorization after a queue mismatch");
assert.equal(observerSandbox.desiredPlaybackIntent, false,
  "a queue mismatch revokes the previous trusted Play authorization");
assert.equal(vm.runInContext(`recordPausedSelectionUserPlayAuthorization({
  isTrusted: true,
  target: { closest() { return {
    getAttribute(name) { return name === "aria-label" ? "Play" : null; },
    textContent: "",
  }; } },
})`, observerSandbox), true,
"a fresh trusted Play click reauthorizes the still-matching ready selection");
observerSandbox.window.luna.lib.PlayState.state = "PLAYING";
observerSandbox.window.luna.lib.PlayState.desiredState = "PLAYING";
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "a deliberate PLAY is intercepted for the engine once the exact selected stream has complete paused diagnostics");
assert.equal(observerSandbox.pausedSelectionConfirmedTrackId, null,
  "the confirmed selection latch is released only by a ready matching stream");
assert.equal(observerSandbox.desiredPlaybackIntent, true,
  "a deliberate PLAY after diagnostics are ready starts the matching engine stream");
observerSandbox.setCurrentStatusData({ trackId: 42, playbackGeneration: 4, requestId: "r42" });
observerSandbox.prepareNextSelectionPaused = true;
observerSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = null;
assert.equal(observerCallbacks["playbackControls/PLAY"](), true,
  "missing queue identity blocks the original PLAY action to prevent starting an unknown track");
assert.equal(observerSandbox.pausedSelectionPending, null,
  "a failed subsequent selection clears stale pending identity so no previous stream can be prepared");
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "missing queue identity leaves TIDAL paused");
assert.equal(observerCallbacks["playbackControls/SEEK"](33.5), true,
  "a native seek is cancelled after it is forwarded to libmpv");
observerDocumentListeners.get("input")({ target: observerTimelineSlider });
assert.ok(observedTidalEvents.some((event) => Array.isArray(event)
  && event[0] === "seek" && event[1] === 33.5 && event[2] === "native-ui"),
"TIDAL's progressBar id preserves seek provenance when Redux SEEK is bypassed");
observerDocumentListeners.get("change")({ target: {
  ...observerTimelineSlider,
  getAttribute(name) { return name === "aria-label" ? "Volume" : null; },
} });
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "seek" && event[2] === "native-ui").length, 1,
"the volume slider is not mistaken for a native timeline seek");
assert.equal(await observerSandbox.forwardTidalVolume({ volume: 35 }), true,
  "TIDAL's native volume control is forwarded to the matching engine track");
assert.equal(JSON.stringify(observerSandbox.getSentVolumeCommands()), JSON.stringify([["volume", {
  volume: 35, trackId: 42, playbackGeneration: 4, requestId: "r42",
}]]), "volume commands carry the active playback identity");
assert.equal(await observerSandbox.forwardTidalVolume(101), false,
  "invalid TIDAL volume values cannot reach the engine");
const nativePlaybackChanges = [];
const nativePlaybackTraces = [];
const nativePlayState = {
  state: "PLAYING",
  desiredState: "PLAYING",
  playQueue: { currentIndex: 0, elements: [{ mediaItemId: "227401768" }] },
};
const nativeCommandSandbox = {
  window: {
    __ipcRenderer: { send(...args) { nativeIpcCalls.push(args); } },
    luna: { lib: {
      PlayState: nativePlayState,
      ipcRenderer: { send(...args) { nativeIpcCalls.push(args); } },
      redux: { actions: {
      "playbackControls/SET_DESIRED_PAUSE_STATE": (paused) => {
        nativePlayState.desiredState = paused ? "PAUSED" : "PLAYING";
      },
      "playbackControls/SET_PLAYBACK_STATE": (value) => { nativePlayState.state = value; },
      } },
    } },
  },
  unloads: new Set(),
  prepareNextSelectionPaused: true,
  pausedSelectionPending: null,
  pausedSelectionConfirmedTrackId: null,
  pausedSelectionUserPlayAuthorization: null,
  currentStatusData: {},
  desiredPlaybackIntent: true,
  playIntentChangedAt: 0,
  pluginDisposed: false,
  readNativeMediaClockDiagnostic: () => ({ testStub: true }),
  testUpdatePausedSelectionUi(message) { this.pausedSelectionMessage = message; },
  testOnPlaybackChange() { nativePlaybackChanges.push(true); },
  testTracePlaybackEvent(event, detail) { nativePlaybackTraces.push({ event, detail }); },
  console: { info() {}, warn() {} },
};
vm.createContext(nativeCommandSandbox);
vm.runInContext(`
  let prepareNextSelectionPaused = true;
  let pausedSelectionPending = null;
  let pausedSelectionConfirmedTrackId = null;
    let stalledPlayAuthorization = null;
  let pausedSelectionUserPlayAuthorization = null;
  let desiredPlaybackIntent = true;
  let mirroringTidalPlaybackState = false;
  let playIntentChangedAt = 0;
  let pendingExplicitPlayIntent = null;
  let transportIntentRevision = 0;
  const pluginDisposed = false;
  const unloads = globalThis.unloads;
  const window = globalThis.window;
  function tracePlaybackEvent(...args) { globalThis.testTracePlaybackEvent(...args); }
  function updatePausedSelectionUi(message) { globalThis.testUpdatePausedSelectionUi(message); }
  function getTidalPlayState() { return window.luna.lib.PlayState; }
  function getCurrentTrackId() { return "227401768"; }
  const supportedFormats = ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"];
  function dispatchTidalAction(type, payload) {
    const action = window.luna.lib.redux.actions[type];
    if (typeof action !== "function") return false;
    action(payload);
    return true;
  }
  ${extractFunction("setTidalUiPlaybackState")}
  ${extractFunction("setTidalPlaybackIntent")}
  ${extractFunction("getQueuedTrackId")}
  ${extractFunction("isPausedSelectionDiagnosticsReady")}
  ${extractFunction("hasPausedSelectionUserPlayAuthorization")}
  ${extractFunction("recordPausedSelectionUserPlayAuthorization")}
  ${extractFunction("handleNativePlayerPlay")}
  globalThis.getNativePendingSelection = () => pausedSelectionPending;
  globalThis.getNativePlaybackIntent = () => desiredPlaybackIntent;
  globalThis.setNativePausedSelectionArmed = (value) => { prepareNextSelectionPaused = value; };
  globalThis.setNativeConfirmedTrackId = (value) => { pausedSelectionConfirmedTrackId = value; };
  globalThis.getNativeConfirmedTrackId = () => pausedSelectionConfirmedTrackId;
  globalThis.setNativePlayAuthorization = (value) => { pausedSelectionUserPlayAuthorization = value; };
`, nativeCommandSandbox);
nativeCommandSandbox.handleNativePlayerPlay(nativeCommandSandbox.testOnPlaybackChange);
assert.equal(nativeCommandSandbox.getNativePendingSelection().trackId, "227401768",
  "a paused row selection captures the verified current queue identity");
assert.equal(nativeCommandSandbox.getNativePlaybackIntent(), false,
  "the blocked native start converts an armed selection to paused intent");
assert.equal(nativePlayState.state, "PAUSED");
assert.equal(nativePlayState.desiredState, "PAUSED");
assert.ok(nativePlaybackChanges.length > 0, "the paused selection requests an immediate sync");
nativeCommandSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = null;
nativeCommandSandbox.setNativePausedSelectionArmed(true);
nativeCommandSandbox.handleNativePlayerPlay(nativeCommandSandbox.testOnPlaybackChange);
assert.equal(nativeCommandSandbox.getNativePendingSelection(), null,
  "an unknown queue identity cannot leave an older track pending");
nativeCommandSandbox.setNativeConfirmedTrackId("227401768");
nativeCommandSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "9988";
nativePlayState.state = "PLAYING";
nativePlayState.desiredState = "PLAYING";
nativeCommandSandbox.handleNativePlayerPlay(nativeCommandSandbox.testOnPlaybackChange);
assert.equal(nativeCommandSandbox.getNativePlaybackIntent(), false,
  "native Play remains fail-closed when the queue identity differs from the confirmed selection");
assert.equal(nativePlayState.state, "PAUSED",
  "a native Play event for a mismatched queue cannot start audio");
assert.match(nativeCommandSandbox.pausedSelectionMessage, /queue changed/i,
  "the native mismatch gate reports why playback remains paused");
nativeCommandSandbox.window.luna.lib.PlayState.playQueue.elements[0].mediaItemId = "227401768";
nativeCommandSandbox.setNativeConfirmedTrackId("227401768");
nativeCommandSandbox.currentStatusData = {
  trackId: 227401768, paused: true, format: "AC4_IMS", sourceCodec: "ac-4",
  activeDecoder: "pcm_s16le", sourceBitrate: 256000, sourceSampleRate: 48000,
  sourceChannels: 2, streamProxyFailures: 0,
};
nativeCommandSandbox.setNativePlayAuthorization({
  trackId: "227401768", expiresAt: Date.now() + 1000,
});
nativePlayState.state = "PLAYING";
nativePlayState.desiredState = "PLAYING";
nativeCommandSandbox.pausedSelectionMessage = "TIDAL confirmed the selected track. Preparing its stream paused.";
nativeCommandSandbox.handleNativePlayerPlay(nativeCommandSandbox.testOnPlaybackChange);
assert.equal(nativeCommandSandbox.getNativeConfirmedTrackId(), null,
  "a trusted Play after matching diagnostics releases the paused-selection latch");
assert.equal(nativeCommandSandbox.pausedSelectionMessage, undefined,
  "successful playback clears the stale paused-preparation caption");

const nativeGuardSourceMatch = source.match(/const NATIVE_PLAYER_GUARD_MODULE_CODE = String\.raw`([\s\S]*?)`;/);
assert.ok(nativeGuardSourceMatch, "the plugin embeds its TidaLuna native guard module");
const nativeGuardCalls = [];
const nativeGuardMainCalls = [];
class MockPlayerController {}
MockPlayerController.prototype.sendPlayerMessage = function sendPlayerMessage(message) {
  nativeGuardMainCalls.push(["native-player-send", message]);
};
MockPlayerController.prototype.onPlayerMessage = function originalPlayerMessage(_event, message) {
  this.sendPlayerMessage(message);
};
const nativeGuardSandbox = {
  module: { exports: {} },
  process: { resourcesPath: "C:\\TIDAL\\resources" },
  require(name) {
    if (name === "path") return path;
    assert.match(name, /original\.asar.*PlayerController\.js$/,
      "the guard resolves only the installed TIDAL PlayerController module");
    return { default: MockPlayerController };
  },
  globalThis: null,
  console: { error() {} },
};
nativeGuardSandbox.globalThis = nativeGuardSandbox;
nativeGuardSandbox.luna = {
  modules: {},
  sendToRender(...args) { nativeGuardCalls.push(args); },
};
vm.runInNewContext(nativeGuardSourceMatch[1], nativeGuardSandbox);
const nativeGuardModule = nativeGuardSandbox.module.exports;
assert.equal(nativeGuardModule.install("reload-a").installed, true,
  "the TidaLuna native module installs the guard on TIDAL's native player controller");
const playerController = new MockPlayerController();
playerController.onPlayerMessage({}, JSON.stringify({ command: "media.play" }));
assert.equal(nativeGuardMainCalls.length, 0,
  "media.play is stopped before the original TIDAL handler can reach native Player.send");
assert.equal(nativeGuardCalls.length, 1,
  "a blocked media.play intent is forwarded to the renderer for libmpv control");
playerController.onPlayerMessage({}, JSON.stringify({ command: "media.pause" }));
assert.equal(nativeGuardMainCalls.length, 1,
  "pause commands continue through TIDAL's original native-player handler");
assert.equal(nativeGuardModule.install("reload-b").installed, true,
  "reloading the renderer plugin can share the installed main-process guard");
MockPlayerController.prototype.onPlayerMessage = function otherPluginPlayerMessage(...args) {
  this.sendPlayerMessage(args[1]);
};
playerController.onPlayerMessage({}, JSON.stringify({ command: "media.play" }));
assert.equal(nativeGuardMainCalls.length, 1,
  "later method assignment remains behind the synchronous play guard");
nativeGuardModule.uninstall("reload-a");
assert.equal(typeof Object.getOwnPropertyDescriptor(MockPlayerController.prototype, "onPlayerMessage").get, "function",
  "unloading one renderer instance keeps the guard while another instance still uses it");
nativeGuardModule.uninstall("reload-b");
assert.equal(Object.getOwnPropertyDescriptor(MockPlayerController.prototype, "onPlayerMessage").value,
  MockPlayerController.prototype.onPlayerMessage,
  "final plugin unload restores a callable native player method");
playerController.onPlayerMessage({}, JSON.stringify({ command: "media.play" }));
assert.equal(nativeGuardMainCalls.length, 2,
  "after final unload, native player dispatch is restored");
assert.deepEqual(observedTidalEvents.slice(0, 2), [
  ["playback-change", "PAUSED"],
  ["playback-change", { id: 42 }],
], "TidaLuna state, track-transition, and SEEK actions reach the immediate sync observers");
assert.ok(observedTidalEvents.some((event) => Array.isArray(event)
  && event[0] === "seek" && event[1] === 33.5),
"TidaLuna seeks are delivered to the immediate libmpv observer");
const expectedCleanupCount = observerCleanups.size;
for (const cleanup of observerCleanups) cleanup();
assert.ok(observedTidalEvents.filter((event) => String(event).includes("unsubscribed")).length >= expectedCleanupCount,
  "all TidaLuna observers, trusted Play listeners, and transport interceptions register unload cleanup callbacks");
assert.equal(observerDocumentListeners.size, 0,
  "native timeline listeners are removed cleanly when the plugin unloads");
vm.runInContext(extractFunction("mirrorEngineStateToTidal"), observerSandbox);
observerSandbox.desiredPlaybackIntent = true;
assert.equal(observerSandbox.mirrorEngineStateToTidal({
  trackId: 42, playbackState: "playing", paused: false, timePos: 12.75,
}, 42), true);
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PLAYING",
  "libmpv's confirmed running state is reflected in TIDAL's native playback state");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PLAYING",
  "libmpv state mirroring also corrects TIDAL's desired playback state");
assert.ok(observedTidalEvents.some((event) => Array.isArray(event)
  && event[0] === "time" && event[1] === 12.75),
"libmpv's clock is published through TIDAL TIME_UPDATE");
assert.match(extractFunction("applyNativeTimelinePosition"),
  /mirrorEngineDurationToTidal\(\s*engineDuration/,
  "the native footer timeline projects the verified engine duration into TidaLuna");
const timeEventsBeforeStateOnlyProjection = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.desiredPlaybackIntent = false;
observerSandbox.mirrorEnginePlaybackStateToTidal({
  trackId: 42, playbackState: "playing", paused: false, timePos: 13,
});
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "state-only projection preserves a pending pause intent");
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event) && event[0] === "time").length,
  timeEventsBeforeStateOnlyProjection,
  "playback-state projection does not mutate the timeline");
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "playing", paused: false, timePos: 13,
}, 42, 12.75);
assert.ok(observedTidalEvents.some((event) => Array.isArray(event)
  && event[0] === "time" && event[1] === 13),
"clock projection remains available after seek analysis");
const timeEventsBeforeAgeCompensation = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.lastMirroredUiTime = null;
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 20, duration: 100, timePosSampledAtUnixMs: 999900,
}, 42, 20);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").at(-1)[1], 20.1,
"Redux clock projection compensates a 100 ms service-response delay using the engine sample timestamp");
assert.ok(observerSandbox.nativeTimelineDiagnosticWindow.reduxUpdates > 0,
  "successful native TIME_UPDATE dispatches are counted by the runtime diagnostics");
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, timeEventsBeforeAgeCompensation + 1,
"the age-compensated playing position is dispatched once");
observerSandbox.lastMirroredUiTime = null;
const timeEventsBeforeStaleProjection = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 21, duration: 100, timePosSampledAtUnixMs: 999700,
}, 42, 21);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, timeEventsBeforeStaleProjection,
"a service sample older than the freshness limit does not update TIDAL Redux");
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 22, duration: 100,
}, 42, 22);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").at(-1)[1], 22,
"legacy status payloads without a sample timestamp retain their unprojected position");
const timeEventsBeforePausedPrecision = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.lastMirroredUiTime = 28.734555;
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 28.782,
}, 42, 28.734555);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, timeEventsBeforePausedPrecision + 1,
"paused engine clock corrections project sub-50ms settle offsets");
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").at(-1)[1], 28.782,
"paused TIDAL time is corrected to the settled libmpv position");
const timeEventsBeforePlayingJitter = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.lastMirroredUiTime = 28.782;
observerSandbox.mirrorEngineClockToTidal({
  trackId: 42, playbackState: "playing", paused: false, timePos: 28.82,
}, 42, 28.782);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, timeEventsBeforePlayingJitter + 1,
"moving-clock corrections below 50ms are still mirrored at millisecond resolution");
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").at(-1)[1], 28.82,
"the sub-50ms active correction publishes the current engine position");
observedTidalPosition = 0; // Simulate TIDAL's native player resetting its clock after the first mirror.
observerSandbox.mirrorEngineStateToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 12.75,
}, 42, observedTidalPosition);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time" && event[1] === 12.75).length, 2,
"a stale TIDAL clock is corrected even when libmpv's time has not changed");
observerSandbox.desiredPlaybackIntent = false;
observerSandbox.mirrorEngineStateToTidal({
  trackId: 42, playbackState: "playing", paused: false, timePos: 13.0,
}, 42, observedTidalPosition);
assert.equal(observerSandbox.window.luna.lib.PlayState.state, "PAUSED",
  "a pending pause intent cannot be overwritten by a stale running engine sample");
assert.equal(observerSandbox.window.luna.lib.PlayState.desiredState, "PAUSED",
  "a pending pause intent remains authoritative for TIDAL's UI state");
assert.equal(observerSandbox.mirrorEngineStateToTidal({
  trackId: 43, playbackState: "playing", paused: false, timePos: 5,
}, 42), false, "engine telemetry from a different track cannot overwrite TIDAL state");
const mirroredTimeUpdateCount = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
for (const invalidPosition of [null, undefined, "", Number.NaN]) {
  observerSandbox.mirrorEngineStateToTidal({
    trackId: 42, playbackState: "paused", paused: true, timePos: invalidPosition,
  }, 42, 0);
}
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, mirroredTimeUpdateCount,
"null, absent, empty, or NaN engine timestamps never fabricate TIME_UPDATE(0)");
observerSandbox.mirrorEngineStateToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 0,
}, 42, 12.75);
assert.ok(observedTidalEvents.some((event) => Array.isArray(event)
  && event[0] === "time" && event[1] === 0),
"a genuine engine position of zero remains publishable");
const clockEventsBeforeHold = observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length;
observerSandbox.mirrorEngineStateToTidal({
  trackId: 42, playbackState: "paused", paused: true, timePos: 209.599826,
}, 42, 104.71616, true);
assert.equal(observedTidalEvents.filter((event) => Array.isArray(event)
  && event[0] === "time").length, clockEventsBeforeHold,
"a reconciliation hold mirrors state without overwriting the seek target");

const resumeGuardSeekCommands = [];
const resumeGuardSeekEvents = [];
const resumeGuardSeekSandbox = {
  desiredPlaybackIntent: true,
  playIntentChangedAt: 1000,
  currentStatusData: {
    trackId: 42, playbackGeneration: 4, requestId: "r42",
    serviceInstanceId: "svc-1", mpvInstanceId: "mpv-1",
    playbackState: "prepared", paused: true, timePos: 160.61,
  },
  pluginDisposed: false,
  previousEngineSample: { trackId: 42, at: 1000, tidalPosition: 0, enginePosition: 160.61, tidalPlaying: false },
  lastTidalSeekIntent: null,
  seekToEndPlaybackIdentity: null,
  seekToEndTargetPosition: null,
  seekToEndProgressObserved: false,
  previousEngineSample: null,
  lastMirroredUiTime: null,
  Date: { now: () => 1250 },
  console: { info: (message) => resumeGuardSeekEvents.push(["info", message]) },
  dispatchTidalAction: (type, value) => { resumeGuardSeekEvents.push([type, value]); return true; },
  getTidalPlaybackSnapshot: () => ({ trackId: 42 }),
  getCurrentTrackId: () => 42,
  sendEngineCommand: async (...args) => { resumeGuardSeekCommands.push(args); return true; },
};
vm.createContext(resumeGuardSeekSandbox);
vm.runInContext(`const PLAY_INTENT_RECONCILIATION_WINDOW_MS = 1000;\nfunction tracePlaybackEvent() {}\n${extractFunction("parsePlaybackPosition")}\n${extractFunction("getPlaybackInstanceIdentity")}\n${extractFunction("hasRecentTidalSeekIntent")}\n${extractFunction("handleImmediateTidalSeek")}`, resumeGuardSeekSandbox);
assert.equal(await resumeGuardSeekSandbox.handleImmediateTidalSeek(0), true,
  "the transient zero seek emitted during Play startup is consumed");
assert.equal(resumeGuardSeekCommands.length, 0,
  "Play startup cannot send a zero seek to a prepared libmpv stream");
assert.ok(resumeGuardSeekEvents.some((event) => event[0] === "playbackControls/TIME_UPDATE" && event[1] === 160.61),
  "the prepared engine position is restored to TIDAL after its startup reset");
resumeGuardSeekSandbox.lastTidalSeekIntent = null;
assert.equal(await resumeGuardSeekSandbox.handleImmediateTidalSeek(0, "native-ui"), true,
  "an explicit native UI seek to zero bypasses the transient Play-startup zero guard");
assert.equal(JSON.stringify(resumeGuardSeekCommands.at(-1)), JSON.stringify(["seek", {
  timePos: 0, trackId: 42, playbackGeneration: 4, requestId: "r42", expectedPaused: true,
}]), "native UI zero seek reaches libmpv with the active playback identity");
assert.equal(resumeGuardSeekEvents.filter((event) => event[0] === "playbackControls/TIME_UPDATE" && event[1] === 160.61).length, 1,
  "an intentional native zero seek is not overwritten by the startup position mirror");
resumeGuardSeekCommands.length = 0;
resumeGuardSeekSandbox.lastTidalSeekIntent = null;
resumeGuardSeekSandbox.playIntentChangedAt = 0;
assert.equal(await resumeGuardSeekSandbox.handleImmediateTidalSeek(0), true,
  "an intentional seek to zero still reaches libmpv after the Play transition guard expires");
assert.equal(JSON.stringify(resumeGuardSeekCommands.at(-1)), JSON.stringify(["seek", {
  timePos: 0, trackId: 42, playbackGeneration: 4, requestId: "r42", expectedPaused: true,
}]), "ordinary zero seeks preserve the active playback identity");

const durationMirrorEvents = [];
let durationMirrorNow = 0;
const durationMirrorPlayState = {
  playbackControls: {
    mediaProduct: { productId: 227401769 },
    playbackContext: { actualProductId: 227401769, actualDuration: 0 },
  },
};
const durationMirrorSandbox = {
  currentStatusData: { trackId: 227401769 },
  lastMirroredUiDurationKey: null,
  lastMirroredUiDurationAttemptKey: null,
  lastMirroredUiDurationAttemptAt: -Infinity,
  lastTidalDurationMirrorDiagnostic: null,
  getCurrentTrackId: () => 227401769,
  getTidalPlayState: () => durationMirrorPlayState,
  getMonotonicNow: () => durationMirrorNow,
  parsePlaybackPosition: (value) => {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : null;
  },
  dispatchTidalAction: (type, value) => {
    durationMirrorEvents.push([type, value]);
    durationMirrorPlayState.playbackControls.playbackContext.actualDuration = value;
    return true;
  },
};
vm.createContext(durationMirrorSandbox);
vm.runInContext(`${extractFunction("readTidalPlaybackDuration")}\n${extractFunction("mirrorEngineDurationToTidal")}`, durationMirrorSandbox);
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), true,
  "a verified engine duration is mirrored through TidaLuna's native duration action");
assert.deepEqual(durationMirrorEvents, [["playbackControls/SET_DURATION", 187.818667]]);
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), true,
  "repeated clock frames do not dispatch the same track duration again");
assert.equal(durationMirrorEvents.length, 1);
assert.equal(durationMirrorSandbox.lastTidalDurationMirrorDiagnostic.verified, true,
  "duration mirroring is counted only after TidaLuna Redux reports the matching duration");
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(0), false,
  "an empty engine duration never overwrites TIDAL's duration");
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667, 227401768), false,
  "a duration cannot be written while TIDAL and the prepared engine track differ");
durationMirrorPlayState.playbackControls.mediaProduct.productId = 227401768;
durationMirrorPlayState.playbackControls.playbackContext.actualProductId = 227401768;
durationMirrorPlayState.playbackControls.playbackContext.actualDuration = 0;
durationMirrorSandbox.currentStatusData = { trackId: 227401768 };
durationMirrorSandbox.getCurrentTrackId = () => 227401768;
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), true,
  "a new matching track receives its own duration even when the seconds are identical");
assert.equal(durationMirrorEvents.length, 2);

durationMirrorPlayState.playbackControls.playbackContext.actualDuration = 0;
durationMirrorSandbox.lastMirroredUiDurationKey = null;
durationMirrorSandbox.lastMirroredUiDurationAttemptKey = null;
durationMirrorSandbox.lastMirroredUiDurationAttemptAt = -Infinity;
durationMirrorSandbox.dispatchTidalAction = (type, value) => {
  durationMirrorEvents.push([type, value]);
  return true;
};
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), false,
  "a successful action call without a matching Redux readback is not accepted");
assert.equal(durationMirrorSandbox.lastMirroredUiDurationKey, null,
  "an unverified duration is not cached as mirrored");
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), false,
  "a failed readback is throttled instead of dispatching on every animation frame");
assert.equal(durationMirrorEvents.length, 3);
durationMirrorNow = 251;
assert.equal(durationMirrorSandbox.mirrorEngineDurationToTidal(187.818667), false,
  "a failed duration readback is retried after the bounded backoff");
assert.equal(durationMirrorEvents.length, 4);

class FakeElement {
  constructor(tagName, registry) {
    this.tagName = tagName;
    this.registry = registry;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.parentElement = null;
    this.isConnected = false;
    this.disabled = false;
    this.value = "";
    this.innerText = "";
    this.textContent = "";
    this._id = "";
    this.className = "";
  }
  set id(value) {
    this._id = value;
    if (value) this.registry.set(value, this);
  }
  get id() { return this._id; }
  get nextSibling() {
    if (!this.parentElement) return null;
    const siblings = this.parentElement.children;
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }
  appendChild(child) {
    if (child.parentElement) {
      child.parentElement.children = child.parentElement.children.filter((item) => item !== child);
    }
    child.parentElement = this;
    this.children.push(child);
    child.isConnected = this.isConnected;
    return child;
  }
  insertBefore(child, reference) {
    if (child === reference) return child;
    if (child.parentElement) {
      child.parentElement.children = child.parentElement.children.filter((item) => item !== child);
    }
    const index = reference === null ? this.children.length : this.children.indexOf(reference);
    if (index < 0) throw new Error("reference node is not a child");
    this.children.splice(index, 0, child);
    child.parentElement = this;
    child.isConnected = this.isConnected;
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) {
      if (child.id && this.registry.get(child.id) === child) this.registry.delete(child.id);
      child.parentElement = null;
      child.isConnected = false;
    }
    this.children = [];
    for (const child of children) this.appendChild(child);
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    }
    const unregister = (node) => {
      if (node.id && node.registry.get(node.id) === node) node.registry.delete(node.id);
      node.children.forEach(unregister);
    };
    unregister(this);
    this.parentElement = null;
    this.isConnected = false;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  getBoundingClientRect() {
    if (this.style.display === "none") {
      return { left: 50, top: 900, right: 50, bottom: 900, width: 0, height: 0, x: 50, y: 900 };
    }
    return { left: 50, top: 900, right: 250, bottom: 960, width: 200, height: 60, x: 50, y: 900 };
  }
  querySelectorAll(selector = "*") {
    const found = [];
    const visit = (node) => {
      for (const child of node.children) { found.push(child); visit(child); }
    };
    visit(this);
    const selectors = String(selector).split(",").map((value) => value.trim());
    return found.filter((node) => selectors.some((rule) => {
      if (rule === "*") return true;
      if (rule === "button") return String(node.tagName).toLowerCase() === "button";
      const attribute = rule.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (!attribute) return false;
      const value = attribute[1] === "class"
        ? node.className || node.getAttribute("class")
        : node.getAttribute(attribute[1]);
      return value !== null && value !== undefined
        && (attribute[2] === undefined || String(value) === attribute[2]);
    }));
  }
  contains(candidate) {
    if (candidate === this) return true;
    return this.querySelectorAll().includes(candidate);
  }
  addEventListener(name, handler) { (this.listeners ??= new Map()).set(name, handler); }
  removeEventListener(name, handler) {
    if (this.listeners?.get(name) === handler) this.listeners.delete(name);
  }
}

const registry = new Map();
const body = new FakeElement("body", registry);
body.isConnected = true;
let footerControls = null;
const document = {
  body,
  createElement: (tag) => new FakeElement(tag, registry),
  getElementById: (id) => {
    if (registry.has(id)) return registry.get(id);
    if (id === "omniphony-close-btn") {
      const close = new FakeElement("button", registry);
      close.id = id;
      return close;
    }
    return null;
  },
  querySelector: (selector) => ['[data-test="playback-controls"]', '[data-test="footer-player"]', 'footer', '[class*="playbackControls"]'].includes(selector) ? footerControls : null,
  querySelectorAll: () => sandbox.mediaElements ?? [],
};

let currentConfig = {
  priority: ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"],
  audio_device: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}",
};
let intervalCallback = null;
let intervalId = 0;
const intervals = new Map();
const clearedIntervals = new Set();
let pluginCleanup = null;
let configLoadGeneration = 0;
const supportedFormats = ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"];
const formatLabels = { EAC3_JOC: "Atmos JOC", AC4_IMS: "AC-4 IMS", HI_RES_LOSSLESS: "Hi-Res FLAC", LOSSLESS: "Lossless FLAC" };
const badgeMountDiagnostics = [];
const testConsole = Object.create(console);
testConsole.info = (...args) => {
  const prefix = "[Omniphony EX] Footer addon mount ";
  if (typeof args[0] === "string" && args[0].startsWith(prefix)) {
    badgeMountDiagnostics.push([prefix.trim(), JSON.parse(args[0].slice(prefix.length))]);
  } else {
    badgeMountDiagnostics.push(args);
  }
};
const sandbox = {
  document,
  window: {
    innerWidth: 1920, innerHeight: 985, location: { href: "https://desktop.tidal.com/" },
    addEventListener: (name, handler) => ((sandbox.windowListeners ??= new Map()).set(name, handler)),
    removeEventListener: (name, handler) => {
      if (sandbox.windowListeners?.get(name) === handler) sandbox.windowListeners.delete(name);
    },
  },
  getComputedStyle: (element) => ({ display: element.style.display || "", visibility: element.style.visibility || "visible", opacity: "1", zIndex: element.style.zIndex || "auto" }),
  REQUIRED_AUDIO_DEVICE: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}",
  currentConfig,
  configLoadGeneration,
  supportedFormats,
  formatLabels,
  availableDevices: [],
  currentStatusData: {},
  mirroringTidalPlaybackState: false,
  activeNativeQualityPill: null,
  nativeQualityPillStates: new Map(),
  currentFormatLabel: "Omniphony EX",
  API_BASE: "http://127.0.0.1:8765",
  crypto: globalThis.crypto,
  AbortController,
  console: testConsole,
  setInterval: (callback) => { intervalCallback = callback; const id = ++intervalId; intervals.set(id, callback); return id; },
  clearInterval: (id) => { clearedIntervals.add(id); intervals.delete(id); },
  fetchConfig: async () => ({ ...sandbox.currentConfig, priority: sandbox.currentConfig.priority.slice() }),
  fetchDevices: async () => {},
  sendEngineCommand: async () => true,
  fetch: async (url, options = {}) => {
    if (url.endsWith("/devices")) {
      return { ok: true, status: 200, json: async () => ({ devices: [
        { name: "device-a", description: "Device A" },
        { name: "device-b", description: "Device B" },
        { name: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}", description: "Voicemeeter Input (VB-Audio Voicemeeter VAIO)" },
      ] }) };
    }
    if (!options.body) return { ok: true, status: 200, json: async () => ({}) };
    const payload = JSON.parse(options.body);
    currentConfig = { ...currentConfig, priority: payload.priority };
    sandbox.currentConfig = currentConfig;
    return { ok: true, status: 200, json: async () => ({ status: "saved", config: currentConfig }) };
  },
  requestWithTimeout: async (url, options) => sandbox.fetch(url, options),
  setTimeout,
  clearTimeout,
};

vm.createContext(sandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let pollTimer = null;
  let clockPollTimer = null;
  const SEEK_RECONCILIATION_WINDOW_MS = 2000;
  const SEEK_RECONCILIATION_TOLERANCE_SECONDS = 0.25;
  let settingsButtonTimer = null;
  let prepareNextSelectionPaused = false;
  let pausedSelectionPending = null;
  let nativePlayerCommandHookInstalled = true;
  let settingsUiVerified = false;
  let mediaInterceptTimer = null;\n  let currentMediaElement = null;
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let lastPlayAttempt = { trackId: null, at: 0 };
  let previousEngineSample = { trackId: 227401768, enginePosition: 100 };
  let lastMirroredPlaybackState = "playing";
  let lastTidalSeekIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastBadgeMountSignature = null;
  let pluginDisposed = false;
  let desiredPlaybackIntent = null;
  let playIntentChangedAt = 0;
  let pendingExplicitPlayIntent = null;
  let transportIntentRevision = 0;
  let nativeTimelineProjection = null;
  let nativeTimelineFrameId = null;
  const mediaEventHandlers = new Map();
  const nativeMediaClockEventNames = ["timeupdate", "seeking", "seeked", "playing", "pause"];
  const nativeMediaClockObserver = { media: null, listeners: null, counts: null };
  function permutations(items) {
    if (items.length <= 1) return [items.slice()];
    return items.flatMap((item, index) =>
      permutations(items.filter((_, candidate) => candidate !== index)).map((tail) => [item, ...tail])
    );
  }
  const priorityCompactLabels = {
    AC4_IMS: "AC-4", EAC3_JOC: "JOC", HI_RES_LOSSLESS: "Hi-Res", LOSSLESS: "Lossless",
  };
  const priorityPresets = permutations(supportedFormats).map((value) => ({
    label: value.map((format) => priorityCompactLabels[format]).join(" → "),
    fullLabel: value.map((format) => formatLabels[format]).join(" → "),
    value,
  }));
  ${extractFunction("parsePlaybackPosition")}
  ${extractFunction("cancelNativeTimelineFrame")}
  ${extractFunction("clearNativeTimelineProjection")}
  ${extractFunction("getTidalPosition")}
  ${extractFunction("hasRecentTidalSeekIntent")}
  ${extractFunction("shouldHoldEngineClockAfterTidalSeek")}
  ${extractFunction("getNativeTimelinePosition")}
  ${extractFunction("getPlaybackFooter")}
  ${extractFunction("hasVisibleLayout")}
  ${extractFunction("findNativeQualityPill")}
  ${extractFunction("findNativeQualityBadgeSlot")}
  ${extractFunction("findNativeAudioOutputButton")}
  ${extractFunction("hideNativeQualityPill")}
  ${extractFunction("restoreNativeQualityPills")}
  ${extractFunction("reportBadgePlacement")}
  ${extractFunction("attachPlaybackBadge")}
  ${extractFunction("injectUI")}
  ${extractFunction("isVerifiedOmniphonyJocRoute")}
  ${extractFunction("getAudioParam")}
  ${extractFunction("formatSampleRate")}
  ${extractFunction("formatChannelCount")}
  ${extractFunction("getEngineOutputDetails")}
  ${extractFunction("getEngineOutputSummary")}
  ${extractFunction("formatBitrate")}
  ${extractFunction("getPcmOutputBitrate")}
  ${extractFunction("getSourceBitrate")}
  ${extractFunction("getSourceBitrateKind")}
  ${extractFunction("formatSourceBitrate")}
  ${extractFunction("getSourcePlaybackSummary")}
  ${extractFunction("updateSettingsPanelStatus")}
  ${extractFunction("getTidalPlayState")}
  ${extractFunction("detachNativeMediaClockObserver")}
  ${extractFunction("readNativeMediaClockDiagnostic")}
  ${extractFunction("dispatchTidalAction")}
  ${extractFunction("setTidalUiPlaybackState")}
  ${extractFunction("setTidalPlaybackIntent")}
  ${extractFunction("updatePausedSelectionUi")}
  ${extractFunction("refreshSettingsTelemetry")}
  ${extractFunction("localRequestFailureSummary")}
  ${extractFunction("verifySettingsPanelControls")}
  ${extractFunction("triggerEnginePlay")}
  ${extractFunction("cancelPendingEnginePlay")}
  ${extractFunction("shouldResumeEngine")}
  ${extractFunction("hasTidalClockDiscontinuity")}
  ${extractFunction("shouldSuppressStaleZeroSeek")}
  ${extractFunction("invalidateEngineStatus")}
  function getCurrentTrackId() { return globalThis.currentTrackId ?? null; }
  function getCurrentTrackMeta() { return globalThis.currentTrackMeta ?? {}; }
  function getTidalPlaybackSnapshot() { return { trackId: globalThis.currentTrackId ?? 2, playing: globalThis.currentTidalPlaying ?? true, position: 0, title: "Track", artist: "Artist" }; }
  ${extractFunction("updateBadgeUI")}
  ${extractFunction("toggleSettingsPanel")}
  ${extractFunction("populateDeviceSelector")}
  ${extractFunction("fetchDevices")}
  ${extractFunction("preventNativeMediaPlayback")}
  ${extractFunction("restoreNativeMediaPlayback")}
  ${extractFunction("interceptMediaElements")}
  ${extractFunction("installPlaybackObservers")}
  const PLAY_INTENT_RECONCILIATION_WINDOW_MS = 1000;
  const EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS = 15000;
  ${extractFunction("getPlaybackInstanceIdentity")}
  let seekToEndPlaybackIdentity = null;
  let seekToEndTargetPosition = null;
  let seekToEndProgressObserved = false;
  ${extractFunction("handleImmediateTidalSeek")}
  ${extractFunction("hasPendingExplicitPlayIntent")}
  ${extractFunction("shouldPreserveExplicitPlayIntent")}
  let nativeTimelineRangeObserverCleanup = null;
  let nativeProgressFillProjectionState = null;
  let nativeSliderInteractionActive = false;
  let nativeSliderInteractionLastAt = -Infinity;
  let nativeSliderInteractionObservedFooter = null;
  let nativeSliderInteractionCleanup = null;
  const NATIVE_SLIDER_INTERACTION_HOLD_MS = 250;
  const unloads = { add(callback) { globalThis.pluginCleanup = callback; }, delete() {} };
  ${extractFunction("readInlineTransform")}
  ${extractFunction("disconnectNativeSliderInteractionGuards")}
  ${extractFunction("restoreNativeProgressFillProjection")}
  ${extractFunction("disconnectNativeTimelineRangeObserver")}
  ${source.slice(source.indexOf("unloads.add(() => {"), source.indexOf("\n});", source.indexOf("unloads.add(() => {")) + 4)}
  globalThis.seedClockSample = () => { previousEngineSample = { trackId: 227401768, enginePosition: 100 }; };
  globalThis.getPreviousEngineSample = () => previousEngineSample;
  globalThis.priorityPresets = priorityPresets;
  globalThis.getSettingsUiVerified = () => settingsUiVerified;
  globalThis.getPausedSelectionArmed = () => prepareNextSelectionPaused;
  globalThis.setLastTidalSeekIntent = (intent) => { lastTidalSeekIntent = intent; };
  globalThis.getLastTidalSeekIntent = () => lastTidalSeekIntent;
`, sandbox);

assert.equal(sandbox.getTidalPosition({ currentTime: 0, playTime: 0.122 }), 0.122,
  "the live playTime wins when TIDAL exposes a stale zero currentTime");
assert.equal(sandbox.getTidalPosition({ currentTime: 12.5 }), 12.5,
  "currentTime remains a fallback when playTime is unavailable");
assert.equal(sandbox.localRequestFailureSummary("GET /config", { name: "AbortError" }),
  "GET /config: request timed out or was canceled");
assert.equal(sandbox.localRequestFailureSummary("GET /devices", { name: "TypeError" }),
  "GET /devices: browser network/CORS fetch failed");
assert.equal(sandbox.localRequestFailureSummary("GET /status", { httpStatus: 503 }),
  "GET /status: HTTP 503");
assert.equal(sandbox.getTidalPosition({ currentTime: 0, playTime: 0 }), 0,
  "a genuine zero position is preserved");
assert.equal(sandbox.getTidalPosition({ currentTime: 4, playTime: "invalid" }), 4,
  "an invalid playTime falls back to a valid currentTime");
assert.equal(sandbox.shouldHoldEngineClockAfterTidalSeek(
  { trackId: 99, position: 104.71616, pending: true }, 99,
  { paused: true, timePos: 209.599826 }, 1000
), true, "a pending seek cannot be overwritten by a stale engine clock");
assert.equal(sandbox.shouldHoldEngineClockAfterTidalSeek(
  { trackId: 99, position: 104.71616, pending: false, settledAt: 1000 }, 99,
  { paused: true, timePos: 209.599826 }, 1033
), true, "the reconciliation window protects the UI while libmpv settles");
assert.equal(sandbox.shouldHoldEngineClockAfterTidalSeek(
  { trackId: 99, position: 104.71616, pending: false, settledAt: 1000 }, 99,
  { paused: true, timePos: 104.71616 }, 1100
), false, "clock mirroring resumes once libmpv reaches the seek target");
assert.equal(sandbox.shouldHoldEngineClockAfterTidalSeek(
  { trackId: 99, position: 104.71616, pending: false, settledAt: 1000 }, 99,
  { paused: true, timePos: 209.599826 }, 3001
), false, "a stalled seek cannot suppress engine reconciliation indefinitely");
assert.equal(sandbox.shouldResumeEngine(false, { paused: true, playbackState: "prepared" }, 42, 42), false,
  "a prepared stream stays paused while TIDAL remains paused");
assert.equal(sandbox.shouldResumeEngine(true, { paused: true, playbackState: "prepared" }, 42, 42), true,
  "a fresh sync pass can commit a prepared stream after TIDAL resumes the same track");
assert.equal(sandbox.shouldResumeEngine(true, { paused: true, playbackState: "prepared" }, 42, 42, true), false,
  "the status poll cannot race the still-pending transaction's final PlayState check");
assert.equal(sandbox.shouldResumeEngine(true, { paused: true, playbackState: "prepared" }, 43, 42), false,
  "a prepared stream cannot commit after TIDAL selects another track");
assert.equal(sandbox.shouldResumeEngine(true, { paused: true, playbackState: "paused" }), true,
  "ordinary active streams remain resumable after the prepared commit");
const pausedTidalSample = { trackId: 227401768, tidalPlaying: false, tidalPosition: 120, at: 1000 };
assert.equal(sandbox.hasTidalClockDiscontinuity(pausedTidalSample, 227401768, 20, 0.1), false,
  "a stale paused clock cannot be mistaken for a backward scrub");
assert.equal(sandbox.hasTidalClockDiscontinuity(pausedTidalSample, 227401768, 122, 0.1), false,
  "a stale paused clock cannot be mistaken for a forward scrub");
assert.equal(sandbox.hasTidalClockDiscontinuity(pausedTidalSample, 227401768, 120.1, 0.1), false,
  "normal clock progression is not mistaken for a seek");
assert.equal(sandbox.hasTidalClockDiscontinuity(pausedTidalSample, 99, 20, 0.1), false,
  "a different track cannot inherit a previous track's seek sample");
assert.equal(sandbox.hasTidalClockDiscontinuity(pausedTidalSample, 227401768, 0, 0.1, true), false,
  "TIDAL's zeroed clock on a pause-to-play transition is not a playing discontinuity");
assert.equal(sandbox.hasTidalClockDiscontinuity(
  { ...pausedTidalSample, tidalPlaying: true }, 227401768, 20, 0.1, true
), true, "an in-play clock jump remains detectable for diagnostics");
assert.equal(sandbox.shouldSuppressStaleZeroSeek(
  0, { paused: true, timePos: 160.61 }, 227401768, 1000
), true, "a stale native zero cannot rewind a prepared nonzero engine");
assert.equal(sandbox.shouldSuppressStaleZeroSeek(
  0, { paused: true, timePos: 0 }, 227401768, 1000
), false, "a native zero is not suppressed after the engine already reached zero");
assert.equal(sandbox.shouldSuppressStaleZeroSeek(
  0, { paused: false, timePos: null }, 227401768, 1000
), false, "missing engine time cannot be mistaken for a nonzero engine position");
sandbox.setLastTidalSeekIntent({ trackId: 227401768, position: 0, at: 950, source: "native-ui" });
assert.equal(sandbox.shouldSuppressStaleZeroSeek(
  0, { paused: false, timePos: 160.61 }, 227401768, 1000
), false, "a recent user-originated zero seek remains allowed");
assert.equal(sandbox.shouldSuppressStaleZeroSeek(
  0, { paused: false, timePos: 160.61 }, 227401768, 3100
), true, "a stale zero is blocked again after the user seek intent expires");
assert.match(source, /previousSample\?\.tidalPlaying !== true \|\| currentTidalIsPlaying !== true/,
  "clock-discontinuity diagnostics ignore paused or transitioning TIDAL state");
assert.match(source, /hasTidalClockDiscontinuity\(\s*previousEngineSample,\s*mediaTrackId,\s*trustedTidalPosition/,
  "the sync loop detects discontinuities from TIDAL's independent native clock");
assert.match(source, /const trustedTidalPosition = staleZeroReset \? null : nativeTidalPosition/,
  "a stale startup zero is excluded from clock diagnostics");
assert.match(source, /mirrorEnginePlaybackStateToTidal\(status\)/,
  "transport state is projected before awaited reconciliation commands");
assert.match(source, /mirrorEngineClockToTidal\(status,\s*mediaTrackId,\s*tidal\.tidalPosition\)/,
  "engine clock projection follows TIDAL clock observation");

// Exercise the real synchronization loop when a paused native clock is stale,
// then inject a stale zero during the transition back to playback.
const syncClockCommands = [];
const syncClockSandbox = {
  URL,
  API_BASE: "http://127.0.0.1:8765",
  REQUIRED_SERVICE_VERSION: requiredServiceVersion,
  currentStatusData: null,
  currentFormatLabel: "AC-4 IMS",
  reduxState: {
    entities: {
      tracks: { entities: { "99": { relationships: { lyrics: { data: [] } } } } },
      lyrics: { entities: {} },
    },
  },
  reduxGetStateCalls: 0,
  window: {
    luna: {
      lib: {
        redux: {
          store: {
            getState() {
              syncClockSandbox.reduxGetStateCalls += 1;
              return syncClockSandbox.reduxState;
            },
          },
        },
      },
    },
    updateLyricsStyle() { syncClockSandbox.lyricsRefreshCount += 1; },
  },
  console: { info() {}, log() {}, debug() {}, warn() {} },
  PLUGIN_BUILD: "test",
  tracePlaybackEvent() {},
  interceptMediaElements() {},
  injectUI() {},
  startSyncLoop() {},
  pluginDisposed: false,
  unloadCallbacks: new Set(),
  unloads: {
    add(callback) { syncClockSandbox.unloadCallbacks.add(callback); },
    delete(callback) { return syncClockSandbox.unloadCallbacks.delete(callback); },
  },
  scheduledRecoveryCallbacks: [],
  setTimeout(callback) { syncClockSandbox.scheduledRecoveryCallbacks.push(callback); return callback; },
  clearTimeout() {},
  currentTrackId: 99,
  getCurrentTrackId: () => syncClockSandbox.currentTrackId,
  lyricsRefreshCount: 0,
  enginePlayRequestId: null,
  enginePlayAllowPaused: false,
  enginePlayRequestTrackId: null,
  lastPlayAttempt: { trackId: null, at: 0 },
  lastMirroredPlaybackState: null,
  clockOrderingInstanceId: null,
  clockOrderingMpvInstanceId: null,
  clockOrderingSequence: -1,
  activeClockServiceInstanceId: null,
  activeClockMpvInstanceId: null,
  lastFastClockSample: null,
  lastEngineStateCommandAt: 0,
  previousEngineSample: null,
  mirrorClockSuppression: [],
  syncStatus: {
    trackId: 99, playbackGeneration: 7, requestId: "sync-99",
    serviceInstanceId: "sync-service", mpvInstanceId: "sync-mpv", timePosSampleSequence: 1,
    playbackState: "paused", paused: true, timePos: 80, duration: 180,
  },
  tidalSnapshot: {
    trackId: 99, playing: false, position: 80, tidalPosition: 80,
    title: "Track", artist: "Artist", actual: "PAUSED", desired: "PAUSED",
  },
  syncNow: 1000,
  syncPoll: null,
  syncClockPoll: null,
  syncEventLog: [],
  cancelPendingCount: 0,
  projectionCalls: [],
  projectionClears: 0,
  SYNC_POLL_INTERVAL_MS: 100,
  CLOCK_POLL_INTERVAL_MS: 50,
  Date: { now: () => syncClockSandbox.syncNow },
  setInterval: (callback, interval) => {
    if (interval === 50) syncClockSandbox.syncClockPoll = callback;
    else syncClockSandbox.syncPoll = callback;
    return interval;
  },
  clearInterval() {},
  queueMicrotask,
  requestWithTimeout: async (url) => ({
    ok: true, status: 200,
    json: async () => url.endsWith("/health")
      ? { version: requiredServiceVersion, serviceInstanceId: "sync-service", mpvInstanceId: "sync-mpv" }
      : url.endsWith("/clock")
        ? syncClockSandbox.clockStatus
        : {
          ...syncClockSandbox.syncStatus,
          serviceInstanceId: syncClockSandbox.syncStatus.serviceInstanceId ?? "sync-service",
          mpvInstanceId: syncClockSandbox.syncStatus.mpvInstanceId ?? "sync-mpv",
        },
  }),
  getTidalPlaybackSnapshot: () => syncClockSandbox.tidalSnapshot,
  projectEngineClockToNativeTimeline(status, trackId, sampledAt) {
    syncClockSandbox.syncEventLog.push("project");
    syncClockSandbox.projectionCalls.push({ timePos: status.timePos, trackId, sampledAt });
    return true;
  },
  clearNativeTimelineProjection() { syncClockSandbox.projectionClears += 1; },
  reportNativeTimelineProjection() {},
  getMonotonicNow: () => syncClockSandbox.syncNow,
  updateBadgeUI() {},
  refreshSettingsTelemetry() {},
  getCurrentTrackId: () => 99,
  cancelPendingEnginePlay() { syncClockSandbox.cancelPendingCount += 1; },
  triggerEnginePlay: async () => false,
  mirrorEnginePlaybackStateToTidal() {
    syncClockSandbox.syncEventLog.push("state");
    return true;
  },
  mirrorEngineClockToTidal() {
    syncClockSandbox.syncEventLog.push("clock");
    return true;
  },
  mirrorEngineStateToTidal(_status, _trackId, _position, suppressClock) {
    syncClockSandbox.mirrorClockSuppression.push(Boolean(suppressClock));
    return true;
  },
  shouldResumeEngine: () => false,
  sendEngineCommand: async (endpoint, body) => {
    syncClockCommands.push({ endpoint, timePos: body.timePos });
    syncClockSandbox.syncEventLog.push(endpoint === "seek" ? `seek:${body.timePos}` : endpoint);
    if (endpoint === "seek") syncClockSandbox.syncStatus.timePos = body.timePos;
    return true;
  },
  installPlaybackObservers() {},
};
syncClockSandbox.window.luna.core = {
  modules: {
    "@luna/core": {
      reduxStore: syncClockSandbox.window.luna.lib.redux.store,
    },
  },
};
vm.createContext(syncClockSandbox);
vm.runInContext(`
  const unloads = globalThis.unloads;
  let radiantSyntheticLyricsSnapshot = null;
  const radiantLyricsRecoveryActivations = new Set();
  let radiantLyricsActivation = { trackId: null, generation: 0 };
  const radiantLyricsOverlay = {
    store: null, originalDescriptor: null, originalGetState: null,
    wrapper: null, cleanupRegistered: false,
  };
  let pollTimer = null;
  let clockPollTimer = null;
  let currentStatusData = syncStatus;
  let currentFormatLabel = "AC-4 IMS";
  let pluginDisposed = false;
  let enginePlayRequestId = null;
  let enginePlayAllowPaused = false;
  let enginePlayRequestTrackId = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  let lastMirroredPlaybackState = null;
  let lastEngineStateCommandAt = 0;
  let previousEngineSample = null;
  let lastTidalSeekIntent = null;
  let seekToEndPlaybackIdentity = null;
  let seekToEndTargetPosition = null;
  let seekToEndProgressObserved = false;
  let desiredPlaybackIntent = null;
  let playIntentChangedAt = 0;
    let pendingExplicitPlayIntent = null;
    let prepareNextSelectionPaused = false;
  let stalledPlayAuthorization = null;
    let pausedSelectionUserPlayAuthorization = null;
    let transportIntentRevision = 0;
    globalThis.setSyncPlaybackIntent = (intent, changedAt) => {
    desiredPlaybackIntent = intent;
    playIntentChangedAt = changedAt;
      transportIntentRevision += 1;
      pendingExplicitPlayIntent = intent === true
        ? { trackId: "99", startedAt: changedAt }
        : null;
  };
  function isEnginePlayStartupPending(trackId) {
    const key = String(trackId ?? "");
    if (!key || desiredPlaybackIntent !== true || enginePlayAllowPaused === true) return false;
    return (String(enginePlayPendingTrackId ?? "") === key && Boolean(enginePlayPending))
      || (String(enginePlayRequestTrackId ?? "") === key && Boolean(enginePlayRequestId));
  }
  function setTidalUiPlaybackState(state) { syncEventLog.push("ui:" + state); }
  let pausedSelectionPending = null;
  let pausedSelectionConfirmedTrackId = null;
  globalThis.getPreviousEngineSample = () => previousEngineSample;
  globalThis.setSeekIntent = (intent) => { lastTidalSeekIntent = intent; };
  const SYNC_POLL_INTERVAL_MS = 100;
  let radiantReduxInitialGetState = null;
  let autoAdvanceEnabled = true;
  let lastAutoAdvancedTrackId = null;
  const PLAY_INTENT_RECONCILIATION_WINDOW_MS = 1000;
  const EXPLICIT_PLAY_HANDOFF_TIMEOUT_MS = 15000;
  ${extractFunction("hasRecentExplicitPlayIntent")}
  ${extractFunction("hasPendingExplicitPlayIntent")}
  const SEEK_RECONCILIATION_WINDOW_MS = 2000;
  const SEEK_RECONCILIATION_TOLERANCE_SECONDS = 0.25;
  const MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS = 250;
  function tracePlaybackEvent(event) { syncEventLog.push(event); }
  function invalidateEngineStatus() {}
  function updatePausedSelectionUi() {}
  console.warn = (...args) => globalThis.syncWarnings.push(args);
  ${extractFunction("parsePlaybackPosition")}
  ${extractFunction("isAtNaturalEnd")}
  ${extractFunction("getPlaybackInstanceIdentity")}
  ${extractFunction("ensureEngineClockAuthority")}
  ${extractFunction("acceptEngineClockSampleOrdering")}
  ${extractFunction("commitEngineClockSampleOrdering")}
  ${extractFunction("isCurrentEngineClockSample")}
  ${extractFunction("isSyncObservationStillCurrent")}
  ${extractFunction("mergeStatusWithLatestFastClockSample")}
  ${extractFunction("resolveEngineSampleAnchor")}
  ${extractFunction("holdSyncForPausedSelection")}
  ${extractFunction("isUninterceptedStalledPlayIntent")}
  ${extractFunction("getStalledPlayRecoveryMode")}
  ${extractFunction("hasStalledPlayAuthorization")}
  ${extractFunction("consumeStalledPlayAuthorization")}
  ${extractFunction("hasRecentTidalSeekIntent")}
  ${extractFunction("hasTidalClockDiscontinuity")}
  ${extractFunction("shouldSuppressStaleZeroSeek")}
  ${extractFunction("shouldHoldEngineClockAfterTidalSeek")}
  ${extractFunction("shouldPreserveExplicitPlayIntent")}
  ${extractFunction("captureRadiantSyntheticLyricsSnapshot")}
  ${extractFunction("overlayCapturedRadiantLyricsState")}
  ${extractFunction("parseRadiantLyricsRequest")}
  ${extractFunction("normalizeLyricsMatchText")}
  ${extractFunction("normalizeRadiantLyricsComparisonTitle")}
  ${extractFunction("radiantLyricsTitlesMatch")}
  ${extractFunction("formatRadiantLyricsTimestamp")}
  ${extractFunction("buildRadiantLyricsSnapshot")}
  ${extractFunction("getRadiantUnwrappedReduxState")}
  ${extractFunction("getRadiantLyricsActivation")}
  ${extractFunction("advanceRadiantLyricsObserverGeneration")}
  ${extractFunction("isRadiantLyricsObserverGenerationCurrent")}
  ${extractFunction("schedulePluginTimeout")}
  ${extractFunction("scheduleRadiantLyricsRecovery")}
  ${extractFunction("installRadiantLyricsPersistenceOverlay")}
  ${extractFunction("inspectRadiantLyricsReduxState")}  ${extractFunction("localRequestFailureSummary")}
  ${extractFunction("startSyncLoop")}
  globalThis.inspectLyricsReduxState = inspectRadiantLyricsReduxState;
  globalThis.captureRadiantLyricsSnapshot = captureRadiantSyntheticLyricsSnapshot;
  globalThis.overlayRadiantLyricsState = overlayCapturedRadiantLyricsState;
  globalThis.parseRadiantLyricsRequest = parseRadiantLyricsRequest;
  globalThis.buildRadiantLyricsSnapshot = buildRadiantLyricsSnapshot;
  globalThis.installRadiantLyricsOverlay = installRadiantLyricsPersistenceOverlay;
  globalThis.setRadiantLyricsSnapshot = (snapshot) => { radiantSyntheticLyricsSnapshot = snapshot; };
  globalThis.getRadiantLyricsActivation = getRadiantLyricsActivation;
  globalThis.scheduleLyricsRecovery = scheduleRadiantLyricsRecovery;
  globalThis.getCurrentTrackId = () => globalThis.currentTrackId;
`, syncClockSandbox);
syncClockSandbox.syncWarnings = [];
assert.equal(JSON.stringify(syncClockSandbox.parseRadiantLyricsRequest(
  "https://api.radiant-lyrics.org/rl-api?title=Cool%20Out&artist=Imagine%20Dragons",
)), JSON.stringify({ title: "Cool Out", artist: "Imagine Dragons" }),
"lyrics observer accepts only a recognized HTTPS Radiant timed-lyrics request");
assert.equal(syncClockSandbox.parseRadiantLyricsRequest(
  "http://api.radiant-lyrics.org/rl-api?title=Cool%20Out&artist=Imagine%20Dragons",
), null, "lyrics observer rejects plaintext HTTP requests");
assert.equal(syncClockSandbox.parseRadiantLyricsRequest(
  "https://example.com/rl-api?title=Cool%20Out&artist=Imagine%20Dragons",
), null, "lyrics observer rejects unrelated endpoints");
assert.equal(syncClockSandbox.parseRadiantLyricsRequest(
  "https://api.radiant-lyrics.org/rl-api?title=Cool%20Out&artist=Imagine%20Dragons",
  { method: "POST" },
), null, "lyrics observer never inspects mutating requests");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("We Found Love (Calvin Harris Extended Mix)", "We Found Love (Calvin Harris Extended Mix / Dolby Atmos)"), true, "Radiant title without the Atmos suffix matches TIDAL’s Atmos edition");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("We Found Love", "We Found Love (Album Version / Dolby Atmos)"), true, "Radiant base title matches TIDAL’s album/Atmos edition");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("Natural", "Natural"), true, "exact lyric titles remain matched");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("We Found Love", "We Found Love (Calvin Harris Extended Mix)"), false, "different mix titles are not accepted by prefix");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("Dolby Atmos", ""), false, "empty titles never match");
assert.equal(syncClockSandbox.radiantLyricsTitlesMatch("Dolby Atmos", "Album Version"), false, "suffix-only titles cannot collapse to an empty match");

// Exercise the fetch observer as an actual wrapper: the caller must receive
// the original Response object, stale-track payloads must be ignored, and the
// global fetch descriptor must be restored when the plugin unloads.
const lyricsFetchResponse = {
  ok: true,
  clone() {
    return { json: () => {
      lyricsFetchSandbox.cloneJsonCalls += 1;
      return {
        then(onFulfilled) {
          onFulfilled({ type: "Line", data: [{ startTime: 1, text: "line" }] });
          return { catch() {} };
        },
      };
    } };
  },
};
const lyricsFetchOriginal = async () => lyricsFetchResponse;
const lyricsFetchDescriptor = {
  configurable: true, enumerable: true, writable: true, value: lyricsFetchOriginal,
};
const lyricsFetchSandbox = {
  URL,
  PLUGIN_BUILD: "test",
  location: { href: "https://desktop.tidal.com/" },
  fetch: lyricsFetchOriginal,
  console: { log() {}, debug() {}, info() {}, warn() {}, error() {} },
  pluginDisposed: false,
  currentTrackId: "99",
  getCurrentTrackId() { return globalThis.currentTrackId; },
  getPlaybackFooter() {
    return {
      querySelector(selector) {
        if (selector.includes("/track/")) return { textContent: "Natural" };
        if (selector.includes("/artist/")) return { textContent: "Imagine Dragons" };
        return null;
      },
    };
  },
  projectedLyrics: [],
  cloneJsonCalls: 0,
  projectRadiantApiLyrics(trackId, payload) {
    globalThis.projectedLyrics.push({ trackId, payload });
    return true;
  },
  timers: [],
  setTimeout: (callback) => { lyricsFetchSandbox.timers.push(callback); return callback; },
  clearTimeout() {},
  unloads: new Set(),
};
Object.defineProperty(lyricsFetchSandbox, "fetch", lyricsFetchDescriptor);
vm.createContext(lyricsFetchSandbox);
vm.runInContext(`
  let radiantLyricsFetchObserver = {
    originalDescriptor: null, originalFetch: null, wrapper: null, cleanupRegistered: false, generation: null,
  };
  let observerTrackId = "99";
  const unloads = globalThis.unloads;
  ${extractFunction("advanceRadiantLyricsObserverGeneration")}
  ${extractFunction("isRadiantLyricsObserverGenerationCurrent")}
  ${extractFunction("schedulePluginTimeout")}
  function tracePlaybackEvent() {}
  function ensureLocalOmniFusionService() { return Promise.resolve(); }
  function interceptMediaElements() {}
  function injectUI() {}
  function startSyncLoop() {}
  function getCurrentTrackId() { return observerTrackId; }
  function projectRadiantApiLyrics(trackId, payload) {
    projectedLyrics.push({ trackId, payload });
    return true;
  }
  ${extractFunction("parseRadiantLyricsRequest")}
  ${extractFunction("normalizeLyricsMatchText")}
  function matchesCurrentRadiantLyricsRequest(request) {
    return request?.title === "Natural" && request?.artist === "Imagine Dragons"
      && observerTrackId === "99" ? "99" : null;
  }
  ${extractFunction("formatRadiantLyricsTimestamp")}
  ${extractFunction("buildRadiantLyricsSnapshot")}
  ${extractFunction("installRadiantLyricsFetchObserver")}  function installLyricsClickListener() {}
  ${extractFunction("onLoad")}
  globalThis.installLyricsFetchObserver = installRadiantLyricsFetchObserver;
  globalThis.runPluginOnLoad = onLoad;
  globalThis.matchesLyricsRequest = matchesCurrentRadiantLyricsRequest;
  globalThis.setObserverTrack = (value) => { observerTrackId = value; };
`, lyricsFetchSandbox);
assert.equal(lyricsFetchSandbox.matchesLyricsRequest({ title: "Natural", artist: "Imagine Dragons" }), "99",
  "lyrics observer associates the request with the matching current TIDAL track");
lyricsFetchSandbox.runPluginOnLoad();
assert.notEqual(lyricsFetchSandbox.fetch, lyricsFetchOriginal,
  "plugin startup installs the passive Radiant lyrics fetch observer");
const wrappedFetch = lyricsFetchSandbox.fetch;
const preservedResponse = await wrappedFetch(
  "https://api.radiant-lyrics.org/rl-api?title=Natural&artist=Imagine%20Dragons",
);
assert.equal(preservedResponse, lyricsFetchResponse,
  "fetch observer returns the exact original Response object to TIDAL/Radiant");
await new Promise((resolve) => setImmediate(resolve));
await new Promise((resolve) => setTimeout(resolve, 0));
await Promise.resolve();
assert.equal(lyricsFetchSandbox.cloneJsonCalls, 1,
  "observer parses only the cloned response body while preserving the original response");
assert.equal(lyricsFetchSandbox.timers.length, 1,
  "valid timed lyrics are queued for projection after parsing the cloned response");
const lyricsCleanupCountBeforeFire = lyricsFetchSandbox.unloads.size;
lyricsFetchSandbox.setObserverTrack("100");
lyricsFetchSandbox.timers.shift()();
assert.equal(lyricsFetchSandbox.unloads.size, lyricsCleanupCountBeforeFire - 1,
  "a completed lyrics timeout removes its unload cleanup callback instead of retaining it for the whole session");
assert.equal(lyricsFetchSandbox.projectedLyrics.length, 0,
  "a response arriving after a track change is not projected onto the new track");
lyricsFetchSandbox.setObserverTrack("99");
for (const cleanup of lyricsFetchSandbox.unloads) cleanup();
assert.deepEqual(Object.getOwnPropertyDescriptor(lyricsFetchSandbox, "fetch"), lyricsFetchDescriptor,
  "plugin unload restores the exact original fetch property descriptor");
assert.equal(lyricsFetchSandbox.fetch, lyricsFetchOriginal,
  "plugin unload removes its wrapper and restores the original fetch function");

let resolveStaleLyricsJson;
const staleLyricsResponse = {
  ok: true,
  clone() {
    return { json: () => new Promise(resolve => { resolveStaleLyricsJson = resolve; }) };
  },
};
const staleLyricsFetchOriginal = async () => staleLyricsResponse;
Object.defineProperty(lyricsFetchSandbox, "fetch", {
  ...lyricsFetchDescriptor, value: staleLyricsFetchOriginal,
});
lyricsFetchSandbox.runPluginOnLoad();
const oldGenerationFetch = lyricsFetchSandbox.fetch;
await oldGenerationFetch("https://api.radiant-lyrics.org/rl-api?title=Natural&artist=Imagine%20Dragons");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(typeof resolveStaleLyricsJson, "function",
  "a deferred Radiant payload remains pending for the unload/reload race test");
lyricsFetchSandbox.pluginDisposed = true;
for (const cleanup of [...lyricsFetchSandbox.unloads]) cleanup();
lyricsFetchSandbox.runPluginOnLoad();
resolveStaleLyricsJson({ type: "Line", data: [{ startTime: 1, text: "stale" }] });
await new Promise((resolve) => setImmediate(resolve));
await Promise.resolve();
assert.equal(lyricsFetchSandbox.timers.length, 0,
  "a pending old-generation lyrics response cannot schedule projection after plugin reload");
assert.equal(lyricsFetchSandbox.projectedLyrics.length, 0,
  "a pending old-generation payload cannot project lyrics into the reloaded plugin instance");
for (const cleanup of [...lyricsFetchSandbox.unloads]) cleanup();

const projectedLyricsSnapshot = JSON.parse(JSON.stringify(
  syncClockSandbox.buildRadiantLyricsSnapshot(99, {
    type: "Line", data: [{ startTime: 1.234, text: "A line" }, { startTime: 2, text: "Next" }],
  }),
));
assert.equal(projectedLyricsSnapshot.entity.attributes.lrcText, "[00:01.23]A line\n[00:02.00]Next",
  "validated Radiant timed lines convert into TIDAL's expected LRC representation");
assert.equal(syncClockSandbox.buildRadiantLyricsSnapshot(99, { type: "Line", data: [] }), null,
  "empty upstream lyrics are not projected into TIDAL state");
const lyricsProbeStore = syncClockSandbox.window.luna.lib.redux.store;
const lyricsProbeOriginalDescriptor = Object.getOwnPropertyDescriptor(lyricsProbeStore, "getState");
const lyricsProbe = JSON.parse(JSON.stringify(syncClockSandbox.inspectLyricsReduxState(99)));
assert.equal(lyricsProbe.trackEntityPresent, true,
  "lyrics probe detects the current TIDAL track entity");
assert.equal(lyricsProbe.trackLyricsCount, 0,
  "lyrics probe reports an empty native lyrics relationship");
assert.equal(lyricsProbe.syntheticLyricsEntityPresent, false,
  "lyrics probe reports no synthetic lyrics entity before registration");
assert.equal(lyricsProbe.getStateAssignmentWorks, true,
  "lyrics probe detects when the exposed Redux store accepts a getState wrapper");
assert.equal(lyricsProbe.getStateAssignmentRestored, true,
  "lyrics probe restores the original Redux getState method after the reversible check");
assert.equal(lyricsProbe.radiantCoreModuleAvailable, true,
  "lyrics probe finds the @luna/core module Radiant Lyrics imports");
assert.equal(lyricsProbe.radiantStoreSameAsExposed, true,
  "lyrics probe identifies when Radiant and TidaLuna expose the same Redux store object");
assert.equal(lyricsProbe.radiantGetStateAssignmentWorks, true,
  "lyrics probe checks wrapper assignment on Radiant's actual imported store");
assert.equal(lyricsProbe.radiantTrackEntityPresent, true,
  "lyrics probe checks track entity presence on Radiant's actual imported store");
assert.equal(lyricsProbe.radiantGetStateReferenceChanged, false,
  "lyrics probe records the initial unwrapped getState reference");
assert.equal(lyricsProbe.radiantLyricsPersistenceOverlayInstalled, false,
  "lyrics probe leaves Radiant's original getter untouched until Radiant installs its own overlay");
assert.equal(lyricsProbeStore.getState, lyricsProbeOriginalDescriptor.value,
  "lyrics probe does not preempt Radiant's native Redux wrapper");
assert.equal(syncClockSandbox.reduxGetStateCalls, 1,
  "lyrics probe performs one Redux snapshot after its reversible capability check");
const syntheticLyricsId = "radiant-lyrics-99";
const syntheticReduxState = {
  entities: {
    tracks: { entities: { 99: { relationships: { lyrics: { data: [{ id: syntheticLyricsId, type: "lyrics" }] } } } } },
    lyrics: { ids: [syntheticLyricsId], entities: { [syntheticLyricsId]: {
      id: syntheticLyricsId, type: "lyrics", attributes: { lrcText: "[00:01.00]line" },
    } } },
  },
};
const capturedLyrics = JSON.parse(JSON.stringify(
  syncClockSandbox.captureRadiantLyricsSnapshot(syntheticReduxState, 99)
));
assert.equal(capturedLyrics.lyricsId, syntheticLyricsId,
  "persistence capture accepts Radiant's matching synthetic lyrics relationship");
const emptyReduxState = {
  entities: {
    tracks: { entities: { 99: { relationships: {} } } },
    lyrics: { ids: [], entities: {} },
  },
};
const lyricsViewState = {
  ...emptyReduxState,
  settings: { nowPlayingActiveView: "lyrics" },
};
syncClockSandbox.currentTrackId = 100;
syncClockSandbox.reduxState = { ...emptyReduxState, settings: { nowPlayingActiveView: "lyrics" } };
syncClockSandbox.setRadiantLyricsSnapshot({ ...capturedLyrics, trackId: "100" });
assert.equal(syncClockSandbox.scheduleLyricsRecovery(100, { settings: { nowPlayingActiveView: "credits" } }, false, false), false,
  "lyrics recovery does not run while the lyrics view is closed");
assert.equal(syncClockSandbox.scheduleLyricsRecovery(100, { settings: { nowPlayingActiveView: "lyrics" } }, false, true), false,
  "lyrics recovery preserves an existing native lyrics relationship");
assert.equal(syncClockSandbox.scheduleLyricsRecovery(100, lyricsViewState, false, false), true,
  "lost captured lyrics schedule one refresh only while the lyrics view is selected");
assert.equal(syncClockSandbox.scheduleLyricsRecovery(100, lyricsViewState, false, false), false,
  "lyrics recovery is one-shot per playback activation");
syncClockSandbox.scheduledRecoveryCallbacks.shift()();
assert.equal(syncClockSandbox.lyricsRefreshCount, 1,
  "one-shot recovery calls Radiant's existing lyrics refresh hook");
const orphanedSyntheticEntityState = {
  ...emptyReduxState,
  entities: {
    ...emptyReduxState.entities,
    lyrics: { ids: [syntheticLyricsId], entities: { [syntheticLyricsId]: { id: syntheticLyricsId, type: "lyrics" } } },
  },
  settings: { nowPlayingActiveView: "lyrics" },
};
syncClockSandbox.getRadiantLyricsActivation("101");
syncClockSandbox.currentTrackId = 100;
syncClockSandbox.setRadiantLyricsSnapshot({ ...capturedLyrics, trackId: "100" });
assert.equal(syncClockSandbox.scheduleLyricsRecovery(100, orphanedSyntheticEntityState, true, false), true,
  "an orphaned synthetic entity without the track relationship is treated as a lost registration");
syncClockSandbox.scheduledRecoveryCallbacks.shift()();
assert.equal(syncClockSandbox.lyricsRefreshCount, 2,
  "an orphaned synthetic entity can trigger the bounded in-view refresh");
syncClockSandbox.currentTrackId = 99;
syncClockSandbox.window.luna.lib.redux.store.getState = () => syncClockSandbox.reduxState;
const radiantGetStateDescriptor = Object.getOwnPropertyDescriptor(
  syncClockSandbox.window.luna.lib.redux.store,
  "getState",
);
syncClockSandbox.reduxState = {
  ...syntheticReduxState,
  settings: { nowPlayingActiveView: "lyrics" },
};
const radiantRegistrationProbe = JSON.parse(JSON.stringify(syncClockSandbox.inspectLyricsReduxState(99)));
assert.equal(radiantRegistrationProbe.radiantLyricsSnapshotCaptured, true,
  "lyrics probe captures the synthetic entity once Radiant installs its own wrapper");
assert.equal(radiantRegistrationProbe.radiantLyricsPersistenceOverlayInstalled, true,
  "persistence wrapper installs after Radiant's getter changes");
syncClockSandbox.reduxState = lyricsViewState;
const radiantLossProbe = JSON.parse(JSON.stringify(syncClockSandbox.inspectLyricsReduxState(99)));
assert.equal(radiantLossProbe.radiantSyntheticLyricsEntityPresent, false,
  "raw Redux probe detects Radiant clearing the synthetic entity");
assert.equal(radiantLossProbe.radiantLyricsSnapshotCaptured, true,
  "captured lyrics snapshot survives the raw state loss");
syncClockSandbox.scheduledRecoveryCallbacks.shift()();
assert.equal(syncClockSandbox.lyricsRefreshCount, 3,
  "observed Radiant state loss triggers one delayed in-view refresh");
const restoredReduxState = JSON.parse(JSON.stringify(
  syncClockSandbox.overlayRadiantLyricsState(emptyReduxState, capturedLyrics)
));
assert.deepEqual(restoredReduxState.entities.tracks.entities[99].relationships.lyrics.data,
  [{ id: syntheticLyricsId, type: "lyrics" }],
  "captured synthetic lyrics remain visible after Radiant clears its native relationship");
assert.equal(restoredReduxState.entities.lyrics.entities[syntheticLyricsId].attributes.lrcText,
  "[00:01.00]line",
  "persistence overlay restores the corresponding lyrics entity");
const nativeLyricsState = {
  ...emptyReduxState,
  entities: {
    ...emptyReduxState.entities,
    tracks: { entities: { 99: { relationships: { lyrics: { data: [{ id: "native-lyrics", type: "lyrics" }] } } } } },
  },
};
assert.equal(syncClockSandbox.overlayRadiantLyricsState(nativeLyricsState, capturedLyrics), nativeLyricsState,
  "persistence overlay leaves a real native lyrics relationship untouched");
syncClockSandbox.currentTrackId = 100;
assert.equal(syncClockSandbox.overlayRadiantLyricsState(emptyReduxState, capturedLyrics), emptyReduxState,
  "captured lyrics for track A are not overlaid while track B is active");
syncClockSandbox.currentTrackId = 99;
assert.equal(syncClockSandbox.captureRadiantLyricsSnapshot(emptyReduxState, 99), null,
  "persistence capture rejects missing synthetic entities");
let persistentReduxState = emptyReduxState;
const persistentLyricsStore = { getState() { return persistentReduxState; } };
const persistentLyricsStoreDescriptor = Object.getOwnPropertyDescriptor(persistentLyricsStore, "getState");
assert.equal(syncClockSandbox.installRadiantLyricsOverlay(persistentLyricsStore), true,
  "persistence wrapper installs on the verified writable Redux store");
assert.deepEqual(Object.getOwnPropertyDescriptor(lyricsProbeStore, "getState"), radiantGetStateDescriptor,
  "moving the persistence wrapper restores Radiant's exact original shared-store descriptor");
persistentReduxState = syntheticReduxState;
persistentLyricsStore.getState();
persistentReduxState = emptyReduxState;
const persistentReduxReadObject = persistentLyricsStore.getState();
assert.equal(persistentLyricsStore.getState(), persistentReduxReadObject,
  "persistence wrapper returns the same React external-store snapshot for an unchanged Redux state");
const persistentReduxRead = JSON.parse(JSON.stringify(persistentReduxReadObject));
assert.deepEqual(persistentReduxRead.entities.tracks.entities[99].relationships.lyrics.data,
  [{ id: syntheticLyricsId, type: "lyrics" }],
  "Redux getter keeps the captured lyrics registered after Radiant clears its own state");
persistentReduxState = { ...emptyReduxState, settings: { nowPlayingActiveView: "lyrics" } };
const nextPersistentReduxRead = persistentLyricsStore.getState();
assert.notEqual(nextPersistentReduxRead, persistentReduxReadObject,
  "persistence wrapper rebuilds its overlaid snapshot after Redux state changes");
assert.equal(persistentLyricsStore.getState(), nextPersistentReduxRead,
  "rebuilt persistence snapshot remains stable across repeated reads");
syncClockSandbox.currentTrackId = 100;
const trackBReduxRead = persistentLyricsStore.getState();
assert.equal(trackBReduxRead, persistentReduxState,
  "active-track changes invalidate the cached overlay and expose raw Redux state");
assert.equal(trackBReduxRead.entities.lyrics.entities[syntheticLyricsId], undefined,
  "track B never receives track A's captured lyrics entity");
syncClockSandbox.currentTrackId = 99;
const trackAReduxReadAgain = persistentLyricsStore.getState();
assert.deepEqual(JSON.parse(JSON.stringify(
  trackAReduxReadAgain.entities.tracks.entities[99].relationships.lyrics.data,
)),
  [{ id: syntheticLyricsId, type: "lyrics" }],
  "returning to track A restores only its matching captured lyrics");
const multiTrackSyntheticState = {
  entities: {
    tracks: {
      entities: {
        10: { relationships: { lyrics: { data: [{ id: "radiant-lyrics-10", type: "lyrics" }] } } },
        99: { relationships: {} },
      },
    },
    lyrics: {
      ids: ["radiant-lyrics-10"],
      entities: {
        "radiant-lyrics-10": { id: "radiant-lyrics-10", type: "lyrics", attributes: { lrcText: "[00:01.00]old" } },
      },
    },
  },
};
syncClockSandbox.setRadiantLyricsSnapshot({ ...capturedLyrics, trackId: "99" });
persistentReduxState = multiTrackSyntheticState;
const multiTrackRead = JSON.parse(JSON.stringify(persistentLyricsStore.getState()));
assert.deepEqual(multiTrackRead.entities.tracks.entities[99].relationships.lyrics.data,
  [{ id: "radiant-lyrics-99", type: "lyrics" }],
  "active track 99 keeps its projected lyrics even when older track 10 still has a synthetic entity in Redux");
const reentrantLyricsState = {
  ...emptyReduxState,
  playbackControls: { mediaProduct: { productId: 99 } },
};
const reentrantLyricsStore = { getState() { return reentrantLyricsState; } };
syncClockSandbox.window.neptune = { store: reentrantLyricsStore };
syncClockSandbox.getCurrentTrackId = () => syncClockSandbox.window.neptune.store
  .getState()?.playbackControls?.mediaProduct?.productId;
syncClockSandbox.setRadiantLyricsSnapshot(capturedLyrics);
assert.equal(syncClockSandbox.installRadiantLyricsOverlay(reentrantLyricsStore), true,
  "lyrics overlay installs before exercising a same-store track-ID fallback");
const omniGetState = reentrantLyricsStore.getState;
const laterPluginGetState = function (...args) {
  return Reflect.apply(omniGetState, this, args);
};
reentrantLyricsStore.getState = laterPluginGetState;
assert.doesNotThrow(() => reentrantLyricsStore.getState(),
  "lyrics overlay breaks a reentrant getState call from track-ID fallback");
assert.deepEqual(reentrantLyricsStore.getState().entities.tracks.entities[99].relationships.lyrics.data,
  [{ id: syntheticLyricsId, type: "lyrics" }],
  "reentrant third-party wrapper still returns matching projected lyrics");
syncClockSandbox.window.neptune = undefined;
syncClockSandbox.getCurrentTrackId = () => syncClockSandbox.currentTrackId;
for (const cleanup of syncClockSandbox.unloadCallbacks) cleanup();
assert.equal(reentrantLyricsStore.getState, laterPluginGetState,
  "plugin unload preserves a later third-party wrapper around its getter");
assert.equal(reentrantLyricsStore.getState(), reentrantLyricsState,
  "a third-party wrapper retained after unload sees raw Redux state without the stale lyrics overlay");
assert.deepEqual(Object.getOwnPropertyDescriptor(persistentLyricsStore, "getState"), persistentLyricsStoreDescriptor,
  "plugin unload restores the exact original Redux getter descriptor");
assert.deepEqual(Object.getOwnPropertyDescriptor(lyricsProbeStore, "getState"), radiantGetStateDescriptor,
  "plugin unload preserves Radiant's original Redux wrapper descriptor");
syncClockSandbox.startSyncLoop();
async function pollSyncClock(now) {
  syncClockSandbox.syncNow = now;
  syncClockSandbox.syncStatus.timePosSampleSequence += 1;
  syncClockSandbox.syncPoll();
  await new Promise((resolve) => setTimeout(resolve, 0));
}
await pollSyncClock(1000);
assert.ok(syncClockSandbox.syncEventLog.indexOf("clock") < syncClockSandbox.syncEventLog.indexOf("project"),
  "native slider projection follows TIDAL's Redux clock update so the final render cannot clamp it back");
let clockProjectionCount = syncClockSandbox.syncEventLog.filter((entry) => entry === "clock").length;
const nativeTimelineProjectionCount = syncClockSandbox.projectionCalls.length;
syncClockSandbox.setSeekIntent({
  trackId: 99, position: 40, pending: true, at: 1000,
});
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, position: 40, tidalPosition: 40,
};
await pollSyncClock(1100);
assert.equal(syncClockSandbox.syncEventLog.filter((entry) => entry === "clock").length, clockProjectionCount,
  "the sync loop suppresses stale engine time while a TIDAL seek request is pending");
assert.ok(syncClockSandbox.projectionClears > 0,
  "the native timeline projection is cleared while a TIDAL seek is settling");
syncClockSandbox.setSeekIntent({
  trackId: 99, position: 40, pending: false, settledAt: 1100,
});
syncClockSandbox.syncStatus = { ...syncClockSandbox.syncStatus, timePos: 40 };
await pollSyncClock(1200);
assert.ok(syncClockSandbox.syncEventLog.filter((entry) => entry === "clock").length > clockProjectionCount,
  "the sync loop resumes clock mirroring when MPV reaches the seek target");
assert.ok(syncClockSandbox.projectionCalls.length > nativeTimelineProjectionCount,
  "the native timeline projection resumes after the engine accepts the TIDAL seek");
clockProjectionCount = syncClockSandbox.syncEventLog.filter((entry) => entry === "clock").length;
syncClockSandbox.setSeekIntent(null);
syncClockSandbox.syncStatus = { ...syncClockSandbox.syncStatus, timePos: 80 };
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, position: 80, tidalPosition: 80,
};
await pollSyncClock(1300);
syncClockSandbox.tidalSnapshot = { ...syncClockSandbox.tidalSnapshot, tidalPosition: 160 };
await pollSyncClock(1400);
assert.deepEqual(syncClockCommands, [],
  "a stale paused PlayState clock cannot send a seek against the paused engine");
syncClockSandbox.tidalSnapshot = { ...syncClockSandbox.tidalSnapshot, tidalPosition: 0 };
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, playing: true, actual: "PLAYING", desired: "PLAYING",
};
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus, playbackState: "playing", paused: false, timePos: 80,
};
await pollSyncClock(1500);
for (let now = 1600; now <= 3800; now += 100) {
  syncClockSandbox.syncStatus = {
    ...syncClockSandbox.syncStatus,
    timePos: 80 + (now - 1300) / 1000,
  };
  await pollSyncClock(now);
}
assert.deepEqual(syncClockCommands, [],
  "a paused timeline projection and a stale zero during resume do not issue inferred seeks");
assert.equal(syncClockSandbox.getPreviousEngineSample().tidalPosition, 160,
  "suppressed stale zero samples never replace the last trusted TIDAL clock sample");
assert.deepEqual(syncClockSandbox.syncWarnings, [],
  "the long stale-zero playback sample sequence completes without sync-loop errors");

// Reproduce the live failure: a playing TIDAL clock jumps backward relative
// to the engine, then remains stale. The sample may be diagnosed and mirrored
// back to TIDAL, but it must never seek the libmpv playback clock.
syncClockCommands.length = 0;
syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "sync-99", timePosSampleSequence: 31,
  playbackState: "playing", paused: false, timePos: 10, duration: 180,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: true, position: 20, tidalPosition: 20,
  title: "Track", artist: "Artist", actual: "PLAYING", desired: "PLAYING",
};
vm.runInContext(`
  previousEngineSample = {
    trackId: 99, at: 3900, tidalPosition: 10, enginePosition: 10, tidalPlaying: true,
  };
  lastTidalSeekIntent = null;
`, syncClockSandbox);
await pollSyncClock(4000);
assert.deepEqual(syncClockCommands, [],
  "a sampled TIDAL discontinuity cannot issue an engine seek");
assert.ok(syncClockSandbox.syncEventLog.includes("tidal-clock-discontinuity-ignored"),
  "the unexpected clock jump remains visible in the diagnostic trace");
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus, timePos: 10.1,
};
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, position: 10, tidalPosition: 10,
};
await pollSyncClock(4100);
for (let now = 4200; now <= 6000; now += 100) {
  syncClockSandbox.syncStatus = {
    ...syncClockSandbox.syncStatus, timePos: 10.1 + (now - 4100) / 1000,
  };
  await pollSyncClock(now);
}
assert.deepEqual(syncClockCommands, [],
  "persistent TIDAL clock lag cannot trigger a delayed automatic engine seek");
assert.ok(syncClockSandbox.syncEventLog.includes("clock"),
  "the authoritative engine clock continues to mirror into TIDAL");
assert.deepEqual(syncClockSandbox.syncWarnings, [],
  "the stale-clock regression completes without sync-loop errors");

// /play deliberately prepares libmpv paused while TIDAL already owns Play
// intent. The sync poll must not mirror PAUSED into TIDAL or cancel that start.
syncClockCommands.length = 0;
syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.cancelPendingCount = 0;
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "startup-99",
  playbackState: "paused", paused: true, timePos: 80,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: true, position: 80, tidalPosition: 80,
  title: "Track", artist: "Artist", actual: "PLAYING", desired: "PLAYING",
};
vm.runInContext(`
  desiredPlaybackIntent = true;
  enginePlayPending = Promise.resolve(true);
  enginePlayPendingTrackId = "99";
  enginePlayRequestId = "startup-99";
  enginePlayRequestTrackId = "99";
  enginePlayAllowPaused = false;
`, syncClockSandbox);
await pollSyncClock(4200);
assert.deepEqual(syncClockCommands, [],
  "startup does not issue a premature pause or resume while /play is preparing the stream");
assert.equal(syncClockSandbox.cancelPendingCount, 0,
  "the paused preparation snapshot does not cancel the matching TIDAL Play request");
assert.ok(syncClockSandbox.syncEventLog.includes("ui:PLAYING"),
  "the native TIDAL transport remains on Play during paused engine preparation");

syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, playing: false, actual: "PAUSED", desired: "PAUSED",
};
vm.runInContext("desiredPlaybackIntent = false;", syncClockSandbox);
await pollSyncClock(4300);
assert.equal(syncClockSandbox.cancelPendingCount, 1,
  "an explicit user Pause still cancels the in-flight engine start immediately");
assert.deepEqual(syncClockCommands, [],
  "explicit Pause during startup does not accidentally resume the prepared engine");

// A TIDAL native-state rollback after a successful prepared-stream resume
// must not turn the stale snapshot into an immediate engine pause.
syncClockCommands.length = 0;
syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "prepared-99",
  playbackState: "playing", paused: false, timePos: 80.1, duration: 189.525333,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: false, position: 80.1, tidalPosition: 80.1,
  title: "Track", artist: "Artist", actual: "NOT_PLAYING", desired: "PAUSED",
};
syncClockSandbox.syncNow = 4400;
vm.runInContext(`
  desiredPlaybackIntent = true;
  playIntentChangedAt = syncNow;
  enginePlayPending = null;
  enginePlayPendingTrackId = null;
  enginePlayRequestId = null;
  enginePlayRequestTrackId = null;
  enginePlayAllowPaused = false;
  lastEngineStateCommandAt = 0;
  lastMirroredPlaybackState = null;
`, syncClockSandbox);
await pollSyncClock(4400);
assert.deepEqual(syncClockCommands, [],
  "a recent matching Play intent prevents a stale TIDAL NOT_PLAYING snapshot from pausing an already-running libmpv stream");

syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, playing: true, actual: "PLAYING", desired: "PLAYING",
};
await pollSyncClock(5600);
assert.deepEqual(syncClockCommands, [],
  "once the running engine state is mirrored into TIDAL, playback stays stable after the bounded Play-intent window");

vm.runInContext("desiredPlaybackIntent = false;", syncClockSandbox);
syncClockSandbox.tidalSnapshot = {
  ...syncClockSandbox.tidalSnapshot, playing: false, actual: "PAUSED", desired: "PAUSED",
};
await pollSyncClock(5700);
assert.deepEqual(syncClockCommands.map((command) => command.endpoint), ["pause"],
  "an explicit Pause still reaches the running engine after a stale TIDAL state rollback");

const panelFormat = new FakeElement("div", registry);
panelFormat.id = "omniphony-panel-format";
const panelSub = new FakeElement("div", registry);
panelSub.id = "omniphony-panel-sub";
sandbox.currentTrackId = 123;
const panelDot = new FakeElement("span", registry);
panelDot.id = "omniphony-panel-status-dot";
sandbox.refreshSettingsTelemetry({
  trackId: 123, format: "AC4_IMS", paused: true, playbackState: "paused", engineClockAdvancing: false,
  sourceBitrate: 256000, sourceSampleRate: 48000, sourceChannels: 2, sourceCodec: "ac-4",
  sourceBitrateBasis: "MP4 encoded audio packet bytes / packet duration",
  label: "Dolby AC-4 IMS",
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "s16" },
  audioCodec: "pcm_s16le",
  device: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}"
});
assert.equal(panelFormat.innerText, "AC-4 IMS · Paused");
assert.match(panelSub.innerText, /Source AC-4 \u00b7 48 kHz \u00b7 2 source ch \u00b7 256 kbps avg source \u00b7 MP4 packet average/);
assert.match(panelSub.innerText, /Decoded 48 kHz \u00b7 Stereo \u00b7 PCM S16/);
assert.match(panelSub.innerText, /Output 96 kHz \u00b7 8 ch \u00b7 PCM S16 \u00b7 12288 kbps PCM payload/);
assert.match(panelSub.innerText, /Voicemeeter Input · WASAPI/);
assert.equal(panelDot.style.background, "#ffc34d", "paused playback is never shown as clock advancing");
const ac4PanelPlaying = {
  trackId: 123, format: "AC4_IMS", paused: false, playbackState: "playing", engineClockAdvancing: true,
  sourceBitrate: 256000, sourceSampleRate: 48000, sourceChannels: 2, sourceCodec: "ac-4",
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "float" },
};
sandbox.updateSettingsPanelStatus(ac4PanelPlaying, 123, true, { title: "Natural" });
assert.equal(panelFormat.innerText, "AC-4 IMS · Clock advancing",
  "AC-4 clock advancement does not require an Omniphony Studio route");
assert.equal(panelDot.style.background, "#00e18b",
  "AC-4 clock advancement is represented consistently in the panel");
const flacPanelPlaying = {
  trackId: 123, format: "HI_RES_LOSSLESS", paused: false, playbackState: "playing", engineClockAdvancing: true,
  sourceCodec: "FLAC", sourceBitDepth: 24, sourceSampleRate: 44100, sourceChannels: 2,
  audioParams: { samplerate: 44100, "channel-count": 2, format: "s32" },
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "float" },
};
sandbox.updateSettingsPanelStatus(flacPanelPlaying, 123, true, { title: "Thunder" });
assert.equal(panelFormat.innerText, "Hi-Res FLAC · Clock advancing",
  "FLAC clock advancement does not require an Omniphony Studio route");
const jocPanelUnverified = {
  trackId: 123, format: "EAC3_JOC", paused: false, playbackState: "playing", engineClockAdvancing: true,
  omniphonyRoute: { ready: false, checks: { studio: false } },
  audioParams: { samplerate: 48000, "channel-count": 2 },
};
sandbox.updateSettingsPanelStatus(jocPanelUnverified, 123, true, { title: "We Found Love" });
assert.equal(panelFormat.innerText, "E-AC-3 JOC · Omniphony route unverified",
  "JOC playback stays unverified until its Studio route is confirmed");
sandbox.refreshSettingsTelemetry({ trackId: null, playbackState: "idle", label: "Loading", audioOutputParams: null, audioCodec: "", device: "" });
assert.equal(panelFormat.innerText, "No active stream match");
assert.match(panelSub.innerText, /bitrate appears when a matched stream is loaded/);
assert.doesNotMatch(panelSub.innerText, /48 kHz|Stereo|FLAC|source bitrate n\/a/);

assert.equal(sandbox.priorityPresets.length, 24, "all fallback orders are exposed");
assert.equal(new Set(sandbox.priorityPresets.map((item) => item.value.join(","))).size, 24);

// With no playback-controls element and no media loaded, idle settings must still mount.
sandbox.injectUI();
const badge = document.getElementById("omniphony-badge");
assert.ok(badge, "idle settings button exists");
assert.equal(badge.parentElement, body, "button falls back to persistent document body");
assert.equal(badge.style.position, "fixed");
assert.equal(badge.innerText, "Omniphony EX \u00b7 Idle");
assert.equal(typeof badge.onclick, "function");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "fixed-fallback");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.visible, true,
  "fallback button is laid out and visible within the TIDAL viewport");

// The same button moves into the native footer if TIDAL creates it later.
footerControls = new FakeElement("div", registry);
footerControls.isConnected = true;
intervalCallback();
assert.equal(badge.parentElement, footerControls, "a recognized TIDAL footer keeps the badge inside the native player");
assert.equal(badge.style.position, "static");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "footer-inline");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.badgeInsideFooter, true);
footerControls.remove();
footerControls = null;
intervalCallback();
const fallbackBadge = document.getElementById("omniphony-badge");
assert.ok(fallbackBadge, "settings button is recreated after TIDAL replaces its controls tree");
assert.equal(fallbackBadge.parentElement, body, "button returns to fallback if TIDAL replaces its controls tree");
assert.equal(fallbackBadge.style.position, "fixed");
assert.equal(typeof fallbackBadge.onclick, "function");

// A TidaLuna hot reload may retain the badge node but discard its old module's
// click closure. Repeated attach must restore the settings-panel entry point.
fallbackBadge.onclick = null;
intervalCallback();
assert.equal(typeof fallbackBadge.onclick, "function",
  "an existing footer badge gets a fresh click handler after plugin reload");

// TIDAL may retain a quality slot in the footer DOM while hiding it at zero size.
// Keep the diagnostics control in the footer without mounting it into that slot.
footerControls = new FakeElement("footer", registry);
footerControls.isConnected = true;
const hiddenQualitySlot = new FakeElement("div", registry);
hiddenQualitySlot.className = "qualityBadgeContainer";
hiddenQualitySlot.style.display = "none";
footerControls.appendChild(hiddenQualitySlot);
intervalCallback();
const visibleFallbackBadge = document.getElementById("omniphony-badge");
assert.equal(visibleFallbackBadge.parentElement, footerControls,
  "a hidden quality slot uses an inline control inside the actual TIDAL footer");
assert.equal(visibleFallbackBadge.style.position, "static");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "footer-inline");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.badgeInsideFooter, true,
  "the readiness gate sees the same footer that contains the visible badge");
footerControls.remove();
footerControls = null;
intervalCallback();

// Hidden native quality pills must not outrank visible controls in the footer.
footerControls = new FakeElement("footer", registry);
footerControls.isConnected = true;
const collapsedQualityGroup = new FakeElement("div", registry);
footerControls.appendChild(collapsedQualityGroup);
const hiddenNativePill = new FakeElement("button", registry);
hiddenNativePill.innerText = "24-bit 44.1 kHz";
hiddenNativePill.textContent = hiddenNativePill.innerText;
hiddenNativePill.style.display = "none";
collapsedQualityGroup.appendChild(hiddenNativePill);
const visibleOutputGroup = new FakeElement("div", registry);
footerControls.appendChild(visibleOutputGroup);
const visibleOutputButton = new FakeElement("button", registry);
visibleOutputButton.setAttribute("aria-label", "Sound output");
visibleOutputGroup.appendChild(visibleOutputButton);
intervalCallback();
assert.equal(document.getElementById("omniphony-badge").parentElement, visibleOutputGroup,
  "a hidden native quality pill falls back to a visible native audio-control group");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "footer-audio-control");
footerControls.remove();
footerControls = null;
intervalCallback();

// If no quality pill exists, join the native Sound output control group.
footerControls = new FakeElement("footer", registry);
footerControls.isConnected = true;
const audioControlGroup = new FakeElement("div", registry);
audioControlGroup.isConnected = true;
footerControls.appendChild(audioControlGroup);
const outputButton = new FakeElement("button", registry);
outputButton.setAttribute("aria-label", "Sound output");
const volumeButton = new FakeElement("button", registry);
audioControlGroup.appendChild(outputButton);
audioControlGroup.appendChild(volumeButton);
intervalCallback();
const controlGroupBadge = document.getElementById("omniphony-badge");
assert.equal(controlGroupBadge.parentElement, audioControlGroup);
assert.equal(audioControlGroup.children.indexOf(controlGroupBadge), audioControlGroup.children.indexOf(outputButton) + 1,
  "the add-on joins TIDAL's existing right-side audio-control row");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "footer-audio-control");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.badgeInsideFooter, true);
assert.equal(controlGroupBadge.style.position, "static");
footerControls.remove();
footerControls = null;
intervalCallback();

// The add-on replaces TIDAL's native quality pill in its right-footer slot.
footerControls = new FakeElement("footer", registry);
footerControls.isConnected = true;
const rightGroup = new FakeElement("div", registry);
rightGroup.isConnected = true;
footerControls.appendChild(rightGroup);
const leadingControl = new FakeElement("button", registry);
const trailingControl = new FakeElement("button", registry);
const nativePill = new FakeElement("button", registry);
nativePill.innerText = "24-bit 44.1 kHz";
nativePill.textContent = nativePill.innerText;
rightGroup.appendChild(leadingControl);
rightGroup.appendChild(nativePill);
rightGroup.appendChild(trailingControl);
intervalCallback();
const activeBadge = document.getElementById("omniphony-badge");
assert.equal(nativePill.style.display, "none", "native quality pill is hidden while add-on pill is mounted");
assert.equal(activeBadge.parentElement, rightGroup, "add-on button occupies the native right-side quality slot");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.slot, "native-quality-pill");
assert.equal(badgeMountDiagnostics.at(-1)?.[1]?.visible, true,
  "native footer badge stays within the visible viewport");
assert.equal(rightGroup.children.indexOf(activeBadge), rightGroup.children.indexOf(nativePill) + 1,
  "add-on badge stays immediately after the replaced quality pill");
assert.ok(rightGroup.children.indexOf(trailingControl) > rightGroup.children.indexOf(activeBadge),
  "controls following TIDAL's native pill retain their order");
assert.equal(activeBadge.style.position, "static", "native footer placement clears fallback fixed positioning");
assert.equal(activeBadge.className, "omniphony-native-quality-badge", "the badge does not inherit TIDAL hashed CSS classes");
assert.equal(activeBadge.style.maxWidth, "184px", "the telemetry pill fits the measured native quality slot");
assert.equal(activeBadge.style.width, "184px", "the telemetry pill uses the full native quality slot");
assert.equal(activeBadge.style.minWidth, "0", "the badge can shrink safely when the footer is constrained");
assert.equal(activeBadge.style.padding, "4px 6px", "the native two-line pill retains visible internal spacing");
assert.equal(activeBadge.style.minHeight, "32px", "the two-line pill fits the native footer row");
assert.equal(activeBadge.style.whiteSpace, "pre-line", "source format and bitrate may use a native two-line pill");
assert.equal(activeBadge.style.flexDirection, "column", "two-line source details stack without replacing native footer controls");
intervalCallback();
assert.equal(activeBadge.parentElement, rightGroup,
  "repeated attach retains the hidden native quality pill and keeps the badge in the footer slot");
assert.equal(activeBadge.style.position, "static",
  "repeated attach never moves the badge into a fixed overlay");
sandbox.currentTrackId = 227401768;
sandbox.currentTrackMeta = { title: "Natural" };
sandbox.currentStatusData = {
  trackId: 227401768, format: "AC4_IMS", label: "Dolby AC-4 IMS",
  paused: false, playbackState: "playing", engineClockAdvancing: true, audioCodec: "pcm_s16le", audioBitrate: 1536000,
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "s16" },
  sourceBitrate: 256000, sourceSampleRate: 48000, sourceChannels: 2, sourceCodec: "ac-4",
  sourceBitrateBasis: "MP4 encoded audio packet bytes / packet duration",
  device: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}"
};
sandbox.availableDevices = [{
  name: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}", description: "Voicemeeter Input"
}];
sandbox.currentConfig.audio_device = "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}";
sandbox.updateBadgeUI("Dolby AC-4 IMS");
assert.equal(activeBadge.innerText, "[TIDAL] AC-4 IMS \u00b7 256 kbps avg\n48 kHz \u00b7 2 ch",
  "native footer badge shows source codec, measured bitrate, sample rate, and channel count");
assert.match(activeBadge.title, /Natural .*AC-4 IMS .*Engine clock advancing via OmniFusion/);
assert.match(activeBadge.title, /Source AC-4 \u00b7 48 kHz \u00b7 2 source ch \u00b7 256 kbps avg source \u00b7 MP4 packet average/);
assert.match(activeBadge.title, /Decoded 48 kHz \u00b7 Stereo \u00b7 PCM S16/);
assert.match(activeBadge.title, /Output 96 kHz \u00b7 8 ch \u00b7 PCM S16 \u00b7 12288 kbps PCM payload \u00b7 Voicemeeter Input \u00b7 WASAPI/);
const ac4StatusForBadge = sandbox.currentStatusData;
sandbox.currentStatusData = { trackId: 227401768, format: "EAC3_JOC", audioCodec: "eac3", audioBitrate: 768000 };
sandbox.updateBadgeUI("Atmos JOC");
assert.equal(activeBadge.innerText, "[TIDAL] E-AC-3 JOC \u00b7 bitrate pending\nrate n/a \u00b7 ch n/a",
  "footer marks estimated bitrate and refuses to invent missing source format metrics");
assert.equal(activeBadge.dataset.omniphonyState, "route-unverified",
  "JOC is never represented as active before Studio, renderer, and orender decoder are confirmed");
assert.match(activeBadge.title, /Omniphony route unverified/);
const jocRouteChecks = {
  studio: true, mpv: true,
  orenderModuleLoaded: true, studioRendererConnected: true,
};
const jocStatus = {
  trackId: 227401768, format: "EAC3_JOC", audioCodec: "eac3", audioBitrate: 768000,
  activeDecoder: "orender", omniphonyRoute: { ready: true, checks: jocRouteChecks },
  paused: true, playbackState: "paused", sourceSampleRate: 48000, sourceChannels: 6,
  audioParams: { samplerate: 48000, "channel-count": 2 },
  audioOutputParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
};
assert.equal(sandbox.isVerifiedOmniphonyJocRoute(jocStatus), true);
sandbox.currentStatusData = jocStatus;
sandbox.updateBadgeUI("Atmos JOC");
assert.equal(activeBadge.dataset.omniphonyState, "paused");
assert.equal(activeBadge.innerText, "[TIDAL] E-AC-3 JOC \u00b7 bitrate pending\n48 kHz \u00b7 6 ch",
  "JOC badge reports encoded source channels while the tooltip reports rendered output channels");
assert.match(activeBadge.title, /Omniphony Studio ↔ orender connected · decoder confirmed/);
for (const check of Object.keys(jocRouteChecks)) {
  const missing = { ...jocStatus, omniphonyRoute: { ready: true, checks: { ...jocRouteChecks, [check]: false } } };
  assert.equal(sandbox.isVerifiedOmniphonyJocRoute(missing), false, `JOC route requires ${check}`);
}
const embeddedRouteOnly = {
  ...jocStatus,
  omniphonyRoute: {
    ready: true,
    checks: {
      studio: true, mpv: true, orenderModuleLoaded: true,
      studioRendererConnected: true, renderer: false, rendererPipeConfigured: false,
    },
  },
};
assert.equal(sandbox.isVerifiedOmniphonyJocRoute(embeddedRouteOnly), true,
  "embedded Studio↔MPV routing does not require a separate orender.exe process or named pipe");
sandbox.currentStatusData = {
  trackId: 227401768, format: "HI_RES_LOSSLESS", audioCodec: "flac", audioBitrate: 3200000,
  sourceSampleRate: 96000, sourceChannels: 2,
  audioParams: { samplerate: 96000, "channel-count": 2, format: "s32" },
  audioOutputParams: { samplerate: 96000, "channel-count": 2, format: "s32" },
};
sandbox.updateBadgeUI("Hi-Res FLAC");
assert.equal(activeBadge.innerText, "[TIDAL] Hi-Res FLAC \u00b7 bitrate pending\n96 kHz \u00b7 2 ch");
assert.match(activeBadge.title, /Output 96 kHz · Stereo · PCM S32 · 6144 kbps PCM payload/,
  "valid 96 kHz stereo Hi-Res output is reported without a 48 kHz cap");
sandbox.currentStatusData = { trackId: 227401768, format: "LOSSLESS", audioCodec: "flac", audioBitrate: 0 };
sandbox.updateBadgeUI("Lossless FLAC");
assert.equal(activeBadge.innerText, "[TIDAL] Lossless FLAC \u00b7 bitrate pending\nrate n/a \u00b7 ch n/a");
sandbox.currentTrackId = 10582679;
sandbox.currentStatusData = {
  trackId: 10582679, format: "LOSSLESS", audioCodec: "flac", sourceCodec: "FLAC",
  sourceBitrate: 1050773, sourceBitrateBasis: "TIDAL DASH Representation bandwidth (declared)",
  sourceSampleRate: 44100, sourceChannels: 2, sourceBitDepth: 16,
  audioParams: { samplerate: 44100, "channel-count": 2, format: "s16" },
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "float" },
};
sandbox.updateBadgeUI("Lossless FLAC");
assert.equal(activeBadge.innerText, "[TIDAL] Lossless FLAC \u00b7 1050.773 kbps MPD\n44.1 kHz · 2 ch · 16-bit");
assert.match(activeBadge.title, /1050\.773 kbps MPD source · TIDAL DASH Representation bandwidth \(declared\)/);
sandbox.currentTrackId = 227401768;
sandbox.currentStatusData = ac4StatusForBadge;
sandbox.updateBadgeUI("Dolby AC-4 IMS");
assert.match(activeBadge.getAttribute("aria-label"), /256 kbps avg/);
assert.match(activeBadge.title, /12288 kbps PCM payload/);
assert.equal(sandbox.getEngineOutputSummary({ audioParams: { samplerate: 48000, "channel-count": 2 } }), "Output unavailable",
  "decoder parameters are never labeled as endpoint output parameters");
assert.equal(sandbox.getEngineOutputSummary({ audioParams: {} }), "Output unavailable");
assert.equal(sandbox.getEngineOutputSummary({ audioOutputParams: {} }), "Output unavailable");
const unknownSourceStatus = {
  format: "EAC3_JOC", audioCodec: "eac3", audioBitrate: 768000,
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
  audioOutputParams: {},
};
assert.equal(sandbox.getSourceBitrate(unknownSourceStatus), 0,
  "mpv's audioBitrate is never substituted for a measured or manifest source bitrate");
assert.equal(sandbox.getPcmOutputBitrate(unknownSourceStatus), 0,
  "decoder parameters and mpv bitrate are never substituted for missing endpoint parameters");
const unknownDetails = sandbox.getEngineOutputDetails(unknownSourceStatus);
assert.match(unknownDetails, /Source codec unavailable.*source rate unavailable.*source channels unavailable.*source bitrate unavailable/);
assert.match(unknownDetails, /Decoded 48 kHz.*Stereo.*PCM S16/);
assert.match(unknownDetails, /Output unavailable/);
assert.doesNotMatch(unknownDetails, /768 kbps/,
  "an encoded source bitrate is not inferred from mpv's bitrate field");
const unverifiedJocCodec = sandbox.getEngineOutputDetails({
  format: "EAC3_JOC", codec: "eac3_joc", sourceCodec: null,
});
assert.match(unverifiedJocCodec, /Source codec unavailable/,
  "the selected JOC format is not substituted for a missing source codec");
assert.doesNotMatch(unverifiedJocCodec, /Source EAC3_JOC/);
const floatOutputStatus = {
  audioOutputParams: { samplerate: 96000, "channel-count": 8, format: "float" },
};
assert.equal(sandbox.getPcmOutputBitrate(floatOutputStatus), 24576000,
  "96 kHz × 8-channel float output is calculated as a 24,576 kbps PCM payload");
assert.equal(sandbox.formatBitrate(sandbox.getPcmOutputBitrate(floatOutputStatus)), "24576 kbps");
assert.equal(sandbox.formatBitrate(1000000), "1000 kbps");
assert.equal(sandbox.formatBitrate(1008500), "1008.5 kbps");
assert.equal(sandbox.formatBitrate(1008050), "1008.05 kbps");
assert.equal(sandbox.formatBitrate(769243), "769.243 kbps",
  "source bitrate preserves integer-bps precision at three decimal kbps places");
assert.equal(sandbox.formatBitrate(128456), "128.456 kbps",
  "fractional kbps precision is not truncated to two decimal places");
assert.equal(sandbox.isVerifiedOmniphonyJocRoute({ format: "AC4_IMS" }), false,
  "AC-4 is never treated as a verified Omniphony JOC route");
assert.equal(sandbox.isVerifiedOmniphonyJocRoute({ format: "EAC3_JOC", omniphonyRoute: { ready: false } }), false,
  "an unready JOC route is not verified");
const malformedBitDepth = {
  format: "LOSSLESS", sourceBitDepth: Infinity,
  sourceCodec: "FLAC", sourceSampleRate: 44100, sourceChannels: 2,
};
assert.doesNotMatch(sandbox.getSourcePlaybackSummary(malformedBitDepth, "Lossless FLAC"), /Infinity-bit/);
assert.doesNotMatch(sandbox.getEngineOutputDetails(malformedBitDepth), /Infinity-bit/);
assert.match(sandbox.getEngineOutputDetails(floatOutputStatus), /Output 96 kHz.*8 ch.*PCM FLOAT.*24576 kbps PCM payload/);
assert.match(sandbox.getEngineOutputDetails({
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" },
  audioCodec: "pcm_s16le",
  device: "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}"
}), /Decoded 48 kHz · Stereo · PCM S16/);
assert.doesNotMatch(sandbox.getEngineOutputDetails({ audioParams: {}, device: "auto" }), /48000|FLAC|Multi/);
sandbox.currentStatusData.engineClockAdvancing = false;
sandbox.updateBadgeUI("Dolby AC-4 IMS");
assert.match(activeBadge.title, /Playback clock not confirmed via OmniFusion/);
sandbox.currentStatusData.engineClockAdvancing = true;
sandbox.currentTrackId = 999;
sandbox.updateBadgeUI("stale");
assert.equal(activeBadge.innerText, "Omniphony EX \u00b7 Not matched");
assert.doesNotMatch(activeBadge.innerText, /AC-4 IMS/);
sandbox.currentTrackId = 227401768;
sandbox.currentTrackMeta = { title: "Natural" };
sandbox.currentStatusData = {
  trackId: 227401768, format: "AC4_IMS", paused: false, engineClockAdvancing: true,
  audioParams: { samplerate: 48000, "channel-count": 2, format: "s16" }
};
sandbox.updateBadgeUI("Dolby AC-4 IMS");
assert.equal(activeBadge.innerText, "[TIDAL] AC-4 IMS \u00b7 bitrate pending\nrate n/a \u00b7 ch n/a");
sandbox.seedClockSample();
sandbox.invalidateEngineStatus("service unavailable");
assert.equal(activeBadge.innerText, "Omniphony EX \u00b7 Idle");
assert.doesNotMatch(activeBadge.innerText, /Playing/);
assert.equal(sandbox.currentStatusData.engineClockAdvancing, false);
assert.equal(sandbox.currentStatusData.trackId, null);
assert.equal(sandbox.getPreviousEngineSample(), null);
assert.equal(panelFormat.innerText, "Engine unavailable");
assert.equal(panelSub.innerText, "service unavailable");
footerControls.remove();
footerControls = null;
intervalCallback();

// Opening settings while idle loads the persisted config and creates the full-order selector.
currentConfig.priority = ["LOSSLESS", "AC4_IMS", "EAC3_JOC", "HI_RES_LOSSLESS"];
currentConfig.audio_device = "wasapi/{30714dd9-2d0e-4cfb-9b98-558a8c5992da}";
sandbox.currentConfig = currentConfig;
let resolveStaleConfig;
sandbox.fetchConfig = () => new Promise((resolve) => { resolveStaleConfig = resolve; });
sandbox.toggleSettingsPanel();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(document.getElementById("omniphony-priority-select").disabled, true);
sandbox.toggleSettingsPanel(); // close before its delayed GET completes
sandbox.fetchConfig = async () => ({ ...sandbox.currentConfig, priority: sandbox.currentConfig.priority.slice() });
sandbox.toggleSettingsPanel();
await new Promise((resolve) => setTimeout(resolve, 0));
const panel = document.getElementById("omniphony-settings-panel");
assert.ok(panel, "settings panel opens without a track");
assert.match(source, /positionPanelAboveFooter/,
  "settings panel positions itself clear of the live TIDAL footer");
assert.match(source, /removeEventListener\("resize", panel\._omniphonyResizeHandler\)/,
  "settings panel releases its resize listener when closed");
const priority = document.getElementById("omniphony-priority-select");
assert.ok(priority, "priority selector is present");
assert.ok(priority.children.every((option) => option.innerText.length <= 35),
  `priority order labels stay compact enough for the narrow native-style selector: ${JSON.stringify(priority.children.map((option) => option.innerText))}`);
const ac4FirstOption = priority.children.find((option) => option.value === "AC4_IMS,EAC3_JOC,HI_RES_LOSSLESS,LOSSLESS");
assert.equal(ac4FirstOption?.innerText, "AC-4 → JOC → Hi-Res → Lossless",
  "compact selector text still exposes the selected quality order");
assert.equal(ac4FirstOption?.title, "AC-4 IMS → Atmos JOC → Hi-Res FLAC → Lossless FLAC",
  "hover text preserves the full codec names");
assert.equal(priority.getAttribute("aria-describedby"), "omniphony-priority-help",
  "priority selector references an accessible format legend");
assert.match(document.getElementById("omniphony-priority-help").innerText, /E-AC-3 JOC/,
  "the format legend explains the compact JOC label");
const preparePausedButton = document.getElementById("omniphony-prepare-paused");
assert.ok(preparePausedButton, "paused preparation is exposed in the add-on settings, not the native footer controls");
assert.match(preparePausedButton.getAttribute("aria-label"), /without starting playback/);
preparePausedButton.onclick();
assert.equal(sandbox.getPausedSelectionArmed(), true, "paused preparation can be armed before choosing a TIDAL track");
assert.equal(preparePausedButton.getAttribute("aria-pressed"), "true");
assert.match(document.getElementById("omniphony-prepare-paused-status").textContent, /next Play action will be held paused/);
preparePausedButton.onclick();
assert.equal(sandbox.getPausedSelectionArmed(), false, "the armed paused preparation can be canceled");
assert.equal(priority.children.length, 24);
assert.equal(priority.disabled, false);
assert.equal(priority.value, currentConfig.priority.join(","), "saved custom order is selected on open");
const deviceSelect = document.getElementById("omniphony-device-select");
await sandbox.fetchDevices();
assert.equal(deviceSelect.children.length, 1, "only the requested Voicemeeter WASAPI endpoint is selectable");
assert.equal(deviceSelect.value, sandbox.REQUIRED_AUDIO_DEVICE, "the requested Voicemeeter WASAPI endpoint stays selected");
assert.match(document.getElementById("omniphony-device-load-status").innerText,
  /Voicemeeter WASAPI endpoint detected/, "the settings panel confirms the requested device was enumerated");
assert.equal(sandbox.getSettingsUiVerified(), true,
  "priority, playback status, saved settings, and the requested output device are verified before dispatch");
resolveStaleConfig({ priority: ["EAC3_JOC", "AC4_IMS", "HI_RES_LOSSLESS", "LOSSLESS"] });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(priority.value, currentConfig.priority.join(","), "late closed-panel response cannot replace current priority");

// Save AC-4 first and assert the exact confirmed service order is reflected in UI state.
priority.value = "AC4_IMS,EAC3_JOC,HI_RES_LOSSLESS,LOSSLESS";
await priority.onchange();
assert.equal(sandbox.currentConfig.priority.join(","), priority.value);
assert.match(document.getElementById("omniphony-priority-save-status").innerText, /Saved/);

const savedOrder = sandbox.currentConfig.priority.slice();
sandbox.fetch = async () => ({ ok: false, status: 503, json: async () => ({ error: "service offline" }) });
priority.value = "HI_RES_LOSSLESS,AC4_IMS,EAC3_JOC,LOSSLESS";
await priority.onchange();
assert.deepEqual(sandbox.currentConfig.priority, savedOrder, "failed save does not change committed state");
assert.equal(priority.value, savedOrder.join(","), "failed save restores the visible selection");
assert.match(document.getElementById("omniphony-priority-save-status").innerText, /Save failed/);

// A new selection must abort and supersede an in-flight resolver immediately.
const playbackRequests = [];
sandbox.pluginDisposed = false;
sandbox.checkPlaybackReadiness = async () => ({ ready: true });
sandbox.reportPlaybackReadinessBlock = (reason) => { sandbox.readinessBlockReason = reason; };
const originalSeekTrack = sandbox.currentTrackId;
const originalSeekStatus = sandbox.currentStatusData;
const immediateSeekCommands = [];
const immediateSeekUiUpdates = [];
sandbox.currentTrackId = 2;
sandbox.currentStatusData = { trackId: 2, playbackGeneration: 77, paused: true };
sandbox.lastMirroredUiTime = null;
sandbox.dispatchTidalAction = (type, value) => {
  immediateSeekUiUpdates.push({ type, value });
  return true;
};
sandbox.sendEngineCommand = async (endpoint, payload) => {
  immediateSeekCommands.push({ endpoint, payload });
  return true;
};
assert.equal(await sandbox.handleImmediateTidalSeek(41.125), true,
  "a matching TidaLuna seek is mirrored immediately to libmpv");
assert.deepEqual(JSON.parse(JSON.stringify(immediateSeekCommands)), [{
  endpoint: "seek",
  payload: { timePos: 41.125, trackId: 2, playbackGeneration: 77, expectedPaused: true },
}]);
assert.deepEqual(JSON.parse(JSON.stringify(immediateSeekUiUpdates)), [{
  type: "playbackControls/TIME_UPDATE", value: 41.125,
}], "an accepted libmpv seek is immediately mirrored into TIDAL's native clock");
assert.equal(sandbox.lastMirroredUiTime, 41.125,
  "the seek target becomes the last mirrored TIDAL position");
let resolveSupersededSeek;
const overlappingSeekCalls = [];
sandbox.sendEngineCommand = (endpoint, payload) => {
  overlappingSeekCalls.push({ endpoint, payload });
  if (endpoint === "seek" && payload.timePos === 50) {
    return new Promise((resolve) => { resolveSupersededSeek = resolve; });
  }
  return Promise.resolve(true);
};
const supersededSeek = sandbox.handleImmediateTidalSeek(50);
assert.equal(await sandbox.handleImmediateTidalSeek(75), true,
  "a newer seek is dispatched while an earlier seek request is still pending");
resolveSupersededSeek(true);
assert.equal(await supersededSeek, true,
  "the superseded request may finish without invalidating the accepted latest seek");
assert.equal(sandbox.lastMirroredUiTime, 75,
  "a delayed response from an older seek cannot rewind the UI clock");
assert.deepEqual(JSON.parse(JSON.stringify(immediateSeekUiUpdates.slice(-1))), [{
  type: "playbackControls/TIME_UPDATE", value: 75,
}], "only the latest overlapping seek publishes its target into TIDAL");
assert.deepEqual(overlappingSeekCalls.map(({ endpoint, payload }) => [endpoint, payload.timePos]), [
  ["seek", 50], ["seek", 75],
], "overlapping seek requests are both forwarded in user order");
let resolveStaleGenerationSeek;
sandbox.currentStatusData = { trackId: 2, playbackGeneration: 77, requestId: "r-old", paused: true };
sandbox.sendEngineCommand = () => new Promise((resolve) => { resolveStaleGenerationSeek = resolve; });
const staleGenerationSeek = sandbox.handleImmediateTidalSeek(60);
sandbox.currentStatusData = { trackId: 2, playbackGeneration: 78, requestId: "r-new", paused: true };
const uiUpdatesBeforeStaleAck = immediateSeekUiUpdates.length;
resolveStaleGenerationSeek(true);
assert.equal(await staleGenerationSeek, false,
  "a successful seek acknowledgment from an older playback generation is rejected");
assert.equal(immediateSeekUiUpdates.length, uiUpdatesBeforeStaleAck,
  "a late same-track seek acknowledgment cannot overwrite the new generation's TIDAL clock");
assert.equal(sandbox.getLastTidalSeekIntent(), null,
  "a stale-generation seek acknowledgment clears its obsolete reconciliation intent");
sandbox.sendEngineCommand = async () => true;
sandbox.currentStatusData = { trackId: 3, playbackGeneration: 78 };
assert.equal(await sandbox.handleImmediateTidalSeek(50), false,
  "a seek from a different TIDAL track cannot move the current engine stream");
assert.equal(immediateSeekCommands.length, 1,
  "the stale-track case is rejected before reaching the engine command mock");
sandbox.currentTrackId = originalSeekTrack;
sandbox.currentStatusData = originalSeekStatus;
sandbox.requestWithTimeout = (url, options) => new Promise((resolve, reject) => {
  if (url.endsWith("/cancel-play")) {
    resolve({ ok: true });
    return;
  }
  playbackRequests.push({ url, options, resolve, reject, signal: options.signal });
  options.signal.addEventListener("abort", () => {
    const error = new Error("superseded");
    error.name = "AbortError";
    reject(error);
  }, { once: true });
});
// A failed preflight must stop before the /play request is sent.
sandbox.currentTrackId = 7;
sandbox.checkPlaybackReadiness = async () => ({ ready: false, reason: "stale service" });
assert.equal(await sandbox.triggerEnginePlay(7, 0, {}), false,
  "failed readiness check rejects engine dispatch");
assert.equal(playbackRequests.length, 0, "blocked readiness never sends /play");
assert.equal(sandbox.readinessBlockReason, "stale service");
let resolvePausedReadiness;
let pausedSnapshot = { trackId: 11, playing: true };
const pausedDispatchRequests = [];
const pausedPreflightSandbox = {
  API_BASE: "http://127.0.0.1:8765",
  crypto: globalThis.crypto,
  AbortController,
  pluginDisposed: false,
  currentConfig: { priority: ["AC4_IMS"], audio_device: "configured" },
  currentStatusData: {},
  console: { log() {}, warn() {}, error() {} },
  checkPlaybackReadiness: () => new Promise((resolve) => { resolvePausedReadiness = resolve; }),
  getTidalPlaybackSnapshot: () => pausedSnapshot,
  reportPlaybackReadinessBlock() {},
  updateBadgeUI() {},
  requestWithTimeout: async (url, options) => {
    if (url.endsWith("/play")) pausedDispatchRequests.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ generation: 99, format: "LOSSLESS" }) };
  },
};
vm.createContext(pausedPreflightSandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let desiredPlaybackIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastMirroredPlaybackState = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  let currentStatusData = globalThis.currentStatusData;
  function hasPendingExplicitPlayIntent() { return false; }
  function shouldPreserveExplicitPlayIntent() { return false; }
  function cancelPendingEnginePlay() {}
  ${extractFunction("triggerEnginePlay")}
`, pausedPreflightSandbox);
const pausedInFlightPlay = pausedPreflightSandbox.triggerEnginePlay(11, 15, {});
for (let attempt = 0; attempt < 5 && !resolvePausedReadiness; attempt += 1) await Promise.resolve();
pausedSnapshot = { trackId: 11, playing: false };
resolvePausedReadiness({ ready: true, config: { priority: ["LOSSLESS"] } });
assert.equal(await pausedInFlightPlay, false,
  "a TIDAL pause during preflight cancels the pending external playback dispatch");
assert.equal(pausedDispatchRequests.length, 0,
  "a stale playing snapshot never reaches the /play endpoint");
assert.deepEqual(pausedPreflightSandbox.currentConfig.priority, ["AC4_IMS"],
  "a cancelled pause race does not commit the preflight config");

// An already-paused TIDAL selection can be resolved for diagnostics without
// resuming or cancelling the service's prepared MPV stream.
const pausedPreparationCalls = [];
const pausedPreparationCommands = [];
const pausedPreparationSandbox = {
  API_BASE: "http://127.0.0.1:8765",
  crypto: globalThis.crypto,
  AbortController,
  pluginDisposed: false,
  currentConfig: { priority: ["AC4_IMS"], audio_device: "configured" },
  currentStatusData: {},
  console: { log() {}, warn() {}, error() {} },
  checkPlaybackReadiness: async () => ({
    ready: true,
    config: { priority: ["EAC3_JOC", "AC4_IMS"], audio_device: "configured" },
  }),
  getTidalPlaybackSnapshot: () => ({
    trackId: 238551884, playing: false, position: 55.04,
    title: "Demons", artist: "Imagine Dragons",
  }),
  setTidalPlaybackIntent() {},
  reportPlaybackReadinessBlock() {},
  updateBadgeUI() {},
  sendEngineCommand: async (endpoint, body) => {
    pausedPreparationCommands.push({ endpoint, body });
    return true;
  },
  requestWithTimeout: async (url, options = {}) => {
    const endpoint = url.split("/").at(-1);
    if (endpoint === "play") {
      const body = JSON.parse(options.body);
      pausedPreparationCalls.push({ endpoint, body });
      return { ok: true, json: async () => ({
        generation: 121, requestId: body.requestId,
        format: "EAC3_JOC", label: "Atmos JOC",
      }) };
    }
    pausedPreparationCalls.push({ endpoint, body: JSON.parse(options.body || "{}") });
    return { ok: true };
  },
};
vm.createContext(pausedPreparationSandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let desiredPlaybackIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastMirroredPlaybackState = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  let currentStatusData = globalThis.currentStatusData;
  ${extractFunction("triggerEnginePlay")}
`, pausedPreparationSandbox);
assert.equal(await pausedPreparationSandbox.triggerEnginePlay(
  238551884, 55.04, { title: "Demons", artist: "Imagine Dragons" }, { allowPaused: true }
), true, "a selected paused TIDAL track can be prepared for diagnostics");
assert.equal(pausedPreparationCalls.length, 1);
assert.equal(pausedPreparationCalls[0].endpoint, "play");
assert.equal(pausedPreparationCalls[0].body.startPaused, true,
  "paused preparation asks the service to keep libmpv paused");
assert.equal(pausedPreparationCalls[0].body.timePos, 55.04,
  "paused preparation aligns the engine position to TIDAL's current clock");
assert.deepEqual(pausedPreparationCommands, [],
  "paused preparation never sends a resume, pause, seek, or cancellation command");
assert.equal(pausedPreparationSandbox.currentStatusData.playbackGeneration, 121,
  "the response generation is retained for the later exact-track resume");
assert.equal(pausedPreparationSandbox.currentConfig.priority[0], "EAC3_JOC",
  "verified playback settings are committed only after paused-track state is revalidated");

// A paused preparation can race with TIDAL briefly reporting PLAYING while
// the service resolves. That transient state must never resume libmpv, and
// the plugin must restore TIDAL's intended paused state after resolution.
let resolvePausedRacePlay;
let pausedRaceSnapshot = { trackId: 17, playing: false, position: 9 };
const pausedRaceCommands = [];
const pausedRaceIntent = [];
const pausedRaceSandbox = {
  API_BASE: "http://127.0.0.1:8765",
  crypto: globalThis.crypto,
  AbortController,
  pluginDisposed: false,
  currentConfig: {},
  currentStatusData: {},
  console: { log() {}, warn() {}, error() {} },
  checkPlaybackReadiness: async () => ({ ready: true, config: {} }),
  getTidalPlaybackSnapshot: () => pausedRaceSnapshot,
  setTidalPlaybackIntent: (playing) => pausedRaceIntent.push(playing),
  reportPlaybackReadinessBlock() {},
  updateBadgeUI() {},
  sendEngineCommand: async (endpoint, body) => {
    pausedRaceCommands.push({ endpoint, body });
    return true;
  },
  requestWithTimeout: (url, options = {}) => {
    const endpoint = url.split("/").at(-1);
    if (endpoint === "play") {
      const body = JSON.parse(options.body);
      return new Promise((resolve) => { resolvePausedRacePlay = () => resolve({
        ok: true,
        json: async () => ({ generation: 171, requestId: body.requestId, format: "AC4_IMS" }),
      }); });
    }
    return Promise.resolve({ ok: true });
  },
};
vm.createContext(pausedRaceSandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let desiredPlaybackIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastMirroredPlaybackState = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  let currentStatusData = globalThis.currentStatusData;
  function hasPendingExplicitPlayIntent() { return false; }
  function shouldPreserveExplicitPlayIntent() { return false; }
  function cancelPendingEnginePlay() {}
  ${extractFunction("triggerEnginePlay")}
`, pausedRaceSandbox);
const pausedRaceTask = pausedRaceSandbox.triggerEnginePlay(17, 9, {}, { allowPaused: true });
for (let attempt = 0; attempt < 5 && !resolvePausedRacePlay; attempt += 1) await Promise.resolve();
assert.ok(resolvePausedRacePlay, "paused preparation reaches the service before the TIDAL state race");
pausedRaceSnapshot = { trackId: 17, playing: true, position: 9 };
resolvePausedRacePlay();
assert.equal(await pausedRaceTask, true, "transient TIDAL PLAYING does not discard a valid paused preparation");
assert.deepEqual(pausedRaceCommands, [], "paused preparation never resumes, pauses, or cancels libmpv during the TIDAL race");
assert.deepEqual(pausedRaceIntent, [false], "paused preparation restores TIDAL's intended paused state after the race");
assert.equal(pausedRaceSandbox.currentStatusData.playbackGeneration, 171,
  "the paused stream generation remains available for a later explicit Play");

// If TIDAL pauses after /play has reached the service, the resolved stream must
// remain paused and the plugin must not send a resume command.
let deferredServicePlay;
let serviceResolutionSnapshot = { trackId: 12, playing: true };
const deferredServiceCommands = [];
const deferredServiceSandbox = {
  API_BASE: "http://127.0.0.1:8765",
  crypto: globalThis.crypto,
  pluginDisposed: false,
  currentConfig: {},
  currentStatusData: {},
  AbortController,
  console: { log() {}, warn() {}, error() {} },
  checkPlaybackReadiness: async () => ({ ready: true, config: {} }),
  getTidalPlaybackSnapshot: () => serviceResolutionSnapshot,
  reportPlaybackReadinessBlock() {},
  updateBadgeUI() {},
  requestWithTimeout: (url, options = {}) => {
    const endpoint = url.split("/").at(-1);
    if (endpoint === "play") {
      deferredServiceCommands.push({ endpoint, body: JSON.parse(options.body) });
      return new Promise((resolve) => { deferredServicePlay = resolve; });
    }
    if (endpoint === "cancel-play") {
      deferredServiceCommands.push({ endpoint, body: JSON.parse(options.body) });
      return Promise.resolve({ ok: true });
    }
    if (endpoint === "resume" || endpoint === "pause") {
      deferredServiceCommands.push({ endpoint, body: JSON.parse(options.body) });
      return Promise.resolve({ ok: true });
    }
    throw new Error(`Unexpected deferred service request: ${url}`);
  },
};
vm.createContext(deferredServiceSandbox);
vm.runInContext(`
  function tracePlaybackEvent() {}
  let enginePlayGeneration = 0;
  let enginePlayAbortController = null;
  let enginePlayPending = null;
  let enginePlayPendingTrackId = null;
  let enginePlayRequestId = null;
  let enginePlayRequestTrackId = null;
  let enginePlayAllowPaused = false;
  let desiredPlaybackIntent = null;
  let currentFormatLabel = "Omniphony EX";
  let lastMirroredPlaybackState = null;
  let lastPlayAttempt = { trackId: null, at: 0 };
  function hasPendingExplicitPlayIntent() { return false; }
  function shouldPreserveExplicitPlayIntent() { return false; }
  let transportIntentRevision = 0;
  const transportCommandQueues = new Map();
  const transportCommandsInFlight = new Map();
  ${extractFunction("triggerEnginePlay")}
  ${extractFunction("cancelPendingEnginePlay")}
  ${extractFunction("queueTransportCommand")}
  ${extractFunction("sendEngineCommand")}
  ${extractFunction("sendEngineCommandDirect")}
`, deferredServiceSandbox);
const deferredServicePlayTask = deferredServiceSandbox.triggerEnginePlay(12, 6, {});
for (let attempt = 0; attempt < 5 && !deferredServicePlay; attempt += 1) await Promise.resolve();
assert.ok(deferredServicePlay, "track playback dispatch reaches the paused service resolution path");
serviceResolutionSnapshot = { trackId: 12, playing: false };
deferredServicePlay({ ok: true, json: async () => ({
  generation: 34, requestId: deferredServiceCommands[0].body.requestId,
  format: "LOSSLESS", label: "Lossless FLAC",
}) });
assert.equal(await deferredServicePlayTask, false);
assert.equal(deferredServiceCommands[0].body.startPaused, true);
assert.deepEqual(deferredServiceCommands.map(({ endpoint }) => endpoint), ["play", "cancel-play"],
  "pausing during stream resolution cancels the prepared request without resuming it");

const readinessBlocks = [];
sandbox.reportPlaybackReadinessBlock = (reason) => readinessBlocks.push(reason);
let resolveStalePreflight;
sandbox.currentTrackId = 8;
sandbox.checkPlaybackReadiness = () => new Promise((resolve) => { resolveStalePreflight = resolve; });
const stalePreflightPlay = sandbox.triggerEnginePlay(8, 0, {});
for (let attempt = 0; attempt < 5 && !resolveStalePreflight; attempt += 1) await Promise.resolve();
sandbox.checkPlaybackReadiness = async () => ({ ready: true });
sandbox.currentTrackId = 9;
const latestPreflightPlay = sandbox.triggerEnginePlay(9, 0, {});
for (let attempt = 0; attempt < 5 && playbackRequests.length < 1; attempt += 1) await Promise.resolve();
assert.equal(playbackRequests.length, 1, "latest track proceeds after its own successful preflight");
playbackRequests[0].resolve({ ok: true, json: async () => ({
  format: "LOSSLESS", label: "Lossless FLAC",
  requestId: JSON.parse(playbackRequests[0].options.body).requestId,
}) });
assert.equal(await latestPreflightPlay, true);
resolveStalePreflight({ ready: false, reason: "stale preflight failure" });
assert.equal(await stalePreflightPlay, false);
assert.deepEqual(readinessBlocks, [], "a superseded failed preflight cannot overwrite current diagnostics");

const failedSeekTraceEvents = [];
const failedSeekTraceSandbox = {
  API_BASE: "http://127.0.0.1:8765",
  pluginDisposed: false,
  tracePlaybackEvent: (event, detail) => failedSeekTraceEvents.push({ event, ...detail }),
  requestWithTimeout: async () => ({
    ok: false,
    status: 502,
    json: async () => ({
      error: "mpv did not settle at the requested seek position",
      seekDiagnostic: {
        targetTimePos: 240.25,
        seekMode: "absolute+exact",
        preSeekTimePos: 320.1,
        preSeekInProgress: false,
        settlementTimePos: 240.25,
        postSeekTimePos: 279.04,
        postSeekInProgress: false,
        paused: true,
        settlementSamples: Array.from({ length: 65 }, (_, index) => ({
          elapsedMs: index * 50,
          timePos: 240.25 + index / 100,
          seeking: false,
          paused: true,
          inTolerance: true,
          consecutive: index,
          stableMs: index * 10,
          failure: index === 64 ? "[redacted]" : undefined,
          secret: "must-not-be-logged",
        })),
        authToken: "must-not-be-logged",
      },
    }),
  }),
  console: { warn() {} },
};
vm.createContext(failedSeekTraceSandbox);
failedSeekTraceSandbox.transportIntentRevision = 1;
failedSeekTraceSandbox.transportCommandQueues = new Map();
failedSeekTraceSandbox.transportCommandsInFlight = new Map();
vm.runInContext(extractFunction("queueTransportCommand"), failedSeekTraceSandbox);
vm.runInContext(extractFunction("sendEngineCommand"), failedSeekTraceSandbox);
vm.runInContext(extractFunction("sendEngineCommandDirect"), failedSeekTraceSandbox);
assert.equal(await failedSeekTraceSandbox.sendEngineCommand("seek", {
  timePos: 240.25, trackId: 42,
}), false, "a failed service seek remains rejected");
const failedSeekDiagnostic = failedSeekTraceEvents.find((event) =>
  event.event === "engine-command-rejected");
assert.ok(failedSeekDiagnostic, "a failed seek is logged with its response diagnostics");
assert.deepEqual(JSON.parse(JSON.stringify(failedSeekDiagnostic.seekDiagnostic)), {
  targetTimePos: 240.25,
  seekMode: "absolute+exact",
  preSeekTimePos: 320.1,
  preSeekInProgress: false,
  settlementTimePos: 240.25,
  postSeekTimePos: 279.04,
  postSeekInProgress: false,
  paused: true,
  settlementSamples: Array.from({ length: 64 }, (_, index) => ({
    elapsedMs: (index + 1) * 50,
    timePos: 240.25 + (index + 1) / 100,
    seeking: false,
    paused: true,
    inTolerance: true,
    consecutive: index + 1,
    stableMs: (index + 1) * 10,
  })),
}, "the trace keeps only numeric and boolean seek diagnostics");
assert.equal(JSON.stringify(failedSeekDiagnostic).includes("must-not-be-logged"), false,
  "seek diagnostics cannot leak unrelated response fields");
playbackRequests.length = 0;
sandbox.currentTrackId = 1;
const obsoletePlay = sandbox.triggerEnginePlay(1, 0, {});
for (let attempt = 0; attempt < 5 && playbackRequests.length < 2; attempt += 1) {
  await Promise.resolve(); // readiness preflight is awaited before the /play fetch
}
assert.equal(playbackRequests.length, 1, "first request starts after its readiness preflight");
sandbox.currentTrackId = 2;
const currentPlay = sandbox.triggerEnginePlay(2, 0, {});
for (let attempt = 0; attempt < 5 && playbackRequests.length < 2; attempt += 1) {
  await Promise.resolve();
}
assert.equal(playbackRequests.length, 2, "new track dispatch does not wait for stale resolver");
assert.equal(playbackRequests[0].signal.aborted, true, "old browser request is aborted on track change");
playbackRequests[1].resolve({ ok: true, json: async () => ({
  format: "AC4_IMS", label: "AC-4 IMS",
  requestId: JSON.parse(playbackRequests[1].options.body).requestId,
}) });
assert.equal(await currentPlay, true);
assert.equal(await obsoletePlay, false, "stale request cannot report success after newer selection");

// Plugin cleanup must stop interception and release listeners before reload.
const media = new FakeElement("audio", registry);
media.dataset = {};
media.volume = 0.5;
media.currentTime = 0;
sandbox.mediaElements = [media];
sandbox.getCurrentTrackId = () => null;
sandbox.getCurrentTrackMeta = () => ({});
sandbox.sendEngineCommand = () => {};
  media.pause = () => { media.pauseCount = (media.pauseCount ?? 0) + 1; };
  media.play = () => { media.nativePlayCount = (media.nativePlayCount ?? 0) + 1; return Promise.resolve(); };
  media.addEventListener = FakeElement.prototype.addEventListener;
  media.removeEventListener = FakeElement.prototype.removeEventListener;
sandbox.interceptMediaElements();
const mediaTimerId = [...intervals.keys()].at(-1);
intervals.get(mediaTimerId)();
assert.equal(media.listeners?.size ?? 0, 1, "audio element has one cleanup-scoped play guard");
await media.play();
assert.equal(media.nativePlayCount ?? 0, 0, "native media playback is blocked before it can produce a second audio stream");
assert.ok(media.pauseCount > 0, "native audio is paused without muting the TIDAL client");
assert.equal(media.muted, undefined, "the bridge does not mute the TIDAL media element");
sandbox.pluginCleanup();
assert.ok(clearedIntervals.has(mediaTimerId), "plugin cleanup clears media interception interval");
assert.equal(media.listeners?.size ?? 0, 0, "plugin cleanup leaves no media event listeners");
assert.equal(media.dataset.omniphonyHooked, undefined, "plugin cleanup permits a clean reload");
await media.play();
assert.equal(media.nativePlayCount, 1, "plugin unload restores the original native media play method");

const diagnosticAudio = new EventTarget();
Object.assign(diagnosticAudio, {
  currentSrc: "https://audio.invalid/segment?token=must-not-be-logged",
  readyState: 4,
  currentTime: 32.125,
  paused: true,
  seeking: false,
  isConnected: true,
});
sandbox.mediaElements = [diagnosticAudio];
sandbox.window.luna = { lib: { PlayState: {
  currentTime: 32.124,
  playTime: 32.123,
  state: "PAUSED",
} } };
const initialNativeClockDiagnostic = sandbox.readNativeMediaClockDiagnostic();
assert.equal(initialNativeClockDiagnostic.uniqueLoadedElement, true,
  "passive native-clock diagnostics attach only to one uniquely loaded audio element");
assert.equal(initialNativeClockDiagnostic.playStateCurrentTime, 32.124,
  "diagnostics capture TidaLuna's active-player currentTime separately");
assert.equal(initialNativeClockDiagnostic.reduxPlayTime, 32.123,
  "diagnostics capture TidaLuna's Redux playTime separately");
assert.equal(initialNativeClockDiagnostic.media.currentTime, 32.125,
  "diagnostics capture the selected native media position");
assert.equal(JSON.stringify(initialNativeClockDiagnostic).includes("must-not-be-logged"), false,
  "native-clock diagnostics never include media URLs or signed tokens");
diagnosticAudio.dispatchEvent(new Event("timeupdate"));
const nativeClockEventDiagnostic = sandbox.readNativeMediaClockDiagnostic();
assert.equal(nativeClockEventDiagnostic.media.events.timeupdate.synthetic, 1,
  "the diagnostic counts synthetic timeupdate events without dispatching any");
assert.equal(nativeClockEventDiagnostic.media.events.timeupdate.trusted, 0,
  "synthetic and browser-originated timing events remain distinguishable");
assert.equal(sandbox.readNativeMediaClockDiagnostic().media.events.timeupdate.synthetic, 1,
  "routine media-element polling does not discard accumulated event counts");
assert.equal(sandbox.readNativeMediaClockDiagnostic({ resetCounts: true }).media.events.timeupdate.synthetic, 1,
  "the two-second diagnostic window receives the accumulated native event count");
assert.equal(sandbox.readNativeMediaClockDiagnostic().media.events.timeupdate.synthetic, 0,
  "event counts reset only after a diagnostic window is collected");
sandbox.mediaElements = [diagnosticAudio, Object.assign(new EventTarget(), {
  currentSrc: "https://audio.invalid/other",
  readyState: 4,
  currentTime: 0,
  paused: true,
  seeking: false,
  isConnected: true,
})];
const ambiguousNativeClockDiagnostic = sandbox.readNativeMediaClockDiagnostic();
assert.equal(ambiguousNativeClockDiagnostic.uniqueLoadedElement, false,
  "diagnostics do not instrument media when more than one loaded audio element is present");
assert.equal(ambiguousNativeClockDiagnostic.media, null,
  "ambiguous active-player identity is reported without choosing an element");
sandbox.restoreNativeMediaPlayback();
diagnosticAudio.dispatchEvent(new Event("timeupdate"));
console.log("PASS: native media and the two TidaLuna clock sources are passively compared without exposing URLs or synthesizing events.");

let timelineFrameId = 0;
let timelineNow = 1000;
let timelineInputEvents = 0;
const timelineFrames = new Map();
const timelineCancelledFrames = [];
const timelineRangeObservers = [];
class TimelineRangeObserver {
  constructor(callback) { this.callback = callback; this.target = null; this.options = null; this.disconnected = false; timelineRangeObservers.push(this); }
  observe(target, options) { this.target = target; this.options = options; }
  trigger(records) { this.callback(records, this); }
  disconnect() { this.disconnected = true; }
}
const nativeProgressSlider = {
  id: "progressBar",
  tagName: "INPUT",
  type: "range",
  visible: true,
  isConnected: true,
  value: "0",
  min: "0",
  max: "200",
  active: false,
  matches(selector) { return selector === ":active" && this.active; },
  setAttribute() {},
  dispatchEvent() { timelineInputEvents += 1; },
};
const nativeProgressFooter = {
  candidates: [nativeProgressSlider],
  querySelectorAll(selector) {
    assert.equal(selector, "#progressBar", "projection reacquires TIDAL's native progress slider");
    return this.candidates;
  },
  querySelector(selector) {
    assert.equal(selector, "#progressBar", "projection targets TIDAL's existing footer range input");
    return nativeProgressSlider;
  },
  contains(element) { return this.candidates.includes(element) && element.isConnected !== false; },
};
const timelineProjectionSandbox = {
  MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS: 250,
  NATIVE_TIMELINE_DIAGNOSTIC_INTERVAL_MS: 2000,
  NATIVE_PRESENTATION_DIAGNOSTIC_INTERVAL_MS: 2000,
  lastNativePresentationDiagnosticAt: -Infinity,
  nativeTimelineDiagnosticWindow: {
    startedAt: null,
    previousFrameAt: null,
    frames: 0,
    syncWrites: 0,
    reduxUpdates: 0,
    sampleAgeCount: 0,
    sampleBracketCount: 0,
    preWriteTargetDifferenceCount: 0,
    postWriteReadbackDifferenceCount: 0,
    customSliderAriaValueSampleCount: 0,
    skipped: {},
    maxSampleAgeMs: 0,
    maxFrameIntervalMs: 0,
    maxSampleBracketHalfWidthMs: 0,
    maxPreWriteTargetDifferenceMs: 0,
    maxPostWriteReadbackDifferenceMs: 0,
    maxCustomSliderAriaTargetDifferenceMs: 0,
    domCommitCount: 0,
    maxDomCommitDelayMs: 0,
    maxDomSliderTargetDifferenceMs: 0,
    nativeFillProjectionCount: 0,
    maxNativeFillPreCorrectionErrorMs: 0,
    maxNativeFillPostCorrectionErrorMs: 0,
    lastNativeFillProjection: null,
    last: null,
    lastDomCommit: null,
  },
  nativeProgressFillProjectionState: null,
  nativeSliderInteractionActive: false,
  nativeSliderInteractionLastAt: -Infinity,
  nativeSliderInteractionObservedFooter: null,
  nativeSliderInteractionCleanup: null,
  NATIVE_SLIDER_INTERACTION_HOLD_MS: 250,
  lastTidalSeekIntent: null,
  currentStatusData: { trackId: 42 },
  lastMirroredUiDurationKey: null,
  lastMirroredUiDurationAttemptKey: null,
  lastMirroredUiDurationAttemptAt: -Infinity,
  lastTidalDurationMirrorDiagnostic: null,
  nativeTimelineProjection: null,
  nativeTimelineFrameId: null,
  lastMirroredUiTime: null,
  reduxTimelineEvents: [],
  nativeTimelineRangeObserver: null,
  nativeTimelineObservedFooter: null,
  nativeTimelineRangeObserverCleanup: null,
  lastNativeTimelineProjectionDiagnostic: null,
  nativeTimelineDispatchSequence: 0,
  nativeTimelineDispatchHistory: [],
  tidalPlayState: {
    playTime: 0,
    currentTime: 0,
    playbackControls: {
      mediaProduct: { productId: 42 },
      playbackContext: { actualProductId: 42, actualDuration: 0 },
    },
  },
  pluginDisposed: false,
  readNativeMediaClockDiagnostic: () => ({ testStub: true }),
  unloads: new Set(),
  performance: { now: () => timelineNow },
  performance: { now: () => timelineNow },
  timelineDiagnosticMessages: [],
  console: { info(message) { timelineProjectionSandbox.timelineDiagnosticMessages.push(message); } },
  window: {
    luna: { lib: { redux: { actions: { "playbackControls/TIME_UPDATE": () => {} } } } },
    MutationObserver: TimelineRangeObserver,
    requestAnimationFrame(callback) {
      const id = ++timelineFrameId;
      timelineFrames.set(id, callback);
      return id;
    },
    getComputedStyle() {
      return {
        display: "block", visibility: "visible", position: "absolute",
        width: "20px", height: "4px", left: "0px", transform: "none",
        backgroundColor: "rgb(0, 255, 200)", color: "rgb(255, 255, 255)",
        textShadow: "0px 0px 10px rgb(255, 255, 255)", fontWeight: "700", opacity: "1",
        transitionProperty: "width", transitionDuration: "0s",
        animationName: "none", animationDuration: "0s",
      };
    },
    cancelAnimationFrame(id) {
      timelineCancelledFrames.push(id);
      timelineFrames.delete(id);
    },
  },
  getPlaybackFooter: () => nativeProgressFooter,
  getCurrentTrackId: () => 42,
  getTidalPlayState() { return timelineProjectionSandbox.tidalPlayState; },
  getTidalPosition(playState) {
    for (const candidate of [playState?.playTime, playState?.currentTime]) {
      const parsed = Number(candidate);
      if (Number.isFinite(parsed) && parsed >= 0) return parsed;
    }
    return null;
  },
  hasVisibleLayout: (element) => element.visible === true && element.isConnected !== false,
  dispatchTidalAction(action, value) {
    timelineProjectionSandbox.reduxTimelineEvents.push([action, value]);
    if (action === "playbackControls/TIME_UPDATE") {
      timelineProjectionSandbox.tidalPlayState.playTime = Number(value);
      const sequence = ++timelineProjectionSandbox.nativeTimelineDispatchSequence;
      timelineProjectionSandbox.nativeTimelineDispatchHistory.push({
        sequence,
        position: Number(value),
        atMs: timelineNow,
      });
      const customSlider = nativeProgressFooter.candidates.find((element) =>
        String(element.tagName || "").toUpperCase() === "DIV"
        && element.getAttribute?.("role") === "slider");
      customSlider?.setAttribute?.("aria-valuenow", String(value));
    } else if (action === "playbackControls/SET_DURATION") {
      timelineProjectionSandbox.tidalPlayState.playbackControls.playbackContext.actualDuration = Number(value);
    }
    return true;
  },
};
vm.createContext(timelineProjectionSandbox);
vm.runInContext([
  "const MAX_NATIVE_TIMELINE_SAMPLE_AGE_MS = 250;",
  "const NATIVE_TIMELINE_DIAGNOSTIC_INTERVAL_MS = 2000;",
  extractFunction("parsePlaybackPosition"),
  extractFunction("readTidalPlaybackDuration"),
  extractFunction("mirrorEngineDurationToTidal"),
  extractFunction("parseNonNegativeFiniteNumber"),
  extractFunction("getTidalPosition"),
  extractFunction("getMonotonicNow"),
  extractFunction("recordNativeTimelineDiagnostic"),
  extractFunction("summarizeNativePresentationElement"),
  extractFunction("parseNativeTransformMatrix"),
  extractFunction("readInlineTransform"),
  extractFunction("disconnectNativeSliderInteractionGuards"),
  extractFunction("observeNativeSliderInteractionGuards"),
  extractFunction("isNativeSliderScrubbingOrSeeking"),
  extractFunction("restoreNativeProgressFillProjection"),
  extractFunction("applyNativeProgressFillProjection"),
  extractFunction("recordNativePresentationTopology"),
  extractFunction("applyNativeTimelinePosition"),
  extractFunction("disconnectNativeTimelineRangeObserver"),
  extractFunction("observeNativeTimelineRange"),
  extractFunction("reportNativeTimelineProjection"),
  extractFunction("cancelNativeTimelineFrame"),
  extractFunction("clearNativeTimelineProjection"),
  extractFunction("resolveEngineSampleAnchor"),
  extractFunction("getFreshNativeTimelinePosition"),
  extractFunction("scheduleNativeTimelineFrame"),
  extractFunction("projectEngineClockToNativeTimeline"),
].join("\n"), timelineProjectionSandbox);
timelineProjectionSandbox.presentationProbeNode = {
  tagName: "DIV",
  id: "progressFill",
  className: "progress-fill active",
  textContent: "PRIVATE LYRICS MUST NOT BE INCLUDED",
  getAttribute(name) {
    return ({
      role: "presentation", "data-test": "fill", "aria-current": "true",
      "aria-valuenow": "42", "aria-valuemin": "0", "aria-valuemax": "100",
    })[name] ?? null;
  },
  getBoundingClientRect() {
    return { left: 10, top: 20, right: 30, bottom: 24, width: 20, height: 4 };
  },
};
const presentationSummary = vm.runInContext(
  'summarizeNativePresentationElement(presentationProbeNode, "fill")',
  timelineProjectionSandbox,
);
assert.equal(presentationSummary.path, "fill",
  "presentation discovery assigns a stable local node path");
assert.equal(presentationSummary.rect.width, 20,
  "presentation discovery captures actual native element geometry");
assert.equal(presentationSummary.style.transitionProperty, "width",
  "presentation discovery captures computed transition metadata");
assert.equal(presentationSummary.style.color, "rgb(255, 255, 255)",
  "presentation discovery captures lyric foreground style without text");
assert.equal(presentationSummary.style.textShadow, "0px 0px 10px rgb(255, 255, 255)",
  "presentation discovery captures highlight styling without lyric content");
assert.equal(presentationSummary.style.fontWeight, "700",
  "presentation discovery captures emphasis style without lyric content");
assert.equal(presentationSummary.ariaValueNow, "42",
  "presentation discovery records only accessibility position attributes, not element text");
assert.equal(presentationSummary.activeHint, true,
  "presentation discovery marks active state from DOM attributes/classes");
assert.equal(Object.hasOwn(presentationSummary, "textContent"), false,
  "presentation discovery never records lyrics or other DOM text");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 40.25, duration: 200, sampleSequence: 10,
  timePosSampleBracketHalfWidthMs: null,
}, 42, 1000), true);
assert.equal(nativeProgressSlider.value, "40.25",
  "the native TIDAL range input receives the confirmed libmpv position immediately");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.sampleSequence, 10,
  "timeline instrumentation carries the accepted engine sample sequence into the projection");
assert.equal(timelineProjectionSandbox.nativeTimelineProjection.sampleBracketHalfWidthMs, null,
  "a missing clock bracket width remains unknown instead of becoming a false zero");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(undefined), null,
  "an absent bracket width remains unknown");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(null), null,
  "a null bracket width remains unknown");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(-0.1), null,
  "a negative bracket width is rejected");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(Number.NaN), null,
  "a non-finite bracket width is rejected");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(0), 0,
  "a measured zero-width bracket remains a valid measurement");
assert.equal(timelineProjectionSandbox.parseNonNegativeFiniteNumber(0.25), 0.25,
  "a positive bracket width remains intact");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.sliderAfter, 40.25,
  "timeline instrumentation records the actual native slider value after writing it");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.postWriteReadbackDifferenceMs, 0,
  "post-write readback difference measures range-property acceptance, not independent clock sync");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.syncWrites, 1,
  "immediate clock projections are counted separately from animation frames");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.timelineDispatchOutcome,
  "playstate-converged",
  "a successful TIDAL action is distinguished from a PlayState readback that actually converged");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.timelineDispatchAttempted, true,
  "the timeline diagnostic records when the correction branch invokes TIDAL Redux");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.tidalReduxPositionAfterDispatch, 40.25,
  "the timeline diagnostic records the PlayState position observed after dispatch");
assert.equal(timelineProjectionSandbox.nativeTimelineFrameId, 1,
  "active playback schedules a frame-based interpolation between engine samples");
const firstTimelineFrame = timelineFrames.get(1);
timelineFrames.delete(1);
firstTimelineFrame(1250);
assert.equal(nativeProgressSlider.value, "40.5",
  "the visible native slider advances from the engine sample at the current playback rate");
assert.deepEqual(timelineProjectionSandbox.reduxTimelineEvents.at(-1),
  ["playbackControls/TIME_UPDATE", 40.5],
  "each active animation frame publishes its projected position through TIDAL's native Redux action");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.frameAtMs, 1250,
  "timeline instrumentation records the animation-frame projection timestamp");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.sliderBefore, 40.25,
  "timeline instrumentation records the slider value before each frame write");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.postWriteReadbackDifferenceMs, 0,
  "the frame records that the range property accepted the written target");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.frames, 1,
  "rAF projections are counted separately from immediate clock writes");
vm.runInContext(`recordNativeTimelineDiagnostic("redux", {
  action: "playbackControls/TIME_UPDATE", position: 40.5, thresholdMs: 50
}, 3001)`, timelineProjectionSandbox);
const timelineDiagnosticSummary = timelineProjectionSandbox.timelineDiagnosticMessages
  .find((message) => message.includes("[timeline-diag]"));
assert.ok(timelineDiagnosticSummary, "bounded timeline diagnostic windows emit a summary");
assert.match(timelineDiagnosticSummary, /"reduxUpdates":3/,
  "timeline summary includes native Redux TIME_UPDATE events");
assert.match(timelineDiagnosticSummary, /"syncWrites":1/,
  "timeline summary separates polling writes from animation-frame projections");
assert.match(timelineDiagnosticSummary, /"maxPostWriteReadbackDifferenceMs":0/,
  "timeline summary labels the self-readback separately from independent sync");
assert.match(timelineDiagnosticSummary, /"sampleSequence":10/,
  "timeline summary includes the engine clock sequence used for projection");
timelineProjectionSandbox.nativeTimelineDiagnosticWindow.maxPreWriteTargetDifferenceMs = 12;
timelineProjectionSandbox.nativeTimelineDiagnosticWindow.maxSampleAgeMs = 7;
vm.runInContext(`recordNativeTimelineDiagnostic("frame", {
  projectionSource: "sync", sampleAgeMs: null,
  preWriteTargetDifferenceMs: null, postWriteReadbackDifferenceMs: null
}, 3002)`, timelineProjectionSandbox);
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.maxPreWriteTargetDifferenceMs, 12,
  "missing pre-write measurements do not silently become zero");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.maxSampleAgeMs, 7,
  "a missing sample age is excluded instead of counted as zero");
vm.runInContext(`recordNativeTimelineDiagnostic("frame", {
  projectionSource: "sync", sampleAgeMs: null,
  sampleBracketHalfWidthMs: null,
  preWriteTargetDifferenceMs: null, postWriteReadbackDifferenceMs: null
}, 5001)`, timelineProjectionSandbox);
const noValidMeasurementSummary = timelineProjectionSandbox.timelineDiagnosticMessages.at(-1);
assert.match(noValidMeasurementSummary, /"preWriteTargetDifferenceCount":0/,
  "diagnostics report zero valid comparisons when measurements are missing");
assert.match(noValidMeasurementSummary, /"maxPreWriteTargetDifferenceMs":null/,
  "an empty measurement window reports null instead of a fabricated zero error");
assert.match(noValidMeasurementSummary, /"sampleBracketCount":0/,
  "missing timing brackets do not increment the valid measurement count");
assert.match(noValidMeasurementSummary, /"maxSampleBracketHalfWidthMs":null/,
  "an empty timing-bracket window reports null rather than zero");
assert.match(noValidMeasurementSummary, /"maxSampleAgeMs":null/,
  "an empty sample-age window reports null rather than zero");
assert.match(noValidMeasurementSummary, /"maxPostWriteReadbackDifferenceMs":null/,
  "an empty slider-readback window reports null rather than zero");
vm.runInContext(`recordNativeTimelineDiagnostic("skip", {
  reason: "native-scrub-active", sequence: 10
}, 5002)`, timelineProjectionSandbox);
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.skipped["native-scrub-active"], 1,
  "skipped timeline updates retain a reason and source sample sequence");
assert.equal(timelineProjectionSandbox.nativeTimelineFrameId, 2,
  "frame interpolation continues only while the engine clock is playing");
assert.equal(timelineInputEvents, 0,
  "visual clock projection dispatches no input/change event and cannot trigger a seek");
nativeProgressSlider.active = true;
timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 50, duration: 200,
}, 42, 2000);
assert.equal(nativeProgressSlider.value, "40.5",
  "the native slider remains under user control during an active scrub");
nativeProgressSlider.active = false;
const replacementProgressSlider = {
  ...nativeProgressSlider,
  value: "0",
  active: false,
};
const hiddenDuplicateSlider = {
  ...nativeProgressSlider,
  value: "0",
  visible: false,
};
nativeProgressFooter.candidates = [hiddenDuplicateSlider, replacementProgressSlider];
timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 55, duration: 200,
}, 42, 2500);
assert.equal(replacementProgressSlider.value, "55",
  "a remounted native slider receives the next engine sample");
assert.equal(hiddenDuplicateSlider.value, "0",
  "a hidden duplicate timeline is never modified");
assert.equal(nativeProgressSlider.value, "40.5",
  "the detached prior slider is not retained or updated");
timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "paused", paused: true, timePos: 90.123, duration: 200,
}, 42, 3000);
assert.equal(replacementProgressSlider.value, "90.123",
  "a paused engine sample pins the existing TIDAL slider to the settled position");
const pausedTimelineFrameId = timelineProjectionSandbox.nativeTimelineFrameId;
assert.ok(pausedTimelineFrameId !== null,
  "a paused engine sample schedules one post-render native timeline correction");
const pausedTimelineFrame = timelineFrames.get(pausedTimelineFrameId);
timelineFrames.delete(pausedTimelineFrameId);
pausedTimelineFrame(3100);
assert.equal(timelineProjectionSandbox.nativeTimelineFrameId, null,
  "paused playback gets one post-render correction without continuous interpolation");
assert.ok(timelineCancelledFrames.includes(2),
  "pausing cancels the pending animation frame");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 91, duration: 200,
}, 77, 4000), false,
"stale engine samples from a different TIDAL track are rejected");
assert.equal(timelineProjectionSandbox.nativeTimelineProjection, null,
  "a track mismatch clears the old animation sample");
nativeProgressFooter.candidates = [replacementProgressSlider];
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: false,
  timePos: 100, duration: 200,
}, 42, 6000), true,
"a stalled engine sample remains authoritative for the slider position");
assert.equal(replacementProgressSlider.value, "100",
  "a stalled engine cancels interpolation and renders its latest position");
const stalledTimelineFrameId = timelineProjectionSandbox.nativeTimelineFrameId;
assert.ok(stalledTimelineFrameId !== null,
  "a stalled engine schedules one post-render correction but does not interpolate");
const stalledTimelineFrame = timelineFrames.get(stalledTimelineFrameId);
timelineFrames.delete(stalledTimelineFrameId);
stalledTimelineFrame(6050);
assert.equal(timelineProjectionSandbox.nativeTimelineFrameId, null,
  "a stalled engine stops after the one post-render correction");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 100.1, duration: 200,
}, 42, 6100), true,
"a later advancing sample re-anchors timeline interpolation");
assert.equal(replacementProgressSlider.value, "100.1",
  "clock recovery starts from the fresh authoritative position");
const recoveredFrameId = timelineProjectionSandbox.nativeTimelineFrameId;
assert.ok(recoveredFrameId !== null,
  "clock recovery schedules a new animation frame");
const recoveredTimelineFrame = timelineFrames.get(recoveredFrameId);
timelineFrames.delete(recoveredFrameId);
recoveredTimelineFrame(6200);
assert.ok(Math.abs(Number(replacementProgressSlider.value) - 100.2) < 1e-9,
  "clock recovery resumes interpolation from the new sample, not the stale anchor");
assert.equal(timelineProjectionSandbox.getFreshNativeTimelinePosition({
  playing: true, timePos: 1, duration: 200, sampledAt: 1000,
}, 1250), 1.25,
"a sample at the maximum permitted age remains projectable");
assert.equal(timelineProjectionSandbox.getFreshNativeTimelinePosition({
  playing: true, timePos: 1, duration: 200, sampledAt: 1000,
}, 1251), null,
"a sample older than the freshness bound is rejected");
assert.equal(timelineProjectionSandbox.getFreshNativeTimelinePosition({
  playing: false, timePos: 189.525167, duration: 189.525333, sampledAt: 1000,
}, 5000), 189.525167,
"a settled paused endpoint remains projectable after normal service-poll latency");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 40, duration: 200, timePosSampledAtUnixMs: 999935,
}, 42, 8000, 1000000), true,
"a delayed playing response is accepted when its engine sample is fresh");
assert.equal(timelineProjectionSandbox.nativeTimelineProjection.sampledAt, 7935,
"a 65 ms response delay is removed from the animation-frame anchor");
assert.equal(replacementProgressSlider.value, "40.065",
"the slider immediately renders the response-time position without a one-frame backward jump");
assert.equal(timelineProjectionSandbox.getFreshNativeTimelinePosition(
  timelineProjectionSandbox.nativeTimelineProjection, 8000,
), 40.065,
"the corrected timeline is projected to response time without a fixed offset");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 50, duration: 200, timePosSampledAtUnixMs: 999830,
}, 42, 8000, 1000000), true,
"a 170 ms delayed playing response remains within the existing freshness bound");
assert.equal(timelineProjectionSandbox.nativeTimelineProjection.sampledAt, 7830,
"the longer measured delay also moves the projection anchor by its measured age");
assert.equal(replacementProgressSlider.value, "50.17",
"a 170 ms response delay is reflected in the first slider render");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "paused", paused: true,
  timePos: 90.123, duration: 200, timePosSampledAtUnixMs: 999830,
}, 42, 8000, 1000000), true,
"a delayed paused response remains usable without advancing its position");
assert.deepEqual(timelineProjectionSandbox.reduxTimelineEvents.at(-1),
  ["playbackControls/TIME_UPDATE", 90.123],
  "a corrected paused engine position is mirrored through TIDAL Redux after a restart");
assert.equal(timelineProjectionSandbox.getFreshNativeTimelinePosition(
  timelineProjectionSandbox.nativeTimelineProjection, 12000,
), 90.123,
"paused samples are never extrapolated despite their corrected age");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 91, duration: 200, timePosSampledAtUnixMs: 999749,
}, 42, 8000, 1000000), false,
"samples older than 250 ms are rejected instead of being made artificially fresh");
assert.equal(timelineProjectionSandbox.nativeTimelineProjection, null,
"rejecting an old sample clears the previous projection");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 91, duration: 200, timePosSampledAtUnixMs: 1000006,
}, 42, 8000, 1000000), false,
"a timestamp more than 5 ms in the future is rejected");
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, engineClockAdvancing: true,
  timePos: 91, duration: 200, timePosSampledAtUnixMs: "invalid",
}, 42, 8000, 1000000), false,
"a malformed sample timestamp is rejected");
replacementProgressSlider.max = "189.46665954589844";
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "paused", paused: true,
  timePos: 189.525167, duration: 189.525333,
}, 42, 7000), true,
"a paused EOF sample remains eligible for native timeline alignment");
assert.equal(replacementProgressSlider.max, "189.525333",
  "the native slider range follows libmpv duration when TIDAL's catalog duration ends early");
assert.equal(replacementProgressSlider.value, "189.525167",
  "the native slider reaches the confirmed libmpv position past the shorter catalog endpoint");
const eofTimelineFrameId = timelineProjectionSandbox.nativeTimelineFrameId;
const eofTimelineFrame = timelineFrames.get(eofTimelineFrameId);
timelineFrames.delete(eofTimelineFrameId);
eofTimelineFrame(7400);
assert.equal(replacementProgressSlider.value, "189.525167",
  "the post-render frame preserves the exact paused engine endpoint");
assert.ok(timelineProjectionSandbox.nativeTimelineRangeObserver,
  "a footer observer watches for React restoring the shorter catalog range");
replacementProgressSlider.max = "189.46665954589844";
replacementProgressSlider.value = "189.46665954589844";
timelineProjectionSandbox.nativeTimelineRangeObserver.callback([
  { type: "attributes", attributeName: "max" },
]);
const observerCorrectionId = timelineProjectionSandbox.nativeTimelineFrameId;
assert.ok(observerCorrectionId !== null,
  "a React range reset schedules a native timeline correction");
const observerCorrectionFrame = timelineFrames.get(observerCorrectionId);
timelineFrames.delete(observerCorrectionId);
observerCorrectionFrame(7440);
assert.equal(replacementProgressSlider.max, "189.525333",
  "the observed React rerender cannot restore the shorter catalog maximum");
assert.equal(replacementProgressSlider.value, "189.525167",
  "the observed React rerender is corrected back to the paused libmpv position");
nativeProgressFooter.candidates = [];
assert.equal(timelineProjectionSandbox.projectEngineClockToNativeTimeline({
  trackId: 42, playbackState: "playing", paused: false, timePos: 92, duration: 200,
}, 42, 5000), true,
"a temporarily absent footer slider does not invalidate the engine sample");
assert.equal(timelineProjectionSandbox.nativeTimelineFrameId, null,
  "missing progress control does not leave a runaway animation loop");
assert.ok(timelineProjectionSandbox.timelineDiagnosticMessages.some(message => message.endsWith(": active.")),
  "the live projection path records a local, non-identifying success diagnostic");
assert.ok(timelineProjectionSandbox.timelineDiagnosticMessages.some(message => message.endsWith(": no-visible-slider.")),
  "missing native controls produce a distinct local diagnostic");

console.log("PASS: idle button mounts without playback controls.");
console.log("PASS: settings access survives native playback-control replacement.");
console.log("PASS: all 24 priority orders render and a persisted custom order selects on open.");
console.log("PASS: a delayed response from a closed panel cannot overwrite the reopened selector.");
console.log("PASS: successful save updates confirmed state; HTTP failure restores the saved selection.");
console.log("PASS: asynchronously discovered audio devices refresh the idle selector and preserve the saved device.");
console.log("PASS: playback snapshot waits for actual TidaLuna state and rejects navigation-only track IDs.");
console.log("PASS: playback preflight requires verified controls, current service/device, and rejects stale shared-state writes.");
console.log("PASS: TidaLuna state/track/seek observers register cleanup, and matching seek actions reach libmpv immediately.");
console.log("PASS: native stream privilege, playback, seek, queue, and volume controls are redirected to the libmpv-backed UI state.");
console.log("PASS: one-shot paused preparation holds a TIDAL track selection without setting its playback intent to PLAYING.");
console.log("PASS: libmpv clock ordering preserves fresh transport state and mirrors advancing samples through TIDAL Redux.");
console.log("PASS: footer diagnostics mount beside native audio controls and bitrate labels omit trailing zeroes.");
console.log("PASS: TIDAL audio elements are blocked without muting and restored cleanly on plugin unload.");
console.log("PASS: a track change aborts the older request and dispatches the latest selection immediately.");
console.log("PASS: plugin cleanup clears interception timers and releases media listeners for reload.");
console.log("PASS: the native TIDAL progress slider smoothly follows libmpv without dispatching seek events.");

syncClockCommands.length = 0;
syncClockSandbox.setSyncPlaybackIntent(true, 1000);
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "sync-99",
  playbackState: "playing", paused: false, timePos: 50, duration: 180,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: false, position: 50, tidalPosition: 0,
  title: "Track", artist: "Artist", actual: "PAUSED", desired: "NOT_PLAYING",
};
await pollSyncClock(8000);
assert.deepEqual(syncClockCommands.map((command) => command.endpoint), [],
  "a stale paused PlayState cannot pause the same track after explicit Play is confirmed running by libmpv");
assert.equal(syncClockSandbox.shouldPreserveExplicitPlayIntent(
  { playbackState: "playing", paused: false }, 99, 99, true,
), true, "an explicit Play intent is preserved only while its matching engine track is running");
assert.equal(syncClockSandbox.shouldPreserveExplicitPlayIntent(
  { playbackState: "paused", paused: true }, 99, 99, true,
), false, "a paused or ended engine is not resumed from a stale explicit Play intent");
assert.equal(syncClockSandbox.shouldPreserveExplicitPlayIntent(
  { playbackState: "playing", paused: false }, 98, 99, true,
), false, "an explicit Play intent cannot keep a different track running");
syncClockCommands.length = 0;
syncClockSandbox.setSyncPlaybackIntent(false, 8100);
await pollSyncClock(8100);
assert.deepEqual(syncClockCommands.map((command) => command.endpoint), ["pause"],
  "an explicit Pause still wins over a running engine");

syncClockCommands.length = 0;
syncClockSandbox.shouldResumeEngine = (playing, status, tidalTrackId, engineTrackId) =>
  playing && status?.paused === true && String(tidalTrackId) === String(engineTrackId);
syncClockSandbox.setSyncPlaybackIntent(true, 8200);
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "sync-99",
  playbackState: "paused", paused: true, timePos: 50, duration: 180,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: false, position: 50, tidalPosition: 0,
  title: "Track", artist: "Artist", actual: "PAUSED", desired: "NOT_PLAYING",
};
await pollSyncClock(8200);
assert.deepEqual(syncClockCommands.map((command) => command.endpoint), ["resume"],
  "a matching explicit Play survives a stale paused TIDAL snapshot until libmpv confirms resume");

syncClockCommands.length = 0;
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus,
  playbackState: "playing", paused: false, timePos: 51,
};
await pollSyncClock(8300);
assert.deepEqual(syncClockCommands, [],
  "confirmed libmpv playback completes the pending Play handoff without another command");

syncClockCommands.length = 0;
syncClockSandbox.setSyncPlaybackIntent(false, 8400);
await pollSyncClock(8400);
assert.deepEqual(syncClockCommands.map((command) => command.endpoint), ["pause"],
  "an explicit Pause cancels the pending Play handoff and pauses the matching engine");

syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.projectionCalls.length = 0;
syncClockSandbox.projectionClears = 0;
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "sync-99",
  playbackState: "paused", paused: true, timePos: 52,
  timePosSampledAtUnixMs: 8149,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: false, position: 52, tidalPosition: 52,
  title: "Track", artist: "Artist", actual: "PAUSED", desired: "PAUSED",
};
await pollSyncClock(8400);
assert.equal(syncClockSandbox.syncEventLog.includes("clock"), false,
  "a stale engine sample never dispatches a TIDAL Redux TIME_UPDATE");
assert.equal(syncClockSandbox.projectionCalls.length, 0,
  "a stale engine sample never reaches native timeline projection");
assert.ok(syncClockSandbox.projectionClears > 0,
  "a stale engine sample clears any previous native timeline projection");

const transportQueueSandbox = {};
vm.createContext(transportQueueSandbox);
vm.runInContext(`${extractFunction("queueTransportCommand")}
  globalThis.queueTransportCommand = queueTransportCommand;`, transportQueueSandbox);
const queuedTransportCommands = [];
const transportQueues = new Map();
const transportInFlight = new Map();
const transportBody = { trackId: 99, playbackGeneration: 7, requestId: "sync-99" };
let completeResume;
const firstResume = transportQueueSandbox.queueTransportCommand(
  "resume", transportBody, 1,
  () => { queuedTransportCommands.push("resume"); return new Promise(resolve => { completeResume = resolve; }); },
  transportQueues, transportInFlight,
);
const duplicateResume = transportQueueSandbox.queueTransportCommand(
  "resume", transportBody, 1,
  () => { queuedTransportCommands.push("duplicate-resume"); return Promise.resolve(true); },
  transportQueues, transportInFlight,
);
const newerPause = transportQueueSandbox.queueTransportCommand(
  "pause", transportBody, 2,
  () => { queuedTransportCommands.push("pause"); return Promise.resolve(true); },
  transportQueues, transportInFlight,
);
for (let attempt = 0; attempt < 6 && queuedTransportCommands.length === 0; attempt += 1) await Promise.resolve();
assert.equal(firstResume, duplicateResume,
  "duplicate outstanding resume requests share one command for the same transport revision");
assert.deepEqual(queuedTransportCommands, ["resume"],
  "an opposing Pause waits behind the in-flight Resume instead of racing it");
completeResume(true);
assert.equal(await firstResume, true);
assert.equal(await newerPause, true);
assert.deepEqual(queuedTransportCommands, ["resume", "pause"],
  "a newer explicit Pause is preserved and executes after the Resume settles");

const staleIntentSandbox = {
  sentCommands: [],
  traceEvents: [],
  simulatedPaused: false,
  blockNextCommand: true,
  dispatchEngineCommand(endpoint) {
    this.sentCommands.push(endpoint);
    if (this.blockNextCommand) {
      this.blockNextCommand = false;
      return new Promise((resolve) => {
        this.completeBlockedCommand = (success) => {
          this.simulatedPaused = endpoint === "pause";
          resolve(success);
        };
      });
    }
    this.simulatedPaused = endpoint === "pause";
    return Promise.resolve(true);
  },
};
vm.createContext(staleIntentSandbox);
vm.runInContext(`
  let transportIntentRevision = 0;
  const transportCommandQueues = new Map();
  const transportCommandsInFlight = new Map();
  function tracePlaybackEvent(event, detail) { globalThis.traceEvents.push({ event, ...detail }); }
  function sendEngineCommandDirect(endpoint, body) {
    return globalThis.dispatchEngineCommand(endpoint, body);
  }
  ${extractFunction("queueTransportCommand")}
  ${extractFunction("sendEngineCommand")}
  globalThis.setIntentRevision = (revision) => { transportIntentRevision = revision; };
`, staleIntentSandbox);
const initialPause = staleIntentSandbox.sendEngineCommand("pause", transportBody);
for (let attempt = 0; attempt < 6 && !staleIntentSandbox.completeBlockedCommand; attempt += 1) await Promise.resolve();
staleIntentSandbox.setIntentRevision(1);
const staleQueuedResume = staleIntentSandbox.sendEngineCommand("resume", transportBody);
staleIntentSandbox.setIntentRevision(2);
const currentPause = staleIntentSandbox.sendEngineCommand("pause", transportBody);
staleIntentSandbox.completeBlockedCommand(true);
assert.equal(await initialPause, true);
assert.equal(await staleQueuedResume, false,
  "a queued Resume is discarded when a newer Pause intent arrives before dispatch");
assert.equal(await currentPause, true,
  "the current Pause intent still dispatches after the earlier command settles");
assert.deepEqual(staleIntentSandbox.sentCommands, ["pause", "pause"],
  "the stale Resume never reaches the service");
assert.equal(staleIntentSandbox.simulatedPaused, true,
  "the newer Pause remains the final engine state after the older in-flight command completes");
assert.ok(staleIntentSandbox.traceEvents.some((event) => event.event === "engine-command-stale-intent"),
  "discarded stale transport commands emit a diagnostic trace");

staleIntentSandbox.setIntentRevision(3);
assert.equal(await staleIntentSandbox.sendEngineCommand("resume", transportBody), true,
  "a fresh Resume dispatches normally after the stale Resume has been discarded");
assert.deepEqual(staleIntentSandbox.sentCommands, ["pause", "pause", "resume"],
  "stale-command cleanup does not suppress a later current Resume");
assert.equal(staleIntentSandbox.simulatedPaused, false);

staleIntentSandbox.blockNextCommand = true;
staleIntentSandbox.completeBlockedCommand = null;
staleIntentSandbox.setIntentRevision(4);
const blockedPauseForRepeatPlay = staleIntentSandbox.sendEngineCommand("pause", transportBody);
for (let attempt = 0; attempt < 6 && !staleIntentSandbox.completeBlockedCommand; attempt += 1) await Promise.resolve();
staleIntentSandbox.setIntentRevision(5);
const staleRepeatedPlayResume = staleIntentSandbox.sendEngineCommand("resume", transportBody);
// A repeated same-state Play intent advances the revision while the command is queued.
staleIntentSandbox.setIntentRevision(6);
const replacementResume = staleIntentSandbox.sendEngineCommand("resume", transportBody);
staleIntentSandbox.completeBlockedCommand(true);
assert.equal(await blockedPauseForRepeatPlay, true);
assert.equal(await staleRepeatedPlayResume, false,
  "a duplicate same-state intent cannot let its older queued transport command execute");
assert.equal(await replacementResume, true,
  "reconciliation's replacement command executes after same-state revision churn");
assert.deepEqual(staleIntentSandbox.sentCommands, ["pause", "pause", "resume", "pause", "resume"]);
assert.equal(staleIntentSandbox.simulatedPaused, false,
  "same-state revision churn recovers to the current requested playing state");

// A newer 50 ms /clock sample owns position ordering, while a fresh /status
// response with the same playback identity must still refresh transport state.
syncClockCommands.length = 0;
syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.setSyncPlaybackIntent(true, 8990);
syncClockSandbox.syncStatus = {
  trackId: 99, playbackGeneration: 7, requestId: "sync-99",
  serviceInstanceId: "sync-service", mpvInstanceId: "sync-mpv",
  timePosSampleSequence: 100,
  playbackState: "paused", paused: true, timePos: 80, duration: 180,
};
syncClockSandbox.tidalSnapshot = {
  trackId: 99, playing: false, position: 80, tidalPosition: 80,
  title: "Track", artist: "Artist", actual: "PAUSED", desired: "PAUSED",
};
syncClockSandbox.activeClockServiceInstanceId = null;
syncClockSandbox.activeClockMpvInstanceId = null;
syncClockSandbox.clockOrderingInstanceId = null;
syncClockSandbox.clockOrderingMpvInstanceId = null;
syncClockSandbox.clockOrderingSequence = -1;
await pollSyncClock(9000);
const previouslyAcceptedStatus = vm.runInContext("currentStatusData", syncClockSandbox);
syncClockSandbox.clockStatus = {
  ...previouslyAcceptedStatus,
  sampleSequence: 110,
  timePos: 80.01,
  timePosSampledAtUnixMs: 9050,
  paused: false,
};
syncClockSandbox.syncNow = 9050;
syncClockSandbox.syncClockPoll();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(syncClockSandbox.clockOrderingSequence, 110,
  "the newer read-only clock observation advances the ordering watermark");
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus,
  timePosSampleSequence: 105,
  timePos: 70,
  timePosSampledAtUnixMs: 9040,
  playbackState: "playing",
  paused: false,
};
await pollSyncClock(9100);
assert.equal(syncClockCommands.some((command) => command.endpoint === "pause"), false,
  "a fresh same-identity playing status cannot trigger a contradictory Pause");
assert.ok(syncClockCommands.every((command) => command.endpoint === "resume"),
  "only an idempotent Resume is allowed while the same-track Play intent is being reconciled");
const reconciledStatus = vm.runInContext("currentStatusData", syncClockSandbox);
assert.equal(reconciledStatus.timePos, 80.01,
  "the older /status clock value is replaced with the latest accepted fast clock sample");
assert.equal(reconciledStatus.timePosSampleSequence, 110,
  "the shared clock ordering watermark is preserved while merging status control fields");
assert.equal(reconciledStatus.playbackState, "playing",
  "same-identity /status transport metadata is refreshed instead of discarded");
assert.equal(reconciledStatus.paused, false,
  "the fast MPV pause sample remains authoritative in the merged status");
assert.ok(syncClockSandbox.syncEventLog.includes("stale-status-control-merged"),
  "reconciled status ordering is visible in the playback trace");

syncClockSandbox.syncEventLog.length = 0;
syncClockSandbox.clockStatus = {
  ...syncClockSandbox.clockStatus,
  sampleSequence: 120,
  timePos: 80.06,
  timePosSampledAtUnixMs: 9100,
  paused: false,
};
syncClockSandbox.syncNow = 9100;
syncClockSandbox.syncClockPoll();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(syncClockSandbox.lastFastClockSample.engineClockAdvancing, true,
  "two advancing MPV clock samples prove playback without depending on a stale /status state");
assert.ok(syncClockSandbox.syncEventLog.includes("clock"),
  "the high-rate clock path mirrors time into TIDAL for lyric and transport consumers");
assert.ok(syncClockSandbox.syncEventLog.includes("project"),
  "the advancing fast clock continues to project the native footer timeline");

syncClockCommands.length = 0;
syncClockSandbox.setSyncPlaybackIntent(false, 9190);
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus,
  playbackGeneration: 8,
  requestId: "sync-100",
  timePosSampleSequence: 130,
  timePos: 80.02,
  playbackState: "paused",
  paused: true,
};
await pollSyncClock(9200);
assert.equal(vm.runInContext("currentStatusData.playbackGeneration", syncClockSandbox), 8,
  "a newer valid playback generation still acquires clock authority");
assert.deepEqual(syncClockCommands, [],
  "stale-response rejection does not issue transport commands for the new paused generation");

// A status response can be accepted before an awaited transport command, then
// lose clock ordering while that command is pending. It must not project its
// older timeline after the newer /clock sample has committed.
syncClockSandbox.syncNow = 10010;
syncClockSandbox.lastMirroredPlaybackState = "playing";
syncClockSandbox.syncStatus = {
  ...syncClockSandbox.syncStatus,
  playbackGeneration: 8,
  requestId: "sync-100",
  timePosSampleSequence: 132,
  timePos: 81.01,
  timePosSampledAtUnixMs: 10010,
  playbackState: "playing",
  paused: false,
};
let finishDelayedPause;
syncClockSandbox.sendEngineCommand = async (endpoint, body) => {
  syncClockCommands.push({ endpoint, timePos: body.timePos });
  return new Promise((resolve) => { finishDelayedPause = resolve; });
};
const delayedStatusSync = syncClockSandbox.syncPoll();
for (let attempt = 0; attempt < 8 && !finishDelayedPause; attempt += 1) await Promise.resolve();
assert.equal(typeof finishDelayedPause, "function",
  "the status pass is suspended inside transport reconciliation");
assert.equal(syncClockSandbox.isSyncObservationStillCurrent(
  syncClockSandbox.syncStatus, 132,
), true, "the accepted status identity is current before the clock advances");
syncClockSandbox.clockStatus = {
  ...syncClockSandbox.syncStatus,
  sampleSequence: 133,
  timePos: 81.05,
  timePosSampledAtUnixMs: 10020,
  paused: false,
};
syncClockSandbox.syncNow = 10020;
syncClockSandbox.syncClockPoll();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(syncClockSandbox.isSyncObservationStillCurrent(
  syncClockSandbox.syncStatus, 132,
), false, "a committed fast sample invalidates the suspended status observation");
const newestProjection = syncClockSandbox.projectionCalls.at(-1);
assert.equal(newestProjection.timePos, 81.05,
  "the newer fast clock sample projects while the status command is pending");
finishDelayedPause(true);
await delayedStatusSync;
assert.equal(syncClockSandbox.projectionCalls.at(-1).timePos, 81.05,
  "the older status sample cannot overwrite the newer timeline after its await");

// The installed TIDAL build uses a custom DIV[role=slider], not a range input.
// Older plugin code left value/max expandos on this node and mistook readback
// for rendered progress. Verify it now relies on TIDAL's Redux action only.
const oldTimelineCandidates = nativeProgressFooter.candidates;
const tidalCustomProgressDiv = {
  id: "progressBar",
  tagName: "DIV",
  visible: true,
  isConnected: true,
  role: "slider",
  value: "20",
  max: "200",
  attributes: { role: "slider", "aria-valuenow": "20" },
  getAttribute(name) { return this.attributes[name] ?? null; },
  setAttribute(name, value) { this.attributes[name] = String(value); },
  matches() { return false; },
};
nativeProgressFooter.candidates = [tidalCustomProgressDiv];
timelineProjectionSandbox.nativeTimelineProjection = {
  trackId: "42", playing: true, timePos: 45, duration: 200, sampledAt: timelineNow, sampleSequence: 900,
};
timelineProjectionSandbox.lastMirroredUiTime = null;
assert.equal(vm.runInContext(
  'applyNativeTimelinePosition(45, 200, 10000, "sync")',
  timelineProjectionSandbox,
), true, "the custom TIDAL DIV receives a Redux timeline update");
assert.equal("value" in tidalCustomProgressDiv, false,
  "the custom progress DIV has no plugin-owned value expando after projection");
assert.equal("max" in tidalCustomProgressDiv, false,
  "the custom progress DIV has no plugin-owned max expando after projection");
assert.deepEqual(timelineProjectionSandbox.reduxTimelineEvents.at(-1),
  ["playbackControls/TIME_UPDATE", 45],
  "the custom progress DIV is updated through TIDAL's native Redux action");
assert.ok(timelineProjectionSandbox.reduxTimelineEvents.some((event) =>
  event[0] === "playbackControls/SET_DURATION" && event[1] === 200),
"the custom progress DIV receives the authoritative libmpv duration through TidaLuna");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.sliderAfter, null,
  "custom DIV property readback is excluded from synchronization diagnostics");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.customSliderAriaValueBefore, 20,
  "diagnostics capture TIDAL's rendered custom-slider ARIA value before Redux projection");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.customSliderAriaValueAfter, 45,
  "diagnostics read back the custom slider after the native Redux update");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.last.customSliderAriaTargetDifferenceMs, -25000,
  "the pre-projection ARIA delta is reported explicitly in milliseconds");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.customSliderAriaValueSampleCount, 1,
  "custom-slider ARIA deltas contribute to diagnostic sample counts");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.maxCustomSliderAriaTargetDifferenceMs, 25000,
  "diagnostic summaries retain the maximum absolute custom-slider ARIA delta");
assert.ok(timelineRangeObservers[0].options.attributeFilter.includes("aria-valuenow"),
  "the passive timeline observer watches React's committed custom-slider ARIA value");
const framesBeforeAriaCommit = timelineFrames.size;
timelineRangeObservers[0].trigger([{
  type: "attributes",
  attributeName: "aria-valuenow",
  target: tidalCustomProgressDiv,
}]);
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.domCommitCount, 1,
  "a committed custom-slider ARIA mutation is measured separately from the pre-dispatch value");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.lastDomCommit.dispatchSequence,
  timelineProjectionSandbox.nativeTimelineDispatchSequence,
  "the DOM commit diagnostic correlates the observed value with its matching TIME_UPDATE sequence");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.lastDomCommit.dispatchToCommitDelayMs, 0,
  "the commit diagnostic measures delay from dispatch to matching aria-valuenow mutation");
assert.equal(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.lastDomCommit.sliderTargetDifferenceMs, 0,
  "the committed slider value is compared with the engine projection at mutation time");
assert.equal(timelineFrames.size, framesBeforeAriaCommit,
  "ARIA-only commits are observed passively and do not schedule extra projection frames");
assert.ok(timelineProjectionSandbox.timelineDiagnosticMessages.some((message) =>
  message.includes('"nativeMediaClock":{"testStub":true}')),
"periodic timeline telemetry includes the passive native-media clock snapshot");

// A fresh TIDAL renderer can reset its Redux clock after the engine position
// was already mirrored. The plugin's last-mirrored cache alone must not hide
// that host-state reset from the paused footer timeline.
timelineProjectionSandbox.lastMirroredUiTime = 63.539449;
timelineProjectionSandbox.tidalPlayState.playTime = 0;
tidalCustomProgressDiv.setAttribute("aria-valuenow", "0");
timelineProjectionSandbox.nativeTimelineProjection = {
  trackId: "42", playing: false, timePos: 63.539449,
  duration: 189.466689, sampledAt: timelineNow, sampleSequence: 901,
};
const preReloadCorrectionEventCount = timelineProjectionSandbox.reduxTimelineEvents.length;
assert.equal(vm.runInContext(
  'applyNativeTimelinePosition(63.539449, 189.466689, 11000, "sync")',
  timelineProjectionSandbox,
), true, "a paused engine clock repairs a TIDAL Redux reset after reload");
assert.equal(timelineProjectionSandbox.reduxTimelineEvents.length,
  preReloadCorrectionEventCount + 2,
  "a host-state mismatch restores both the verified engine duration and position");
assert.deepEqual(timelineProjectionSandbox.reduxTimelineEvents.at(-1),
  ["playbackControls/TIME_UPDATE", 63.539449],
  "the reload correction uses TIDAL's native Redux TIME_UPDATE action");
assert.equal(timelineProjectionSandbox.tidalPlayState.playTime, 63.539449,
  "the native TIDAL playback clock recovers from zero to the confirmed engine position");
assert.equal(tidalCustomProgressDiv.getAttribute("aria-valuenow"), "63.539449",
  "the paused custom progress slider recovers from zero to the engine position");

// Verify native progress fill duration-scale correction, scrubbing guards, and
// conditional restoration without altering native aria-valuemax.
const makeCustomIndicator = (initialInline = "matrix(1, 0, 0, 1, -768, 0)", priority = "") => {
  const styleStore = { transform: initialInline };
  const priorityStore = { transform: priority };
  return {
    isConnected: true,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 1000, bottom: 4, width: 1000, height: 4 }),
    style: {
      get transform() { return styleStore.transform ?? ""; },
      set transform(v) { styleStore.transform = String(v ?? ""); priorityStore.transform = ""; },
      getPropertyValue(prop) { return prop === "transform" ? (styleStore.transform ?? "") : ""; },
      getPropertyPriority(prop) { return prop === "transform" ? (priorityStore.transform ?? "") : ""; },
      setProperty(prop, val, prio = "") {
        if (prop === "transform") {
          styleStore.transform = String(val ?? "");
          priorityStore.transform = String(prio ?? "");
        }
      },
      removeProperty(prop) {
        if (prop === "transform") {
          delete styleStore.transform;
          delete priorityStore.transform;
        }
      },
    },
  };
};
const fillIndicator = makeCustomIndicator("matrix(1, 0, 0, 1, -773.922059, 0)");
const fillSlider = {
  id: "progressBar",
  tagName: "DIV",
  visible: true,
  isConnected: true,
  attributes: {
    role: "slider",
    "aria-valuenow": "48.662",
    "aria-valuemin": "0",
    "aria-valuemax": "215.226667",
  },
  getAttribute(name) { return this.attributes[name] ?? null; },
  setAttribute(name, value) { this.attributes[name] = String(value); },
  removeAttribute(name) { delete this.attributes[name]; },
  matches(sel) { return sel === ":active" ? Boolean(this.activeState) : false; },
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 1000, bottom: 4, width: 1000, height: 4 }),
  querySelector(sel) { return sel === '[data-test="progress-indicator"]' ? fillIndicator : null; },
};
const origGetComputedStyle = timelineProjectionSandbox.window.getComputedStyle;
timelineProjectionSandbox.window.getComputedStyle = (el) => {
  const base = origGetComputedStyle(el);
  if (el === fillIndicator) {
    return { ...base, transform: fillIndicator.style.transform || "none" };
  }
  return { ...base, direction: "ltr" };
};
nativeProgressFooter.candidates = [fillSlider];
timelineProjectionSandbox.nativeTimelineProjection = {
  trackId: "42",
  playing: false,
  timePos: 48.662,
  duration: 215.168,
  sampledAt: timelineNow,
  sampleSequence: 910,
};
assert.equal(vm.runInContext(
  'applyNativeTimelinePosition(48.662, 215.168, 11500, "sync")',
  timelineProjectionSandbox,
), true, "applyNativeTimelinePosition succeeds with native fill indicator present");
assert.equal(fillSlider.getAttribute("aria-valuemax"), "215.226667",
  "native aria-valuemax is preserved untouched for accessibility consistency");
const expectedTranslateX = Number((-1000 * (1 - 48.662 / 215.168)).toFixed(6));
assert.equal(fillIndicator.style.transform,
  `matrix(1, 0, 0, 1, ${expectedTranslateX}, 0)`,
  "native progress indicator transform is rescaled to the confirmed engine duration");
assert.ok(
  Math.abs(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.lastNativeFillProjection.preCorrectionErrorMs) > 10,
  "pre-correction duration-scale error (~13.26 ms) is captured before transform update",
);
assert.ok(
  Math.abs(timelineProjectionSandbox.nativeTimelineDiagnosticWindow.lastNativeFillProjection.postCorrectionErrorMs) < 0.001,
  "post-correction duration-scale error converges under 1 microsecond",
);

// Verify active scrubbing skips transform overwrite.
fillSlider.activeState = true;
fillIndicator.style.transform = "matrix(1, 0, 0, 1, -500, 0)";
const scrubAttempt = vm.runInContext(
  "applyNativeProgressFillProjection(getPlaybackFooter().candidates[0], 48.662, 215.168)",
  timelineProjectionSandbox,
);
assert.equal(scrubAttempt.applied, false, "fill projection pauses while slider is active");
assert.equal(fillIndicator.style.transform, "matrix(1, 0, 0, 1, -500, 0)",
  "active user scrubbing transform is never overwritten");
fillSlider.activeState = false;

// Verify conditional cleanup does not clobber a newer React transform, and
// restores original inline transform when the last plugin-written value is still present.
vm.runInContext(
  "applyNativeProgressFillProjection(getPlaybackFooter().candidates[0], 48.662, 215.168)",
  timelineProjectionSandbox,
);
fillIndicator.style.transform = "matrix(1, 0, 0, 1, -250, 0)";
vm.runInContext("restoreNativeProgressFillProjection()", timelineProjectionSandbox);
assert.equal(fillIndicator.style.transform, "matrix(1, 0, 0, 1, -250, 0)",
  "conditional cleanup preserves a newer React-committed transform on unload");

const cleanIndicator = makeCustomIndicator("matrix(1, 0, 0, 1, -800, 0)", "important");
fillSlider.querySelector = (sel) => (sel === '[data-test="progress-indicator"]' ? cleanIndicator : null);
timelineProjectionSandbox.window.getComputedStyle = (el) => {
  const base = origGetComputedStyle(el);
  if (el === cleanIndicator) return { ...base, transform: cleanIndicator.style.transform || "none" };
  return { ...base, direction: "ltr" };
};
vm.runInContext(
  "applyNativeProgressFillProjection(getPlaybackFooter().candidates[0], 107.584, 215.168)",
  timelineProjectionSandbox,
);
assert.equal(cleanIndicator.style.transform, "matrix(1, 0, 0, 1, -500, 0)",
  "50% engine position maps to -50% indicator width");
assert.equal(cleanIndicator.style.getPropertyPriority("transform"), "important",
  "original inline transform priority is preserved when writing corrected matrix");
vm.runInContext("clearNativeTimelineProjection()", timelineProjectionSandbox);
assert.equal(cleanIndicator.style.transform, "matrix(1, 0, 0, 1, -800, 0)",
  "clearNativeTimelineProjection restores the original inline transform when unmodified by React");
timelineProjectionSandbox.window.getComputedStyle = origGetComputedStyle;

nativeProgressFooter.candidates = oldTimelineCandidates;
