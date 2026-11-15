import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = await readFile(path.join(root, "omniphony.mjs"), "utf8");
const positionFunction = source.match(/function getTidalPosition\(playState\) \{[\s\S]*?\n\}/)?.[0];

test("TIDAL position reads Redux playTime without touching the legacy activePlayer getter", () => {
  assert.ok(positionFunction, "getTidalPosition exists");
  const executableFunction = positionFunction.replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(executableFunction, /(?:playState|PlayState)(?:\?\.)?\.currentTime/);
  assert.doesNotMatch(source, /PlayState\?\.activePlayer/);

  const getPosition = vm.runInNewContext(`${positionFunction}; getTidalPosition`, {
    parsePlaybackPosition(value) {
      return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
    },
  });
  const playState = {
    playTime: 42.25,
    get currentTime() {
      throw new Error("legacy getter must not be accessed");
    },
  };

  assert.equal(getPosition(playState), 42.25);
  assert.equal(getPosition({ playTime: 0 }), 0);
  assert.equal(getPosition({ playTime: Number.NaN }), null);
  assert.equal(getPosition(null), null);
  assert.equal(getPosition({ get playTime() { throw new Error("not ready"); } }), null);
});
