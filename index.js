/**
 * Omniphony EX Audio Engine for TIDAL Luna
 * Bundled zero-dependency lifecycle hooks
 */
import * as omniphony from './omniphony.mjs';

export const unloads = omniphony.unloads || new Set();

let serviceProcess = null;

export async function onLoad() {
  console.log('[Omniphony EX] Initializing ultra-native TidaLuna plugin with bundled API...');
  
  // Auto-launch the bundled API service if it is not already running
  try {
    const res = await fetch('http://127.0.0.1:8765/health', { method: 'GET' }).catch(() => null);
    if (!res || !res.ok) {
      console.log('[Omniphony EX] Local audio service offline, spawning bundled runtime...');
      if (window.require) {
        const { spawn } = window.require('child_process');
        const path = window.require('path');
        const apiDir = path.join(__dirname, 'api');
        const pythonExe = path.join(apiDir, 'runtime', 'python.exe');
        const scriptPath = path.join(apiDir, 'omnifusion_service.py');
        serviceProcess = spawn(pythonExe, [scriptPath], {
          cwd: apiDir,
          windowsHide: true,
          stdio: 'ignore'
        });
        serviceProcess.unref();
        console.log('[Omniphony EX] Spawned bundled background service successfully');
      }
    } else {
      console.log('[Omniphony EX] Local audio engine already running');
    }
  } catch (err) {
    console.warn('[Omniphony EX] Service check error:', err);
  }
}

unloads.add(() => {
  // Send graceful shutdown to OmniFusion API service and MPV on TIDAL exit/unload
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
