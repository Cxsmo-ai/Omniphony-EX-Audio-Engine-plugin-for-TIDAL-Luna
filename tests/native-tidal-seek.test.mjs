import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = await readFile(path.join(root, "omniphony.mjs"), "utf8");
const pairedFunction = source.match(
  /function isPairedNativeTimelineSeek\([\s\S]*?\n\}/,
)?.[0];
const preserveFunction = source.match(
  /function shouldPreservePlaybackForTidalSeek\([\s\S]*?\n\}/,
)?.[0];
const deferResumeFunction = source.match(
  /function shouldDeferResumeDuringTidalSeek\([\s\S]*?\n\}/,
)?.[0];
const reconciliationFunction = source.match(
  /function getPlaybackReconciliationCommand\([\s\S]*?\n\}/,
)?.[0];
assert.ok(pairedFunction, "paired native/Redux seek detector exists");
assert.ok(preserveFunction, "playing seek transport guard exists");
assert.ok(deferResumeFunction, "pending seek resume guard exists");
assert.ok(reconciliationFunction, "transport command decision exists");

function makeHarness({ intentRevision = 4, desiredIntent = true, disposed = false } = {}) {
  const context = {
    transportIntentRevision: intentRevision,
    desiredPlaybackIntent: desiredIntent,
    pluginDisposed: disposed,
    SEEK_RECONCILIATION_WINDOW_MS: 2000,
    SEEK_PENDING_PLAYBACK_HOLD_MS: 33_000,
    parsePlaybackPosition(value) {
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 ? number : null;
    },
  };
  vm.createContext(context);
  vm.runInContext(
    [
      pairedFunction,
      preserveFunction,
      deferResumeFunction,
      reconciliationFunction,
      "globalThis.paired = isPairedNativeTimelineSeek;",
      "globalThis.preserve = shouldPreservePlaybackForTidalSeek;",
      "globalThis.deferResume = shouldDeferResumeDuringTidalSeek;",
      "globalThis.command = getPlaybackReconciliationCommand;",
    ].join("\n"),
    context,
  );
  return context;
}

test("native pointer seek and TIDAL Redux seek for one scrub are coalesced", () => {
  const { paired } = makeHarness();
  const first = { trackId: 227401768, position: 30.42785, at: 1000, source: "native-ui" };
  assert.equal(paired(first, 227401768, 30.19139, "redux", 1002), true);
  assert.equal(paired(first, 227401768, 30.95, "redux", 1002), false);
  assert.equal(paired(first, 227401768, 30.4, "native-ui", 1002), false);
  assert.equal(paired(first, 227401769, 30.4, "redux", 1002), false);
  assert.equal(paired(first, 227401768, 30.4, "redux", 1200), false);
  assert.equal(paired({ ...first, failed: true }, 227401768, 30.4, "redux", 1002), false);
});

test("an in-flight seek keeps a previously playing engine from being paused by TIDAL seek state", () => {
  const { preserve } = makeHarness();
  const intent = {
    trackId: 227401768,
    playbackGeneration: 9,
    requestId: "seek-test",
    at: 1000,
    pending: true,
    preservePlaying: true,
    transportIntentRevision: 4,
  };
  const status = {
    trackId: 227401768,
    playbackGeneration: 9,
    requestId: "seek-test",
    playbackState: "playing",
    paused: false,
  };
  assert.equal(preserve(intent, 227401768, status, 4500), true);
  assert.equal(preserve({ ...intent, pending: false, settledAt: 1000 }, 227401768, status, 2999), true);
  assert.equal(preserve({ ...intent, pending: false, settledAt: 1000 }, 227401768, status, 3001), false);
  assert.equal(preserve({ ...intent, pending: false, failed: true, settledAt: 3500 }, 227401768, status, 32999), true);
  assert.equal(preserve({ ...intent, pending: false, failed: true, settledAt: 3500 }, 227401768, status, 34001), false);
});

test("an explicit pause or a different playback identity cancels seek playback preservation", () => {
  const base = {
    trackId: 227401768,
    playbackGeneration: 9,
    requestId: "seek-test",
    at: 1000,
    pending: true,
    preservePlaying: true,
    transportIntentRevision: 4,
  };
  const status = { trackId: 227401768, playbackGeneration: 9, requestId: "seek-test" };
  assert.equal(makeHarness({ intentRevision: 5 }).preserve(base, 227401768, status, 1100), false);
  assert.equal(makeHarness({ desiredIntent: false }).preserve(base, 227401768, status, 1100), false);
  assert.equal(makeHarness({ disposed: true }).preserve(base, 227401768, status, 1100), false);
  assert.equal(makeHarness().preserve(base, 227401769, status, 1100), false);
  assert.equal(makeHarness().preserve(base, 227401768, { ...status, playbackGeneration: 10 }, 1100), false);
  assert.equal(makeHarness().preserve(base, 227401768, { ...status, playbackState: "failed" }, 1100), false);
});

test("a transient pause during a pending seek does not race the seek with resume", () => {
  const { deferResume } = makeHarness();
  const intent = {
    trackId: 227401768,
    playbackGeneration: 9,
    requestId: "seek-test",
    at: 1000,
    pending: true,
    preservePlaying: true,
    transportIntentRevision: 4,
  };
  const pausedStatus = {
    trackId: 227401768,
    playbackGeneration: 9,
    requestId: "seek-test",
    paused: true,
  };
  assert.equal(deferResume(intent, 227401768, pausedStatus, 1100), true);
  assert.equal(deferResume({ ...intent, pending: false }, 227401768, pausedStatus, 1100), false);
  assert.equal(
    makeHarness({ intentRevision: 5 }).deferResume(intent, 227401768, pausedStatus, 1100),
    false,
  );
});

test("pending seek reconciliation emits no transport command, while explicit pause wins", () => {
  const { command } = makeHarness();
  assert.equal(command({
    transportPlaying: true,
    enginePlaying: false,
    resumeNeeded: true,
    deferResume: true,
  }), null);
  assert.equal(command({
    transportPlaying: false,
    enginePlaying: true,
    resumeNeeded: false,
    deferResume: false,
  }), "pause");
  assert.equal(command({
    transportPlaying: true,
    enginePlaying: false,
    resumeNeeded: true,
    deferResume: false,
  }), "resume");
});

test("the sync loop applies seek preservation before reconciling pause state", () => {
  const start = source.indexOf("const seekPreservesPlayback = shouldPreservePlaybackForTidalSeek(");
  const end = source.indexOf("const desiredState = transportPlaying ?", start);
  assert.ok(start >= 0 && end > start, "seek transport preservation is part of sync reconciliation");
  const reconciliation = source.slice(start, end);
  assert.match(reconciliation, /\|\| seekPreservesPlayback/);
  assert.match(source, /const needsResume = shouldResumeEngine\(/);
  assert.match(source, /const reconciliationCommand = getPlaybackReconciliationCommand\(/);
});
