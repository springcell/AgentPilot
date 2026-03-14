/**
 * 文件工具 - listDir, readFile
 */
import fs from 'fs';
import path from 'path';
import { normalizeAndCheckPath } from '../security/pathPolicy.js';

const LIST_DIR_TIMEOUT_MS = 5000;
const LIST_DIR_MAX_ENTRIES = 500;

function expandEnvVars(p) {
  if (!p || typeof p !== 'string') return p;
  return p
    .replace(/\$env:(\w+)/gi, (_, k) => process.env[k] ?? `$env:${k}`)
    .replace(/%(\w+)%/g, (_, k) => process.env[k] ?? `%${k}%`);
}

function getAllowedRoots() {
  try {
    const config = JSON.parse(
      fs.readFileSync(path.join(process.cwd(), 'config.json'), 'utf-8')
    );
    return config?.execution?.allowedRoots || [process.cwd(), '.'];
  } catch (_) {
    return [process.cwd(), '.'];
  }
}

export async function listDir(args) {
  const rawPath = args?.path || '.';
  const roots = getAllowedRoots();
  const safePath = normalizeAndCheckPath(rawPath, roots);

  const listPromise = fs.promises.readdir(safePath, { withFileTypes: true }).then((entries) => {
    const limited = entries.slice(0, LIST_DIR_MAX_ENTRIES).map((e) => ({
      name: e.name,
      isDir: e.isDirectory(),
    }));
    const truncated = entries.length > LIST_DIR_MAX_ENTRIES;
    return { entries: limited, truncated, total: entries.length };
  });

  const timeoutPromise = new Promise((_, reject) =>
    setTimeout(() => reject(new Error('list_dir timeout (5s)')), LIST_DIR_TIMEOUT_MS)
  );

  const result = await Promise.race([listPromise, timeoutPromise]);
  if (result.truncated) {
    return result.entries.concat([{ name: `... and ${result.total - LIST_DIR_MAX_ENTRIES} more`, isDir: false }]);
  }
  return result.entries;
}

export async function readFile(args) {
  const rawPath = args?.path;
  if (!rawPath) throw new Error('path is required');
  const expandedPath = expandEnvVars(rawPath);
  const roots = getAllowedRoots();
  const safePath = normalizeAndCheckPath(expandedPath, roots);
  return fs.readFileSync(safePath, 'utf-8');
}

export async function writeFile(args) {
  const rawPath = args?.path;
  const content = (args?.content ?? '').replace(/\\n/g, '\n');
  if (!rawPath) throw new Error('path is required');
  const expandedPath = expandEnvVars(rawPath);
  const roots = getAllowedRoots();
  const safePath = normalizeAndCheckPath(expandedPath, roots);

  fs.appendFileSync(path.join(process.cwd(), 'write_debug.log'), `[${new Date().toISOString()}] path: ${safePath}\n`);

  fs.writeFileSync(safePath, String(content), 'utf-8');
  return `saved: ${safePath}`;
}
