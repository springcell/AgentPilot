/**
 * OpenAI-compatible API server for Cursor
 * Exposes /v1/chat/completions - forwards to ChatGPT Web (CDP)
 */

import http from 'http';
import * as chatgptWeb from './chatgpt-web-client.js';
import * as xmlTools from './xml-tools.js';
import bridge from './bridge.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

// agent_loop.py poll cache: key = agentId (default "default"), value = { text, generating, updatedAt }
const _replyCache = {};

// Match intermediate state (search/think/browse); supports "ChatGPT says: ..." style prefix
const _INTERMEDIATE_RE = [
  /正在搜索/, /正在思考/, /正在浏览/, /正在查找/,
  /Searching/i, /Thinking/i, /Looking up/i, /Browsing/i,
];

function _isIntermediate(text) {
  if (!text) return true;
  // Task complete (highest priority); accept both EN and legacy CN
  if (text.includes('✅ Task complete') || text.includes('Task complete:') ||
      text.includes('✅ 任务完成') || text.includes('任务完成：')) return false;
  if (text.trim().length < 20) return true;
  const cleaned = text.trim().replace(/^[\s\S]{0,30}?ChatGPT\s*[^\n]*[：:]\s*/i, '').trim();
  return _INTERMEDIATE_RE.some(r => r.test(cleaned));
}

function getContentFromItem(item) {
  if (typeof item.content === 'string') return item.content;
  if (Array.isArray(item.content)) {
    const parts = item.content
      .filter(c => c.type === 'text' || c.type === 'input_text')
      .map(c => c.text || '');
    return parts.join('\n').trim() || (item.content[0]?.text ?? '');
  }
  return '';
}

function getLastUserContent(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const role = m.role || m.role_name;
    if (role === 'user' || role === 'human') {
      return getContentFromItem(m) || '';
    }
  }
  return '';
}

function inputToMessages(input) {
  if (!Array.isArray(input)) return [];
  return input.map(item => ({
    role: item.role || item.role_name || 'user',
    content: getContentFromItem(item) || (typeof item.content === 'string' ? item.content : ''),
  })).filter(m => m.content);
}

const VALIDATION_PROBES = ['hi', 'hello', 'test', 'ping', 'you are a test assistant', 'just say hi'];
function isValidationProbe(text) {
  const t = (text || '').toLowerCase().trim();
  return VALIDATION_PROBES.some(p => t === p || t.startsWith(p + ' ') || t.includes('test'));
}

async function handleChatCompletions(body) {
  const { model = 'gpt-4', stream = false } = body;
  let messages = body.messages;
  if (!messages?.length && body.input) {
    messages = inputToMessages(body.input);
  }
  if (!messages?.length) {
    messages = body.messages || body.input || [];
  }
  if (!Array.isArray(messages) || !messages.length) {
    return { error: { message: 'messages is required' }, status: 400 };
  }

  let text = getLastUserContent(messages);
  if (!text.trim()) {
    return { error: { message: 'No user message content' }, status: 400 };
  }

  const hasTools = !!(body.tools?.length);
  if (hasTools) {
    text = 'Answer directly in plain text only. Do not use tools, XML, <function_calls>, or <invoke>. Just reply with your answer.\n\n' + text;
  }

  const config = loadConfig();
  const cdp = config?.llm?.cdp ?? {};

  if (isValidationProbe(text)) {
    const data = {
      id: 'chatcmpl-' + Date.now(),
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: model,
      choices: [{ index: 0, message: { role: 'assistant', content: 'Hello! AgentPilot ready.' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 5, total_tokens: 6 },
    };
    return { data, stream, id: data.id, content: 'Hello! AgentPilot ready.' };
  }

  try {
    const result = await chatgptWeb.chat(text, {
      cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
      chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
      pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
      pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
      pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
    });

    let content = result.text || '';
    if (hasTools) {
      content = xmlTools.stripToolCalls(content) || content;
    }

    const id = 'chatcmpl-' + Date.now();
    const response = {
      id,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: model,
      choices: [{
        index: 0,
        message: {
          role: 'assistant',
          content,
        },
        finish_reason: 'stop',
      }],
      usage: {
        prompt_tokens: Math.ceil(text.length / 4),
        completion_tokens: Math.ceil(content.length / 4),
        total_tokens: 0,
      },
    };
    response.usage.total_tokens = response.usage.prompt_tokens + response.usage.completion_tokens;

    return { data: response, stream, id, content };
  } catch (err) {
    return {
      error: { message: err.message || 'ChatGPT Web request failed' },
      status: 500,
    };
  }
}

function getPathname(url) {
  try {
    let p = new URL(url || '/', 'http://localhost').pathname;
    return p.replace(/\/+$/, '') || '/';  // 去除尾部斜杠，避免 /chat/ 导致 404
  } catch (_) {
    return ((url || '/').split('?')[0]).replace(/\/+$/, '') || '/';
  }
}

const server = http.createServer(async (req, res) => {
  const pathname = getPathname(req.url);
  console.log('[%s] %s %s', new Date().toISOString().slice(11, 19), req.method, pathname);
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === 'GET' && (pathname === '/' || pathname === '/v1/models')) {
    const models = [
      { id: 'gpt-4o', object: 'model', created: Date.now() },
      { id: 'gpt-4o-mini', object: 'model', created: Date.now() },
      { id: 'gpt-4', object: 'model', created: Date.now() },
      { id: 'gpt-4-turbo', object: 'model', created: Date.now() },
      { id: 'gpt-3.5-turbo', object: 'model', created: Date.now() },
    ];
    res.writeHead(200);
    res.end(JSON.stringify({ object: 'list', data: models }));
    return;
  }

  if (req.method === 'POST' && pathname === '/v1/chat/completions') {
    let body = '';
    for await (const chunk of req) body += chunk;
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch (_) {
      res.writeHead(400);
      res.end(JSON.stringify({ error: { message: 'Invalid JSON' } }));
      return;
    }
    if (!parsed.messages && parsed.input) {
      console.log('[chat] Cursor Responses API format (input) detected, converting to messages');
    }

    const result = await handleChatCompletions(parsed);
    if (result.error) {
      res.writeHead(result.status || 500);
      res.end(JSON.stringify({ error: result.error }));
      return;
    }
    if (result.stream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
      });
      const content = result.content || '';
      const chunkSize = 50;
      for (let i = 0; i < content.length; i += chunkSize) {
        const chunk = content.slice(i, i + chunkSize);
        const sse = {
          id: result.id,
          object: 'chat.completion.chunk',
          created: Math.floor(Date.now() / 1000),
          model: parsed.model || 'gpt-4o',
          choices: [{ index: 0, delta: { content: chunk }, finish_reason: null }],
        };
        res.write('data: ' + JSON.stringify(sse) + '\n\n');
      }
      const done = {
        id: result.id,
        object: 'chat.completion.chunk',
        created: Math.floor(Date.now() / 1000),
        model: parsed.model || 'gpt-4o',
        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
      };
      res.write('data: ' + JSON.stringify(done) + '\n\n');
      res.write('data: [DONE]\n\n');
      res.end();
      return;
    }
    res.writeHead(200);
    res.end(JSON.stringify(result.data));
    return;
  }

  if (req.method === 'POST' && (pathname === '/chat' || pathname === '/v1/chat')) {
    let body = '';
    for await (const chunk of req) body += chunk;
    let parsed;
    try {
      parsed = body ? JSON.parse(body) : {};
    } catch (_) {
      res.writeHead(400);
      res.end(JSON.stringify({ error: { message: 'Invalid JSON' } }));
      return;
    }
    const { message, newChat, agentId = 'default' } = parsed;
    if (!message?.trim()) {
      res.writeHead(400);
      res.end(JSON.stringify({ error: { message: 'message is required' } }));
      return;
    }
    _replyCache[agentId] = { text: '', generating: true, updatedAt: Date.now() };
    try {
      const result = await bridge.chat(message, { newChat: !!newChat });
      const text = result?.result ?? '';
      const intermediate = _isIntermediate(text);
      _replyCache[agentId] = { text, generating: intermediate, updatedAt: Date.now() };
      if (intermediate) {
        console.warn(`  [poll] agentId=${agentId} intermediate (len=${text.length}), keeping generating:true`);
        console.warn(`  [poll]    "${text.slice(0, 60).replace(/\n/g, ' ')}"`);
        _pollDomUntilFinal(agentId).catch(e =>
          console.error('  [poll] DOM poll error:', e.message)
        );
      }
      res.writeHead(200);
      res.end(JSON.stringify(result));
    } catch (e) {
      _replyCache[agentId] = { text: '', generating: false, updatedAt: Date.now() };
      res.writeHead(500);
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

// Background DOM poll: after bridge returns intermediate state, keep reading page until final reply
async function _pollDomUntilFinal(agentId, maxMs = 240_000) {
  const config = loadConfig();
  const cdp = config?.llm?.cdp ?? {};
  const cdpUrl = cdp.url ?? 'http://127.0.0.1:9222';
  const POLL_MS = 1500;
  const deadline = Date.now() + maxMs;

  let puppeteer;
  try {
    puppeteer = (await import('puppeteer-core')).default;
  } catch (e) {
    console.error('  [DOM poll] puppeteer-core not available:', e.message);
    if (_replyCache[agentId]) _replyCache[agentId].generating = false;
    return;
  }

  let browser;
  try {
    browser = await puppeteer.connect({ browserURL: cdpUrl, defaultViewport: null });
  } catch (e) {
    console.error('  [DOM poll] Cannot connect to CDP:', e.message);
    if (_replyCache[agentId]) _replyCache[agentId].generating = false;
    return;
  }

  console.log(`  [DOM poll] agentId=${agentId}, max wait ${maxMs/1000}s`);

  try {
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, POLL_MS));

      const pages = await browser.pages();
      const chatPages = pages.filter(p => p.url().includes('chatgpt.com'));
      if (!chatPages.length) continue;
      let page = chatPages.find(p => true);
      for (const p of chatPages) {
        try {
          const hasStop = await p.evaluate(() => !!document.querySelector('[data-testid="stop-button"]'));
          if (hasStop) { page = p; break; }
        } catch (_) {}
      }
      try { await page.bringToFront(); } catch (_) {}

      const text = await page.evaluate(() => {
        const els = document.querySelectorAll('[data-message-author-role="assistant"]');
        if (!els.length) return '';
        return (els[els.length - 1]?.innerText || '').trim();
      }).catch(() => '');

      if (!text) continue;

      const stillGenerating = await page.evaluate(() =>
        !!document.querySelector('[data-testid="stop-button"]')
      ).catch(() => false);

      console.log(`  [DOM轮询] generating=${stillGenerating} len=${text.length}: ${text.slice(0,60).replace(/\n/g,' ')}`);

      if (!stillGenerating && !_isIntermediate(text)) {
        _replyCache[agentId] = { text, generating: false, updatedAt: Date.now() };
        console.log(`  [DOM poll] agentId=${agentId} got final reply len=${text.length}`);
        return;
      }
      if (_replyCache[agentId]) {
        _replyCache[agentId].text = text;
        _replyCache[agentId].updatedAt = Date.now();
      }
    }
  } finally {
    await browser.disconnect().catch(() => {});
  }

  if (_replyCache[agentId]) {
    _replyCache[agentId].generating = false;
    console.warn(`  [DOM poll] agentId=${agentId} timeout, forcing generating=false`);
  }
}

  // GET /poll?agentId=default — agent_loop.py polls for latest reply
  if (req.method === 'GET' && pathname === '/poll') {
    const agentId = new URL(req.url, 'http://localhost').searchParams.get('agentId') ?? 'default';
    const cached = _replyCache[agentId] ?? { text: '', generating: false, updatedAt: 0 };
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: true, ...cached }));
    return;
  }

  res.writeHead(404);
  res.end(JSON.stringify({ error: { message: 'Not found' } }));
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, '127.0.0.1', () => {
  console.log('AgentPilot API: http://127.0.0.1:' + PORT);
  console.log('  POST /chat - agent dialog (agent_loop.py)');
  console.log('');
  console.log('Cursor: Base URL must be PUBLIC (Cursor routes via api2.cursor.sh)');
  console.log('  Run: npm run ngrok');
  console.log('  Then set Base URL = https://YOUR-NGROK-URL/v1');
  console.log('  API Key = ollama');
}).on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error('');
    console.error('Port ' + PORT + ' in use. Free it with:');
    console.error('  npm run api:stop');
    console.error('');
    process.exit(1);
  }
  throw err;
});
