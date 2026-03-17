/**
 * AgentPilot - Chat entry
 * POST /chat - Chat via ChatGPT Web
 * CLI: node agent.js [message] or no args for interactive
 */
import express from 'express';
import fs from 'fs';
import path from 'path';
import { createInterface } from 'readline';
import { fileURLToPath } from 'url';
import bridge from './bridge.js';
import { checkHealth, isHealthy, lastError } from './health/checker.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

function loadServerConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
    const cfg = JSON.parse(raw);
    return cfg?.server ?? {};
  } catch (_) {
    return {};
  }
}

const serverCfg = loadServerConfig();
const CHAT_TIMEOUT = serverCfg.chatTimeoutMs ?? 90000;
const HEALTH_INTERVAL = serverCfg.healthCheckIntervalMs ?? 30000;

function withTimeout(handler, ms) {
  return async (req, res) => {
    const timeoutPromise = new Promise((resolve) => {
      setTimeout(() => {
        if (!res.headersSent) {
          res.status(503).json({ error: 'Request timeout, try again later' });
        }
        resolve();
      }, ms);
    });
    const handlerPromise = handler(req, res).catch((e) => {
      if (!res.headersSent) {
        res.status(500).json({ ok: false, error: e.message });
      }
    });
    await Promise.race([handlerPromise, timeoutPromise]);
  };
}

const app = express();
app.use(express.json());

checkHealth().then(() => {
  setInterval(checkHealth, HEALTH_INTERVAL);
});

app.post('/chat', withTimeout(async (req, res) => {
  const { message, newChat } = req.body || {};
  if (!message?.trim()) {
    return res.json({ error: 'message is required' });
  }
  try {
    const result = await bridge.chat(message, { newChat: !!newChat });
    res.json(result);
  } catch (e) {
    if (!res.headersSent) {
      res.status(500).json({ ok: false, error: e.message });
    }
  }
}, CHAT_TIMEOUT));

app.get('/health', (_req, res) => {
  if (isHealthy) {
    return res.status(200).json({ status: 'ok' });
  }
  return res.status(500).json({ status: 'error', message: lastError ?? 'System is down' });
});

async function runCli(message, options = {}) {
  process.stdout.write('Thinking...\n');
  try {
    const result = await bridge.chat(message, options);
    if (result.ok) {
      const text = typeof result.result === 'string' ? result.result : String(result.result ?? '');
      const out = text.trim() || '(No response)';
      process.stdout.write(out + '\n\n');
      return { ok: true, text: out, raw: result };
    } else if (result.code === 'CF_BLOCKED') {
      process.stdout.write(`\nNetwork restricted\n${result.error}\n`);
      return { ok: false, text: '', raw: result };
    } else {
      process.stdout.write(`Error: ${result.error}\n`);
      if (result.raw) process.stdout.write(`Raw: ${result.raw.slice(0, 200)}...\n`);
      return { ok: false, text: '', raw: result };
    }
  } catch (e) {
    process.stderr.write(`Error: ${e.message}\n`);
    if (e.message?.includes('not found') || e.message?.includes('timeout') || e.message?.includes('Auth timeout')) {
      process.stderr.write('Tip: Run npm run chrome first, then log in at chatgpt.com\n');
    }
    process.exit(1);
  }
}

function isTaskComplete(text) {
  const value = String(text || '');
  return value.includes('✅ Task complete') || value.includes('Task complete:') ||
    value.includes('✅ 任务完成') || value.includes('任务完成：');
}

async function runInteractive() {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const promptForInstructions = (label = '\nInput (empty to exit)> ') =>
    rl.question(label, async (line) => {
      const input = line?.trim();
      if (!input) {
        rl.close();
        return;
      }
      const newChat = input.startsWith('/new ');
      const msg = newChat ? input.slice(5).trim() : input;
      const cliResult = await runCli(msg, newChat ? { newChat: true } : {});
      if (!cliResult?.ok || !isTaskComplete(cliResult.text)) {
        promptForInstructions();
        return;
      }
      rl.question('Task complete? (yes/no) ', async (answer) => {
        const choice = (answer || '').trim().toLowerCase();
        if (choice === 'yes' || choice === 'y') {
          try {
            await bridge.closeAgent('default');
          } catch (e) {
            process.stderr.write(`Close chat failed: ${e.message}\n`);
          }
          rl.close();
          return;
        }
        if (choice === 'no' || choice === 'n') {
          promptForInstructions('Please provide new instructions\n> ');
          return;
        }
        promptForInstructions('Please answer yes or no\n> ');
      });
    });
  console.log('AgentPilot - Chat with ChatGPT');
  console.log('  Prefix /new to start new chat: /new your message');
  promptForInstructions();
}

async function main() {
  const args = process.argv.slice(2);
  const newChat = args[0] === '/new' || args[0] === '--new';
  const message = newChat ? args.slice(1).join(' ').trim() : args.join(' ').trim();

  if (message) {
    await runCli(message, { newChat });
    return;
  }

  if (process.stdin.isTTY) {
    runInteractive();
    return;
  }

  const PORT = process.env.PORT || 3000;
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`AgentPilot running on http://127.0.0.1:${PORT}`);
    console.log('  POST /chat - message (ChatGPT Web)');
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
