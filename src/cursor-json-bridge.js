/**
 * cursor-json-bridge.js
 *
 * Escaping layer: parse the plain-text reply from ChatGPT Web and convert
 * any embedded JSON tool-call block into an OpenAI-compatible tool_calls array
 * that Cursor can execute natively.
 *
 * Exported API
 * ─────────────
 *   parseCursorToolCalls(text, toolsSchema?)
 *     → { content: string|null, tool_calls: Array|null }
 *
 *   buildCursorPrompt(messages, tools)
 *     → string   (full prompt to send to ChatGPT Web)
 */

// ── JSON extraction helpers (mirrors agent/json_parser.py strategies) ────────

function _clean(raw) {
  return (raw || '').trim().replace(/^\uFEFF|\u200B|\u200C|\u200D|\uFFFE/g, '');
}

function _fixJson(raw) {
  let s = _clean(raw);
  s = s.replace(/(?<!["\w])\/\/[^\n]*/g, '');          // strip // comments
  s = s.replace(/\/\*[\s\S]*?\*\//g, '');               // strip /* */ comments
  s = s.replace(/,\s*([}\]])/g, '$1');                  // trailing commas
  s = s.replace(/(?<!\\)'([^']*)'(?=\s*:)/g, '"$1"');  // single-quote keys
  s = s.replace(/:\s*'([^']*)'/g, ': "$1"');            // single-quote values
  s = s.replace(/\\(?!["\\/bfnrtu])/g, '\\\\');         // bare backslashes
  return s;
}

function _tryParse(raw) {
  if (!raw) return null;
  for (const attempt of [raw, _fixJson(raw)]) {
    try {
      const obj = JSON.parse(attempt);
      if (obj && typeof obj === 'object') return obj;
    } catch (_) {}
  }
  return null;
}

/** Extract the first balanced { … } block starting at offset in text. */
function _extractBraceBlock(text, start = 0) {
  const i = text.indexOf('{', start);
  if (i === -1) return null;
  let depth = 0, inStr = false, escape = false, quote = null, j = i;
  while (j < text.length) {
    const c = text[j];
    if (escape) { escape = false; j++; continue; }
    if (inStr) {
      if (c === '\\') escape = true;
      else if (c === quote) inStr = false;
      j++; continue;
    }
    if (c === '"' || c === "'") { inStr = true; quote = c; j++; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return text.slice(i, j + 1); }
    j++;
  }
  return null;
}

/** Strategy 1 – ```json ... ``` */
function _s1FencedJson(text) {
  const results = [];
  for (const m of text.matchAll(/```\s*json\s*([\s\S]*?)```/gi)) {
    const block = _extractBraceBlock(m[1]);
    if (block) results.push(block);
  }
  return results;
}

/** Strategy 2 – ``` ... ``` (no language tag) */
function _s2FencedAny(text) {
  const results = [];
  for (const m of text.matchAll(/```\s*([\s\S]*?)```/g)) {
    const inner = m[1].trim();
    if (/^\s*json\b/i.test(inner)) continue; // handled by s1
    const block = _extractBraceBlock(inner);
    if (block) results.push(block);
  }
  return results;
}

/** Strategy 3 – bare label line "JSON\n{...}" */
function _s3BareLabel(text) {
  const results = [];
  for (const m of text.matchAll(/(?:^|\n)\s*(?:JSON|json|【JSON】|\[JSON\])[^\n]*\n\s*/g)) {
    const block = _extractBraceBlock(text, m.index + m[0].length);
    if (block) results.push(block);
  }
  return results;
}

/** Strategy 4 – ALL brace pairs in full text (not just the first) */
function _s4AllBraces(text) {
  const results = [];
  let offset = 0;
  while (offset < text.length) {
    const block = _extractBraceBlock(text, offset);
    if (!block) break;
    results.push(block);
    // advance past the end of this block
    const blockStart = text.indexOf('{', offset);
    offset = blockStart + block.length;
  }
  return results;
}

// Strategies S1–S3 search within fences/labels (high confidence).
// S4 scans the full text (low confidence, used only as last resort).
const _FENCED_STRATEGIES = [_s1FencedJson, _s2FencedAny, _s3BareLabel];

/** Max number of S4 brace-block candidates to consider (prevents O(n) blowup on large replies). */
const _S4_MAX_CANDIDATES = 20;

/**
 * Determine if a parsed JSON object looks like a tool-call structure
 * (Format A: tool_calls array, or Format B: tool + arguments).
 */
function _isToolCallShaped(obj) {
  if (!obj || typeof obj !== 'object') return false;
  if (Array.isArray(obj.tool_calls) && obj.tool_calls.length > 0) return true;
  if (typeof obj.tool === 'string' && obj.arguments !== undefined) return true;
  return false;
}

/**
 * Collect ALL candidate JSON objects from the reply in one pass.
 *
 * Returns a sorted candidate list:
 *   { obj, priority, fromLoose }
 *
 * Priority tiers (lower = higher priority):
 *   1 — from fenced ```json block (S1)
 *   2 — from fenced ``` block (S2) or bare-label (S3)
 *   3 — from full-text brace scan (S4), Format A/B shaped
 *   4 — from full-text brace scan (S4), loose-inference candidates
 *
 * Within the same tier, candidates keep source order (first occurrence wins).
 * Duplicate raw strings are skipped.
 */
function _collectCandidates(text) {
  const seen = new Set();
  const candidates = [];

  function add(raw, priority) {
    if (!raw || seen.has(raw)) return;
    seen.add(raw);
    const obj = _tryParse(raw);
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
    const shaped = _isToolCallShaped(obj);
    candidates.push({ obj, priority, fromLoose: false, shaped });
  }

  // S1: ```json blocks  (priority 1)
  for (const raw of _s1FencedJson(text)) add(raw, 1);

  // S2: plain fenced blocks  (priority 2)
  for (const raw of _s2FencedAny(text)) add(raw, 2);

  // S3: bare-label blocks  (priority 2)
  for (const raw of _s3BareLabel(text)) add(raw, 2);

  // S4: full-text brace scan  (priority 3 if tool-call shaped, 4 if loose)
  const s4raw = _s4AllBraces(text).slice(0, _S4_MAX_CANDIDATES);
  for (const raw of s4raw) {
    if (seen.has(raw)) continue;
    seen.add(raw);
    const obj = _tryParse(raw);
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) continue;
    const shaped = _isToolCallShaped(obj);
    candidates.push({ obj, priority: shaped ? 3 : 4, fromLoose: !shaped, shaped });
  }

  // Sort by priority (stable within same priority = source order preserved)
  candidates.sort((a, b) => a.priority - b.priority);
  return candidates;
}

// ── Tool-call structure detection ─────────────────────────────────────────────

/**
 * Model-name → Cursor tool-name normalisation table.
 * ChatGPT may output aliases (Read, readFile, grep, …); map them to the exact
 * names Cursor declares in body.tools so the agent can execute them.
 */
const _TOOL_NAME_MAP = {
  // read_file aliases
  'read':          'read_file',
  'readfile':      'read_file',
  'read_file':     'read_file',
  'file_read':     'read_file',
  'open_file':     'read_file',
  'view_file':     'read_file',
  // codebase_search aliases
  'search':        'codebase_search',
  'grep':          'codebase_search',
  'find':          'codebase_search',
  'codebase_search': 'codebase_search',
  'search_code':   'codebase_search',
  'grep_search':   'grep_search',
  // list_dir aliases
  'ls':            'list_dir',
  'list':          'list_dir',
  'list_dir':      'list_dir',
  'list_directory': 'list_dir',
  // run_terminal aliases
  'run':           'run_terminal',
  'exec':          'run_terminal',
  'execute':       'run_terminal',
  'run_terminal':  'run_terminal',
  'run_command':   'run_terminal',
  'bash':          'run_terminal',
  'shell':         'run_terminal',
  // edit_file aliases
  'edit':          'edit_file',
  'edit_file':     'edit_file',
  'write_file':    'edit_file',
  'write':         'edit_file',
};

/**
 * Normalise a model-produced tool name to the canonical name in allowedNames.
 *
 * Priority:
 *   1. Exact match in allowedNames (e.g. model said "Read", Cursor has "Read")
 *   2. Case-insensitive exact match in allowedNames (e.g. model said "read", Cursor has "Read")
 *   3. Static _TOOL_NAME_MAP lookup, then verify the mapped name is in allowedNames
 *   4. Fuzzy substring match in allowedNames
 *   5. Return original name unchanged (will be filtered out by caller if not allowed)
 */
function _normaliseName(modelName, allowedNames) {
  const raw = (modelName || '').trim();
  const key = raw.toLowerCase();

  // 1. Exact match (fast path)
  if (allowedNames?.has(raw)) return raw;

  // 2. Case-insensitive exact match — handles "read" → "Read", "shell" → "Shell"
  if (allowedNames) {
    for (const allowed of allowedNames) {
      if (allowed.toLowerCase() === key) return allowed;
    }
  }

  // 3. Static map lookup → but only accept if the mapped value is in allowedNames
  const mapped = _TOOL_NAME_MAP[key];
  if (mapped) {
    if (!allowedNames || allowedNames.has(mapped)) return mapped;
    // mapped name not in allowedNames — still try case-insensitive match for the mapped name
    if (allowedNames) {
      for (const allowed of allowedNames) {
        if (allowed.toLowerCase() === mapped.toLowerCase()) return allowed;
      }
    }
  }

  // 4. Fuzzy substring match in allowedNames
  if (allowedNames) {
    for (const allowed of allowedNames) {
      if (allowed.toLowerCase().includes(key) || key.includes(allowed.toLowerCase())) {
        return allowed;
      }
    }
  }

  return raw; // unchanged; caller will filter if not in allowedNames
}

/**
 * Normalise a parsed JSON object into a list of tool calls.
 * Supports:
 *   Format A: { "tool_calls": [ { name, arguments, id? }, ... ] }
 *   Format B: { "tool": "name", "arguments": { ... } }
 */
function _normaliseToCalls(obj) {
  // Format A
  if (Array.isArray(obj.tool_calls)) {
    return obj.tool_calls
      .filter(tc => tc && typeof tc.name === 'string')
      .map(tc => ({
        id:   tc.id || null,
        name: tc.name,
        args: tc.arguments ?? {},
      }));
  }
  // Format B
  if (typeof obj.tool === 'string') {
    return [{ id: null, name: obj.tool, args: obj.arguments ?? {} }];
  }
  return [];
}

/**
 * Loose inference: when a JSON object has no recognised tool-call structure,
 * try to infer intent from its fields:
 *   - has "path" (and no "command") → read_file
 *   - has "query" or "search" → codebase_search
 *
 * @param {Object}  obj          - parsed JSON object with no tool_calls / tool field
 * @param {Set|null} allowedNames - set of Cursor tool names for this request
 * @returns {{ id, name, args }[]}  - inferred calls, or []
 */
// JSON-schema type names that should NOT be treated as file paths
const _SCHEMA_TYPE_VALUES = new Set(['string', 'number', 'integer', 'boolean', 'object', 'array', 'null']);

function _isLikelyFilePath(v) {
  if (typeof v !== 'string' || !v.trim()) return false;
  if (_SCHEMA_TYPE_VALUES.has(v.trim().toLowerCase())) return false;
  // reject schema-like descriptions (contains spaces and no path separator)
  if (v.includes(' ') && !v.includes('/') && !v.includes('\\')) return false;
  return true;
}

function _inferLooseCalls(obj, allowedNames) {
  const keys = Object.keys(obj);
  const has = k => keys.includes(k);

  // {path} or {path, ...} without command → read_file
  // Return inferred call; _buildToolCalls will resolve read_file → Cursor name (e.g. "Read") via _normaliseName
  if (has('path') && !has('command') && !has('cmd') && _isLikelyFilePath(obj.path)) {
    const name = 'read_file';
    console.log(`[cursor-bridge] loose inference: {path} → read_file (path=${obj.path})`);
    return [{ id: null, name, args: { path: obj.path } }];
  }

  // {query} or {search} with non-empty string value → codebase_search
  if (has('query') || has('search')) {
    const val = obj.query ?? obj.search;
    if (typeof val === 'string' && val.trim() && !_SCHEMA_TYPE_VALUES.has(val.trim())) {
      const name = 'codebase_search';
      console.log(`[cursor-bridge] loose inference: {query/search} → codebase_search (val=${val})`);
      return [{ id: null, name, args: { query: val } }];
    }
  }

  return [];
}

/** Build the final OpenAI tool_calls array with unique ids. */
function _buildToolCalls(calls, allowedNames) {
  const ts = Date.now();
  const result = [];
  for (let i = 0; i < calls.length; i++) {
    const { id, args } = calls[i];
    // Normalise name first, then validate
    const name = _normaliseName(calls[i].name, allowedNames);
    if (allowedNames && !allowedNames.has(name)) {
      console.warn(`[cursor-bridge] dropping unknown tool "${calls[i].name}" (normalised: "${name}")`);
      continue;
    }
    const arguments_ = typeof args === 'string' ? args : JSON.stringify(args);
    result.push({
      id:   id || `call_${ts}_${i}`,
      type: 'function',
      function: { name, arguments: arguments_ },
    });
  }
  return result;
}

// ── Main export ───────────────────────────────────────────────────────────────

/**
 * Parse ChatGPT's plain-text reply and return either tool_calls or plain content.
 *
 * @param {string} text          - Raw reply text from ChatGPT Web
 * @param {Array}  [toolsSchema] - tools array from the Cursor request (for name validation)
 * @returns {{ content: string|null, tool_calls: Array|null, hadJson: boolean }}
 *   hadJson: true if any JSON object was parsed from the reply (even loosely).
 *            Used by api-server.js to decide whether to retry with enforcement prompt.
 */
export function parseCursorToolCalls(text, toolsSchema) {
  if (!text || typeof text !== 'string') return { content: text || '', tool_calls: null, hadJson: false, loosePath: null };

  const allowedNames = toolsSchema?.length
    ? new Set(toolsSchema.map(t => t.function?.name || t.name).filter(Boolean))
    : null;

  const candidates = _collectCandidates(text);

  // hadJson: true if at least one candidate came from explicit structure (priority < 4)
  const hadJson = candidates.some(c => !c.fromLoose);

  // loosePath: a file path hinted via bare {path:...} but not yet wrapped in tool_calls
  let loosePath = null;

  for (const { obj, fromLoose } of candidates) {
    // 1. Strict: Format A / Format B
    let calls = _normaliseToCalls(obj);

    // 2. Loose inference — only for S4 candidates that aren't tool-call shaped
    if (!calls.length && fromLoose) {
      calls = _inferLooseCalls(obj, allowedNames);
      if (!calls.length && obj.path && _isLikelyFilePath(obj.path)) {
        loosePath = loosePath ?? obj.path;
      }
    }

    if (!calls.length) continue;

    const toolCalls = _buildToolCalls(calls, allowedNames);
    if (!toolCalls.length) {
      console.warn(`[cursor-bridge] tool_calls found but names not in allowedNames:`,
        calls.map(c => c.name).join(', '),
        '| allowed:', allowedNames ? [...allowedNames].join(', ') : 'any'
      );
      continue;
    }

    // Strip all JSON/code fences from content
    const stripped = text
      .replace(/```\s*json[\s\S]*?```/gi, '')
      .replace(/```[\s\S]*?```/g, '')
      .trim();

    console.log('[cursor-bridge] returning tool_calls:',
      toolCalls.map(tc => `${tc.function.name}(${tc.function.arguments.slice(0, 80)})`).join(' | ')
    );

    return {
      content:    stripped || null,
      tool_calls: toolCalls,
      hadJson:    true,
      loosePath:  null,
    };
  }

  // No valid tool calls found — return plain content
  return { content: text, tool_calls: null, hadJson, loosePath };
}

/**
 * Build the prompt string to send to ChatGPT Web for a Cursor request.
 *
 * Includes:
 *   1. Full conversation context (system / user / assistant / tool results)
 *   2. Available tools injected as a readable list
 *   3. Instruction for the model to output tool calls as ```json blocks
 *
 * @param {Array}  messages   - Full OpenAI messages array from Cursor
 * @param {Array}  [tools]    - tools array from the Cursor request
 * @param {Function} flattenFn - flattenMessagesForChatGPT from api-server
 * @returns {string}
 */
export function buildCursorPrompt(messages, tools, flattenFn) {
  const conversationContext = flattenFn(messages);

  if (!tools?.length) {
    return conversationContext;
  }

  const toolNames = tools.map(t => (t.function ?? t).name).filter(Boolean);
  const toolNamesLower = new Map(toolNames.map(n => [n.toLowerCase(), n]));

  const toolLines = tools.map(t => {
    const fn = t.function ?? t;
    const params = fn.parameters?.properties
      ? Object.entries(fn.parameters.properties)
          .map(([k, v]) => `    ${k}: ${v.description || v.type || ''}`)
          .join('\n')
      : '';
    return `- ${fn.name}: ${fn.description || ''}${params ? '\n' + params : ''}`;
  });

  // Pick the best example tool — find "read file" equivalent by case-insensitive lookup
  const exampleName =
    toolNamesLower.get('read_file') ??
    toolNamesLower.get('read') ??
    toolNamesLower.get('codebase_search') ??
    toolNamesLower.get('search') ??
    toolNames[0] ?? 'Read';

  // Determine example args based on what kind of tool it is
  const exNameLow = exampleName.toLowerCase();
  const exampleArgs =
    exNameLow === 'read_file' || exNameLow === 'read' || exNameLow === 'view_file'
      ? `"path": "README.md"`
      : exNameLow === 'codebase_search' || exNameLow === 'grep_search' || exNameLow === 'semanticsearch'
        ? `"query": "main entry point"`
        : exNameLow === 'list_dir' || exNameLow === 'glob'
          ? `"path": "."`
          : exNameLow === 'run_terminal' || exNameLow === 'shell'
            ? `"command": "ls"`
            : `"path": "README.md"`;

  // ── Framing block (placed BEFORE conversation so ChatGPT reads it first) ──
  const framing = [
    'You are a coding assistant that responds in structured JSON when the user needs information from their codebase.',
    'You do NOT execute commands yourself. Instead, you respond with a JSON data structure that describes what information is needed.',
    'Another system reads your JSON response and retrieves the information for you.',
    '',
    '## Response format',
    'When you need to look up information, respond with ONLY a ```json code block in this exact shape:',
    '```json',
    `{"tool_calls":[{"name":"${exampleName}","arguments":{${exampleArgs}}}]}`,
    '```',
    'For multiple lookups at once:',
    '```json',
    '{"tool_calls":[{"name":"<action1>","arguments":{...}},{"name":"<action2>","arguments":{...}}]}',
    '```',
    '',
    '## Available actions (use EXACT names, no aliases)',
    toolNames.map(n => `  ${n}`).join('\n'),
    '',
    toolLines.join('\n'),
    '',
    '## Rules',
    '1. Paths must be workspace-relative (e.g. `README.md`, `src/index.js`) or absolute. No `skills://` or `file://` URIs.',
    '2. Your response must be ONLY the ```json block — no explanation, no prose, no extra text.',
    '3. Do NOT say "I cannot read files" or "I don\'t have access". You retrieve information by outputting the JSON structure above.',
    '4. If you have enough information to answer without a lookup, respond in plain text (no JSON).',
    '',
    `## Important: to read any file (e.g. README.md), respond with:`,
    '```json',
    `{"tool_calls":[{"name":"read_file","arguments":{"path":"<filename>"}}]}`,
    '```',
    '─────────────────────────────────────────────────────',
    '',
  ].join('\n');

  return framing + conversationContext;
}

/**
 * Build a retry/enforcement prompt when the first reply contained no tool_calls.
 *
 * @param {string}      firstReply  - The plain-text reply that had no tool_calls
 * @param {Array}       tools       - tools array from Cursor request
 * @param {string|null} loosePath   - If non-null, the model already hinted a file path
 *                                    (e.g. "README.md") but didn't wrap it in tool_calls.
 *                                    Pass this to give a more precise correction.
 * @returns {string}
 */
export function buildEnforceJsonPrompt(firstReply, tools, loosePath = null) {
  const toolNames = (tools || [])
    .map(t => t.function?.name || t.name)
    .filter(Boolean);

  const targetName = loosePath ? 'read_file' : (toolNames[0] ?? 'read_file');
  const targetArgs = loosePath
    ? `"path":"${loosePath}"`
    : targetName === 'read_file' || targetName === 'view_file'
      ? `"path":"README.md"`
      : targetName === 'codebase_search' || targetName === 'grep_search'
        ? `"query":"README or project documentation"`
        : `"path":"README.md"`;

  const example = `{"tool_calls":[{"name":"${targetName}","arguments":{${targetArgs}}}]}`;

  const situation = loosePath
    ? `Your previous response mentioned the path "${loosePath}" but did not wrap it in the required JSON structure.`
    : `Your previous response was plain text:\n"${firstReply.slice(0, 300)}${firstReply.length > 300 ? '…' : ''}"`;

  return [
    situation,
    '',
    'The system that reads your responses needs a JSON data structure, not plain text.',
    'Please respond with ONLY the following ```json block (adjust path/query as appropriate):',
    '```json',
    example,
    '```',
    '',
    `Available actions: ${toolNames.join(', ') || '(see above)'}`,
    'Your entire response must be just the ```json block above — nothing else.',
  ].join('\n');
}
