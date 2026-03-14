/**
 * ChatGPT Web 桥接 - 用户输入 → ChatGPT Web → llm_output → router
 */
import * as chatgptWeb from './chatgpt-web-client.js';
import { processLlmOutput } from './router.js';
import { runVerifyLoop } from './agent/verifyLoop.js';
import { runIntentLoop } from './agent/intentLoop.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

const TOOL_PROMPT = `You are a local AI agent. When the user requests system operations, output only a tool block.

Format:
<tool>
name: open_notepad
</tool>

or
<tool>
name: list_dir
args: {"path":"C:\\\\Users"}
</tool>

run_command: use script for PowerShell. MUST use \\n between statements.
Execution rules: (1) NEVER call interactive programs (notepad, mspaint) in script - they block. Use separate run_command with command: "start notepad path" after file exists. (2) Invoke-WebRequest MUST use -TimeoutSec and try/catch; output SUCCESS:path or FAILED:message for verification.
<tool>
name: run_command
args: {"script":"$p = \\"$env:USERPROFILE\\\\Desktop\\\\news.txt\\"\\ntry { Invoke-WebRequest -Uri 'https://...' -TimeoutSec 10 | % { $_.Content | Out-File $p -Encoding UTF8 }; Write-Output SUCCESS:$p } catch { Write-Output FAILED:$($_.Exception.Message) }"}
</tool>
Then open file: <tool>name: run_command args: {"command":"start notepad \\"%USERPROFILE%\\\\Desktop\\\\news.txt\\""}</tool>

PowerShell: Paths with $env: use double quotes. Single quotes block expansion. Script outputs SUCCESS or FAILED prefix.

Termination: FAILED or 404 three times -> stop. Do not guess URLs. read_file path supports $env:USERPROFILE.

Tools: open_notepad, open_cmd, list_dir, read_file, sys_info, run_command(args.script or args.command)
Output only the tool block. No explanation.`;

const VALID_MODES = ['intent', 'verify', 'single'];

let _config = null;
function loadConfig() {
  if (_config) return _config;
  try {
    _config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  } catch (e) {
    throw new Error('config.json 读取失败: ' + e.message);
  }
  return _config;
}

const chatOpts = (cdp) => ({
  cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
  chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
  pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
  pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
  pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
});

export default {
  async chat(userInput, options = {}) {
    const config = loadConfig();
    const cdp = config?.llm?.cdp ?? {};
    const mode = options.mode ?? config?.agent?.mode ?? 'intent';

    if (!VALID_MODES.includes(mode)) {
      throw new Error(`未知 agent mode: ${mode}`);
    }

    try {
      if (mode === 'intent') {
        return await runIntentLoop(userInput, { cdp, newChat: options.newChat });
      }
      if (mode === 'verify') {
        return await runVerifyLoop(userInput, { cdp, newChat: options.newChat });
      }

      const text = TOOL_PROMPT + '\n\nUser: ' + userInput;
      const result = await chatgptWeb.chat(text, { ...chatOpts(cdp), newChat: options.newChat ?? false });
      const llm_output = result?.text || '';
      const out = await processLlmOutput(llm_output);

      if (out.error && out.error === 'no tool') {
        if ((llm_output || '').trim().length > 50) {
          return { ok: true, result: llm_output };
        }
        return { ok: false, error: out.error, raw: llm_output };
      }
      return out;
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
