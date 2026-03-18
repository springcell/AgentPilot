/**
 * ChatGPT Web Client - chat via Chrome CDP + DOM
 * Auth: CDP -> open chatgpt.com -> wait for login (cookie) -> return cookie + userAgent
 * Chat: DOM - fill input, click send, poll DOM for last model reply
 */

import puppeteer from 'puppeteer-core';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUTH_CACHE_PATH = path.join(__dirname, '..', '.chatgpt-auth.json');
const SELECTORS_CONFIG_PATH = path.join(__dirname, '..', 'agent', 'config', 'selectors.json');

const DEFAULT_SELECTORS = {
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
  attachButton: [
    'button[aria-label*="Attach"]',
    'button[aria-label*="attach"]',
    'button[aria-label*="Upload"]',
    'button[aria-label*="upload"]',
    'button[data-testid*="attach"]',
    'label[for*="file"]',
    'button[aria-label*="file"]',
    'label[aria-label*="Attach"]',
  ],
  fileInput: [
    'input[type="file"]',
    'input[accept]',
  ],
  uploadSpinner: [
    '[aria-label*="Uploading"]',
    '[data-testid*="upload-progress"]',
    '.animate-spin',
  ],
  attachmentChip: [
    '[data-testid*="attachment"]',
    '[data-testid*="file-chip"]',
    '[data-testid*="composer-attachment"]',
    'button[aria-label*="Remove file"]',
    'button[aria-label*="remove file"]',
    'button[aria-label*="移除"]',
  ],
  lastReply: [
    '[data-message-author-role="assistant"]',
    '[data-testid="conversation-turn"]',
    'div[data-message-id]',
    '[class*="markdown"]',
    'div.markdown',
    'article',
    '[class*="Message"]',
  ],
  loggedIn: [
    '[data-testid="user-menu"]',
    'button[aria-label*="New chat"]',
    'a[href="/"]',
    'nav',
  ],
};

function loadSelectors() {
  try {
    if (!fs.existsSync(SELECTORS_CONFIG_PATH)) return DEFAULT_SELECTORS;
    const loaded = JSON.parse(fs.readFileSync(SELECTORS_CONFIG_PATH, 'utf-8'));
    return {
      input: loaded.input_box?.length ? loaded.input_box : DEFAULT_SELECTORS.input,
      sendButton: loaded.send_button?.length ? loaded.send_button : DEFAULT_SELECTORS.sendButton,
      sendButtonClickable: loaded.send_button_clickable?.length ? loaded.send_button_clickable : [],
      attachButton: loaded.attach_button?.length ? loaded.attach_button : DEFAULT_SELECTORS.attachButton,
      fileInput: loaded.file_input?.length ? loaded.file_input : DEFAULT_SELECTORS.fileInput,
      uploadSpinner: loaded.upload_spinner?.length ? loaded.upload_spinner : DEFAULT_SELECTORS.uploadSpinner,
      attachmentChip: loaded.attachment_chip?.length ? loaded.attachment_chip : DEFAULT_SELECTORS.attachmentChip,
      lastReply: loaded.reply_area?.length ? loaded.reply_area : DEFAULT_SELECTORS.lastReply,
      loggedIn: loaded.logged_in?.length ? loaded.logged_in : DEFAULT_SELECTORS.loggedIn,
    };
  } catch (e) {
    console.warn('[selectors] Config load failed, using defaults:', e.message);
    return DEFAULT_SELECTORS;
  }
}

const SELECTORS = loadSelectors();

async function getComposerUploadState(page) {
  return page.evaluate((selectors) => {
    const hasAny = (list) => Array.isArray(list) && list.some((sel) => {
      try { return !!document.querySelector(sel); } catch (_) { return false; }
    });
    return {
      uploadInProgress: hasAny(selectors.uploadSpinner),
      hasAttachmentChip: hasAny(selectors.attachmentChip),
      hasFileInput: hasAny(selectors.fileInput),
      hasAttachButton: hasAny(selectors.attachButton),
    };
  }, {
    uploadSpinner: SELECTORS.uploadSpinner ?? [],
    attachmentChip: SELECTORS.attachmentChip ?? [],
    fileInput: SELECTORS.fileInput ?? [],
    attachButton: SELECTORS.attachButton ?? [],
  }).catch(() => ({
    uploadInProgress: false,
    hasAttachmentChip: false,
    hasFileInput: false,
    hasAttachButton: false,
  }));
}

function looksLikeRoutingReply(text) {
  const cleaned = (text || '').trim().replace(/^[\s\S]{0,30}?ChatGPT\s*[^\n]*[：:]\s*/i, '').trim();
  if (!cleaned) return false;
  return (
    /^[\u4e00-\u9fa5a-zA-Z\s,，]+需要\s+[a-z_]+\s+去做[.!]?$/i.test(cleaned) ||
    /^[\u4e00-\u9fa5a-zA-Z\s,，]+,\s*need\s+[a-z_]+\s+to\s+do\s+it[.!]?$/i.test(cleaned)
  );
}

/** Connect to already-running Chrome (--remote-debugging-port=9222) */
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

/** Auth: open chatgpt.com, wait for login, capture cookie + userAgent */
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

function loadAuth() {
  if (!fs.existsSync(AUTH_CACHE_PATH)) return null;
  try {
    return JSON.parse(fs.readFileSync(AUTH_CACHE_PATH, 'utf-8'));
  } catch (e) {
    console.warn('[auth] Cache corrupted, ignoring:', e.message);
    return null;
  }
}

const agentPages = {};
const AGENT_WINDOW_PREFIX = 'agentpilot:';

async function isPageAlive(page) {
  try {
    await page.evaluate(() => true);
    return true;
  } catch (_) {
    return false;
  }
}

async function tagAgentPage(page, agentId) {
  if (!page || !agentId || agentId === 'default') return;
  page.__agentPilotAgentId = agentId;
  try {
    await page.evaluate((tag) => {
      window.name = tag;
    }, `${AGENT_WINDOW_PREFIX}${agentId}`);
  } catch (_) {}
}

async function pageMatchesAgent(page, agentId) {
  if (!page || !agentId) return false;
  if (page === agentPages[agentId]) return true;
  if (page.__agentPilotAgentId === agentId) return true;
  try {
    return await page.evaluate((tag) => window.name === tag, `${AGENT_WINDOW_PREFIX}${agentId}`);
  } catch (_) {
    return false;
  }
}

async function closePageHard(page) {
  if (!page) return;
  try {
    await page.close({ runBeforeUnload: false });
  } catch (_) {}
}

export async function closeAgent(agentId, cdpUrl = 'http://127.0.0.1:9222') {
  const browser = await getBrowser(cdpUrl);
  const targets = new Set();
  const tracked = agentPages[agentId];
  if (tracked) targets.add(tracked);
  for (const page of await browser.pages()) {
    if (await pageMatchesAgent(page, agentId)) {
      targets.add(page);
    }
  }
  if (agentId === 'default' && targets.size === 0) {
    const fallback = (await browser.pages()).find((page) => page.url().includes('chatgpt.com'));
    if (fallback) targets.add(fallback);
  }
  for (const page of targets) {
    await closePageHard(page);
  }
  delete agentPages[agentId];
  delete _agentQueues[agentId];
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
  let createdFreshPage = false;

  if (agentId !== 'default') {
    if (!page || !(await isPageAlive(page))) {
      page = await browser.newPage();
      createdFreshPage = true;
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
      agentPages[agentId] = page;
      await tagAgentPage(page, agentId);
    } else {
      await tagAgentPage(page, agentId);
      await page.bringToFront();
    }
  } else {
    const pages = await browser.pages();
    page = pages.find(p => p.url().includes('chatgpt.com'));
    if (!page) {
      page = await browser.newPage();
      createdFreshPage = true;
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
    } else {
      await page.bringToFront();
    }
  }

  if (newChat && !createdFreshPage) {
    const newChatBtn = await findFirstVisible(page, ['button[aria-label*="New chat"]', '[data-testid="new-chat-button"]']);
    if (newChatBtn) {
      await safeClickSelector(page, newChatBtn, { timeout: 3000 });
    } else {
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
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
  let inputSelector = await waitForSelector(page, SELECTORS.input, { timeout: 1200, interval: 150 });
  if (!inputSelector) {
    inputSelector = await waitForSelector(page, SELECTORS.input, waitOpts);
  }
  if (!inputSelector) throw new Error('Input box not found, ensure you are on chatgpt.com');

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

  // Wait for send button to become enabled (ChatGPT disables it while generating)
  const SEND_READY_TIMEOUT = 60000;
  const sendReady = await waitForSendButtonEnabled(page, sendSelector, SEND_READY_TIMEOUT);
  if (!sendReady) throw new Error('Send button not found');

  const prevLastText = await getLastReplyText(page);
  const prevCount = await getReplyCount(page);

  // ── File capture: Browser download + CDP network interception ─────────────
  // Strategy 1 (preferred): After reply stabilises, find ChatGPT's download button and click it.
  //   Chrome downloads the file natively → Browser.downloadProgress fires → read from disk.
  // Strategy 2 (fallback): CDP Network.responseReceived captures binary responses in-flight.
  // Both run in parallel; whichever fires first wins.
  let _capturedFileBuf = null;
  let _capturedFileExt = null;
  let _dlFilename = null;

  const _os = await import('os');
  const _dlDir = _os.default.tmpdir();

  const cdpSession = await page.createCDPSession().catch(() => null);
  if (cdpSession) {
    // Enable browser download events and redirect downloads to temp dir
    await cdpSession.send('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: _dlDir,
      eventsEnabled: true,
    }).catch(() => {});

    // Strategy 2: network-level interception for files from oaiusercontent.com
    const _SKIP_CT = new Set(['text/html', 'application/json', 'text/javascript', 'text/css']);
    const _dlRequests = {};
    await cdpSession.send('Network.enable').catch(() => {});
    cdpSession.on('Network.responseReceived', (evt) => {
      const url = evt.response?.url ?? '';
      const ct  = (evt.response?.mimeType ?? '').split(';')[0].trim();
      if (!url.includes('oaiusercontent.com') && !url.includes('openai.com/backend-api/files')) return;
      if (_SKIP_CT.has(ct) || ct.startsWith('text/') || ct === 'application/json') return;
      _dlRequests[evt.requestId] = { url, ct };
    });
    cdpSession.on('Network.loadingFinished', async (evt) => {
      if (!_dlRequests[evt.requestId]) return;
      const { url, ct } = _dlRequests[evt.requestId];
      try {
        const r = await cdpSession.send('Network.getResponseBody', { requestId: evt.requestId });
        const buf = r.base64Encoded ? Buffer.from(r.body, 'base64') : Buffer.from(r.body, 'utf-8');
        if (buf.length < 100) return;
        const extMap = {
          'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
          'application/pdf': '.pdf',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
          'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
          'application/zip': '.zip', 'audio/mpeg': '.mp3', 'video/mp4': '.mp4',
        };
        const m = url.match(/\.(png|jpg|jpeg|gif|webp|pdf|docx?|xlsx?|pptx?|zip|mp3|mp4|wav)(\?|$)/i);
        const ext = extMap[ct] ?? (m ? '.' + m[1].toLowerCase() : '.bin');
        if (!_capturedFileBuf || buf.length > _capturedFileBuf.length) {
          _capturedFileBuf = buf; _capturedFileExt = ext;
          console.log(`[doChat] CDP-network captured: ${buf.length}B ext=${ext}`);
        }
      } catch (_) {}
    });

    // Strategy 1: browser native download events
    let _dlGuid = null;
    cdpSession.on('Browser.downloadWillBegin', (evt) => {
      _dlGuid = evt.guid;
      _dlFilename = evt.suggestedFilename || 'download';
      console.log(`[doChat] browser download starting: ${_dlFilename}`);
    });
    cdpSession.on('Browser.downloadProgress', async (evt) => {
      if (evt.state !== 'completed' || !_dlGuid || evt.guid !== _dlGuid) return;
      const dlPath = path.join(_dlDir, _dlFilename);
      try {
        if (fs.existsSync(dlPath)) {
          const buf = fs.readFileSync(dlPath);
          const ext = path.extname(_dlFilename).toLowerCase() || '.bin';
          _capturedFileBuf = buf; _capturedFileExt = ext;
          console.log(`[doChat] browser download complete: ${dlPath} (${buf.length}B)`);
          try { fs.unlinkSync(dlPath); } catch (_) {}
        }
      } catch (_) {}
    });
  }

  const clickableSendSelector = await findFirstVisible(page, SELECTORS.sendButtonClickable || []);
  await safeClickSelector(page, clickableSendSelector || sendSelector, { timeout: SEND_READY_TIMEOUT });

  const _INTER_RE = [
    /正在搜索/, /正在思考/, /正在浏览/, /正在查找/,
    /正在创建/, /正在生成/, /正在处理/, /正在绘制/, /正在渲染/,
    /正在上传/, /正在分析/, /正在修改/, /正在优化/,
    /Searching/i, /Thinking/i, /Looking up/i, /Browsing/i,
    /Creating/i, /Generating/i, /Processing/i, /Drawing/i, /Rendering/i,
    /Uploading/i, /Analyzing/i, /Modifying/i,
  ];
  const _isIntermediate = (t) => {
    if (!t) return true;
    if (looksLikeRoutingReply(t)) return false;
    return t.trim().length < 15 || _INTER_RE.some(r => r.test(t));
  };

  const start = Date.now();
  let lastText = '';
  let lastProgressAt = Date.now();
  let stableCount = 0;
  const STABLE_POLLS = 4;
  const MIN_LEN_FOR_QUICK_STABLE = 150;
  const INTERMEDIATE_STALL_MS = 15000;

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

    if (count <= prevCount && text === prevLastText) continue;

    if (text === lastText) {
      stableCount++;
      const requiredStable = (text.length < MIN_LEN_FOR_QUICK_STABLE && !text.includes('"command"'))
        ? 10
        : STABLE_POLLS;
      if (stableCount >= requiredStable) {
        if (_isIntermediate(text)) {
          if (Date.now() - lastProgressAt >= INTERMEDIATE_STALL_MS) {
            await cdpSession?.detach().catch(() => {});
            return { text, raw: text, generating: true, stalled: true };
          }
          stableCount = 0;
          continue;
        }
        // Reply is stable. Try to trigger a native browser download by clicking
        // ChatGPT's download button (if present). Then wait up to 8s for download to complete.
        if (!_capturedFileBuf) {
          const clicked = await page.evaluate(() => {
            // ChatGPT download button selectors (try all known patterns)
            const selectors = [
              'button[aria-label*="Download"]', 'button[aria-label*="download"]',
              'a[download]', '[data-testid*="download"]',
              'button[aria-label*="下载"]',
            ];
            for (const sel of selectors) {
              const btns = document.querySelectorAll(sel);
              // Click the last one (most recent reply)
              if (btns.length) { btns[btns.length - 1].click(); return true; }
            }
            return false;
          }).catch(() => false);
          if (clicked) console.log('[doChat] clicked download button');
          // Wait for download to complete (up to 8s)
          if (!_capturedFileBuf) await new Promise(r => setTimeout(r, 4000));
          if (!_capturedFileBuf) await new Promise(r => setTimeout(r, 4000));
        }
        await cdpSession?.detach().catch(() => {});
        // Fallback: DOM image capture if download didn't fire
        let fileBuf = _capturedFileBuf;
        let fileExt = _capturedFileExt;
        if (!fileBuf) {
          fileBuf = await _captureLastReplyImage(page).catch(() => null);
          if (fileBuf) fileExt = '.png';
        }
        if (fileBuf) {
          console.log(`[doChat] returning file: ${fileBuf.length}B ext=${fileExt}`);
          return { text, raw: text, downloadedContent: fileBuf, downloadedExt: fileExt };
        }
        return { text, raw: text };
      }
    } else {
      lastText = text;
      lastProgressAt = Date.now();
      stableCount = 0;
    }
  }

  // Poll timed out but we have partial text — return it as intermediate rather than throwing.
  // api-server.js will detect generating=true and launch _pollDomUntilFinal to continue.
  await cdpSession?.detach().catch(() => {});
  if (_capturedFileBuf) {
    return { text: lastText, raw: lastText, downloadedContent: _capturedFileBuf, downloadedExt: _capturedFileExt, generating: true };
  }
  if (lastText) return { text: lastText, raw: lastText, generating: true };
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

async function isSelectorVisible(page, selector) {
  try {
    return await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!(el instanceof Element)) return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.pointerEvents !== 'none'
        && rect.width > 0
        && rect.height > 0;
    }, selector);
  } catch (_) {
    return false;
  }
}

async function findFirstVisible(page, selectors) {
  for (const sel of selectors) {
    if (await isSelectorVisible(page, sel)) return sel;
  }
  return null;
}

async function safeClickSelector(page, selector, options = {}) {
  const timeout = options.timeout ?? 5000;
  if (!selector) return false;
  try {
    await page.waitForSelector(selector, { visible: true, timeout });
    await page.click(selector, { timeout });
    return true;
  } catch (firstErr) {
    try {
      const clicked = await page.evaluate((sel) => {
        const candidates = Array.from(document.querySelectorAll(sel));
        const el = candidates.find((node) => {
          if (!(node instanceof Element)) return false;
          const style = window.getComputedStyle(node);
          const rect = node.getBoundingClientRect();
          return style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.pointerEvents !== 'none'
            && rect.width > 0
            && rect.height > 0;
        });
        if (!(el instanceof HTMLElement)) return false;
        el.scrollIntoView({ block: 'center', inline: 'center' });
        el.click();
        return true;
      }, selector);
      if (clicked) return true;
    } catch (_) {}
    throw firstErr;
  }
}

async function gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs) {
  await page.goto(chatgptUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  return await waitForSelector(page, SELECTORS.input, {
    timeout: pageReadyTimeoutMs,
    interval: 200,
  });
}

async function waitForSendButtonEnabled(page, selector, timeout = 60000) {
  const interval = 500;
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const clickableSelector = SELECTORS.sendButtonClickable?.find(Boolean);
    const enabled = await page.evaluate((sel, clickableSel) => {
      if (clickableSel && document.querySelector(clickableSel)) return true;
      const el = document.querySelector(sel);
      if (!el) return false;
      return !el.disabled && !el.hasAttribute('disabled')
        && getComputedStyle(el).pointerEvents !== 'none'
        && !el.closest('[aria-disabled="true"]');
    }, selector, clickableSelector);
    if (enabled) return true;
    await new Promise(r => setTimeout(r, interval));
  }
  return false;
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

    let els = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const t = getText(els[i]);
        if (isValidReply(t)) return t;
      }
      const last = getText(els[els.length - 1]);
      return isScriptLike(last) ? '' : last || '';
    }

    els = document.querySelectorAll('[data-testid="conversation-turn"]');
    if (els.length > 0) {
      for (let i = els.length - 1; i >= 0; i--) {
        const el = els[i];
        if (el.querySelector('[data-message-author-role="user"]')) continue;
        const t = getText(el);
        if (isValidReply(t)) return t;
      }
    }

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

// ── File upload chat ─────────────────────────────────────────────────────────

/**
 * Upload a local file to ChatGPT and send an optional message alongside it.
 * Returns { text, raw } same as chat().
 *
 * @param {string} filePath  Absolute path to the file to upload
 * @param {string} message   Text prompt to accompany the file (may be empty)
 * @param {object} options   Same options as chat()
 */
export async function chatWithFile(filePath, message = '', options = {}) {
  const id = options.agentId ?? 'default';
  const prev = _agentQueues[id] ?? Promise.resolve();
  _agentQueues[id] = prev
    .catch(() => {})
    .then(() => doChatWithFile(filePath, message, options));
  return _agentQueues[id];
}

async function doChatWithFile(filePath, message, options = {}) {
  const {
    cdpUrl = 'http://127.0.0.1:9222',
    chatgptUrl = 'https://chatgpt.com/',
    pollIntervalMs = 500,
    pollTimeoutMs = 180000,   // files may need more time
    newChat = false,
    pageReadyTimeoutMs = 15000,
    agentId = 'default',
  } = options;

  if (!fs.existsSync(filePath)) throw new Error(`File not found: ${filePath}`);

  const browser = await getBrowser(cdpUrl);
  let page = agentPages[agentId];
  let createdFreshPage = false;

  if (agentId !== 'default') {
    if (!page || !(await isPageAlive(page))) {
      page = await browser.newPage();
      createdFreshPage = true;
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
      agentPages[agentId] = page;
      await tagAgentPage(page, agentId);
    } else {
      await tagAgentPage(page, agentId);
      await page.bringToFront();
    }
  } else {
    const pages = await browser.pages();
    page = pages.find(p => p.url().includes('chatgpt.com'));
    if (!page) {
      page = await browser.newPage();
      createdFreshPage = true;
      const cached = loadAuth();
      if (cached?.cookies?.length) {
        await page.setCookie(...cached.cookies);
        if (cached.userAgent) await page.setUserAgent(cached.userAgent);
      }
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
    } else {
      await page.bringToFront();
    }
  }

  if (newChat && !createdFreshPage) {
    const newChatBtn = await findFirstVisible(page, ['button[aria-label*="New chat"]', '[data-testid="new-chat-button"]']);
    if (newChatBtn) {
      await safeClickSelector(page, newChatBtn, { timeout: 3000 });
    } else {
      await gotoUntilInputReady(page, chatgptUrl, pageReadyTimeoutMs);
    }
  }

  const cfCheck = await checkCloudflareBlock(page);
  if (cfCheck.blocked) {
    await disconnectBrowser();
    throw new Error(`CF_BLOCKED:Edge IP Restricted (1034)\nIP: ${cfCheck.ip}\nRay ID: ${cfCheck.rayId}`);
  }

  // -- Wait for page ready --
  const waitOpts = { timeout: pageReadyTimeoutMs };
  let inputSelector = await waitForSelector(page, SELECTORS.input, { timeout: 1200, interval: 150 });
  if (!inputSelector) {
    inputSelector = await waitForSelector(page, SELECTORS.input, waitOpts);
  }
  if (!inputSelector) throw new Error('Input box not found, ensure you are on chatgpt.com');

  // -- Click the attach/upload button to reveal file input --
  // ChatGPT uses a hidden <input type="file"> triggered by an attach button
  const ATTACH_BTNS = SELECTORS.attachButton?.length ? SELECTORS.attachButton : [
    'button[aria-label*="Attach"]',
    'button[aria-label*="attach"]',
    'button[aria-label*="Upload"]',
    'button[aria-label*="upload"]',
    'button[data-testid*="attach"]',
    'label[for*="file"]',
    'button[aria-label*="file"]',
    'label[aria-label*="Attach"]',
  ];

  const FILE_INPUT_SELS = SELECTORS.fileInput?.length ? SELECTORS.fileInput : [
    'input[type="file"]',
    'input[accept]',
  ];

  // Strategy A: click the attach button first, then setInputFiles
  let fileInputHandle = null;

  const attachBtn = await findFirstVisible(page, ATTACH_BTNS);
  if (attachBtn) {
    await safeClickSelector(page, attachBtn, { timeout: 3000 });
    await new Promise(r => setTimeout(r, 600));
  }

  // Try to find a file input (may become visible after clicking attach)
  for (const sel of FILE_INPUT_SELS) {
    try {
      fileInputHandle = await page.$(sel);
      if (fileInputHandle) break;
    } catch (_) {}
  }

  if (!fileInputHandle) {
    // Strategy B: expose hidden input without clicking
    await page.evaluate(() => {
      const inputs = document.querySelectorAll('input[type="file"]');
      inputs.forEach(el => {
        el.style.display = 'block';
        el.style.visibility = 'visible';
        el.style.opacity = '1';
        el.style.width = '1px';
        el.style.height = '1px';
        el.removeAttribute('hidden');
      });
    });
    for (const sel of FILE_INPUT_SELS) {
      try {
        fileInputHandle = await page.$(sel);
        if (fileInputHandle) break;
      } catch (_) {}
    }
  }

  if (!fileInputHandle) throw new Error('File upload input not found on ChatGPT page');

  // -- Upload the file --
  await fileInputHandle.uploadFile(filePath);
  console.log(`[chatWithFile] uploaded: ${path.basename(filePath)}`);

  // Wait for upload indicator to disappear (ChatGPT shows a spinner)
  const UPLOAD_DONE_TIMEOUT = 30000;
  const uploadStart = Date.now();
  let uploadConfirmed = false;
  while (Date.now() - uploadStart < UPLOAD_DONE_TIMEOUT) {
    await new Promise(r => setTimeout(r, 500));
    const uploadState = await getComposerUploadState(page);
    if (uploadState.hasAttachmentChip) uploadConfirmed = true;
    if (!uploadState.uploadInProgress && uploadConfirmed) break;
  }
  const finalUploadState = await getComposerUploadState(page);
  if (finalUploadState.hasAttachmentChip) uploadConfirmed = true;
  await new Promise(r => setTimeout(r, 500)); // small settle

  // -- Type message (if any) --
  if (message) {
    await safeClickSelector(page, inputSelector, { timeout: 3000 });
    const filled = await page.evaluate((sel, text) => {
      const el = document.querySelector(sel);
      if (!el) return false;
      el.focus();
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        const proto = Object.getPrototypeOf(el);
        const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
        if (descriptor?.set) descriptor.set.call(el, text);
        else el.value = text;
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
      return true;
    }, inputSelector, message);

    if (!filled) {
      await page.type(inputSelector, message, { delay: 0 });
    }
    await new Promise(r => setTimeout(r, 300));
  }

  // -- Send --
  const sendSelector = await waitForSelector(page, SELECTORS.sendButton, waitOpts);
  if (!sendSelector) throw new Error('Send button not found');

  await waitForSendButtonEnabled(page, sendSelector, 60000);

  const prevLastText = await getLastReplyText(page);
  const prevCount = await getReplyCount(page);

  // ── File capture: same logic as doChat ────────────────────────────────────
  // Strategy 1 (preferred): After reply stabilises, click ChatGPT's download button.
  //   Chrome downloads the file natively → Browser.downloadProgress fires → read from disk.
  // Strategy 2 (fallback): CDP Network.responseReceived captures binary from oaiusercontent CDN.
  // Both run in parallel; whichever fires first wins.
  let _capturedFileBuf = null;
  let _capturedFileExt = null;
  let _dlFilename = null;

  const _os2 = await import('os');
  const _dlDir2 = _os2.default.tmpdir();

  const cdpSession = await page.createCDPSession().catch(() => null);
  if (cdpSession) {
    await cdpSession.send('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: _dlDir2,
      eventsEnabled: true,
    }).catch(() => {});

    // Strategy 2: network-level interception for files from oaiusercontent.com
    const _SKIP_CT2 = new Set(['text/html', 'application/json', 'text/javascript', 'text/css']);
    const _dlRequests2 = {};
    await cdpSession.send('Network.enable').catch(() => {});
    cdpSession.on('Network.responseReceived', (evt) => {
      const url = evt.response?.url ?? '';
      const ct  = (evt.response?.mimeType ?? '').split(';')[0].trim();
      if (!url.includes('oaiusercontent.com') && !url.includes('openai.com/backend-api/files')) return;
      if (_SKIP_CT2.has(ct) || ct.startsWith('text/') || ct === 'application/json') return;
      _dlRequests2[evt.requestId] = { url, ct };
    });
    cdpSession.on('Network.loadingFinished', async (evt) => {
      if (!_dlRequests2[evt.requestId]) return;
      const { url, ct } = _dlRequests2[evt.requestId];
      try {
        const r = await cdpSession.send('Network.getResponseBody', { requestId: evt.requestId });
        const buf = r.base64Encoded ? Buffer.from(r.body, 'base64') : Buffer.from(r.body, 'utf-8');
        if (buf.length < 100) return;
        const extMap = {
          'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
          'application/pdf': '.pdf',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
          'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
          'application/zip': '.zip', 'audio/mpeg': '.mp3', 'video/mp4': '.mp4',
        };
        const m = url.match(/\.(png|jpg|jpeg|gif|webp|pdf|docx?|xlsx?|pptx?|zip|mp3|mp4|wav)(\?|$)/i);
        const ext = extMap[ct] ?? (m ? '.' + m[1].toLowerCase() : '.bin');
        if (!_capturedFileBuf || buf.length > _capturedFileBuf.length) {
          _capturedFileBuf = buf; _capturedFileExt = ext;
          console.log(`[chatWithFile] CDP-network captured: ${buf.length}B ext=${ext}`);
        }
      } catch (_) {}
    });

    // Strategy 1: browser native download events
    let _dlGuid = null;
    cdpSession.on('Browser.downloadWillBegin', (evt) => {
      _dlGuid = evt.guid;
      _dlFilename = evt.suggestedFilename || 'download';
      console.log(`[chatWithFile] browser download starting: ${_dlFilename}`);
    });
    cdpSession.on('Browser.downloadProgress', async (evt) => {
      if (evt.state !== 'completed' || !_dlGuid || evt.guid !== _dlGuid) return;
      const dlPath = path.join(_dlDir2, _dlFilename);
      try {
        if (fs.existsSync(dlPath)) {
          const buf = fs.readFileSync(dlPath);
          const ext = path.extname(_dlFilename).toLowerCase() || '.bin';
          _capturedFileBuf = buf; _capturedFileExt = ext;
          console.log(`[chatWithFile] browser download complete: ${dlPath} (${buf.length}B)`);
          try { fs.unlinkSync(dlPath); } catch (_) {}
        }
      } catch (_) {}
    });
  }
  // ── End download interception ──────────────────────────────────────────────

  const clickableSendSelector = await findFirstVisible(page, SELECTORS.sendButtonClickable || []);
  await safeClickSelector(page, clickableSendSelector || sendSelector, { timeout: 60000 });

  // -- Poll for reply --
  const _INTER_RE = [
    /正在搜索/, /正在思考/, /正在浏览/, /正在查找/,
    /正在创建/, /正在生成/, /正在处理/, /正在绘制/, /正在渲染/,
    /正在上传/, /正在分析/, /正在修改/, /正在优化/,
    /Searching/i, /Thinking/i, /Looking up/i, /Browsing/i,
    /Creating/i, /Generating/i, /Processing/i, /Drawing/i, /Rendering/i,
    /Uploading/i, /Analyzing/i, /Modifying/i,
  ];
  const _isIntermediate = (t) => {
    if (!t) return true;
    if (looksLikeRoutingReply(t)) return false;
    return t.trim().length < 15 || _INTER_RE.some(r => r.test(t));
  };

  const start = Date.now();
  let lastText = '';
  let stableCount = 0;
  const STABLE_POLLS = 4;
  const MIN_LEN_FOR_QUICK_STABLE = 150;

  while (Date.now() - start < pollTimeoutMs) {
    await new Promise(r => setTimeout(r, pollIntervalMs));
    const cfCheck2 = await checkCloudflareBlock(page);
    if (cfCheck2.blocked) {
      await disconnectBrowser();
      throw new Error(`CF_BLOCKED:Edge IP Restricted (1034)\nIP: ${cfCheck2.ip}\nRay ID: ${cfCheck2.rayId}`);
    }
    const count = await getReplyCount(page);
    const text = await getLastReplyText(page);
    if (!text || text.length < 2) continue;
    if (count <= prevCount && text === prevLastText) continue;

    if (text === lastText) {
      stableCount++;
      const requiredStable = (text.length < MIN_LEN_FOR_QUICK_STABLE && !text.includes('"command"'))
        ? 10 : STABLE_POLLS;
      if (stableCount >= requiredStable) {
        if (_isIntermediate(text)) { stableCount = 0; continue; }

        // Reply is stable. Try to trigger a native browser download by clicking
        // ChatGPT's download button (if present). Then wait up to 8s for download to complete.
        if (!_capturedFileBuf) {
          const clicked = await page.evaluate(() => {
            const selectors = [
              'button[aria-label*="Download"]', 'button[aria-label*="download"]',
              'a[download]', '[data-testid*="download"]',
              'button[aria-label*="下载"]',
            ];
            for (const sel of selectors) {
              const btns = document.querySelectorAll(sel);
              if (btns.length) { btns[btns.length - 1].click(); return true; }
            }
            return false;
          }).catch(() => false);
          if (clicked) console.log('[chatWithFile] clicked download button');
          if (!_capturedFileBuf) await new Promise(r => setTimeout(r, 4000));
          if (!_capturedFileBuf) await new Promise(r => setTimeout(r, 4000));
        }

        await cdpSession?.detach().catch(() => {});

        // Fallback: DOM image capture
        let fileBuf = _capturedFileBuf;
        let fileExt = _capturedFileExt;
        if (!fileBuf) {
          fileBuf = await _captureLastReplyImage(page).catch(() => null);
          if (fileBuf) fileExt = '.png';
        }
        if (fileBuf) {
          console.log(`[chatWithFile] returning file: ${fileBuf.length}B ext=${fileExt}`);
          return {
            text,
            raw: text,
            downloadedContent: fileBuf,
            downloadedExt: fileExt,
            uploadConfirmed,
            uploadState: finalUploadState,
          };
        }
        return { text, raw: text, uploadConfirmed, uploadState: finalUploadState };
      }
    } else {
      lastText = text;
      stableCount = 0;
    }
  }

  await cdpSession?.detach().catch(() => {});
  if (lastText) {
    if (!_capturedFileBuf) {
      _capturedFileBuf = await _captureLastReplyImage(page).catch(() => null);
      if (_capturedFileBuf) _capturedFileExt = '.png';
    }
    if (_capturedFileBuf) {
      return {
        text: lastText,
        raw: lastText,
        downloadedContent: _capturedFileBuf,
        downloadedExt: _capturedFileExt,
        generating: true,
        uploadConfirmed,
        uploadState: finalUploadState,
      };
    }
    return { text: lastText, raw: lastText, generating: true, uploadConfirmed, uploadState: finalUploadState };
  }
  throw new Error('Reply timeout');
}

// ── Extract generated image from last assistant message ───────────────────────
// ChatGPT shows AI-generated images as <img> elements inside the last assistant
// message bubble. We fetch the highest-resolution src and return it as a Buffer.
async function _captureLastReplyImage(page) {
  const imgUrl = await page.evaluate(() => {
    // Find the last assistant message
    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (!msgs.length) return null;
    const last = msgs[msgs.length - 1];
    // Look for <img> tags — skip tiny icons (< 100px) and avatars
    const imgs = last.querySelectorAll('img');
    let best = null;
    let bestArea = 0;
    for (const img of imgs) {
      // Prefer naturalWidth/naturalHeight; fall back to layout size
      const w = img.naturalWidth || img.width || img.clientWidth || 0;
      const h = img.naturalHeight || img.height || img.clientHeight || 0;
      const area = w * h;
      if (area > bestArea && area > 10000) { // at least ~100x100
        best = img.src || img.currentSrc || null;
        bestArea = area;
      }
    }
    return best;
  }).catch(() => null);

  if (!imgUrl || !imgUrl.startsWith('http')) return null;

  console.log(`[chatWithFile] capturing generated image from DOM: ${imgUrl.slice(0, 80)}`);

  // Fetch the image bytes via page.evaluate (inherits browser cookies/session)
  const b64 = await page.evaluate(async (url) => {
    try {
      const resp = await fetch(url, { credentials: 'include' });
      if (!resp.ok) return null;
      const ab = await resp.arrayBuffer();
      const bytes = new Uint8Array(ab);
      let bin = '';
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin);
    } catch (_) {
      return null;
    }
  }, imgUrl).catch(() => null);

  if (!b64) return null;
  const buf = Buffer.from(b64, 'base64');
  console.log(`[chatWithFile] captured image: ${buf.length} bytes`);
  return buf;
}

const args = process.argv.slice(2);
if (args[0] === 'auth') {
  auth().then(d => console.log('Auth OK:', Object.keys(d))).catch(e => { console.error(e); process.exit(1); });
} else if (args[0] === 'chat') {
  const msg = args.slice(1).join(' ') || 'Hello';
  chat(msg).then(r => console.log(r.text)).catch(e => { console.error(e); process.exit(1); });
}
