/**
 * Omniphony EX Audio Engine for TIDAL Luna
 * Bundled zero-dependency lifecycle hooks
 */
import * as omniphony from './omniphony.mjs';

export const unloads = omniphony.unloads || new Set();

let serviceProcess = null;
let isSpawningService = false;

async function ensureLocalServiceRunning() {
  try {
    const res = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
    if (res && res.ok) {
      console.log('[Omniphony EX] Local audio engine already running and healthy');
      return true;
    }
  } catch (_) {}

  // Guard against concurrent spawn calls during reload
  if (isSpawningService) {
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 300));
      const check = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
      if (check && check.ok) return true;
    }
  }

  isSpawningService = true;
  console.log('[Omniphony EX] Audio service offline, launching bundled runtime...');
  if (window.require) {
    try {
      const { spawn } = window.require('child_process');
      const path = window.require('path');
      const apiDir = path.join(__dirname, 'api');
      const pythonExe = path.join(apiDir, 'runtime', 'python.exe');
      const scriptPath = path.join(apiDir, 'omnifusion_service.py');
      
      serviceProcess = spawn(pythonExe, [scriptPath], {
        cwd: apiDir,
        windowsHide: true,
        stdio: 'ignore',
        detached: true
      });
      serviceProcess.unref();
      console.log('[Omniphony EX] Spawned bundled background service successfully');
      
      // Wait up to 5 seconds for health
      for (let i = 0; i < 10; i++) {
        await new Promise((r) => setTimeout(r, 500));
        const check = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
        if (check && check.ok) {
          console.log('[Omniphony EX] Bundled service became healthy!');
          isSpawningService = false;
          return true;
        }
      }
    } catch (err) {
      console.warn('[Omniphony EX] Could not spawn bundled service via child_process:', err);
    }
  }
  isSpawningService = false;
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
