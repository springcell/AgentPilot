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

/**
 * 认证流程: 打开 chatgpt.com，等待用户登录，抓取 cookie + userAgent
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
  if (fs.existsSync(AUTH_CACHE_PATH)) {
    return JSON.parse(fs.readFileSync(AUTH_CACHE_PATH, 'utf-8'));
  }
  return null;
}

/**
 * 聊天: DOM 模拟发送消息并轮询获取最后一条回复
 */
export async function chat(message, options = {}) {
  const {
    cdpUrl = 'http://127.0.0.1:9222',
    chatgptUrl = 'https://chatgpt.com/',
    pollIntervalMs = 500,
    pollTimeoutMs = 120000,
  } = options;

  const browser = await connectChrome(cdpUrl);
  const pages = await browser.pages();
  let page = pages.find(p => p.url().includes('chatgpt.com'));

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

  // 查找输入框并输入
  const inputSelector = await findFirst(page, SELECTORS.input);
  if (!inputSelector) throw new Error('Input box not found, ensure you are on chatgpt.com');

  await page.click(inputSelector);

  // ChatGPT 使用 ProseMirror contenteditable，需用 native setter 或 execCommand 才能更新 React 状态
  const inputSucceeded = await page.evaluate((sel, text) => {
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
      // contenteditable / ProseMirror：先选中元素内容再插入
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
    return true;
  }, inputSelector, message);

  // 若 evaluate 未生效（ProseMirror 可能拦截），回退到 page.type
  const hasContent = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    const v = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' ? el.value : (el.textContent || el.innerText || '');
    return v.length > 10;
  }, inputSelector);

  if (!hasContent) {
    await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (el) el.textContent = '';
    }, inputSelector);
    await page.type(inputSelector, message, { delay: 20 });
  }

  // 等待 React 状态更新后再点击发送
  await new Promise(r => setTimeout(r, 300));

  // 查找发送按钮并点击
  const sendSelector = await findFirst(page, SELECTORS.sendButton);
  if (!sendSelector) throw new Error('Send button not found');

  // 发送前记录最后一条回复内容，用于判断是否收到新回复（避免延迟一回合）
  const prevLastText = await getLastReplyText(page);
  await page.click(sendSelector);

  const start = Date.now();
  let lastText = '';
  let stableCount = 0;
  const STABLE_POLLS = 4;

  while (Date.now() - start < pollTimeoutMs) {
    await new Promise(r => setTimeout(r, pollIntervalMs));
    const text = await getLastReplyText(page);
    if (!text || text.length < 2) continue;

    // 必须与发送前不同，才是新回复
    if (text === prevLastText) continue;

    if (text === lastText) {
      stableCount++;
      if (stableCount >= STABLE_POLLS) {
        await browser.disconnect();
        return { text, raw: text };
      }
    } else {
      lastText = text;
      stableCount = 0;
    }
  }

  await browser.disconnect();
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
    const isPromptLike = (t) => !t || (t.startsWith('规则：') && t.length < 200) || (t.startsWith('<available_tools>') && !t.includes('?') && !t.includes('!'));

    // 策略 1: 取最后一条 assistant 消息
    let els = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const t = getText(els[i]);
        if (t && !isPromptLike(t) && t.length > 3) return t;
      }
      return getText(els[els.length - 1]) || '';
    }

    // 策略 2: conversation-turn
    els = document.querySelectorAll('[data-testid="conversation-turn"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const el = els[i];
        if (el.querySelector('[data-message-author-role="user"]')) continue;
        const t = getText(el);
        if (t && !isPromptLike(t) && t.length > 3) return t;
      }
    }

    // 策略 3: 主区域最后一段
    const main = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
    const all = main.querySelectorAll('div[class*="markdown"], div[class*="prose"], article');
    for (let i = all.length - 1; i >= 0; i--) {
      const t = getText(all[i]);
      if (t && !isPromptLike(t) && t.length > 5) return t;
    }
    return all.length ? getText(all[all.length - 1]) || '' : '';
  });
}

// CLI 入口
const args = process.argv.slice(2);
if (args[0] === 'auth') {
  auth().then(d => console.log('认证成功:', Object.keys(d))).catch(e => { console.error(e); process.exit(1); });
} else if (args[0] === 'chat') {
  const msg = args.slice(1).join(' ') || 'Hello';
  chat(msg).then(r => console.log(r.text)).catch(e => { console.error(e); process.exit(1); });
}
