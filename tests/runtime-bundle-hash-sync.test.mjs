import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { syncRuntimeBundleHash } from "../tools/sync-runtime-bundle-hash.mjs";

const root = await mkdtemp(path.join(os.tmpdir(), "omniphony-bundle-pin-"));
try {
  const source = 'const bundleSha256 = "' + "0".repeat(64) + '";\n';
  const archive = Buffer.from("deterministic-runtime-archive");
  await writeFile(path.join(root, "omniphony.mjs"), source);
  await writeFile(path.join(root, "runtime.tar.gz"), archive);

  const expected = createHash("sha256").update(archive).digest("hex").toUpperCase();
  assert.equal(await syncRuntimeBundleHash({ root, archive: "runtime.tar.gz" }), expected);
  assert.equal(await readFile(path.join(root, "omniphony.mjs"), "utf8"),
    `const bundleSha256 = "${expected}";\n`);
  assert.equal(await syncRuntimeBundleHash({ root, archive: "runtime.tar.gz" }), expected,
    "repeating the build synchronization is idempotent");

  await writeFile(path.join(root, "omniphony.mjs"), "const bundleSha256 = getRuntimeHash();\n");
  await assert.rejects(
    syncRuntimeBundleHash({ root, archive: "runtime.tar.gz" }),
    /expected exactly one literal bundleSha256 pin; found 0/,
  );
} finally {
  await rm(root, { recursive: true, force: true });
}

console.log("Build-time runtime bundle pin synchronization checks passed.");
