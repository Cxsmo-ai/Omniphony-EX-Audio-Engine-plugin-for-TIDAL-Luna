import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export async function syncManifestHashes(root = process.cwd()) {
  const rootPath = path.resolve(root);
  const distPath = path.join(rootPath, "dist");
  const store = JSON.parse(await readFile(path.join(distPath, "store.json"), "utf8"));
  if (!Array.isArray(store.plugins) || store.plugins.length === 0) {
    throw new Error("built store catalog has no plugin modules");
  }

  const manifests = await Promise.all(
    ["manifest.json", "omniphony.json"].map(async (filename) => {
      const manifestPath = path.join(rootPath, filename);
      const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
      return { filename, manifestPath, manifest };
    }),
  );
  const names = new Set(manifests.map(({ manifest }) => manifest.name));
  if (names.size !== 1) throw new Error("repository plugin manifests identify different plugins");

  const candidates = [];
  for (const filename of store.plugins) {
    if (typeof filename !== "string" || path.basename(filename) !== filename || !filename.endsWith(".mjs")) {
      throw new Error(`invalid built plugin module name: ${String(filename)}`);
    }
    const modulePath = path.join(distPath, filename);
    const packagePath = modulePath.replace(/\.mjs$/i, ".json");
    const pluginPackage = JSON.parse(await readFile(packagePath, "utf8"));
    if (names.has(pluginPackage.name)) {
      candidates.push({ filename, modulePath });
    }
  }
  if (candidates.length !== 1) {
    throw new Error(`expected one built module for the repository plugin; found ${candidates.length}`);
  }

  const { filename } = candidates[0];
  const sourceModuleName = manifests[0].manifest.main.split("?", 1)[0];
  if (!sourceModuleName || path.basename(sourceModuleName) !== sourceModuleName) {
    throw new Error(`repository manifest main must name a root module: ${manifests[0].manifest.main}`);
  }
  if (manifests.some(({ manifest }) => manifest.main.split("?", 1)[0] !== sourceModuleName)) {
    throw new Error("repository plugin manifests point to different source modules");
  }
  const normalizedBytes = (await readFile(path.join(rootPath, sourceModuleName)))
    .toString("utf8")
    .replace(/\r\n/g, "\n");
  const hash = createHash("sha256").update(normalizedBytes, "utf8").digest("hex").toUpperCase();
  for (const { filename: manifestFilename, manifestPath, manifest } of manifests) {
    if (manifest.hash === hash) continue;
    manifest.hash = hash;
    await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    console.log(`Synchronized ${manifestFilename} to source ${sourceModuleName} SHA-256 ${hash} (store module ${filename})`);
  }
  return hash;
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    await syncManifestHashes();
  } catch (error) {
    console.error(`Manifest hash synchronization failed: ${error.message}`);
    process.exitCode = 1;
  }
}
