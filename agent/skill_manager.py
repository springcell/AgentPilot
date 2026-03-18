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

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(_BASE_DIR, "skills")
PROMPTS_DIR = os.path.join(_BASE_DIR, "prompts")
CONFIG_DIR = os.path.join(_BASE_DIR, "config")

# ── Category → identity file mapping ──────────────────────────────────────

_CATEGORY_IDENTITY: dict[str, str] = {
    "write_code":     "identity_programmer",
    "write_ppt":      "identity_presenter",
    "write_doc":      "identity_writer",
    "image_modify":   "identity_painter",
    "image_generate": "identity_painter",
    "control_system": "identity_system",
    "analyze_data":   "identity_analyst",
    "translate":      "identity_translator",
    "run_test":       "identity_tester",
    "design_arch":    "identity_architect",
    "write_readme":   "identity_tech_writer",
    # general / file_ops → no identity injection
}

# Category → reviewer identity file mapping
_CATEGORY_REVIEWER: dict[str, str] = {
    "write_code":     "identity_reviewer_code",
    "run_test":       "identity_reviewer_code",
    "design_arch":    "identity_reviewer_code",
    "image_modify":   "identity_reviewer_image",
    "image_generate": "identity_reviewer_image",
    "write_doc":      "identity_reviewer_doc",
    "write_ppt":      "identity_reviewer_doc",
    "write_readme":   "identity_reviewer_doc",
    "analyze_data":   "identity_reviewer_doc",
    "translate":      "identity_reviewer_doc",
    # control_system / general / file_ops → no reviewer
}

_DEFAULT_PROFILE_CONFIG: dict[str, dict] = {
    "category_identity": dict(_CATEGORY_IDENTITY),
    "category_reviewer": dict(_CATEGORY_REVIEWER),
    "category_flow": {
        "image_modify": "file_chat_first",
        "image_generate": "direct_chat",
        "write_ppt": "script_then_run",
        "write_doc": "script_then_run",
        "write_code": "default",
        "control_system": "control_only",
        "file_ops": "default",
        "general": "default",
    },
    "identity_delivery_rules": {
        "default": [
            "严格产出可落盘、可复核的最终交付物，不要只给过程说明。",
            "若任务要求保存文件，最终回复必须明确给出已保存路径。",
            "连续失败时先给诊断与下一步，不要无限重试。",
        ],
        "by_category": {
            "image_modify": [
                "成功标准是捕获并保存可下载图片，不要只返回解释文字。",
                "若未拿到图片下载结果，应明确报失败原因并停止当前回路。",
            ],
            "image_generate": [
                "成功标准是捕获并保存可下载图片，不要只返回解释文字。",
            ],
            "write_code": [
                "优先交付可执行源码文件，并至少完成一次验证。",
            ],
            "write_doc": [
                "优先交付 markdown、txt、docx 等文档文件，并返回保存路径。",
            ],
            "write_ppt": [
                "主交付物是 ppt/pptx 文件；大纲仅可作为失败回退。",
            ],
            "analyze_data": [
                "交付分析脚本、结果文件和简短结论，输入输出路径需明确。",
            ],
            "run_test": [
                "交付测试命令、测试结果和关键失败摘要。",
            ],
        },
    },
    "loop_policies": {
        "default": {
            "done_conditions": [
                "最终交付物已生成，并且可被本地保存或直接使用。",
            ],
            "stop_conditions": [
                "达到停止条件后立即结束，不做无限重试。",
            ],
            "fallback": [
                "无法完成时返回明确诊断、阻塞原因和下一步建议。",
            ],
        },
        "by_category": {
            "image_modify": {
                "done_conditions": [
                    "捕获到修改后的图片，并已保存到目标路径。",
                ],
                "stop_conditions": [
                    "上传成功但连续只有说明回复且没有图片时，立即停止。",
                ],
                "fallback": [
                    "返回失败原因，并说明未拿到成品图片的原因。",
                ],
            },
            "image_generate": {
                "done_conditions": [
                    "捕获到生成图片，并已保存到目标路径。",
                ],
                "stop_conditions": [
                    "连续只有说明回复且没有图片时，立即停止。",
                ],
                "fallback": [
                    "返回失败原因，并说明未拿到成品图片的原因。",
                ],
            },
            "write_code": {
                "done_conditions": [
                    "代码文件已写入，且至少完成一次本地验证。",
                ],
                "stop_conditions": [
                    "连续验证失败超过阈值时停止并返回诊断。",
                ],
                "fallback": [
                    "返回当前错误、影响文件和建议修复方向。",
                ],
            },
            "write_ppt": {
                "done_conditions": [
                    "ppt/pptx 文件已生成并保存到目标路径。",
                ],
                "stop_conditions": [
                    "无法生成 pptx 时停止，不以脚本本身冒充最终交付。",
                ],
                "fallback": [
                    "回退为大纲或诊断，并明确说明未生成 pptx。",
                ],
            },
        },
    },
    "identity_prompt_styles": {
        "write_code": "executor_json",
        "run_test": "executor_json",
        "design_arch": "executor_json",
        "control_system": "executor_json",
        "analyze_data": "executor_json",
        "write_ppt": "executor_json",
        "write_doc": "direct_delivery",
        "write_readme": "direct_delivery",
        "translate": "direct_delivery",
        "image_modify": "asset_only",
        "image_generate": "asset_only",
        "general": "plain",
    },
    "role_to_identity": {
        "programmer": "programmer",
        "writer": "writer",
        "painter": "painter",
        "presenter": "presenter",
        "system": "system",
        "analyst": "analyst",
        "translator": "translator",
        "tester": "tester",
        "architect": "architect",
        "tech_writer": "tech_writer",
        "tech writer": "tech_writer",
        "reviewer": "reviewer",
        "程序员": "programmer",
        "撰稿人": "writer",
        "画家": "painter",
        "设计师": "painter",
        "演示稿专家": "presenter",
        "系统控制": "system",
        "数据分析师": "analyst",
        "翻译": "translator",
        "测试": "tester",
        "架构师": "architect",
        "技术文档": "tech_writer",
        "审核专家": "reviewer",
    },
    "identity_to_category": {
        "programmer": "write_code",
        "writer": "write_doc",
        "painter": "image_generate",
        "presenter": "write_ppt",
        "system": "control_system",
        "analyst": "write_doc",
        "translator": "general",
        "tester": "write_code",
        "architect": "write_code",
        "tech_writer": "write_doc",
        "reviewer": "general",
    },
    "identity_aliases": {
        "programmer": "programmer",
        "writer": "writer",
        "painter": "painter",
        "designer": "painter",
        "presenter": "presenter",
        "system": "system",
        "analyst": "analyst",
        "translator": "translator",
        "tester": "tester",
        "architect": "architect",
        "tech_writer": "tech_writer",
        "tech writer": "tech_writer",
        "reviewer": "reviewer",
        "程序员": "programmer",
        "撰稿人": "writer",
        "画家": "painter",
        "设计师": "painter",
        "演示稿专家": "presenter",
        "系统控制": "system",
        "数据分析师": "analyst",
        "翻译": "translator",
        "测试": "tester",
        "架构师": "architect",
        "技术文档": "tech_writer",
        "审核专家": "reviewer",
    },
    "identity_to_agent_id": {
        "programmer": "executor_programmer",
        "writer": "executor_writer",
        "painter": "executor_painter",
        "presenter": "executor_presenter",
        "system": "executor_system",
        "analyst": "executor_analyst",
        "translator": "executor_translator",
        "tester": "executor_tester",
        "architect": "executor_architect",
        "tech_writer": "executor_tech_writer",
        "reviewer": "reviewer",
    },
}

_PROFILE_CONFIG_CACHE: dict | None = None


def _load_profile_config() -> dict:
    global _PROFILE_CONFIG_CACHE
    if _PROFILE_CONFIG_CACHE is not None:
        return _PROFILE_CONFIG_CACHE
    config = json.loads(json.dumps(_DEFAULT_PROFILE_CONFIG, ensure_ascii=False))
    path = os.path.join(CONFIG_DIR, "identity_skill_profiles.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            for key in (
                "category_identity",
                "category_reviewer",
                "category_flow",
                "identity_prompt_styles",
                "role_to_identity",
                "identity_to_category",
                "identity_aliases",
                "identity_to_agent_id",
            ):
                if isinstance(user_cfg.get(key), dict):
                    config[key].update(user_cfg[key])
            rules_cfg = user_cfg.get("identity_delivery_rules", {})
            if isinstance(rules_cfg.get("default"), list):
                config["identity_delivery_rules"]["default"] = list(rules_cfg["default"])
            if isinstance(rules_cfg.get("by_category"), dict):
                config["identity_delivery_rules"]["by_category"].update(rules_cfg["by_category"])
            loop_cfg = user_cfg.get("loop_policies", {})
            if isinstance(loop_cfg.get("default"), dict):
                config["loop_policies"]["default"].update(loop_cfg["default"])
            if isinstance(loop_cfg.get("by_category"), dict):
                config["loop_policies"]["by_category"].update(loop_cfg["by_category"])
        except Exception:
            pass
    _PROFILE_CONFIG_CACHE = config
    return config


def _normalize_identity_prompt_name(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("identity_") else "identity_" + raw


def _merge_rule_lines(*groups) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


def _merge_loop_policy(default_policy: dict, category_policy: dict, skill_policy: dict) -> dict:
    merged = {
        "done_conditions": _merge_rule_lines(
            (default_policy or {}).get("done_conditions", []),
            (category_policy or {}).get("done_conditions", []),
            (skill_policy or {}).get("done_conditions", []),
        ),
        "stop_conditions": _merge_rule_lines(
            (default_policy or {}).get("stop_conditions", []),
            (category_policy or {}).get("stop_conditions", []),
            (skill_policy or {}).get("stop_conditions", []),
        ),
        "fallback": _merge_rule_lines(
            (default_policy or {}).get("fallback", []),
            (category_policy or {}).get("fallback", []),
            (skill_policy or {}).get("fallback", []),
        ),
    }
    for source in (default_policy or {}, category_policy or {}, skill_policy or {}):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key in merged:
                continue
            merged[key] = value
    return merged


def get_skill_runtime_profile(category: str, skill: dict = None) -> dict:
    config = _load_profile_config()
    identity_name = _normalize_identity_prompt_name((skill or {}).get("identity", "")) or _normalize_identity_prompt_name(
        config["category_identity"].get(category, "")
    )
    reviewer_name = _normalize_identity_prompt_name((skill or {}).get("reviewer", "")) or _normalize_identity_prompt_name(
        config["category_reviewer"].get(category, "")
    )
    flow = str((skill or {}).get("flow", "")).strip() or str(config["category_flow"].get(category, "default")).strip() or "default"
    config_rules = config["identity_delivery_rules"].get("by_category", {}).get(category, [])
    skill_rules = (skill or {}).get("rules", [])
    loop_policies = config.get("loop_policies", {})
    loop_policy = _merge_loop_policy(
        loop_policies.get("default", {}),
        loop_policies.get("by_category", {}).get(category, {}),
        (skill or {}).get("loop_policy", {}) if isinstance((skill or {}).get("loop_policy", {}), dict) else {},
    )
    return {
        "identity": identity_name,
        "reviewer": reviewer_name,
        "flow": flow,
        "prompt_style": str(config.get("identity_prompt_styles", {}).get(category, "plain")).strip() or "plain",
        "rules": _merge_rule_lines(config["identity_delivery_rules"].get("default", []), config_rules, skill_rules),
        "loop_policy": loop_policy,
    }


def get_identity_prompt(category: str, task_summary: str = "", language: str = "", skill: dict = None, runtime_override: dict = None) -> str:
    """
    Load the identity prompt file for the given category and fill in {任务摘要}.
    Returns "" if no identity file exists for this category.
    """
    runtime = dict(runtime_override) if isinstance(runtime_override, dict) else get_skill_runtime_profile(category, skill)
    identity_name = runtime.get("identity", "")
    if not identity_name:
        return ""
    path = os.path.join(PROMPTS_DIR, identity_name + ".txt")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        template = f.read().strip()
    summary = task_summary[:80] if task_summary else "完成任务"
    rendered = template.replace("{任务摘要}", summary)
    extra_lines: list[str] = []
    if language:
        style = runtime.get("prompt_style", "plain")
        if style == "executor_json":
            extra_lines.append(f"[输出语言] {language}")
        elif style == "asset_only":
            extra_lines.append(f"本次需求使用：{language}")
        else:
            extra_lines.append(f"使用语言：{language}")
    if runtime.get("rules"):
        heading = "[交付规则]"
        if runtime.get("prompt_style") == "asset_only":
            heading = "[成片要求]"
        elif runtime.get("prompt_style") == "direct_delivery":
            heading = "[交付要求]"
        extra_lines.append(heading)
        extra_lines.extend(f"- {rule}" for rule in runtime["rules"])
    loop_policy = runtime.get("loop_policy", {}) or {}
    section_titles = {
        "done_conditions": "[完成条件]",
        "stop_conditions": "[停止条件]",
        "fallback": "[失败回退]",
    }
    if runtime.get("prompt_style") == "asset_only":
        section_titles = {
            "done_conditions": "[成片完成条件]",
            "stop_conditions": "[停止条件]",
            "fallback": "[失败回退]",
        }
    for key in ("done_conditions", "stop_conditions", "fallback"):
        lines = loop_policy.get(key, [])
        if not lines:
            continue
        extra_lines.append(section_titles[key])
        extra_lines.extend(f"- {line}" for line in lines)
    if extra_lines:
        rendered = rendered + "\n" + "\n".join(extra_lines)
    return rendered


def get_reviewer_prompt(category: str, skill: dict = None) -> str:
    """
    Load the reviewer identity prompt for the given category.
    Falls back to skill["reviewer"] field if provided.
    Returns "" if no reviewer applies for this category.
    """
    reviewer_name = get_skill_runtime_profile(category, skill).get("reviewer", "")
    if not reviewer_name:
        return ""
    path = os.path.join(PROMPTS_DIR, reviewer_name + ".txt")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ── Directory layout ────────────────────────────────────────────────────────

SKILLS_PRESET_DIR      = os.path.join(_BASE_DIR, "skills", "preset")
SKILLS_BY_CATEGORY_DIR = os.path.join(_BASE_DIR, "skills", "by_category")
SKILLS_LEARNED_DIR     = os.path.join(_BASE_DIR, "skills", "learned")


def _ensure_dir():
    for d in (SKILLS_DIR, SKILLS_PRESET_DIR, SKILLS_BY_CATEGORY_DIR, SKILLS_LEARNED_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)


def _skill_path(name: str) -> str:
    safe = re.sub(r'[^\w\-]', '_', name)
    return os.path.join(SKILLS_LEARNED_DIR, f"{safe}.json")


def _skill_path_flat(name: str) -> str:
    """Legacy flat path for backward compat (reading only)."""
    safe = re.sub(r'[^\w\-]', '_', name)
    return os.path.join(SKILLS_DIR, f"{safe}.json")


def _load_skill(name: str) -> dict | None:
    # Search learned → flat (legacy)
    for path in (_skill_path(name), _skill_path_flat(name)):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def _save_skill(skill: dict) -> None:
    _ensure_dir()
    path = _skill_path(skill["name"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(skill, f, ensure_ascii=False, indent=2)


# ── Category / flow inference ───────────────────────────────────────────────

_BINARY_EXTS_RE = re.compile(
    r'[\w\-]+\.(?:png|jpg|jpeg|gif|bmp|webp|ico|tiff|mp3|mp4|wav|avi|mov|mkv|pdf|psd|ai|svg)\b',
    re.IGNORECASE,
)
_ABS_PATH_RE = re.compile(
    r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]*'
    r'\.(?:png|jpg|jpeg|gif|bmp|webp|ico|tiff|mp3|mp4|wav|avi|mov|mkv|pdf|psd|ai|svg)\b',
    re.IGNORECASE,
)
_MODIFY_VERBS_RE = re.compile(
    r'细化|修改|优化|美化|修复|调整|改进|增强|重绘|润色|编辑'
    r'|refine|improve|enhance|fix|edit|modify|adjust|update'
    r'|transform|resize|crop|rotate|recolor|rework|beautify|change',
    re.IGNORECASE,
)


def _rule_image_modify(task_lower: str, env_info: dict) -> bool:
    if not _MODIFY_VERBS_RE.search(task_lower):
        return False
    # 修改类动词 + 存在过的图片路径 → image_modify
    desktop = (env_info or {}).get("desktop", "")
    for m in _ABS_PATH_RE.finditer(task_lower):
        if os.path.isfile(os.path.normpath(m.group(0).strip())):
            return True
    for m in _BINARY_EXTS_RE.finditer(task_lower):
        raw = m.group(0).strip()
        if not os.path.isabs(raw) and desktop:
            if os.path.isfile(os.path.normpath(os.path.join(desktop, raw))):
                return True
    # 修改类动词 + 文中出现图片扩展名（如 细化 5.png）→ 即使文件不存在也判为 image_modify
    if _BINARY_EXTS_RE.search(task_lower):
        return True
    return False


def _rule_image_generate(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(
        r'生成图|画一张|画个|绘制|create.*image|generate.*image|draw.*image|render.*image|画.*图',
        task_lower,
    ))


def _rule_write_ppt(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(r'ppt|幻灯片|演示文稿|powerpoint|presentation|汇报.*ppt|开会.*ppt', task_lower))


def _rule_write_doc(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(
        r'\.docx|word文档|会议纪要|excel|\.xlsx|写文档|写报告|readme|api\s*说明|文档',
        task_lower,
    ))


def _rule_write_code(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(
        r'\.(?:py|shader|hlsl|cginc|cs|cpp|cc|cxx|c|h|hpp|hh|js|jsx|ts|tsx|java|go|rs|php|lua|rb|swift|kt)\b'
        r'|python|shader|hlsl|cginc|compile|compiler|syntax|exception|stack\s*trace'
        r'|代码|脚本|报错|编译|编译错误|修复.*syntax|syntax.*error|indentation|爬虫|写.*函数|写.*类|bug|error',
        task_lower,
    ))


def _rule_control_system(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(
        '\u542f\u52a8|\u6253\u5f00|\u8fd0\u884c|launch|open\\s+\\w+|start\\s+\\w+|\\.exe\\b'
        '|\u5220\u9664|\u6e05\u7406|\u79fb\u9664|remove|delete|cleanup|clean\\s*up|clear'
        '|\u684c\u9762|desktop|\u6587\u4ef6|\u76ee\u5f55|folder|directory'
        '|\u56fe\u7247|image|images|photo|photos|picture|pictures|png|jpg|jpeg|gif|bmp|webp',
        task_lower,
    ))


def _rule_file_ops(task_lower: str, env_info: dict) -> bool:
    return bool(re.search(
        r'读取|查看|复制|移动|列出|list\s+file|read\s+file|copy\s+file|move\s+file|find\s+file',
        task_lower,
    ))


# Ordered: first match wins
_CATEGORY_RULES: list[tuple] = [
    (_rule_image_modify,   "image_modify"),
    (_rule_image_generate, "image_generate"),
    (_rule_write_ppt,      "write_ppt"),
    (_rule_write_doc,      "write_doc"),
    (_rule_write_code,     "write_code"),
    (_rule_control_system, "control_system"),
    (_rule_file_ops,       "file_ops"),
    (lambda t, e: True,    "general"),
]

def infer_category(task_text: str, env_info: dict = None) -> str:
    """Infer task category using ordered rules (first match wins)."""
    task_lower = task_text.lower()
    for test_fn, category in _CATEGORY_RULES:
        if test_fn(task_lower, env_info or {}):
            return category
    return "general"


def infer_flow(category: str, executed_blocks: list = None) -> str:
    """Map category → flow. Override by scanning executed_blocks when provided."""
    flow = _load_profile_config().get("category_flow", {}).get(category, "default")
    if not executed_blocks:
        return flow
    actions = [b.get("action", "") for b in executed_blocks if isinstance(b, dict)]
    cmds    = [b.get("command", "") for b in executed_blocks if isinstance(b, dict)]
    paths   = [b.get("path", "") for b in executed_blocks if isinstance(b, dict)]
    if "write_web" in actions:
        return "file_chat_first"
    if any(str(p).endswith(".py") for p in paths if p) and "python" in cmds:
        return "script_then_run"
    if set(actions) <= {"launch", "find_program"} and set(cmds) <= {"file_op", "powershell", "cmd"}:
        return "control_only"
    return flow


def match_skill_by_category(task_text: str, env_info: dict = None) -> tuple:
    """
    Returns (skill_dict | None, category_string).
    Search order: preset → learned → flat (legacy).
    Within each directory, filter by category then rank by pattern matches.
    """
    _ensure_dir()
    category  = infer_category(task_text, env_info)
    task_lower = task_text.lower()

    def _best_in_dir(directory: str) -> dict | None:
        if not os.path.isdir(directory):
            return None
        best, best_score = None, (-1, 0)
        for fname in os.listdir(directory):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as f:
                    s = json.load(f)
            except Exception:
                continue
            if s.get("category", "") != category:
                continue
            pat_score = sum(1 for p in s.get("patterns", []) if p.lower() in task_lower)
            # Secondary sort: success_count (higher = more proven)
            score = (pat_score, s.get("success_count", 0))
            if score > best_score:
                best_score, best = score, s
        return best

    for directory in (SKILLS_PRESET_DIR, SKILLS_BY_CATEGORY_DIR, SKILLS_LEARNED_DIR, SKILLS_DIR):
        found = _best_in_dir(directory)
        if found is not None:
            return found, category

    return None, category


def list_skills() -> list[dict]:
    """返回所有已保存的 skill 列表（摘要）"""
    _ensure_dir()
    skills = []
    seen_names: set[str] = set()
    # Scan all directories: preset, by_category, learned, flat (legacy)
    for directory in (SKILLS_PRESET_DIR, SKILLS_BY_CATEGORY_DIR, SKILLS_LEARNED_DIR, SKILLS_DIR):
        if not os.path.isdir(directory):
            continue
        for fname in os.listdir(directory):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as f:
                    s = json.load(f)
                name = s.get("name", "")
                if name in seen_names:
                    continue
                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": s.get("description", ""),
                    "patterns": s.get("patterns", []),
                    "category": s.get("category", ""),
                    "flow": s.get("flow", ""),
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
        if s.get("identity"):
            lines.append(f"Identity: {s['identity']}")
        if s.get("flow"):
            lines.append(f"Preferred flow: {s['flow']}")
        lines.append(f"Use when: {', '.join(s.get('patterns', []))}")
        if s.get("rules"):
            lines.append("Rules:")
            for rule in s.get("rules", []):
                lines.append(f"  - {rule}")
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
    # 过滤乱码 pattern（非ASCII率超过50%则丢弃）
    clean = []
    for p in patterns:
        non_ascii = sum(1 for c in p if ord(c) > 127)
        if len(p) == 0 or non_ascii / len(p) > 0.5:
            continue
        clean.append(p)
    return clean[:8]


def _is_valid_skill(skill: dict) -> bool:
    """检查 skill 是否有效（无乱码、有基本字段）"""
    desc = skill.get("description", "")
    name = skill.get("name", "")
    if not name or not desc:
        return False
    for text in (desc, name):
        non_ascii = sum(1 for c in text if ord(c) > 127)
        if len(text) > 0 and non_ascii / len(text) > 0.5:
            return False
    return True


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
    category: str = "",
    flow: str = "",
) -> str:
    """
    任务成功后调用，保存或更新 skill。
    新增 category / flow 参数：不传则自动推断。
    保存到 skills/learned/（不覆盖 preset）。
    """
    if not executed_blocks:
        return ""

    name = _infer_skill_name(task_text)
    if not name or sum(1 for c in name if ord(c) > 127) / max(len(name), 1) > 0.3:
        return ""

    # Auto-infer category / flow when not supplied
    if not category:
        category = infer_category(task_text)
    if not flow:
        flow = infer_flow(category, executed_blocks)

    existing = _load_skill(name)

    if existing:
        existing["steps"] = _extract_steps(executed_blocks)
        existing["success_count"] = existing.get("success_count", 1) + 1
        existing["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if notes:
            existing["notes"] = notes
        # Write category/flow only if missing (don't overwrite preset values)
        if not existing.get("category"):
            existing["category"] = category
        if not existing.get("flow"):
            existing["flow"] = flow
        new_pats = _infer_patterns(task_text, name)
        old_pats = existing.get("patterns", [])
        existing["patterns"] = list(dict.fromkeys(old_pats + new_pats))[:12]
        _save_skill(existing)
        return name
    else:
        skill = {
            "name": name,
            "category": category,
            "flow": flow,
            "description": _infer_description(task_text),
            "patterns": _infer_patterns(task_text, name),
            "rules": [],
            "steps": _extract_steps(executed_blocks),
            "notes": notes,
            "success_count": 1,
            "last_used": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _save_skill(skill)
        return name


# ── AI-based dispatch reply parser ──────────────────────────────────────────


def parse_dispatch_reply(reply: str) -> tuple[str, str, str]:
    """
    Parse the AI dispatch reply to extract language, identity key, and category.

    Expected format (either language):
      "中文，需要 writer 去做"
      "English, need programmer to do it"
      "[语言], need [role] to do it"

    Returns:
      (language, identity_key, category)
      Falls back to ("", "", "general") if parsing fails.
    """
    text = reply.strip().lower()
    profile_config = _load_profile_config()
    role_to_identity = profile_config.get("role_to_identity", {})

    # Extract role name: look for "need <role>" or "需要 <role>"
    identity_key = ""
    import re as _re2
    # English pattern: "need <role>"
    m = _re2.search(r'need\s+([a-z_\s]+?)\s+to\s+do', text)
    if m:
        raw_role = m.group(1).strip()
        identity_key = role_to_identity.get(raw_role, "")
    if not identity_key:
        # Chinese pattern: "需要 <role> 去做"
        m = _re2.search(r'需要\s*(.+?)\s*去做', reply.strip())
        if m:
            raw_role = m.group(1).strip()
            identity_key = role_to_identity.get(raw_role, "")
    if not identity_key:
        # Fuzzy: check if any known role appears in text
        for role_key in sorted(role_to_identity.keys(), key=len, reverse=True):
            if role_key.lower() in text:
                identity_key = role_to_identity[role_key.lower()]
                break

    # Extract language: everything before the first comma/，
    language = ""
    m = _re2.match(r'^([^,，]+)[,，]', reply.strip())
    if m:
        language = m.group(1).strip()

    category = profile_config.get("identity_to_category", {}).get(identity_key, "general")
    return language, identity_key, category


def normalize_identity_key(raw_identity: str) -> str:
    """Normalize a target identity name from AI/user text to an internal key."""
    if not raw_identity:
        return ""
    text = raw_identity.strip().lower()
    aliases = _load_profile_config().get("identity_aliases", {})
    if text in aliases:
        return aliases[text]
    return _load_profile_config().get("role_to_identity", {}).get(raw_identity.strip(), "")


def get_agent_id_for_identity(identity_key: str) -> str:
    """Map an identity key to a stable web slot id."""
    mapping = _load_profile_config().get("identity_to_agent_id", {})
    return mapping.get(identity_key, f"executor_{identity_key or 'default'}")


def get_category_for_identity(identity_key: str, task_text: str = "") -> str:
    """Resolve category from identity, with a small override for painter tasks."""
    category = _load_profile_config().get("identity_to_category", {}).get(identity_key, "general")
    if identity_key == "painter" and task_text:
        task_lower = task_text.lower()
        if _MODIFY_VERBS_RE.search(task_lower) and _BINARY_EXTS_RE.search(task_lower):
            return "image_modify"
    return category
