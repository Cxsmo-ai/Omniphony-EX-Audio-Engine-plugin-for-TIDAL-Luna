/**
 * Omniphony EX Audio Engine for TIDAL Luna
 * Bundled zero-dependency lifecycle hooks
 */
import * as omniphony from './omniphony.mjs';

export const unloads = omniphony.unloads || new Set();

let serviceProcess = null;
let isSpawningService = false;

async function ensureLocalServiceRunning() {
  if (!window?.require) {
    console.error('[Omniphony EX] TidaLuna native Node access is unavailable; cannot manage the bundled service.');
    return false;
  }
  const path = window.require('path');
  const fs = window.require('fs');
  const pluginRoot = path.resolve(__dirname);
  const dataRoot = path.join(pluginRoot, 'data');
  const apiDir = path.join(pluginRoot, 'api');
  const pythonExe = path.join(apiDir, 'runtime', 'python.exe');
  const scriptPath = path.join(apiDir, 'omnifusion_service.py');
  let runtimePaths = {};
  try {
    runtimePaths = JSON.parse(fs.readFileSync(path.join(dataRoot, 'config', 'runtime-paths.json'), 'utf8'));
  } catch (_) {}
  const mpvDir = String(runtimePaths?.mpvDir || '').trim()
    ? path.resolve(String(runtimePaths.mpvDir).trim())
    : path.join(pluginRoot, 'external', 'mpv');
  const studioExe = String(runtimePaths?.studioExe || '').trim()
    ? path.resolve(String(runtimePaths.studioExe).trim())
    : path.join(pluginRoot, 'external', 'omniphony-studio-egui.exe');
  const missingRuntimeFiles = [
    path.join(mpvDir, 'mpv.exe'),
    path.join(mpvDir, 'orender.dll'),
    studioExe,
  ].filter((file) => !fs.existsSync(file));
  if (missingRuntimeFiles.length) {
    console.error('[Omniphony EX] Runtime paths are incomplete:', missingRuntimeFiles);
    return false;
  }
  const samePath = (left, right) => typeof left === 'string' && typeof right === 'string'
    && path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
  const readHealth = async () => {
    try {
      const response = await fetch('http://127.0.0.1:8765/health', { cache: 'no-store' });
      return response.ok ? await response.json() : null;
    } catch (_) {
      return null;
    }
  };
  const healthMatches = (health) => health?.status === 'ok'
    && health.version === '2.1.29'
    && samePath(health.pluginRoot, pluginRoot)
    && samePath(health.dataRoot, dataRoot)
    && samePath(health.mpvDirectory, mpvDir)
    && samePath(health.studioExecutable, studioExe);
  const stopOwnedService = async (health) => {
    if (!samePath(health?.pluginRoot, pluginRoot)) return false;
    try {
      await fetch('http://127.0.0.1:8765/shutdown', {
        method: 'POST',
        headers: { Origin: 'http://127.0.0.1:8765' },
        cache: 'no-store',
      });
    } catch (_) {}
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (!(await readHealth())) return true;
    }
    return false;
  };

  if (isSpawningService) {
    for (let i = 0; i < 30; i++) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (healthMatches(await readHealth())) return true;
    }
    return false;
  }

  isSpawningService = true;
  try {
    const health = await readHealth();
    if (health?.status === 'ok') {
      if (healthMatches(health)) {
        console.log('[Omniphony EX] Local audio engine already running with the configured MPV and Studio paths');
        return true;
      }
      if (!(await stopOwnedService(health))) {
        console.error('[Omniphony EX] Existing plugin service could not be safely restarted for the configured paths');
        return false;
      }
    }
    console.log('[Omniphony EX] Audio service offline, launching bundled runtime...');
    const { spawn } = window.require('child_process');
    const processEnv = window.require('process').env;
    if (!fs.existsSync(pythonExe) || !fs.existsSync(scriptPath)) {
      throw new Error('Bundled Python runtime or service script is missing');
    }
    serviceProcess = spawn(pythonExe, [scriptPath], {
      cwd: apiDir,
      windowsHide: true,
      stdio: 'ignore',
      detached: true,
      env: {
        ...processEnv,
        OMNIFUSION_PLUGIN_DIR: pluginRoot,
        OMNIFUSION_DATA_DIR: dataRoot,
        OMNIFUSION_MPV_DIR: mpvDir,
        OMNIFUSION_STUDIO_EXE: studioExe,
        OMNIFUSION_OXIDE_DIR: path.join(apiDir, 'oxideav'),
        OMNIFUSION_HARLETTY_DIR: path.join(apiDir, 'harletty', 'v0.9.0'),
      },
    });
    serviceProcess.unref();
    console.log('[Omniphony EX] Spawned bundled background service successfully');

    // Wait for the service to report the exact paths used by this plugin.
    for (let i = 0; i < 30; i++) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (healthMatches(await readHealth())) {
        console.log('[Omniphony EX] Bundled service became healthy!');
        return true;
      }
    }
  } catch (err) {
    console.warn('[Omniphony EX] Could not start bundled service with the configured paths:', err);
  } finally {
    isSpawningService = false;
  }
  return false;
}

export async function onLoad() {
  console.log('[Omniphony EX] Initializing ultra-native TidaLuna plugin with bundled API...');
  await ensureLocalServiceRunning();
}

unloads.add(() => {
  // Do NOT terminate the background engine on plugin / UI reload!
  // The service watchdog in omnifusion_service.py automatically monitors TIDAL.exe
  // and terminates MPV and the service cleanly when TIDAL closes.
  console.log('[Omniphony EX] Plugin unhooked; background audio engine preserved for session');
});

export { Settings } from "./omniphony.mjs";
