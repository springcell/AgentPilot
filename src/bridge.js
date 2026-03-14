/**
 * ChatGPT Web 桥接 - 纯对话模式
 * 用户输入 → ChatGPT Web → 返回回复文本
 */
import * as chatgptWeb from './chatgpt-web-client.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

let _config = null;
function loadConfig() {
  if (_config) return _config;
  try {
    _config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  } catch (e) {
    throw new Error('config.json read failed: ' + e.message);
  }
  return _config;
}

const chatOpts = (cdp) => ({
  cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
  chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
  pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
  pollTimeoutMs: cdp.pollTimeoutMs ?? 120000,
  pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
});

export default {
  async chat(userInput, options = {}) {
    const config = loadConfig();
    const cdp = config?.llm?.cdp ?? {};

    try {
      const result = await chatgptWeb.chat(userInput, {
        ...chatOpts(cdp),
        newChat: options.newChat ?? false,
      });

      const text = result?.text || '';
      return { ok: true, result: text };
    } catch (e) {
      if (e.message?.startsWith('CF_BLOCKED')) {
        return {
          ok: false,
          error: e.message.replace('CF_BLOCKED:', '').trim(),
          code: 'CF_BLOCKED',
        };
      }
      throw e;
    }
  },
};
