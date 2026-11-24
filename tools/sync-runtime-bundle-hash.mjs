import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PIN_PATTERN = /(const bundleSha256 = ")[A-Fa-f0-9]{64}(";)/g;

export async function syncRuntimeBundleHash({ root = process.cwd(), archive }) {
  if (!archive) throw new Error("runtime archive path is required");
  const rootPath = path.resolve(root);
  const modulePath = path.join(rootPath, "omniphony.mjs");
  const archivePath = path.isAbsolute(archive) ? archive : path.join(rootPath, archive);
  const [moduleText, archiveBytes] = await Promise.all([
    readFile(modulePath, "utf8"),
    readFile(archivePath),
  ]);
  const matches = [...moduleText.matchAll(PIN_PATTERN)];
  if (matches.length !== 1) {
    throw new Error(`expected exactly one literal bundleSha256 pin; found ${matches.length}`);
  }
  const digest = createHash("sha256").update(archiveBytes).digest("hex").toUpperCase();
  const updated = moduleText.replace(PIN_PATTERN, `$1${digest}$2`);
  if (updated !== moduleText) await writeFile(modulePath, updated, "utf8");
  console.log(`Synchronized plugin runtime bundle SHA-256 to ${digest}`);
  return digest;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const archiveIndex = process.argv.indexOf("--archive");
  const archive = archiveIndex >= 0 ? process.argv[archiveIndex + 1] : undefined;
  try {
    await syncRuntimeBundleHash({ archive });
  } catch (error) {
    console.error(`Runtime bundle hash synchronization failed: ${error.message}`);
    process.exitCode = 1;
  }
}
