"""
json_parser.py — AI response JSON instruction parser

Supports multiple formats with fallback strategies:
  策略1  标准代码块   ```json { } ```
  策略2  无语言标记   ``` { } ```
  策略3  裸标记行     JSON\n{ }  /  json\n{ }  /  【JSON】\n{ }
  策略4  首个大括号   直接在文本里找第一个合法 JSON 对象
  策略5  宽松大括号   花括号不完整时补全后尝试
  策略6  多块提取     文本中存在多个 JSON 对象时全部提取

每个策略提取到候选字符串后，统一送入 _try_parse() 做：
  - BOM / 零宽字符清理
  - 单引号 → 双引号
  - 尾逗号修复
  - 注释删除
  - 反斜杠修复
"""

import json
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SUPPORTED_COMMANDS = {"powershell", "cmd", "python", "python3", "file_op", "request_help"}
_WINDOWS_PATH_JSON_FIELDS = ("path", "dst", "src", "file_path", "target_file")


# ──────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────

@dataclass
class ParseResult:
    blocks: list = field(default_factory=list)       # 成功解析的指令块列表
    strategy: str = ""                               # 命中的策略名
    raw_candidates: list = field(default_factory=list)  # 调试用原始候选串
    warnings: list = field(default_factory=list)     # 非致命警告


# ──────────────────────────────────────────────────────────
# 单字符串修复 + 解析
# ──────────────────────────────────────────────────────────

def _clean(raw: str) -> str:
    """基础清理：BOM、零宽字符、首尾空白"""
    return raw.strip().strip("\ufeff\u200b\u200c\u200d\ufffe")


def _fix_json(raw: str) -> str:
    """一系列宽松修复，不改变语义"""
    s = _clean(raw)
    fields = "|".join(re.escape(name) for name in _WINDOWS_PATH_JSON_FIELDS)
    path_pattern = re.compile(
        rf'("(?P<key>{fields})"\s*:\s*")(?P<value>(?:[^"\\]|\\.)*)(")',
        re.IGNORECASE,
    )

    def _escape_path_value(match):
        return match.group(0)

    s = path_pattern.sub(_escape_path_value, s)

    # 删除单行 // 注释（不在字符串内的）
    s = re.sub(r'(?<!["\w])//[^\n]*', '', s)
    # 删除多行 /* */ 注释
    s = re.sub(r'/\*[\s\S]*?\*/', '', s)
    # 尾逗号（对象或数组末尾的多余逗号）
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # 单引号键值 → 双引号（简单情况）
    s = re.sub(r"(?<![\\])'([^']*)'(?=\s*:)", r'"\1"', s)
    s = re.sub(r":\s*'([^']*)'", r': "\1"', s)
    # 修复裸反斜杠（Windows 路径）：\ 后不是 "\/bfnrtu 就加转义
    s = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)

    fields = "|".join(re.escape(name) for name in _WINDOWS_PATH_JSON_FIELDS)
    path_pattern = re.compile(
        rf'("(?P<key>{fields})"\s*:\s*")(?P<value>(?:[^"\\]|\\.)*)(")',
        re.IGNORECASE,
    )

    def _normalize_path_value(match):
        value = match.group("value")
        value = re.sub(r'(?<!\\)\\(?!\\)', r'\\\\', value)
        value = re.sub(r'\\\\{3,}', r'\\\\', value)
        return f'{match.group(1)}{value}{match.group(4)}'

    s = path_pattern.sub(_normalize_path_value, s)

    return s


def _try_parse(raw: str) -> dict | None:
    """尝试解析一段字符串为含 command 字段的 dict，失败返回 None"""
    if not raw:
        return None
    for attempt in (raw, _fix_json(raw)):
        try:
            obj = json.loads(attempt)
            if isinstance(obj, dict) and "command" in obj:
                cmd = str(obj["command"]).lower().strip()
                if cmd == "file_op" and "action" not in obj:
                    logger.debug("file_op 缺少 action 字段")
                    return None
                if cmd not in SUPPORTED_COMMANDS:
                    logger.debug("跳过不支持的 command: %s", cmd)
                    return None
                return obj
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _extract_brace_block(text: str, start: int = 0) -> str | None:
    """从 start 位置起，用大括号配对提取完整 JSON 对象"""
    i = text.find("{", start)
    if i == -1:
        return None
    depth, j = 0, i
    in_str = False
    escape = False
    quote = None
    while j < len(text):
        c = text[j]
        if escape:
            escape = False
            j += 1
            continue
        if in_str:
            if c == "\\":
                escape = True
            elif c == quote:
                in_str = False
            j += 1
            continue
        if c in '"\'':
            in_str = True
            quote = c
            j += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i: j + 1]
        j += 1
    return None


# ──────────────────────────────────────────────────────────
# 各策略提取函数
# ──────────────────────────────────────────────────────────

def _s1_fenced_json(text: str) -> list:
    """策略1: ```json ... ``` 标准代码块（用大括号配对，支持嵌套）"""
    results = []
    for m in re.finditer(r"```\s*json\s*([\s\S]*?)\s*```", text, re.IGNORECASE):
        block = _extract_brace_block(m.group(1))
        if block:
            results.append(block)
    return results


def _s2_fenced_any(text: str) -> list:
    """策略2: ``` ... ``` 无语言标记代码块"""
    results = []
    for m in re.finditer(r"```\s*([\s\S]*?)\s*```", text):
        inner = m.group(1).strip()
        if re.match(r'^\s*json\b', inner, re.IGNORECASE):
            continue  # 已由策略1处理
        block = _extract_brace_block(inner)
        if block:
            results.append(block)
    return results


def _s3_bare_label(text: str) -> list:
    """策略3: 裸标记行 —— JSON / json / 【JSON】/ [JSON] 后紧跟 { }"""
    results = []
    for m in re.finditer(r"(?:^|\n)\s*(?:JSON|json|【JSON】|\[JSON\])[^\n]*\n\s*", text):
        block = _extract_brace_block(text, m.end())
        if block:
            results.append(block)
    return results


def _s4_first_brace(text: str) -> list:
    """策略4: 在文本中找第一个大括号配对的 JSON 对象"""
    block = _extract_brace_block(text)
    return [block] if block else []


def _s5_loose_brace(text: str) -> list:
    """策略5: 宽松提取——找所有 { ... } 区段（允许不完整，补全后尝试）"""
    results = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        results.append(m.group())
    for m in re.finditer(r"\{[\s\S]{10,}\}", text):
        candidate = m.group()
        if candidate not in results:
            results.append(candidate)
    return results


def _s6_multi_block(text: str) -> list:
    """策略6: 提取文本中所有独立 JSON 对象（多指令场景）"""
    results = []
    pos = 0
    while pos < len(text):
        block = _extract_brace_block(text, pos)
        if not block:
            break
        results.append(block)
        pos = text.find(block, pos) + len(block)
    return results


def _s7_inline_command_object(text: str) -> list:
    """策略7: 在自然语言中围绕 "command" 锚点回溯提取最近的 JSON 对象"""
    results = []
    seen: set[str] = set()
    for match in re.finditer(r'"command"\s*:', text, re.IGNORECASE):
        brace_start = text.rfind("{", 0, match.start())
        if brace_start == -1:
            continue
        block = _extract_brace_block(text, brace_start)
        if not block:
            continue
        if block in seen:
            continue
        seen.add(block)
        results.append(block)
    return results


# ──────────────────────────────────────────────────────────
# 主解析入口
# ──────────────────────────────────────────────────────────

_STRATEGIES = [
    ("标准代码块 ```json```", _s1_fenced_json),
    ("无标记代码块 ```...```", _s2_fenced_any),
    ("裸标记行 JSON\\n{...}", _s3_bare_label),
    ("自然语言中的内联 JSON 对象", _s7_inline_command_object),
    ("首个大括号", _s4_first_brace),
    ("宽松花括号扫描", _s5_loose_brace),
    ("全文多块提取", _s6_multi_block),
]


def parse(text: str, debug: bool = False) -> ParseResult:
    """
    从 AI 响应文本中提取所有可执行 JSON 指令块。

    参数
    ----
    text  : AI 返回的原始文本
    debug : True 时将候选串写入 ParseResult.raw_candidates

    返回
    ----
    ParseResult
        .blocks    — 成功解析的指令 dict 列表（可直接传给 executor）
        .strategy  — 命中的策略名称
        .warnings  — 非致命提示
    """
    result = ParseResult()
    seen_ids: set = set()

    for idx, (strategy_name, extractor) in enumerate(_STRATEGIES):
        candidates = extractor(text)
        if not candidates:
            continue

        if debug:
            result.raw_candidates.extend(candidates)

        parsed_this_round = []
        for raw in candidates:
            obj = _try_parse(raw)
            if obj is None:
                continue
            uid = json.dumps(obj, sort_keys=True, ensure_ascii=False)
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            parsed_this_round.append(obj)

        if parsed_this_round:
            result.blocks.extend(parsed_this_round)
            if not result.strategy:
                result.strategy = strategy_name
            logger.debug("[%s] 提取到 %d 个块", strategy_name, len(parsed_this_round))
            if idx < 3:
                break

    if not result.blocks:
        result.warnings.append("No strategy found a valid JSON instruction block")
        logger.warning("parse() extracted no blocks, raw text len=%d", len(text))
    else:
        logger.info("Parsed [%s], %d block(s)", result.strategy, len(result.blocks))

    return result


# ──────────────────────────────────────────────────────────
# 便捷函数（兼容旧接口）
# ──────────────────────────────────────────────────────────

def extract_json_blocks(text: str) -> list:
    """
    兼容旧版 executor.extract_json_blocks() 的直接替换。
    返回解析到的指令 dict 列表，未找到时返回空列表。
    """
    return parse(text).blocks


if __name__ == "__main__":
    print("Paste AI response (type END to finish):")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    text = "\n".join(lines)

    result = parse(text, debug=True)
    print(f"\nStrategy: {result.strategy or 'none'}")
    print(f"Blocks: {len(result.blocks)}")
    if result.warnings:
        print(f"Warnings: {result.warnings}")
    for i, b in enumerate(result.blocks, 1):
        print(f"\n── 块 {i} ──")
        print(json.dumps(b, ensure_ascii=False, indent=2))
