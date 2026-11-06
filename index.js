import * as omniphony from './omniphony.mjs';

export const unloads = omniphony.unloads || new Set();

export function onLoad() {
  console.log('[Omniphony EX] Initializing plugin...');
}
