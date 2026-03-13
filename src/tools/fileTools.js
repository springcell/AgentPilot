/**
 * 文件工具 - listDir, readFile
 */
import fs from 'fs';
import path from 'path';
import { normalizeAndCheckPath } from '../security/pathPolicy.js';

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
  return fs.readdirSync(safePath, { withFileTypes: true }).map((e) => ({
    name: e.name,
    isDir: e.isDirectory(),
  }));
}

export async function readFile(args) {
  const rawPath = args?.path;
  if (!rawPath) throw new Error('path is required');
  const roots = getAllowedRoots();
  const safePath = normalizeAndCheckPath(rawPath, roots);
  return fs.readFileSync(safePath, 'utf-8');
}
