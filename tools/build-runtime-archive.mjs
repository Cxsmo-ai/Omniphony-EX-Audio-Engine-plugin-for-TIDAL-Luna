import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bundledPython = path.join(root, "api", "runtime", "python.exe");
const python = existsSync(bundledPython) ? bundledPython : (process.env.PYTHON || "python");
const outputIndex = process.argv.indexOf("--output");
const output = outputIndex >= 0 ? process.argv[outputIndex + 1] : undefined;
if (!output) {
  console.error("Usage: node tools/build-runtime-archive.mjs --output <archive-path>");
  process.exit(2);
}

const result = spawnSync(python, [
  path.join(root, "tools", "package-runtime.py"),
  "--repo-root", root,
  "--output", path.resolve(root, output),
], { cwd: root, stdio: "inherit", windowsHide: true });
if (result.error) {
  console.error(`Runtime archive builder could not start Python: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
