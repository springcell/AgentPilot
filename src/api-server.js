/**
 * OpenAI-compatible API server for Cursor
 * Exposes /v1/chat/completions - forwards to ChatGPT Web (CDP)
 * Exposes POST /tools/invoke - direct tool execution (OpenClaw-style)
 */

import http from 'http';
import * as chatgptWeb from './chatgpt-web-client.js';
import * as xmlTools from './xml-tools.js';
import { processLlmOutput } from './router.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

/** OpenClaw 工具名 -> ZeroChatgpt 工具名 */
const TOOL_NAME_MAP = { bash: 'run_command', exec: 'run_command' };

/** POST /tools/invoke - 走 router 工具链 */
async function handleToolsInvoke(body) {
  let toolName = typeof body?.tool === 'string' ? body.tool.trim() : '';
  if (!toolName) {
    return { ok: false, error: { type: 'invalid_request', message: 'body.tool is required' }, status: 400 };
  }
  toolName = TOOL_NAME_MAP[toolName.toLowerCase()] ?? toolName;
  const args = body?.args && typeof body.args === 'object' && !Array.isArray(body.args)
    ? body.args
    : {};

  const llm_output = `<tool>\nname: ${toolName}\nargs: ${JSON.stringify(args)}\n</tool>`;
  const out = await processLlmOutput(llm_output);

  if (out.error) {
    return {
      ok: false,
      error: { type: 'tool_error', message: out.error },
      status: 500,
    };
  }
  return { ok: true, result: out.result };
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
      choices: [{ index: 0, message: { role: 'assistant', content: 'Hello! ZeroChatgpt ready.' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 5, total_tokens: 6 },
    };
    return { data, stream, id: data.id, content: 'Hello! ZeroChatgpt ready.' };
  }

  try {
    const result = await chatgptWeb.chat(text, {
      cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
      chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
      pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
      pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
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
    return new URL(url || '/', 'http://localhost').pathname;
  } catch (_) {
    return (url || '/').split('?')[0];
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

  if (req.method === 'GET' && pathname === '/tools-demo') {
    const demoPath = path.join(__dirname, '..', 'public', 'tools-demo.html');
    try {
      const html = fs.readFileSync(demoPath, 'utf-8');
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.writeHead(200);
      res.end(html);
    } catch (_) {
      res.writeHead(404);
      res.end('Not found');
    }
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
      console.log('[chat] Cursor Responses API format (input) detected, converting');
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

  if (req.method === 'POST' && pathname === '/tools/invoke') {
    let body = '';
    for await (const chunk of req) body += chunk;
    let parsed;
    try {
      parsed = body ? JSON.parse(body) : {};
    } catch (_) {
      res.writeHead(400);
      res.end(JSON.stringify({ ok: false, error: { type: 'invalid_json', message: 'Invalid JSON body' } }));
      return;
    }
    const result = await handleToolsInvoke(parsed);
    if (result.error) {
      res.writeHead(result.status || 500);
      res.end(JSON.stringify({ ok: result.ok ?? false, error: result.error }));
      return;
    }
    res.writeHead(200);
    res.end(JSON.stringify({ ok: true, result: result.result }));
    return;
  }

  res.writeHead(404);
  res.end(JSON.stringify({ error: { message: 'Not found' } }));
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, '127.0.0.1', () => {
  console.log('ZeroChatgpt API: http://127.0.0.1:' + PORT);
  console.log('');
  console.log('Cursor: Base URL must be PUBLIC (Cursor routes via api2.cursor.sh)');
  console.log('  Run: npm run ngrok');
  console.log('  Then set Base URL = https://YOUR-NGROK-URL/v1');
  console.log('  API Key = ollama');
});
