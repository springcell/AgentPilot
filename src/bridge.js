/**
 * ChatGPT Web 桥接 - 用户输入 → ChatGPT Web → llm_output → router
 */
import * as chatgptWeb from './chatgpt-web-client.js';
import { processLlmOutput } from './router.js';
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

run_command: use script for PowerShell. MUST use \\n between statements. SendKeys does NOT support Chinese - use Set-Content + Start-Process.
<tool>
name: run_command
args: {"script":"Set-Content -Path \"$env:USERPROFILE\\Desktop\\你好.txt\" -Value '你好' -Encoding UTF8\\nStart-Process notepad \"$env:USERPROFILE\\Desktop\\你好.txt\""}
</tool>
Rule: script must have \\n (backslash-n) between lines. Example: "line1\\nline2\\nline3". For Chinese: Set-Content then Start-Process.

Tools: open_notepad, open_cmd, list_dir, read_file, sys_info, run_command(args.script or args.command)
Output only the tool block. No explanation.`;

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

export default {
  async chat(userInput) {
    const config = loadConfig();
    const cdp = config?.llm?.cdp ?? {};
    const text = TOOL_PROMPT + '\n\nUser: ' + userInput;

    const result = await chatgptWeb.chat(text, {
      cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
      chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
      pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
      pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
    });

    const llm_output = result?.text || '';
    const out = await processLlmOutput(llm_output);

    if (out.error && out.error === 'no tool') {
      return { ok: false, error: out.error, raw: llm_output };
    }
    return out;
  },
};
