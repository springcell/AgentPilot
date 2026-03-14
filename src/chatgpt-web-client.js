/**
 * ChatGPT Web Client - 通过 Chrome CDP + DOM 模拟完成聊天
 * 
 * 认证: CDP 连接 → 打开 chatgpt.com → 等待登录 (检测 cookie) → 返回 cookie + userAgent
 * 聊天: DOM 模拟 - 填输入框、点发送、轮询 DOM 取最后一条模型回复
 * 
 * 参考: https://github.com/linuxhsj/openclaw-zero-token
 */

import puppeteer from 'puppeteer-core';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUTH_CACHE_PATH = path.join(__dirname, '..', '.chatgpt-auth.json');

// ChatGPT 网页 DOM 选择器 (随官网更新可能需要调整)
const SELECTORS = {
  // 主输入区域 - ChatGPT 使用 contenteditable div 或 textarea
  input: [
    '#prompt-textarea',
    'textarea[data-id="root"]',
    '[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"]',
    'textarea',
  ],
  sendButton: [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send"]',
    'button[type="submit"]',
    'form button[type="submit"]',
  ],
  // 最后一条模型回复（ChatGPT 官网 DOM 可能变化，多备选）
  lastReply: [
    '[data-message-author-role="assistant"]',
    '[data-testid="conversation-turn"]',
    'div[data-message-id]',
    '[class*="markdown"]',
    'div.markdown',
    'article',
    '[class*="Message"]',
  ],
  // 检测已登录 (有用户菜单或新对话按钮)
  loggedIn: [
    '[data-testid="user-menu"]',
    'button[aria-label*="New chat"]',
    'a[href="/"]',
    'nav',
  ],
};

/**
 * 连接已启动的 Chrome (需带 --remote-debugging-port=9222)
 */
export async function connectChrome(cdpUrl = 'http://127.0.0.1:9222') {
  const browser = await puppeteer.connect({
    browserURL: cdpUrl,
    defaultViewport: null,
  });
  return browser;
}

let browserInstance = null;
let _connectingPromise = null;

async function getBrowser(cdpUrl) {
  if (browserInstance) {
    try {
      await browserInstance.pages();
      return browserInstance;
    } catch (_) {
      browserInstance = null;
    }
  }
  if (_connectingPromise) return _connectingPromise;
  _connectingPromise = puppeteer
    .connect({ browserURL: cdpUrl, defaultViewport: null })
    .then((b) => {
      browserInstance = b;
      _connectingPromise = null;
      return b;
    })
    .catch((e) => {
      _connectingPromise = null;
      throw e;
    });
  return _connectingPromise;
}

export async function disconnectBrowser() {
  if (browserInstance) {
    try {
      await browserInstance.disconnect();
    } catch (_) {}
    browserInstance = null;
  }
  _connectingPromise = null;
}

/**
 * 认证流程: 打开 chatgpt.com，等待用户登录，抓取 cookie + userAgent
 * 注意: 使用 connectChrome() 创建独立临时连接，不共用 getBrowser() 单例，
 * 认证结束后 browser.disconnect() 关闭，不影响 chat() 使用的 browserInstance
 */
export async function auth(cdpUrl = 'http://127.0.0.1:9222', chatgptUrl = 'https://chatgpt.com/') {
  const browser = await connectChrome(cdpUrl);
  const pages = await browser.pages();
  let page = pages.find(p => p.url().includes('chatgpt.com'));
  
  if (!page) {
    page = await browser.newPage();
    await page.goto(chatgptUrl, { waitUntil: 'networkidle0', timeout: 60000 });
  } else {
    await page.bringToFront();
  }

  // 等待登录: 轮询检测 cookie 或登录标识
  const authTimeoutMs = 120000;
  const pollInterval = 2000;
  const start = Date.now();

  while (Date.now() - start < authTimeoutMs) {
    const cookies = await page.cookies();
    const hasAuth = cookies.some(c => 
      c.name.includes('__Secure') || 
      c.name === '__cf_bm' || 
      c.domain.includes('openai.com')
    );
    
    const hasLoggedInUI = await page.evaluate((sel) => {
      for (const s of sel) {
        const el = document.querySelector(s);
        if (el) return true;
      }
      return false;
    }, SELECTORS.loggedIn);

    if (hasAuth || hasLoggedInUI) {
      const userAgent = await page.evaluate(() => navigator.userAgent);
      const authData = { cookies, userAgent, timestamp: Date.now() };
      fs.writeFileSync(AUTH_CACHE_PATH, JSON.stringify(authData, null, 2), 'utf-8');
      await browser.disconnect();
      return authData;
    }

    await new Promise(r => setTimeout(r, pollInterval));
  }

  await browser.disconnect();
  throw new Error('Auth timeout: Please log in at https://chatgpt.com/ within 2 minutes');
}

/**
 * 加载缓存的认证信息
 */
function loadAuth() {
  if (!fs.existsSync(AUTH_CACHE_PATH)) return null;
  try {
    return JSON.parse(fs.readFileSync(AUTH_CACHE_PATH, 'utf-8'));
  } catch (e) {
    console.warn('[auth] Cache corrupted, ignore:', e.message);
    return null;
  }
}

const agentPages = {};

async function isPageAlive(page) {
  try {
    await page.evaluate(() => true);
    return true;
  } catch (_) {
    return false;
  }
}

export async function closeAgent(agentId) {
  const page = agentPages[agentId];
  if (page) {
    try {
      await page.close();
    } catch (_) {}
    delete agentPages[agentId];
  }
}

export async function closeAllAgents() {
  for (const id of Object.keys(agentPages)) {
    await closeAgent(id);
  }
}

async function checkCloudflareBlock(page) {
  return await page.evaluate(() => {
    const body = document.body?.innerText || '';
    const title = document.title || '';
    if (
      body.includes('Edge IP Restricted') ||
      body.includes('Error reference number: 1034') ||
      title.includes('1034') ||
      (body.includes('Ray ID') && body.includes('chatgpt.com'))
    ) {
      const rayId = body.match(/Ray ID:\s*([a-f0-9]+)/i)?.[1] ?? 'unknown';
      const ip = body.match(/Your IP address:\s*([\d.]+)/)?.[1] ?? 'unknown';
      return { blocked: true, rayId, ip };
    }
    return { blocked: false };
  });
}

const _agentQueues = {};

/**
 * 聊天: DOM 模拟发送消息并轮询获取最后一条回复
 * 按 agentId 分槽串行，同 Tab 不乱序，不同 Tab 可并行
 */
export async function chat(message, options = {}) {
  const id = options.agentId ?? 'default';
  const prev = _agentQueues[id] ?? Promise.resolve();
  _agentQueues[id] = prev
    .catch(() => {})
    .then(() => doChat(message, options));
  return _agentQueues[id];
}

async function doChat(message, options = {}) {
  const {
    cdpUrl = 'http://127.0.0.1:9222',
    chatgptUrl = 'https://chatgpt.com/',
    pollIntervalMs = 500,
    pollTimeoutMs = 120000,
    newChat = false,
    pageReadyTimeoutMs = 15000,
    agentId = 'default',
  } = options;

  const browser = await getBrowser(cdpUrl);
  let page = agentPages[agentId];

  if (agentId !== 'default') {
    if (!page || !(await isPageAlive(page))) {
      page = await browser.newPage();
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await page.goto(chatgptUrl, { waitUntil: 'networkidle0', timeout: 60000 });
      agentPages[agentId] = page;
    } else {
      await page.bringToFront();
    }
  } else {
    const pages = await browser.pages();
    page = pages.find(p => p.url().includes('chatgpt.com'));
    if (!page) {
      page = await browser.newPage();
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await page.goto(chatgptUrl, { waitUntil: 'networkidle0', timeout: 60000 });
    } else {
      await page.bringToFront();
    }
  }

  if (newChat) {
    const newChatBtn = await findFirst(page, ['button[aria-label*="New chat"]', 'a[href="/"]', '[data-testid="new-chat-button"]']);
    if (newChatBtn) {
      await page.click(newChatBtn);
      await new Promise(r => setTimeout(r, 2000));
    } else {
      await page.goto(chatgptUrl, { waitUntil: 'networkidle0', timeout: 60000 });
    }
  }

  const cfCheck = await checkCloudflareBlock(page);
  if (cfCheck.blocked) {
    await disconnectBrowser();
    throw new Error(
      `CF_BLOCKED:Edge IP Restricted (1034)\n` +
      `IP: ${cfCheck.ip} blocked by Cloudflare for chatgpt.com\n` +
      `Ray ID: ${cfCheck.rayId}\n` +
      `Fix: Change network/proxy or wait for unblock`
    );
  }

  const waitOpts = { timeout: pageReadyTimeoutMs };

  const inputSelector = await waitForSelector(page, SELECTORS.input, waitOpts);
  if (!inputSelector) throw new Error('Input box not found, ensure you are on chatgpt.com');

  await page.click(inputSelector);

  // ChatGPT 使用 ProseMirror contenteditable，需用 native setter 或 execCommand 才能更新 React 状态
  const filled = await page.evaluate((sel, text) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.focus();

    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      const proto = Object.getPrototypeOf(el);
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
      if (descriptor?.set) {
        descriptor.set.call(el, text);
      } else {
        el.value = text;
      }
    } else {
      try {
        const r = document.createRange();
        r.selectNodeContents(el);
        const s = window.getSelection();
        s.removeAllRanges();
        s.addRange(r);
        document.execCommand('insertText', false, text);
      } catch (_) {
        el.textContent = text;
      }
    }
    el.dispatchEvent(new InputEvent('input', { bubbles: true }));
    const v = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' ? el.value : (el.textContent || el.innerText || '');
    return v.length > 10;
  }, inputSelector, message);

  if (!filled) {
    await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (el) el.textContent = '';
    }, inputSelector);
    await page.type(inputSelector, message, { delay: 0 });
  }

  await new Promise(r => setTimeout(r, 300));

  const sendSelector = await waitForSelector(page, SELECTORS.sendButton, waitOpts);
  if (!sendSelector) throw new Error('Send button not found');

  // 发送前记录最后一条回复内容与条数，用于判断是否收到新回复
  const prevLastText = await getLastReplyText(page);
  const prevCount = await getReplyCount(page);
  await page.click(sendSelector);

  // ── 中间状态模式：匹配时永不提前返回 ──────────────────────
  const _INTER_RE = [
    /正在搜索/, /正在思考/, /正在浏览/, /正在查找/,
    /Searching/i, /Thinking/i, /Looking up/i, /Browsing/i,
  ];
  const _isIntermediate = (t) => !t || t.trim().length < 15 || _INTER_RE.some(r => r.test(t));

  const start = Date.now();
  let lastText = '';
  let stableCount = 0;
  const STABLE_POLLS = 4;
  // 短回复（如占位符）需更多轮稳定，避免在完整 JSON 渲染前提前返回
  const MIN_LEN_FOR_QUICK_STABLE = 150;

  while (Date.now() - start < pollTimeoutMs) {
    await new Promise(r => setTimeout(r, pollIntervalMs));
    const cfCheck = await checkCloudflareBlock(page);
    if (cfCheck.blocked) {
      await disconnectBrowser();
      throw new Error(
        `CF_BLOCKED:Edge IP Restricted (1034)\n` +
        `IP: ${cfCheck.ip} blocked by Cloudflare for chatgpt.com\n` +
        `Ray ID: ${cfCheck.rayId}\n` +
        `Fix: Change network/proxy or wait for unblock`
      );
    }
    const count = await getReplyCount(page);
    const text = await getLastReplyText(page);
    if (!text || text.length < 2) continue;

    // count 增加或文本变化，才算新回复
    if (count <= prevCount && text === prevLastText) continue;

    if (text === lastText) {
      stableCount++;
      const requiredStable = (text.length < MIN_LEN_FOR_QUICK_STABLE && !text.includes('"command"'))
        ? 10  // 短回复且无 JSON 特征时多等几轮，避免占位符被误判为完成
        : STABLE_POLLS;
      if (stableCount >= requiredStable) {
        // ── 关键修复：中间状态时重置计数，继续等待真实回复 ──
        if (_isIntermediate(text)) {
          stableCount = 0;
          continue;
        }
        return { text, raw: text };
      }
    } else {
      lastText = text;
      stableCount = 0;
    }
  }

  if (lastText) return { text: lastText, raw: lastText };
  throw new Error('Reply timeout');
}

async function findFirst(page, selectors) {
  for (const sel of selectors) {
    try {
      const el = await page.$(sel);
      if (el) {
        await el.dispose();
        return sel;
      }
    } catch (_) {}
  }
  return null;
}

async function waitForSelector(page, selectors, options = {}) {
  const timeout = options.timeout ?? 15000;
  const interval = options.interval ?? 500;
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const sel = await findFirst(page, selectors);
    if (sel) return sel;
    await new Promise(r => setTimeout(r, interval));
  }
  return null;
}

async function getReplyCount(page) {
  return await page.evaluate(() => {
    let els = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (els.length === 0) els = document.querySelectorAll('[data-testid="conversation-turn"]');
    if (els.length === 0) els = document.querySelectorAll('div[data-message-id], [class*="markdown"], article');
    return els.length;
  });
}

async function getLastReplyText(page) {
  return await page.evaluate(() => {
    const getText = (el) => (el?.innerText || el?.textContent || '').trim();
    const isPromptLike = (t) =>
      !t ||
      (t.includes('<tool>') && !t.includes('?') && t.length < 500) ||
      (t.includes('<available_tools>') && t.length < 300);

    const isScriptLike = (t) =>
      !t ||
      t.includes('window.__') ||
      t.includes('requestAnimationFrame') ||
      (t.includes('Date.now()') && t.length < 200) ||
      (t.includes('?.') && t.includes('()') && !t.includes('{') && t.length < 300) ||
      /^[a-zA-Z_$][a-zA-Z0-9_$]*\s*[?=\(]/.test(t.slice(0, 80));

    const isValidReply = (t) => t && t.length > 3 && !isPromptLike(t) && !isScriptLike(t);

    // 策略 1: 取最后一条 assistant 消息
    let els = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const t = getText(els[i]);
        if (isValidReply(t)) return t;
      }
      const last = getText(els[els.length - 1]);
      return isScriptLike(last) ? '' : last || '';
    }

    // 策略 2: conversation-turn
    els = document.querySelectorAll('[data-testid="conversation-turn"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const el = els[i];
        if (el.querySelector('[data-message-author-role="user"]')) continue;
        const t = getText(el);
        if (isValidReply(t)) return t;
      }
    }

    // 策略 3: 主区域最后一段
    const main = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
    const all = main.querySelectorAll('div[class*="markdown"], div[class*="prose"], article');
    for (let i = all.length - 1; i >= 0; i--) {
      const t = getText(all[i]);
      if (isValidReply(t)) return t;
    }
    const fallback = all.length ? getText(all[all.length - 1]) || '' : '';
    return isScriptLike(fallback) ? '' : fallback;
  });
}

// CLI 入口
const args = process.argv.slice(2);
if (args[0] === 'auth') {
  auth().then(d => console.log('Auth OK:', Object.keys(d))).catch(e => { console.error(e); process.exit(1); });
} else if (args[0] === 'chat') {
  const msg = args.slice(1).join(' ') || 'Hello';
  chat(msg).then(r => console.log(r.text)).catch(e => { console.error(e); process.exit(1); });
}
