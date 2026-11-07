/**
 * Omniphony EX Audio Engine for TIDAL Luna
 * Bundled zero-dependency lifecycle hooks
 */
import * as omniphony from './omniphony.mjs';

export const unloads = omniphony.unloads || new Set();

let serviceProcess = null;

async function ensureLocalServiceRunning() {
  try {
    const res = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
    if (res && res.ok) {
      console.log('[Omniphony EX] Local audio engine already running');
      return true;
    }
  } catch (_) {}

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
      
      // Wait up to 3 seconds for health
      for (let i = 0; i < 6; i++) {
        await new Promise((r) => setTimeout(r, 500));
        const check = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
        if (check && check.ok) {
          console.log('[Omniphony EX] Bundled service became healthy!');
          break;
        }
      }
      return true;
    } catch (err) {
      console.warn('[Omniphony EX] Could not spawn bundled service via child_process:', err);
    }
  }
  return false;
}

export async function onLoad() {
  console.log('[Omniphony EX] Initializing ultra-native TidaLuna plugin with bundled API...');
  await ensureLocalServiceRunning();
}

unloads.add(() => {
  try {
    if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
      navigator.sendBeacon('http://127.0.0.1:8765/shutdown', '{}');
    } else {
      fetch('http://127.0.0.1:8765/shutdown', { method: 'POST', body: '{}', keepalive: true }).catch(() => {});
    }
  } catch (_) {}
  if (serviceProcess) {
    try {
      serviceProcess.kill();
    } catch (_) {}
  }
});

export { Settings } from "./omniphony.mjs";
