/**
 * 审计日志
 */
import fs from 'fs';
import path from 'path';

const LOG_PATH = path.join(process.cwd(), 'logs', 'agent.log');

export default function log(data) {
  try {
    const dir = path.dirname(LOG_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const line =
      JSON.stringify({
        time: Date.now(),
        ...data,
      }) + '\n';
    fs.appendFileSync(LOG_PATH, line, 'utf8');
  } catch (e) {
    console.error('[Logger]', e.message);
  }
}
