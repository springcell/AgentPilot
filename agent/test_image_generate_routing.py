import sys

sys.path.insert(0, r"d:\AgentPilot\agent")

from skill_manager import infer_category, match_skill_by_category, get_skill_runtime_profile


def main() -> None:
    task = "我需要帮我设计一个精美的图标，一个小镇主题，放在桌面"
    category = infer_category(task, {"desktop": r"C:\Users\admin\Desktop"})
    assert category == "image_generate", category

    skill, matched_category = match_skill_by_category(task, {"desktop": r"C:\Users\admin\Desktop"}, "image_generate")
    assert matched_category == "image_generate", matched_category
    assert skill and skill.get("name") == "image_generate", skill

    runtime = get_skill_runtime_profile("image_generate", skill)
    assert runtime["flow"] == "direct_chat", runtime

    print("ok")


if __name__ == "__main__":
    main()
