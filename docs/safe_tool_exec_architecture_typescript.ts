# Safe Tool Exec Architecture (Node.js + TypeScript)

下面是一套可直接落地的多文件项目骨架，适合你这种：

- LLM 输出 XML / JSON 工具调用
- 后端解析后执行本地工具
- 强制经过策略校验、审批、审计

---

## package.json

```json
{
  "name": "safe-tool-exec-architecture",
  "version": "1.0.0",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "dev": "tsx watch src/index.ts",
    "start": "node dist/index.js",
    "build": "tsc -p tsconfig.json",
    "check": "tsc --noEmit -p tsconfig.json"
  },
  "dependencies": {
    "express": "^4.21.2",
    "zod": "^3.23.8"
  },
  "devDependencies": {
    "@types/express": "^5.0.0",
    "@types/node": "^22.10.2",
    "tsx": "^4.19.2",
    "typescript": "^5.7.2"
  }
}
```

---

## tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "CommonJS",
    "moduleResolution": "Node",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "forceConsistentCasingInFileNames": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "types": ["node"]
  },
  "include": ["src/**/*.ts"]
}
```

---

## .gitignore

```gitignore
node_modules
dist
logs
.env
```

---

## src/types.ts

```ts
export type RiskLevel = 'low' | 'medium' | 'high';

export type ToolName =
  | 'list_dir'
  | 'read_file'
  | 'write_file'
  | 'open_app'
  | 'run_command_safe';

export type ToolCall = {
  tool: ToolName;
  args: Record<string, unknown>;
  raw?: string;
};

export type ToolResult = {
  ok: boolean;
  tool: ToolName;
  result?: unknown;
  error?: string;
  meta?: Record<string, unknown>;
};

export type PolicyDecision = {
  allowed: boolean;
  requiresApproval: boolean;
  reason: string;
  normalizedCall?: ToolCall;
};

export type ExecutionContext = {
  sessionId: string;
  userId: string;
  allowedRoots: string[];
  appMap: Record<string, string>;
  commandWhitelist: string[];
  audit: {
    log(event: Record<string, unknown>): Promise<void>;
  };
};
```

---

## src/config.ts

```ts
import { ExecutionContext } from './types';
import { AuditLogger } from './audit/logger';

export function createExecutionContext(
  sessionId = 'local-session',
  userId = 'local-user'
): ExecutionContext {
  return {
    sessionId,
    userId,
    allowedRoots: [
      'D:/Workspace',
      'D:/Projects',
      'C:/Users/admin/Desktop/AI_Sandbox'
    ],
    appMap: {
      notepad: 'C:/Windows/System32/notepad.exe',
      unity_hub: 'C:/Program Files/Unity Hub/Unity Hub.exe'
    },
    commandWhitelist: [
      'dir',
      'type',
      'echo',
      'ipconfig',
      'where',
      'tasklist'
    ],
    audit: new AuditLogger('./logs/audit.log')
  };
}
```

---

## src/llm/xmlParser.ts

```ts
import { ToolCall, ToolName } from '../types';

export function parseFunctionCalls(xml: string): ToolCall[] {
  const calls: ToolCall[] = [];
  const invokeRegex = /<invoke\s+name="([^"]+)">([\s\S]*?)<\/invoke>/g;
  let invokeMatch: RegExpExecArray | null;

  while ((invokeMatch = invokeRegex.exec(xml)) !== null) {
    const tool = invokeMatch[1] as ToolName;
    const body = invokeMatch[2];
    const args: Record<string, unknown> = {};

    const paramRegex = /<parameter\s+name="([^"]+)">([\s\S]*?)<\/parameter>/g;
    let paramMatch: RegExpExecArray | null;

    while ((paramMatch = paramRegex.exec(body)) !== null) {
      args[paramMatch[1]] = decodeXml(paramMatch[2].trim());
    }

    calls.push({ tool, args, raw: invokeMatch[0] });
  }

  return calls;
}

function decodeXml(input: string): string {
  return input
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}
```

---

## src/security/pathPolicy.ts

```ts
import path from 'node:path';

export function normalizeAndCheckPath(inputPath: string, allowedRoots: string[]): string {
  const resolved = path.resolve(inputPath);
  const normalized = path.normalize(resolved);

  const ok = allowedRoots.some((root) => {
    const safeRoot = path.normalize(path.resolve(root));
    return normalized.toLowerCase().startsWith(safeRoot.toLowerCase());
  });

  if (!ok) {
    throw new Error(`Path not allowed: ${normalized}`);
  }

  return normalized;
}
```

---

## src/security/commandPolicy.ts

```ts
const DISALLOWED_TOKENS = [
  'powershell -enc',
  'curl ',
  'wget ',
  'bitsadmin',
  'certutil',
  'del ',
  'erase ',
  'rd ',
  'rmdir ',
  'format ',
  'shutdown ',
  'reg add',
  'reg delete',
  'taskkill ',
  'sc delete',
  '&&',
  '||',
  '|',
  '>',
  '<',
  ';'
];

export function checkCommandAllowed(command: string, whitelist: string[]): void {
  const lowered = command.toLowerCase().trim();

  for (const token of DISALLOWED_TOKENS) {
    if (lowered.includes(token)) {
      throw new Error(`Command contains disallowed token: ${token}`);
    }
  }

  const ok = whitelist.some((prefix) => lowered.startsWith(prefix.toLowerCase()));
  if (!ok) {
    throw new Error(`Command not in whitelist: ${command}`);
  }
}
```

---

## src/security/sanitizer.ts

```ts
export function sanitizeOutput(text: string, maxLen = 8000): string {
  const strippedAnsi = text.replace(/\x1B\[[0-9;]*[A-Za-z]/g, '');
  const redacted = strippedAnsi
    .replace(/sk-[A-Za-z0-9_-]{10,}/g, '[REDACTED_API_KEY]')
    .replace(/(Authorization:\s*Bearer\s+)[^\s]+/gi, '$1[REDACTED_TOKEN]');

  if (redacted.length <= maxLen) return redacted;
  return redacted.slice(0, maxLen) + '\n...[truncated]';
}
```

---

## src/security/schema.ts

```ts
import { z } from 'zod';

export const listDirSchema = z.object({
  path: z.string().max(260).optional()
}).strict();

export const readFileSchema = z.object({
  path: z.string().max(260)
}).strict();

export const writeFileSchema = z.object({
  path: z.string().max(260),
  content: z.string().max(20000)
}).strict();

export const openAppSchema = z.object({
  app: z.enum(['notepad', 'unity_hub'])
}).strict();

export const runCommandSafeSchema = z.object({
  command: z.string().max(300)
}).strict();
```

---

## src/audit/logger.ts

```ts
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';

export class AuditLogger {
  constructor(private readonly logPath: string) {}

  async log(event: Record<string, unknown>): Promise<void> {
    await fs.mkdir(path.dirname(this.logPath), { recursive: true });
    const line = JSON.stringify({ ts: new Date().toISOString(), ...event });
    await fs.appendFile(this.logPath, line + os.EOL, 'utf8');
  }
}
```

---

## src/approval/approval.ts

```ts
import { ToolCall } from '../types';

export async function requestApproval(call: ToolCall): Promise<boolean> {
  console.log('Approval required:', JSON.stringify(call, null, 2));

  // 这里先默认 false。
  // 你可以接到：
  // - Electron 弹窗
  // - Web 确认页
  // - CLI y/n
  // - Cursor / MCP 前端确认
  return false;
}
```

---

## src/tools/handlers/listDir.ts

```ts
import fs from 'node:fs/promises';
import { ExecutionContext, ToolResult } from '../../types';
import { normalizeAndCheckPath } from '../../security/pathPolicy';

export async function listDirHandler(
  args: Record<string, unknown>,
  ctx: ExecutionContext
): Promise<ToolResult> {
  const rawPath = String(args.path ?? '.');
  const safePath = normalizeAndCheckPath(rawPath, ctx.allowedRoots);
  const entries = await fs.readdir(safePath, { withFileTypes: true });

  return {
    ok: true,
    tool: 'list_dir',
    result: {
      path: safePath,
      items: entries.map((item) => ({
        name: item.name,
        type: item.isDirectory() ? 'dir' : 'file'
      }))
    }
  };
}
```

---

## src/tools/handlers/readFile.ts

```ts
import fs from 'node:fs/promises';
import { ExecutionContext, ToolResult } from '../../types';
import { normalizeAndCheckPath } from '../../security/pathPolicy';
import { sanitizeOutput } from '../../security/sanitizer';

export async function readFileHandler(
  args: Record<string, unknown>,
  ctx: ExecutionContext
): Promise<ToolResult> {
  const rawPath = String(args.path);
  const safePath = normalizeAndCheckPath(rawPath, ctx.allowedRoots);
  const content = await fs.readFile(safePath, 'utf8');

  return {
    ok: true,
    tool: 'read_file',
    result: {
      path: safePath,
      content: sanitizeOutput(content, 20000)
    }
  };
}
```

---

## src/tools/handlers/writeFile.ts

```ts
import fs from 'node:fs/promises';
import path from 'node:path';
import { ExecutionContext, ToolResult } from '../../types';
import { normalizeAndCheckPath } from '../../security/pathPolicy';

export async function writeFileHandler(
  args: Record<string, unknown>,
  ctx: ExecutionContext
): Promise<ToolResult> {
  const rawPath = String(args.path);
  const content = String(args.content ?? '');
  const safePath = normalizeAndCheckPath(rawPath, ctx.allowedRoots);

  await fs.mkdir(path.dirname(safePath), { recursive: true });
  await fs.writeFile(safePath, content, 'utf8');

  return {
    ok: true,
    tool: 'write_file',
    result: {
      path: safePath,
      bytesWritten: Buffer.byteLength(content, 'utf8')
    }
  };
}
```

---

## src/tools/handlers/openApp.ts

```ts
import { spawn } from 'node:child_process';
import { ExecutionContext, ToolResult } from '../../types';

export async function openAppHandler(
  args: Record<string, unknown>,
  ctx: ExecutionContext
): Promise<ToolResult> {
  const app = String(args.app);
  const exe = ctx.appMap[app];

  if (!exe) {
    return {
      ok: false,
      tool: 'open_app',
      error: `App not allowed: ${app}`
    };
  }

  const child = spawn(exe, [], {
    detached: true,
    windowsHide: true,
    stdio: 'ignore'
  });
  child.unref();

  return {
    ok: true,
    tool: 'open_app',
    result: { app, exe }
  };
}
```

---

## src/tools/handlers/runCommandSafe.ts

```ts
import { spawn } from 'node:child_process';
import { ExecutionContext, ToolResult } from '../../types';
import { checkCommandAllowed } from '../../security/commandPolicy';
import { sanitizeOutput } from '../../security/sanitizer';

export async function runCommandSafeHandler(
  args: Record<string, unknown>,
  ctx: ExecutionContext
): Promise<ToolResult> {
  const command = String(args.command);
  checkCommandAllowed(command, ctx.commandWhitelist);

  const result = await runChild(command, 8000);

  return {
    ok: true,
    tool: 'run_command_safe',
    result: {
      stdout: sanitizeOutput(result.stdout),
      stderr: sanitizeOutput(result.stderr),
      exitCode: result.exitCode
    },
    meta: {
      durationMs: result.durationMs
    }
  };
}

type ChildResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  durationMs: number;
};

function runChild(command: string, timeoutMs: number): Promise<ChildResult> {
  const start = Date.now();

  return new Promise((resolve, reject) => {
    const child = spawn('cmd.exe', ['/d', '/s', '/c', command], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';
    let settled = false;

    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        reject(new Error(`Command timed out after ${timeoutMs}ms`));
      }
    }, timeoutMs);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 100000) stdout = stdout.slice(0, 100000);
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      if (stderr.length > 100000) stderr = stderr.slice(0, 100000);
    });

    child.on('error', (err) => {
      clearTimeout(timer);
      if (!settled) {
        settled = true;
        reject(err);
      }
    });

    child.on('close', (code) => {
      clearTimeout(timer);
      if (!settled) {
        settled = true;
        resolve({
          stdout,
          stderr,
          exitCode: code,
          durationMs: Date.now() - start
        });
      }
    });
  });
}
```

---

## src/tools/registry.ts

```ts
import { z } from 'zod';
import { ToolName, ToolResult, ExecutionContext, RiskLevel } from '../types';
import { listDirHandler } from './handlers/listDir';
import { readFileHandler } from './handlers/readFile';
import { writeFileHandler } from './handlers/writeFile';
import { openAppHandler } from './handlers/openApp';
import { runCommandSafeHandler } from './handlers/runCommandSafe';
import {
  listDirSchema,
  readFileSchema,
  writeFileSchema,
  openAppSchema,
  runCommandSafeSchema
} from '../security/schema';

export type ToolDefinition = {
  name: ToolName;
  risk: RiskLevel;
  requiresApproval: boolean;
  schema: z.ZodTypeAny;
  handler: (args: Record<string, unknown>, ctx: ExecutionContext) => Promise<ToolResult>;
};

export const toolRegistry: Record<ToolName, ToolDefinition> = {
  list_dir: {
    name: 'list_dir',
    risk: 'low',
    requiresApproval: false,
    schema: listDirSchema,
    handler: listDirHandler
  },
  read_file: {
    name: 'read_file',
    risk: 'low',
    requiresApproval: false,
    schema: readFileSchema,
    handler: readFileHandler
  },
  write_file: {
    name: 'write_file',
    risk: 'medium',
    requiresApproval: true,
    schema: writeFileSchema,
    handler: writeFileHandler
  },
  open_app: {
    name: 'open_app',
    risk: 'medium',
    requiresApproval: true,
    schema: openAppSchema,
    handler: openAppHandler
  },
  run_command_safe: {
    name: 'run_command_safe',
    risk: 'high',
    requiresApproval: true,
    schema: runCommandSafeSchema,
    handler: runCommandSafeHandler
  }
};
```

---

## src/security/policyEngine.ts

```ts
import { ZodError } from 'zod';
import { PolicyDecision, ToolCall } from '../types';
import { toolRegistry } from '../tools/registry';

export function evaluatePolicy(call: ToolCall): PolicyDecision {
  const def = toolRegistry[call.tool];
  if (!def) {
    return {
      allowed: false,
      requiresApproval: false,
      reason: `Unknown tool: ${call.tool}`
    };
  }

  try {
    const parsedArgs = def.schema.parse(call.args);
    return {
      allowed: true,
      requiresApproval: def.requiresApproval,
      reason: 'schema_valid',
      normalizedCall: {
        ...call,
        args: parsedArgs
      }
    };
  } catch (error) {
    const reason =
      error instanceof ZodError
        ? error.errors.map((e) => `${e.path.join('.')}: ${e.message}`).join('; ')
        : 'schema_validation_failed';

    return {
      allowed: false,
      requiresApproval: false,
      reason
    };
  }
}
```

---

## src/exec/executeToolCall.ts

```ts
import { requestApproval } from '../approval/approval';
import { toolRegistry } from '../tools/registry';
import { ExecutionContext, ToolCall, ToolResult } from '../types';
import { evaluatePolicy } from '../security/policyEngine';

export async function executeToolCall(
  call: ToolCall,
  ctx: ExecutionContext
): Promise<ToolResult> {
  await ctx.audit.log({
    event: 'tool_call_received',
    sessionId: ctx.sessionId,
    userId: ctx.userId,
    call
  });

  const decision = evaluatePolicy(call);

  await ctx.audit.log({
    event: 'policy_decision',
    sessionId: ctx.sessionId,
    userId: ctx.userId,
    decision,
    call
  });

  if (!decision.allowed || !decision.normalizedCall) {
    return {
      ok: false,
      tool: call.tool,
      error: decision.reason
    };
  }

  if (decision.requiresApproval) {
    const approved = await requestApproval(decision.normalizedCall);

    await ctx.audit.log({
      event: 'approval_result',
      sessionId: ctx.sessionId,
      userId: ctx.userId,
      call,
      approved
    });

    if (!approved) {
      return {
        ok: false,
        tool: call.tool,
        error: 'Execution not approved'
      };
    }
  }

  try {
    const def = toolRegistry[call.tool];
    const result = await def.handler(decision.normalizedCall.args, ctx);

    await ctx.audit.log({
      event: 'tool_call_executed',
      sessionId: ctx.sessionId,
      userId: ctx.userId,
      call,
      result
    });

    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown execution error';

    await ctx.audit.log({
      event: 'tool_call_failed',
      sessionId: ctx.sessionId,
      userId: ctx.userId,
      call,
      error: message
    });

    return {
      ok: false,
      tool: call.tool,
      error: message
    };
  }
}
```

---

## src/server/api.ts

```ts
import express from 'express';
import { createExecutionContext } from '../config';
import { executeToolCall } from '../exec/executeToolCall';
import { parseFunctionCalls } from '../llm/xmlParser';
import { ToolCall } from '../types';

export function createApp() {
  const app = express();
  app.use(express.json({ limit: '1mb' }));

  app.get('/health', (_req, res) => {
    res.json({ ok: true });
  });

  app.post('/tools/invoke', async (req, res) => {
    const { tool, args, sessionId, userId } = req.body as {
      tool: ToolCall['tool'];
      args: Record<string, unknown>;
      sessionId?: string;
      userId?: string;
    };

    const ctx = createExecutionContext(sessionId, userId);
    const result = await executeToolCall({ tool, args }, ctx);
    res.json(result);
  });

  app.post('/llm/xml', async (req, res) => {
    const { xml, sessionId, userId } = req.body as {
      xml: string;
      sessionId?: string;
      userId?: string;
    };

    const ctx = createExecutionContext(sessionId, userId);
    const calls = parseFunctionCalls(xml);
    const results = [];

    for (const call of calls) {
      results.push(await executeToolCall(call, ctx));
    }

    res.json({ ok: true, results });
  });

  return app;
}
```

---

## src/index.ts

```ts
import { createApp } from './server/api';

const PORT = 3000;

async function main() {
  const app = createApp();
  app.listen(PORT, () => {
    console.log(`Safe tool exec server listening on http://127.0.0.1:${PORT}`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

---

## 本地测试 XML 示例

```xml
<function_calls>
  <invoke name="list_dir">
    <parameter name="path">D:/Workspace</parameter>
  </invoke>
</function_calls>
```

---

## 本地测试 JSON 示例

```json
{
  "tool": "read_file",
  "args": {
    "path": "C:/Users/admin/Desktop/AI_Sandbox/test.txt"
  }
}
```

---

## curl 调用示例

### 调用 JSON 工具

```bash
curl -X POST http://127.0.0.1:3000/tools/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "list_dir",
    "args": {"path": "D:/Workspace"}
  }'
```

### 调用 XML 批处理

```bash
curl -X POST http://127.0.0.1:3000/llm/xml \
  -H "Content-Type: application/json" \
  -d '{
    "xml": "<function_calls><invoke name=\"list_dir\"><parameter name=\"path\">D:/Workspace</parameter></invoke></function_calls>"
  }'
```

---

## 启动方式

```bash
npm install
npm run dev
```

构建：

```bash
npm run build
npm start
```

---

## 你接下来最应该改的地方

1. 把 `allowedRoots` 改成你自己的真实白名单目录
2. 把 `requestApproval()` 接成你自己的前端确认逻辑
3. 把 `appMap` 改成你本机允许打开的程序
4. 尽量让模型优先调用专用工具，不要优先走 `run_command_safe`
5. 如果要接你现有的 ZeroChatgpt，只需要把它的 XML 输出扔到 `/llm/xml`

---

## 进一步增强建议

你后面可以继续加：

- Electron 审批弹窗
- SQLite 审计存储
- 用户级权限配置
- 每个工具的 QPS / 次数限制
- 多会话隔离
- Windows Sandbox / 低权限 worker
- 敏感内容自动脱敏
- Tool 输出摘要后再回灌 LLM
