/**
 * MCP Server - 暴露 ask_chatgpt 工具给 Cursor Agent
 * Cursor Agent 使用内置模型，需要调用 ChatGPT 时通过此工具
 * 避免作为主模型时的循环问题
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import * as z from 'zod';
import * as chatgptWeb from './chatgpt-web-client.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

const server = new McpServer({
  name: 'zero-chatgpt',
  version: '1.0.0',
});

server.registerTool('ask_chatgpt', {
  title: 'Ask ChatGPT',
  description: 'Call ChatGPT web once. Send the full user question in message. Returns the complete response. Use ONCE per user request, then present the result. Do not call again.',
  inputSchema: {
    message: z.string().describe('The complete question or message to send to ChatGPT (include full context)'),
  },
}, async ({ message }) => {
  const config = loadConfig();
  const cdp = config?.llm?.cdp ?? {};
  try {
    const result = await chatgptWeb.chat(message, {
      cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
      chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
      pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
      pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
      pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
    });
    const text = result.text || '(empty response)';
    return {
      content: [{ type: 'text', text: `[ChatGPT Response - Final]\n\n${text}` }],
    };
  } catch (err) {
    return {
      content: [{ type: 'text', text: `[ChatGPT Error] ${err.message}` }],
      isError: true,
    };
  }
});

server.registerPrompt('consult_chatgpt', {
  title: 'Consult ChatGPT',
  description: 'Send a question to ChatGPT. Agent will call ask_chatgpt once and present the result.',
  argsSchema: {
    question: z.string().describe('The question to ask ChatGPT'),
  },
}, async ({ question }) => ({
  messages: [
    { role: 'user', content: { type: 'text', text: `Call ask_chatgpt with message: "${question}". Use it ONCE. Present the response to the user. Do not call again.` } },
  ],
}));

const transport = new StdioServerTransport();
await server.connect(transport);
