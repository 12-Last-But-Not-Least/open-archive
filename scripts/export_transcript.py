"""Export user-facing messages from a saved Codex conversation."""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def export(source, destination):
    entries, seen = [], set()
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # An active conversation may still be writing its last record.
        payload = record.get("payload", {})
        item = payload.get("item", {})
        if (payload.get("type") != "item_completed"
                or item.get("type") not in ("UserMessage", "AgentMessage")
                or item.get("id") in seen or item.get("phase") in ("analysis", "summary")):
            continue
        seen.add(item.get("id"))
        role = "User" if item["type"] == "UserMessage" else "Assistant"
        parts = []
        for content in item.get("content", []):
            kind = content.get("type", "").lower()
            if kind == "text":
                parts.append(content.get("text", ""))
            elif "image" in kind:
                parts.append("[Image attachment; image contents are not included in this text export.]")
        text = "\n".join(parts).strip()
        for tag in ("environment_context", "in-app-browser-context", "external_codex_apps_open_page"):
            text = re.sub(r"<" + tag + r"\b[^>]*>.*?</" + tag + ">", "", text, flags=re.S)
        text = re.sub(r"<image\b[^>]*>.*?</image>", "[Image attachment]", text, flags=re.S)
        for home in (str(Path.home()), Path.home().as_posix()):
            text = text.replace(home, "[user directory]")
        text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b",
                      "[credential redacted]", text)
        if text.strip():
            phase = " (progress update)" if item.get("phase") == "commentary" else ""
            entries.append(f"{role}{phase} — {record.get('timestamp', '')}\n\n{text.strip()}")
    if not entries:
        raise ValueError("No user-facing messages found; existing transcript was left unchanged.")
    header = (
        "Open Archive — development conversation\n\n"
        "Exported from the saved chat history in chronological order; timestamps are UTC.\n"
        "Includes user messages, assistant replies, and progress updates describing actions.\n"
        "Internal instructions, reasoning, raw tool calls/results, and image files are omitted.\n"
        "Local user-directory prefixes and credential-shaped strings are redacted. Images are marked.\n"
        "Statements reflect what was said at the time, including plans, corrections, and later changes.\n"
        "Snapshot includes messages saved when the export ran; later replies appear in the next export.\n\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "\n\n--------------------\n\n".join(entries) + "\n", encoding="utf-8")
    print(f"Exported {len(entries)} messages to {destination.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", args.thread_id):
        parser.error("Expected a Codex thread UUID.")
    sources = list((Path.home() / ".codex" / "sessions").rglob(f"*{args.thread_id}.jsonl"))
    if len(sources) != 1:
        parser.error("Expected exactly one saved conversation for this thread.")
    export(sources[0], ROOT / "LLM Transcripts" / "conversation.txt")


if __name__ == "__main__":
    main()
