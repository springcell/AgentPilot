"""
skill_manager.py — Agent skill store

After each successful task, save the JSON instruction sequence;
inject into prompt for similar tasks to reduce trial and error.

Skill 文件格式（JSON）：
{
  "name": "launch_unity",
  "description": "启动 Unity 编辑器",
  "patterns": ["unity", "启动unity", "打开unity"],
  "steps": [
    {"command": "file_op", "action": "find_program", "name": "Unity"},
    {"command": "file_op", "action": "launch", "path": "<从上一步结果获取>"}
  ],
  "notes": "Unity 实际路径因机器而异，需先 find_program",
  "success_count": 3,
  "last_used": "2024-01-01 12:00"
}
"""

import os
import json
import re
from datetime import datetime
from pathlib import Path

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _ensure_dir():
    Path(SKILLS_DIR).mkdir(parents=True, exist_ok=True)


def _skill_path(name: str) -> str:
    safe = re.sub(r'[^\w\-]', '_', name)
    return os.path.join(SKILLS_DIR, f"{safe}.json")


def _load_skill(name: str) -> dict | None:
    path = _skill_path(name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_skill(skill: dict) -> None:
    _ensure_dir()
    path = _skill_path(skill["name"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(skill, f, ensure_ascii=False, indent=2)


def list_skills() -> list[dict]:
    """返回所有已保存的 skill 列表（摘要）"""
    _ensure_dir()
    skills = []
    for fname in os.listdir(SKILLS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(SKILLS_DIR, fname), "r", encoding="utf-8") as f:
                s = json.load(f)
            skills.append({
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "patterns": s.get("patterns", []),
                "success_count": s.get("success_count", 0),
                "last_used": s.get("last_used", ""),
            })
        except Exception:
            continue
    return skills


# ── 任务匹配 ───────────────────────────────────────────────

def match_skills(task_text: str) -> list[dict]:
    """
    根据任务文本匹配已有 skill，返回相关经验列表（最多 3 个）。
    匹配规则：patterns 中任一词出现在任务文本中（忽略大小写）。
    """
    task_lower = task_text.lower()
    matched = []
    for skill in list_skills():
        for pat in skill.get("patterns", []):
            if pat.lower() in task_lower:
                full = _load_skill(skill["name"])
                if full:
                    matched.append(full)
                break
        if len(matched) >= 3:
            break
    return matched


def skills_to_prompt(task_text: str) -> str:
    """
    生成注入 prompt 的经验提示块。
    如果没有匹配的 skill，返回空字符串。
    """
    skills = match_skills(task_text)
    if not skills:
        return ""

    lines = ["## Historical success (follow these steps first; verified)\n"]
    for s in skills:
        lines.append(f"### [{s['name']}] {s['description']}")
        lines.append(f"Use when: {', '.join(s.get('patterns', []))}")
        if s.get("notes"):
            lines.append(f"Note: {s['notes']}")
        lines.append("Steps:")
        for i, step in enumerate(s.get("steps", []), 1):
            step_clean = {k: v for k, v in step.items()}
            lines.append(f"  Step {i}: {json.dumps(step_clean, ensure_ascii=False)}")
        lines.append(f"(Used {s.get('success_count', 1)} time(s), last: {s.get('last_used', '')})\n")

    return "\n".join(lines)


# ── 任务成功后保存 ─────────────────────────────────────────

def _infer_skill_name(task_text: str) -> str:
    """从任务文本推断 skill 名称（英文小写下划线）"""
    # 常见任务类型映射
    mappings = [
        (r'unity',              'launch_unity'),
        (r'photoshop',          'launch_photoshop'),
        (r'blender',            'launch_blender'),
        (r'steam',              'launch_steam'),
        (r'vscode|vs\s*code',   'launch_vscode'),
        (r'启动|打开|运行.*\.exe', 'launch_program'),
        (r'修复.*py|py.*修复|indentation|syntax', 'fix_python'),
        (r'修复.*游戏|游戏.*修复', 'fix_game'),
        (r'安装.*pip|pip.*install', 'pip_install'),
        (r'新闻|资讯|搜索.*保存|整理.*桌面', 'search_and_save'),
        (r'写.*文件|创建.*文件|生成.*文件', 'write_file'),
        (r'读.*文件|查看.*文件', 'read_file'),
    ]
    tl = task_text.lower()
    for pattern, name in mappings:
        if re.search(pattern, tl):
            return name
    # 兜底：取任务前 20 字做名称
    name = re.sub(r'[^\w]', '_', task_text[:20]).strip('_').lower()
    return name or 'unknown_task'


def _infer_description(task_text: str) -> str:
    """取任务文本前 40 字作为描述"""
    desc = task_text.strip().splitlines()[0]
    return desc[:60]


def _infer_patterns(task_text: str, skill_name: str) -> list[str]:
    """从任务文本和 skill 名称推断匹配关键词"""
    patterns = set()
    # 从 skill_name 反推（去掉 launch_ / fix_ 前缀）
    core = re.sub(r'^(launch|fix|run|open|start)_?', '', skill_name)
    if core and len(core) > 1:
        patterns.add(core)
    # 从任务文本提取关键词（中文词和英文词）
    for w in re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z]{3,20}', task_text):
        w_lower = w.lower()
        if w_lower not in ('the', 'and', 'for', 'with', 'that', 'this',
                           '任务', '帮我', '我需要', '请帮', '文件', '路径'):
            patterns.add(w_lower)
    return list(patterns)[:8]


def _extract_steps(executed_blocks: list[dict]) -> list[dict]:
    """
    从执行成功的 JSON 块中提取步骤序列，
    路径中的用户名部分替换为占位符，提高可复用性。
    """
    steps = []
    for block in executed_blocks:
        if not isinstance(block, dict):
            continue
        step = {}
        for k, v in block.items():
            if isinstance(v, str):
                # 替换实际用户名为占位符
                v = re.sub(
                    r'[A-Za-z]:\\Users\\[^\\]+\\',
                    r'C:\\Users\\<用户名>\\',
                    v
                )
            step[k] = v
        steps.append(step)
    return steps


def save_skill_from_success(
    task_text: str,
    executed_blocks: list[dict],
    notes: str = "",
) -> str:
    """
    任务成功后调用，保存或更新 skill。

    参数
    ----
    task_text       : 原始用户任务文本
    executed_blocks : 本次成功执行的所有 JSON 块列表
    notes           : 额外备注（可由 AI 返回文本中提取）

    返回
    ----
    保存的 skill 名称
    """
    if not executed_blocks:
        return ""

    name = _infer_skill_name(task_text)
    existing = _load_skill(name)

    if existing:
        # 更新已有 skill：步骤以本次为准（最新的成功经验），计数+1
        existing["steps"] = _extract_steps(executed_blocks)
        existing["success_count"] = existing.get("success_count", 1) + 1
        existing["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if notes:
            existing["notes"] = notes
        # 合并 patterns（不去重丢失旧关键词）
        new_pats = _infer_patterns(task_text, name)
        old_pats = existing.get("patterns", [])
        merged = list(dict.fromkeys(old_pats + new_pats))
        existing["patterns"] = merged[:12]
        _save_skill(existing)
        return name
    else:
        skill = {
            "name": name,
            "description": _infer_description(task_text),
            "patterns": _infer_patterns(task_text, name),
            "steps": _extract_steps(executed_blocks),
            "notes": notes,
            "success_count": 1,
            "last_used": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _save_skill(skill)
        return name
