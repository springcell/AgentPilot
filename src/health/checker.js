/**
 * Health checker - config validation, optional CDP reachability
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'config.json');

let isHealthy = true;
let lastCheck = 0;
let lastError = null;

export { isHealthy, lastCheck, lastError };

export async function checkHealth() {
  try {
    if (!fs.existsSync(CONFIG_PATH)) {
      isHealthy = false;
      lastError = 'config.json not found';
      lastCheck = Date.now();
      return;
    }
    const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
    JSON.parse(raw);
    isHealthy = true;
    lastError = null;
  } catch (e) {
    isHealthy = false;
    lastError = e.message || 'config invalid';
  }
  lastCheck = Date.now();
}
