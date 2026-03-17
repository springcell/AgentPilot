/**
 * OpenAI-compatible API server for Cursor
 * Exposes /v1/chat/completions - forwards to ChatGPT Web (CDP)
 */

import http from 'http';
import * as chatgptWeb from './chatgpt-web-client.js';
import * as xmlTools from './xml-tools.js';
import { parseCursorToolCalls, buildCursorPrompt, buildEnforceJsonPrompt } from './cursor-json-bridge.js';
import bridge from './bridge.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');
const LOG_PATH    = path.join(__dirname, '..', 'logs', 'api-server.log');
const FILE_CHAT_RETRY_PATH = path.join(__dirname, '..', 'agent', 'config', 'file_chat_retry.json');

// ── Structured logger ─────────────────────────────────────────────────────────
// Writes JSON-lines to logs/api-server.log; also echoes to console.
// Each entry: { ts, level, tag, msg, ...extra }

fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
let _logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' });

function _log(level, tag, msg, extra = {}) {
  const entry = { ts: new Date().toISOString(), level, tag, msg, ...extra };
  const line = JSON.stringify(entry);
  try { _logStream.write(line + '\n'); } catch (_) {}
  // Mirror to console with colour coding
  const prefix = `[${entry.ts.slice(11,19)}][${tag}]`;
  if (level === 'ERROR') console.error(prefix, msg, Object.keys(extra).length ? extra : '');
  else if (level === 'WARN')  console.warn(prefix, msg, Object.keys(extra).length ? extra : '');
  else                        console.log(prefix, msg, Object.keys(extra).length ? extra : '');
}

const log = {
  info:  (tag, msg, extra) => _log('INFO',  tag, msg, extra),
  warn:  (tag, msg, extra) => _log('WARN',  tag, msg, extra),
  error: (tag, msg, extra) => _log('ERROR', tag, msg, extra),
};

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

function loadJsonFile(filePath, fallback = {}) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch (_) {
    return fallback;
  }
}

const FILE_CHAT_RETRY_CFG = loadJsonFile(FILE_CHAT_RETRY_PATH, {
  maxTries: 3,
  retryDelayMs: 2500,
  stateRetryWhenUploadUnconfirmed: true,
  uploadRequestPatterns: [
    "\u8bf7\u4e0a\u4f20",
    "\u8bf7\u91cd\u65b0\u4e0a\u4f20",
    "please\\s+upload",
    "please\\s+re-?upload",
    "upload\\s+(the\\s+)?image",
    "attach\\s+(the\\s+)?image"
  ],
  negativePatterns: [
    "\u65e0\u9700\u4e0a\u4f20",
    "\u4e0d\u9700\u8981\u4e0a\u4f20",
    "do\\s+not\\s+upload",
    "no\\s+need\\s+to\\s+upload",
    "already\\s+uploaded"
  ],
});

const FILE_CHAT_UPLOAD_REQUEST_RES = (FILE_CHAT_RETRY_CFG.uploadRequestPatterns || [])
  .map((pattern) => {
    try { return new RegExp(pattern, 'i'); } catch (_) { return null; }
  })
  .filter(Boolean);

const FILE_CHAT_NEGATIVE_RES = (FILE_CHAT_RETRY_CFG.negativePatterns || [])
  .map((pattern) => {
    try { return new RegExp(pattern, 'i'); } catch (_) { return null; }
  })
  .filter(Boolean);

function analyzeUploadRetry(text) {
  const raw = text || '';
  const negative = FILE_CHAT_NEGATIVE_RES.find((re) => re.test(raw)) ?? null;
  const positive = negative ? null : (FILE_CHAT_UPLOAD_REQUEST_RES.find((re) => re.test(raw)) ?? null);
  return {
    matched: !!positive,
    blockedByNegative: !!negative,
    positivePattern: positive?.source ?? null,
    negativePattern: negative?.source ?? null,
  };
}

// ── Local executor bridge ─────────────────────────────────────────────────────
// Calls executor_server.py (http://127.0.0.1:4001/exec) to run file/shell ops.

const EXECUTOR_URL = 'http://127.0.0.1:4001/exec';

/**
 * Map a Cursor tool_call (name + parsed arguments) to an executor.py JSON block.
 * Returns null if the tool is not supported locally.
 */
function _toLocalInstruction(toolName, args) {
  const n = toolName.toLowerCase();

  // Read / view file
  if (n === 'read' || n === 'read_file' || n === 'view_file') {
    return { command: 'file_op', action: 'read', path: args.path ?? args.target_file ?? '' };
  }
  // Write / overwrite file
  if (n === 'write' || n === 'write_file' || n === 'edit_file' || n === 'strreplace') {
    if (n === 'strreplace') {
      // StrReplace: {path, old_str, new_str}
      return { command: 'file_op', action: 'patch', path: args.path ?? '',
               replacements: [{ old: args.old_str ?? '', new: args.new_str ?? '' }] };
    }
    return { command: 'file_op', action: 'write', path: args.path ?? args.target_file ?? '',
             content: args.content ?? args.new_content ?? '' };
  }
  // Delete file
  if (n === 'delete' || n === 'delete_file') {
    return { command: 'file_op', action: 'delete', path: args.path ?? '' };
  }
  // List directory
  if (n === 'list_dir' || n === 'list' || n === 'ls') {
    return { command: 'file_op', action: 'list', path: args.path ?? '.' };
  }
  // Glob / find files by pattern
  if (n === 'glob' || n === 'find' || n === 'list_dir') {
    return { command: 'file_op', action: 'find',
             path: args.path ?? args.dir ?? '.', pattern: args.pattern ?? args.glob ?? '*' };
  }
  // Grep / search in files
  if (n === 'grep' || n === 'grep_search') {
    return { command: 'powershell', arguments:
      [`Select-String -Path "${args.path ?? '.'}" -Pattern "${args.pattern ?? args.query ?? ''}" -Recurse`] };
  }
  // Semantic / codebase search — use file_op find as best effort
  if (n === 'semanticsearch' || n === 'codebase_search') {
    return { command: 'file_op', action: 'find',
             path: args.dir ?? '.', pattern: `*${args.query ?? args.search ?? ''}*` };
  }
  // Shell / terminal
  if (n === 'shell' || n === 'run_terminal' || n === 'run_terminal_cmd') {
    const cmd = args.command ?? args.cmd ?? '';
    return { command: 'powershell', arguments: [cmd] };
  }

  return null; // unsupported — fall back to Cursor native
}

/**
 * Execute one local instruction via executor_server.py.
 * Returns { success, stdout, stderr, returncode }.
 */
async function _runLocal(instruction) {
  try {
    const res = await fetch(EXECUTOR_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(instruction),
      signal: AbortSignal.timeout(60000),
    });
    return await res.json();
  } catch (err) {
    return { success: false, stdout: '', stderr: `executor_server unreachable: ${err.message}`, returncode: -1 };
  }
}

/**
 * Check if executor_server.py is running.
 */
async function _executorAvailable() {
  try {
    const res = await fetch('http://127.0.0.1:4001/health', { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

// agent_loop.py poll cache: key = agentId (default "default"), value = { text, generating, updatedAt }
const _replyCache = {};

// Match intermediate state (search/think/browse); supports "ChatGPT says: ..." style prefix
const _INTERMEDIATE_RE = [
  /正在搜索/, /正在思考/, /正在浏览/, /正在查找/,
  /正在创建/, /正在生成/, /正在处理/, /正在绘制/, /正在渲染/,
  /正在上传/, /正在分析/, /正在修改/, /正在优化/,
  /Searching/i, /Thinking/i, /Looking up/i, /Browsing/i,
  /Creating/i, /Generating/i, /Processing/i, /Drawing/i, /Rendering/i,
  /Uploading/i, /Analyzing/i, /Modifying/i,
];

const _ROUTING_REPLY_RE = [
  /^[\u4e00-\u9fa5a-zA-Z\s,，]+需要\s+[a-z_]+\s+去做[.!]?$/i,
  /^[\u4e00-\u9fa5a-zA-Z\s,，]+,\s*need\s+[a-z_]+\s+to\s+do\s+it[.!]?$/i,
];

function _looksLikeRoutingReply(text) {
  const cleaned = (text || '').trim().replace(/^[\s\S]{0,30}?ChatGPT\s*[^\n]*[：:]\s*/i, '').trim();
  if (!cleaned) return false;
  return _ROUTING_REPLY_RE.some((re) => re.test(cleaned));
}

function _isIntermediate(text) {
  if (!text) return true;
  // Task complete (highest priority); accept both EN and legacy CN
  if (text.includes('✅ Task complete') || text.includes('Task complete:') ||
      text.includes('✅ 任务完成') || text.includes('任务完成：')) return false;
  if (_looksLikeRoutingReply(text)) return false;
  if (text.trim().length < 20) return true;
  const cleaned = text.trim().replace(/^[\s\S]{0,30}?ChatGPT\s*[^\n]*[：:]\s*/i, '').trim();
  return _INTERMEDIATE_RE.some(r => r.test(cleaned));
}

function getContentFromItem(item) {
  if (typeof item.content === 'string') return item.content;
  if (Array.isArray(item.content)) {
    const parts = item.content
      .filter(c => c.type === 'text' || c.type === 'input_text' || c.type === 'document' || c.type === 'file')
      .map(c => {
        if (c.text) return c.text;
        if (c.source?.data) return c.source.data;
        if (c.source?.text) return c.source.text;
        if (c.file?.file_data) {
          try { return Buffer.from(c.file.file_data, 'base64').toString('utf-8'); } catch (_) { return ''; }
        }
        return '';
      });
    return parts.join('\n').trim() || (item.content[0]?.text ?? '');
  }
  return '';
}

/**
 * Flatten a full Cursor messages array (including tool results) into a single context string.
 * system -> user -> assistant -> tool -> user ...
 */
function flattenMessagesForChatGPT(messages) {
  const lines = [];
  for (const m of messages) {
    const role = m.role || m.role_name || 'user';
    if (role === 'system') {
      lines.push(`[System]\n${getContentFromItem(m)}`);
    } else if (role === 'user') {
      const text = getContentFromItem(m);
      if (text) lines.push(`[User]\n${text}`);
    } else if (role === 'assistant') {
      // assistant may have text content OR tool_calls
      const text = getContentFromItem(m);
      if (text) lines.push(`[Assistant]\n${text}`);
      if (Array.isArray(m.tool_calls)) {
        for (const tc of m.tool_calls) {
          const args = typeof tc.function?.arguments === 'string'
            ? tc.function.arguments
            : JSON.stringify(tc.function?.arguments ?? {});
          lines.push(`[Tool call: ${tc.function?.name ?? tc.id}]\n${args}`);
        }
      }
    } else if (role === 'tool') {
      // tool result message: { role:'tool', tool_call_id, content }
      const content = typeof m.content === 'string' ? m.content : getContentFromItem(m);
      const name = m.name || m.tool_call_id || 'tool';
      lines.push(`[Tool result: ${name}]\n${content}`);
    }
  }
  return lines.join('\n\n');
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

  const hasTools = !!(body.tools?.length);

  // ── Cursor with tools ──────────────────────────────────────────────────────
  if (hasTools) {
    const config = loadConfig();
    const cdpCfg = config?.llm?.cdp ?? {};
    const cdpOpts = {
      cdpUrl:             cdpCfg.url                ?? 'http://127.0.0.1:9222',
      chatgptUrl:         cdpCfg.chatgptUrl          ?? 'https://chatgpt.com/',
      pollIntervalMs:     cdpCfg.replyPollIntervalMs ?? 500,
      pollTimeoutMs:      cdpCfg.replyPollTimeoutMs  ?? 120000,
      pageReadyTimeoutMs: cdpCfg.pageReadyTimeoutMs  ?? 15000,
    };

    // Check if local executor is available (executor_server.py running on :4001)
    const localExecAvailable = await _executorAvailable();
    if (!localExecAvailable) {
      log.warn('cursor-bridge', 'executor_server.py not running on :4001 — run: python agent/executor_server.py');
    }

    // ── Round 1: build prompt → ChatGPT → parse tool_calls ───────────────────
    const prompt = buildCursorPrompt(messages, body.tools, flattenMessagesForChatGPT);
    log.info('cursor-bridge', 'sending to ChatGPT', { msgs: messages.length, promptLen: prompt.length });
    log.info('cursor-bridge', 'prompt HEAD', { head: prompt.slice(0, 300) });

    let reply;
    try {
      reply = (await chatgptWeb.chat(prompt, cdpOpts)).text || '';
    } catch (err) {
      log.error('cursor-bridge', 'ChatGPT request failed', { error: err.message });
      return { error: { message: err.message || 'ChatGPT Web request failed' }, status: 500 };
    }
    log.info('cursor-bridge', 'raw reply', { replyLen: reply.length, preview: reply.slice(0, 400) });

    let { content, tool_calls, hadJson, loosePath } = parseCursorToolCalls(reply, body.tools);
    log.info('cursor-bridge', 'parsed tool_calls', {
      count: tool_calls?.length ?? 0,
      hadJson,
      loosePath: loosePath ?? null,
      names: tool_calls?.map(t => t.function.name) ?? [],
    });

    // ── Retry once if no JSON at all ─────────────────────────────────────────
    if (!tool_calls?.length && !hadJson) {
      log.warn('cursor-bridge', 'no JSON in first reply — enforcing', { loosePath: loosePath ?? null });
      const enforcePrompt = buildEnforceJsonPrompt(reply, body.tools, loosePath);
      let reply2 = '';
      try { reply2 = (await chatgptWeb.chat(enforcePrompt, cdpOpts)).text || ''; } catch (_) {}
      if (reply2) {
        const p2 = parseCursorToolCalls(reply2, body.tools);
        tool_calls = p2.tool_calls;
        content    = p2.content;
        if (tool_calls?.length) {
          log.info('cursor-bridge', 'retry success', { names: tool_calls.map(t => t.function.name) });
        } else {
          content = p2.content || reply2;
          log.warn('cursor-bridge', 'retry still no tool_calls — returning plain content');
        }
      }
    } else if (!tool_calls?.length) {
      log.warn('cursor-bridge', 'JSON found but no mappable tool names — returning plain content');
      content = reply;
    }

    // ── Local execution: intercept tool_calls, run via executor_server ────────
    if (tool_calls?.length && localExecAvailable) {
      log.info('cursor-bridge', 'executing tool calls locally', { count: tool_calls.length });

      const toolResultLines = [];
      for (const tc of tool_calls) {
        const name = tc.function.name;
        let args = {};
        try { args = JSON.parse(tc.function.arguments || '{}'); } catch (_) {}

        const instruction = _toLocalInstruction(name, args);
        if (!instruction) {
          log.warn('cursor-bridge', `${name} not supported locally — skipping`);
          toolResultLines.push(`[Tool: ${name}]\nNot supported locally — skipped.`);
          continue;
        }

        log.info('cursor-bridge', `exec: ${name}`, { instruction });
        const result = await _runLocal(instruction);
        log.info('cursor-bridge', `exec result: ${name}`, {
          success: result.success,
          rc: result.returncode,
          stdoutLen: (result.stdout || '').length,
          stderrPreview: (result.stderr || '').slice(0, 120) || null,
        });

        const resultText = result.success
          ? (result.stdout || '(no output)')
          : `Error: ${result.stderr || 'unknown error'}`;
        toolResultLines.push(`[Tool result: ${name}]\n${resultText}`);
      }

      // Round 2: inject tool results into context, ask ChatGPT for final answer
      const toolResultBlock = toolResultLines.join('\n\n');
      const round2Prompt = [
        prompt,
        '',
        toolResultBlock,
        '',
        'Based on the tool results above, provide your final answer to the user. Respond in plain text — no JSON.',
      ].join('\n');

      log.info('cursor-bridge', 'round 2 → ChatGPT', { round2Len: round2Prompt.length });
      let finalReply = '';
      try {
        finalReply = (await chatgptWeb.chat(round2Prompt, cdpOpts)).text || '';
      } catch (err) {
        log.error('cursor-bridge', 'round 2 ChatGPT failed', { error: err.message });
        finalReply = toolResultBlock;
      }
      log.info('cursor-bridge', 'final reply', { finalLen: finalReply.length, preview: finalReply.slice(0, 300) });

      const id = 'chatcmpl-' + Date.now();
      const data = {
        id,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [{ index: 0, message: { role: 'assistant', content: finalReply }, finish_reason: 'stop' }],
        usage: {
          prompt_tokens:     Math.ceil(round2Prompt.length / 4),
          completion_tokens: Math.ceil(finalReply.length / 4),
          total_tokens:      Math.ceil((round2Prompt.length + finalReply.length) / 4),
        },
      };
      return { data, stream: false, id, content: finalReply };
    }

    // ── Fallback: return tool_calls to Cursor (executor not available) ─────────
    const id = 'chatcmpl-' + Date.now();

    if (tool_calls?.length) {
      log.info('cursor-bridge', 'forwarding tool_calls to Cursor', {
        count: tool_calls.length,
        calls: tool_calls.map(tc => ({ id: tc.id, name: tc.function.name, args: tc.function.arguments })),
      });
      const data = {
        id,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [{
          index: 0,
          message: { role: 'assistant', content: null, tool_calls },
          finish_reason: 'tool_calls',
        }],
        usage: { prompt_tokens: Math.ceil(prompt.length / 4), completion_tokens: 10,
                 total_tokens: Math.ceil(prompt.length / 4) + 10 },
      };
      return { data, stream: false, id, content: null };
    }

    // Plain answer (no tool_calls)
    const finalContent = content || reply;
    log.info('cursor-bridge', 'plain content', { len: finalContent.length });
    const data = {
      id,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, message: { role: 'assistant', content: finalContent }, finish_reason: 'stop' }],
      usage: {
        prompt_tokens:     Math.ceil(prompt.length / 4),
        completion_tokens: Math.ceil(finalContent.length / 4),
        total_tokens:      0,
      },
    };
    data.usage.total_tokens = data.usage.prompt_tokens + data.usage.completion_tokens;
    return { data, stream, id, content: finalContent };
  }

  // ── No tools: use last user message only (original behaviour) ────────────
  let text = getLastUserContent(messages);
  if (!text.trim()) {
    return { error: { message: 'No user message content' }, status: 400 };
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

    const content = result.text || '';
    const id = 'chatcmpl-' + Date.now();
    const response = {
      id,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: model,
      choices: [{
        index: 0,
        message: { role: 'assistant', content },
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

/** Send a text prompt to ChatGPT and build a standard completion response. */
async function handleDirectChatGPT(text, model, stream) {
  if (!text?.trim()) {
    return { error: { message: 'No content to send' }, status: 400 };
  }
  const config = loadConfig();
  const cdp = config?.llm?.cdp ?? {};
  try {
    const result = await chatgptWeb.chat(text, {
      cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
      chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
      pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
      pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
      pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
    });
    const content = result.text || '';
    const id = 'chatcmpl-' + Date.now();
    const data = {
      id,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: {
        prompt_tokens: Math.ceil(text.length / 4),
        completion_tokens: Math.ceil(content.length / 4),
        total_tokens: 0,
      },
    };
    data.usage.total_tokens = data.usage.prompt_tokens + data.usage.completion_tokens;
    return { data, stream, id, content };
  } catch (err) {
    return { error: { message: err.message || 'ChatGPT Web request failed' }, status: 500 };
  }
}

function getPathname(url) {
  try {
    let p = new URL(url || '/', 'http://localhost').pathname;
    return p.replace(/\/+$/, '') || '/';
  } catch (_) {
    return ((url || '/').split('?')[0]).replace(/\/+$/, '') || '/';
  }
}

const server = http.createServer(async (req, res) => {
  const pathname = getPathname(req.url);
  const reqId = Date.now().toString(36);
  log.info('http', `${req.method} ${pathname}`, { reqId });
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
      log.info('chat', 'Cursor Responses API format (input) detected, converting to messages', { reqId });
    }

    const t0 = Date.now();
    const result = await handleChatCompletions(parsed);
    log.info('chat', 'handleChatCompletions done', { reqId, ms: Date.now() - t0, hasError: !!result.error });
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
      const result = await bridge.chat(message, { newChat: !!newChat, agentId });
      const text = result?.result ?? '';
      // intermediate = content looks like an in-progress state, OR doChat returned early with generating:true
      // Exception: if text already contains task-complete marker, it's final regardless of generating flag
      const isTaskDone = text.includes('✅ Task complete') || text.includes('Task complete:') ||
        text.includes('✅ 任务完成') || text.includes('任务完成：');
      // Also consider done if image was captured (no need to poll further)
      const hasImage = !!(result?.downloaded_b64);
      const intermediate = !isTaskDone && !hasImage && (_isIntermediate(text) || result?.generating === true);
      _replyCache[agentId] = { text, generating: intermediate, updatedAt: Date.now(),
        ...(result?.downloaded_b64 ? { downloaded_b64: result.downloaded_b64, downloaded_ext: result.downloaded_ext ?? '.bin' } : {}) };
      if (intermediate) {
        console.warn(`  [poll] agentId=${agentId} intermediate (len=${text.length}), keeping generating:true`);
        console.warn(`  [poll]    "${text.slice(0, 60).replace(/\n/g, ' ')}"`);
        _pollDomUntilFinal(agentId).catch(e =>
          console.error('  [poll] DOM poll error:', e.message)
        );
      } else if (agentId === 'dispatcher') {
        bridge.closeAgent(agentId).catch(e =>
          console.error(`  [dispatcher] close failed: ${e.message}`)
        );
        delete _replyCache[agentId];
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

  if (req.method === 'POST' && pathname === '/close-agent') {
    let body = '';
    for await (const chunk of req) body += chunk;
    let parsed;
    try {
      parsed = body ? JSON.parse(body) : {};
    } catch (_) {
      res.writeHead(400);
      res.end(JSON.stringify({ ok: false, error: 'Invalid JSON' }));
      return;
    }
    const { agentId = 'default' } = parsed;
    try {
      await bridge.closeAgent(agentId);
      delete _replyCache[agentId];
      res.writeHead(200);
      res.end(JSON.stringify({ ok: true, agentId }));
    } catch (e) {
      res.writeHead(500);
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
    return;
  }

  // GET /poll?agentId=default — agent_loop.py polls for latest reply
  if (req.method === 'GET' && pathname === '/poll') {
    const agentId = new URL(req.url, 'http://localhost').searchParams.get('agentId') ?? 'default';
    const cached = _replyCache[agentId] ?? { text: '', generating: false, updatedAt: 0 };
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: true, ...cached }));
    return;
  }

  // GET /capture-image?agentId=default — return any captured file from the last chat reply
  // (No DOM crawling needed — doChat already captures via CDP network interception)
  if (req.method === 'GET' && pathname === '/capture-image') {
    const agentId = new URL(req.url, 'http://localhost').searchParams.get('agentId') ?? 'default';
    const cached = _replyCache[agentId] ?? {};
    const b64  = cached.downloaded_b64  ?? null;
    const ext  = cached.downloaded_ext  ?? null;
    if (b64) console.log(`  [capture-image] returning cached file ${b64.length} chars ext=${ext}`);
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: true, downloaded_b64: b64, downloaded_ext: ext }));
    return;
  }

  // POST /v1/file-chat  — upload a local file to ChatGPT, return its reply
  // Body: { "file_path": "/absolute/path/to/file", "message": "optional prompt", "agentId": "default" }
  if (req.method === 'POST' && pathname === '/v1/file-chat') {
    let body = '';
    for await (const chunk of req) body += chunk;
    let parsed;
    try { parsed = JSON.parse(body); } catch (_) {
      res.writeHead(400);
      res.end(JSON.stringify({ ok: false, error: 'Invalid JSON' }));
      return;
    }
    const { file_path, message = '', agentId = 'default' } = parsed;
    if (!file_path) {
      res.writeHead(400);
      res.end(JSON.stringify({ ok: false, error: 'file_path is required' }));
      return;
    }
    if (!fs.existsSync(file_path)) {
      res.writeHead(400);
      res.end(JSON.stringify({ ok: false, error: `File not found: ${file_path}` }));
      return;
    }

    const config = loadConfig();
    const cdpCfg = config?.llm?.cdp ?? {};
    const cdpOpts = {
      cdpUrl:             cdpCfg.url                ?? 'http://127.0.0.1:9222',
      chatgptUrl:         cdpCfg.chatgptUrl          ?? 'https://chatgpt.com/',
      pollIntervalMs:     cdpCfg.replyPollIntervalMs ?? 500,
      pollTimeoutMs:      cdpCfg.fileChatPollTimeoutMs ?? cdpCfg.replyPollTimeoutMs ?? 600000,
      pageReadyTimeoutMs: cdpCfg.pageReadyTimeoutMs  ?? 15000,
      agentId,
    };

    log.info('file-chat', 'uploading file', { file_path, messageLen: message.length, agentId });
    const maxTries = Number(FILE_CHAT_RETRY_CFG.maxTries ?? 3);
    const retryDelayMs = Number(FILE_CHAT_RETRY_CFG.retryDelayMs ?? 2500);
    const stateRetryWhenUploadUnconfirmed = FILE_CHAT_RETRY_CFG.stateRetryWhenUploadUnconfirmed !== false;
    let result;
    let tries = 0;
    try {
      const t0 = Date.now();
      do {
        result = await chatgptWeb.chatWithFile(file_path, message, cdpOpts);
        const retryAnalysis = analyzeUploadRetry(result.text);
        const uploadUnconfirmed = stateRetryWhenUploadUnconfirmed && result.uploadConfirmed === false;
        const shouldRetryUpload = !result.downloadedContent && (
          retryAnalysis.matched || uploadUnconfirmed
        );
        if (!shouldRetryUpload || tries >= maxTries - 1) break;
        tries += 1;
        log.warn('file-chat', 'reply asks for upload, retrying', {
          file_path,
          try: tries + 1,
          maxTries,
          retryReason: retryAnalysis.matched ? 'reply_pattern' : 'upload_unconfirmed',
          uploadPattern: retryAnalysis.positivePattern,
          blockedByNegative: retryAnalysis.blockedByNegative,
          negativePattern: retryAnalysis.negativePattern,
          uploadConfirmed: result.uploadConfirmed ?? null,
          uploadState: result.uploadState ?? null,
          replyPreview: (result.text || '').slice(0, 160),
        });
        await new Promise(r => setTimeout(r, retryDelayMs));
      } while (tries < maxTries);

      // If still generating after timeout and no download captured, try one more image capture
      if (result.generating && !result.downloadedContent) {
        log.warn('file-chat', 'timed out still generating — attempting final image capture', { file_path });
      }
      const textOnlyFinal = !result.downloadedContent && !result.generating && !!result.text && !_isIntermediate(result.text);
      const response = { ok: true, text: result.text, generating: result.generating ?? false };
      if (typeof result.uploadConfirmed === 'boolean') response.upload_confirmed = result.uploadConfirmed;
      if (textOnlyFinal) response.terminal_text_only = true;
      if (textOnlyFinal && _looksLikeRoutingReply(result.text)) response.terminal_reason = 'routing_reply';
      // If ChatGPT triggered a file download, include the bytes as base64
      if (result.downloadedContent) {
        response.downloaded_b64 = result.downloadedContent.toString('base64');
        response.downloaded_ext = result.downloadedExt ?? '.bin';
        log.info('file-chat', 'download captured', {
          file_path,
          downloadedBytes: result.downloadedContent.length,
          downloadedExt: response.downloaded_ext,
          ms: Date.now() - t0,
        });
      } else if (result.generating) {
        log.warn('file-chat', 'no download captured (still generating)', { file_path, ms: Date.now() - t0 });
        // Seed poll cache and start background DOM poll so client GET /poll can get the image later
        _replyCache[agentId] = { text: result.text || '', generating: true, updatedAt: Date.now() };
        setImmediate(() => {
          _pollDomUntilFinal(agentId).catch(e =>
            console.error('  [file-chat] background DOM poll error:', e.message)
          );
        });
      } else {
        log.info('file-chat', 'done (text only)', {
          file_path,
          replyLen: result.text.length,
          ms: Date.now() - t0,
          terminalTextOnly: textOnlyFinal,
          terminalReason: response.terminal_reason ?? null,
        });
      }
      res.writeHead(200);
      res.end(JSON.stringify(response));
    } catch (err) {
      log.error('file-chat', 'error', { file_path, error: err.message });
      res.writeHead(500);
      res.end(JSON.stringify({ ok: false, error: err.message }));
    }
    return;
  }

  res.writeHead(404);
  res.end(JSON.stringify({ error: { message: 'Not found' } }));
});

// Background DOM poll: after bridge returns intermediate state, keep reading page until final reply
async function _pollDomUntilFinal(agentId, maxMs = 600_000) {
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

  let pollCount = 0;
  let lastLoggedText = '';
  const LOG_EVERY_N = 10; // log at most every N polls to avoid spam when reply is unchanged

  try {
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, POLL_MS));
      pollCount += 1;

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

      const shouldLog = (pollCount % LOG_EVERY_N === 0) || (text !== lastLoggedText);
      if (shouldLog) {
        lastLoggedText = text;
        console.log(`  [DOM轮询] #${pollCount} generating=${stillGenerating} len=${text.length}: ${text.slice(0,60).replace(/\n/g,' ')}`);
      }

      if (!stillGenerating && !_isIntermediate(text)) {
        // Try to capture any generated image in the reply
        let downloaded_b64 = null;
        try {
          const imgUrl = await page.evaluate(() => {
            const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
            if (!msgs.length) return null;
            const last = msgs[msgs.length - 1];
            const imgs = last.querySelectorAll('img');
            let best = null, bestArea = 0;
            for (const img of imgs) {
              const w = img.naturalWidth || img.width || img.clientWidth || 0;
              const h = img.naturalHeight || img.height || img.clientHeight || 0;
              const area = w * h;
              if (area > bestArea && area > 10000) { best = img.src || img.currentSrc || null; bestArea = area; }
            }
            return best;
          });
          if (imgUrl && imgUrl.startsWith('http')) {
            const b64 = await page.evaluate(async (url) => {
              try {
                const resp = await fetch(url, { credentials: 'include' });
                if (!resp.ok) return null;
                const ab = await resp.arrayBuffer();
                const bytes = new Uint8Array(ab);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return btoa(bin);
              } catch (_) { return null; }
            }, imgUrl);
            if (b64) { downloaded_b64 = b64; console.log(`  [DOM poll] captured image ${b64.length} chars`); }
          }
        } catch (_) {}
        _replyCache[agentId] = { text, generating: false, updatedAt: Date.now(),
          ...(downloaded_b64 ? { downloaded_b64 } : {}) };
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

const PORT = process.env.PORT || 3000;
server.listen(PORT, '127.0.0.1', () => {
  log.info('server', `AgentPilot API listening on http://127.0.0.1:${PORT}`, { logFile: LOG_PATH });
  console.log('AgentPilot API: http://127.0.0.1:' + PORT);
  console.log('  POST /chat - agent dialog (agent_loop.py)');
  console.log('  Log file: ' + LOG_PATH);
});
// Allow long-running connections (image generation can take 10+ minutes)
server.timeout = 0;          // disable HTTP server request timeout
server.keepAliveTimeout = 0; // disable keep-alive timeout
server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    log.error('server', `Port ${PORT} in use`, { port: PORT });
    console.error('');
    console.error('Port ' + PORT + ' in use. Free it with:');
    console.error('  npm run api:stop');
    console.error('');
    process.exit(1);
  }
  throw err;
});
