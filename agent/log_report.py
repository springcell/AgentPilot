"""
log_report.py — Consume AgentPilot event logs into readable task summaries.
"""

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_EVENT_LOG = os.path.join(ROOT_DIR, "logs", "agent-events.jsonl")
DEFAULT_REPLAY_DIR = os.path.join(ROOT_DIR, "logs", "replay")


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def load_events(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def group_events_by_task(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        task_id = event.get("task_id", "")
        if task_id:
            grouped[task_id].append(event)
    for task_id in grouped:
        grouped[task_id].sort(key=lambda item: item.get("ts", ""))
    return grouped


def classify_failure_reason(task_events: list[dict]) -> str:
    for event in reversed(task_events):
        if event.get("event") == "task_cannot_complete":
            return "cannot_complete"
        if event.get("event") == "review_result" and event.get("result") == "fail":
            return "review_rejected"
        if event.get("event") == "request_help_failed":
            return "request_help_failed"
        if event.get("event") == "chat_error":
            error = str(event.get("error", "")).lower()
            if "timeout" in error:
                return "chat_timeout"
            return "chat_error"
        if event.get("event") == "task_failed":
            error = str(event.get("error", "")).lower()
            if "max_iterations" in error:
                return "max_iterations"
            if "timeout" in error:
                return "timeout"
            return "task_failed"
    return "unknown"


def _task_duration_seconds(task_events: list[dict]) -> float | None:
    if not task_events:
        return None
    start = _parse_ts(task_events[0].get("ts", ""))
    end = _parse_ts(task_events[-1].get("ts", ""))
    if not start or not end:
        return None
    return max(0.0, (end - start).total_seconds())


def summarize_task(task_id: str, task_events: list[dict], replay_root: str) -> dict:
    status = "running"
    for event in reversed(task_events):
        name = event.get("event", "")
        if name == "task_complete":
            status = "completed"
            break
        if name in ("task_failed", "task_cannot_complete"):
            status = "failed"
            break

    started = task_events[0].get("ts", "") if task_events else ""
    ended = task_events[-1].get("ts", "") if task_events else ""
    replay_path = os.path.join(replay_root, task_id)
    replay_files = sorted(os.listdir(replay_path)) if os.path.isdir(replay_path) else []

    task_text = ""
    identity = ""
    category = ""
    failure_reason = ""
    block_count = 0
    review_started = False
    request_help_count = 0

    for event in task_events:
        if event.get("event") == "task_start" and not task_text:
            task_text = event.get("task_text", "")
        if event.get("event") == "skill_matched":
            identity = event.get("identity", identity)
            category = event.get("category", category)
        if event.get("event") == "json_block_executed":
            block_count += int(event.get("block_count", 0))
        if event.get("event") == "review_start":
            review_started = True
        if event.get("event") == "request_help_sent":
            request_help_count += 1

    if status == "failed":
        failure_reason = classify_failure_reason(task_events)

    return {
        "task_id": task_id,
        "status": status,
        "task_text": task_text,
        "identity": identity,
        "category": category,
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": _task_duration_seconds(task_events),
        "event_count": len(task_events),
        "block_count": block_count,
        "review_started": review_started,
        "request_help_count": request_help_count,
        "failure_reason": failure_reason,
        "replay_files": replay_files,
        "replay_path": replay_path if replay_files else "",
    }


def build_report(events: list[dict], replay_root: str, limit: int = 10) -> dict:
    grouped = group_events_by_task(events)
    summaries = [summarize_task(task_id, task_events, replay_root) for task_id, task_events in grouped.items()]
    summaries.sort(key=lambda item: item.get("started_at", ""), reverse=True)

    status_counter = Counter(item["status"] for item in summaries)
    failure_counter = Counter(item["failure_reason"] for item in summaries if item["failure_reason"])

    completed = status_counter.get("completed", 0)
    total = len(summaries)
    success_rate = (completed / total) if total else 0.0

    return {
        "summary": {
            "total_tasks": total,
            "completed_tasks": completed,
            "failed_tasks": status_counter.get("failed", 0),
            "running_tasks": status_counter.get("running", 0),
            "success_rate": round(success_rate, 4),
            "failure_by_reason": dict(failure_counter),
        },
        "recent_tasks": summaries[:limit],
    }


def render_text(report: dict, task_detail: dict | None = None) -> str:
    lines = []
    summary = report["summary"]
    lines.append("AgentPilot Log Summary")
    lines.append(f"- total_tasks: {summary['total_tasks']}")
    lines.append(f"- completed_tasks: {summary['completed_tasks']}")
    lines.append(f"- failed_tasks: {summary['failed_tasks']}")
    lines.append(f"- running_tasks: {summary['running_tasks']}")
    lines.append(f"- success_rate: {summary['success_rate']:.2%}")

    if summary["failure_by_reason"]:
        lines.append("- failure_by_reason:")
        for reason, count in sorted(summary["failure_by_reason"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"  - {reason}: {count}")

    lines.append("")
    lines.append("Recent tasks:")
    if not report["recent_tasks"]:
        lines.append("- (no tasks found)")
    for item in report["recent_tasks"]:
        duration = item["duration_seconds"]
        duration_text = f"{duration:.1f}s" if duration is not None else "n/a"
        task_text = item["task_text"] or "(missing task text)"
        lines.append(
            f"- {item['task_id']} | {item['status']} | {item['category'] or '-'} | "
            f"events={item['event_count']} | blocks={item['block_count']} | "
            f"help={item['request_help_count']} | duration={duration_text}"
        )
        lines.append(f"  task: {task_text}")
        if item["failure_reason"]:
            lines.append(f"  failure: {item['failure_reason']}")
        if item["replay_path"]:
            lines.append(f"  replay: {item['replay_path']}")

    if task_detail:
        lines.append("")
        lines.append(f"Task detail: {task_detail['task_id']}")
        lines.append(f"- status: {task_detail['status']}")
        lines.append(f"- category: {task_detail['category'] or '-'}")
        lines.append(f"- identity: {task_detail['identity'] or '-'}")
        lines.append(f"- started_at: {task_detail['started_at'] or '-'}")
        lines.append(f"- ended_at: {task_detail['ended_at'] or '-'}")
        lines.append(f"- duration_seconds: {task_detail['duration_seconds']}")
        lines.append(f"- event_count: {task_detail['event_count']}")
        lines.append(f"- block_count: {task_detail['block_count']}")
        lines.append(f"- request_help_count: {task_detail['request_help_count']}")
        lines.append(f"- review_started: {task_detail['review_started']}")
        if task_detail["failure_reason"]:
            lines.append(f"- failure_reason: {task_detail['failure_reason']}")
        if task_detail["replay_path"]:
            lines.append(f"- replay_path: {task_detail['replay_path']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize AgentPilot task logs.")
    parser.add_argument("--event-log", default=DEFAULT_EVENT_LOG, help="Path to logs/agent-events.jsonl")
    parser.add_argument("--replay-dir", default=DEFAULT_REPLAY_DIR, help="Path to logs/replay/")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent tasks to show")
    parser.add_argument("--task-id", default="", help="Show details for a specific task id")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of plain text")
    args = parser.parse_args()

    events = load_events(args.event_log)
    report = build_report(events, args.replay_dir, limit=args.limit)

    task_detail = None
    if args.task_id:
        grouped = group_events_by_task(events)
        task_events = grouped.get(args.task_id, [])
        if not task_events:
            print(f"Task not found: {args.task_id}")
            return 1
        task_detail = summarize_task(args.task_id, task_events, args.replay_dir)

    if args.json:
        output = dict(report)
        if task_detail:
            output["task_detail"] = task_detail
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, task_detail=task_detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
