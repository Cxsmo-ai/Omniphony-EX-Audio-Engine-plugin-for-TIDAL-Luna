import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { syncManifestHashes } from "../tools/sync-manifest-hash.mjs";

test("build synchronization pins repository manifests to the source module they serve", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "omniphony-manifest-hash-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dist = path.join(root, "dist");
  await mkdir(dist);

  const pluginName = "Omniphony EX Audio Engine";
  const moduleName = `${pluginName}.mjs`;
  const sourceModuleBytes = Buffer.from("export const source = true;\r\n");
  const builtModuleBytes = Buffer.from("export const built = true;\n");
  const expectedHash = createHash("sha256")
    .update(sourceModuleBytes.toString("utf8").replace(/\r\n/g, "\n"), "utf8")
    .digest("hex")
    .toUpperCase();
  await writeFile(path.join(dist, "store.json"), JSON.stringify({ plugins: [moduleName] }));
  await writeFile(path.join(dist, moduleName), builtModuleBytes);
  await writeFile(path.join(dist, `${pluginName}.json`), JSON.stringify({ name: pluginName }));
  await writeFile(path.join(root, "omniphony.mjs"), sourceModuleBytes);

  for (const filename of ["manifest.json", "omniphony.json"]) {
    await writeFile(path.join(root, filename), JSON.stringify({
      name: pluginName, main: "omniphony.mjs?version=2.1.51&service=2.1.34", hash: "stale",
    }));
  }

  assert.equal(await syncManifestHashes(root), expectedHash);
  for (const filename of ["manifest.json", "omniphony.json"]) {
    const manifest = JSON.parse(await readFile(path.join(root, filename), "utf8"));
    assert.equal(manifest.hash, expectedHash, `${filename} matches the source module bytes`);
  }
});
