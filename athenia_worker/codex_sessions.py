from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def discover_codex_sessions(codex_home: Path | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    home = codex_home or Path.home() / ".codex"
    index_path = home / "session_index.jsonl"
    if not index_path.exists():
        return []

    rollout_paths = _rollout_paths_by_session_id(home)
    sessions: list[dict[str, Any]] = []
    for row in _read_jsonl(index_path):
        session_id = _clean_str(row.get("id"))
        if not session_id:
            continue
        item: dict[str, Any] = {
            "codex_session_id": session_id,
            "title": _clean_str(row.get("thread_name")) or "Codex Session",
            "last_activity_at": _clean_datetime(row.get("updated_at")),
            "source": "session_index",
            "metadata": {"indexed": True},
        }
        rollout_path = rollout_paths.get(session_id)
        if rollout_path is not None:
            item.update(_rollout_metadata(rollout_path))
            item.setdefault("metadata", {})["rollout_path"] = str(rollout_path)
        sessions.append(item)

    sessions.sort(key=lambda item: item.get("last_activity_at") or "", reverse=True)
    return sessions[: max(1, limit)]


def _rollout_paths_by_session_id(codex_home: Path) -> dict[str, Path]:
    output: dict[str, Path] = {}
    for root in (codex_home / "sessions", codex_home / "archived_sessions"):
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            session_id = _session_id_from_rollout_path(path)
            if session_id and session_id not in output:
                output[session_id] = path
    return output


def _session_id_from_rollout_path(path: Path) -> str | None:
    stem = path.stem
    if "-" not in stem:
        return None
    candidate = stem.rsplit("-", 5)
    if len(candidate) != 6:
        return None
    return "-".join(candidate[1:])


def _rollout_metadata(path: Path) -> dict[str, Any]:
    cwd: str | None = None
    model: str | None = None
    preview: str | None = None
    message_count = 0
    source = "rollout"
    last_activity_at: str | None = None

    for index, row in enumerate(_read_jsonl(path)):
        if index > 500:
            break
        timestamp = _clean_datetime(row.get("timestamp"))
        if timestamp:
            last_activity_at = timestamp
        row_type = _clean_str(row.get("type"))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row_type == "session_meta":
            cwd = _clean_str(payload.get("cwd")) or cwd
            model = _clean_str(payload.get("model")) or _clean_str(payload.get("model_slug")) or model
            source = _clean_str(payload.get("originator")) or source
            continue
        if row_type != "response_item":
            continue
        role = _clean_str(payload.get("role"))
        if role in {"user", "assistant"}:
            message_count += 1
        if preview is None and role == "user":
            preview = _message_text_preview(payload)

    return {
        "cwd": cwd,
        "model": model,
        "source": source,
        "preview": preview,
        "message_count": message_count,
        "last_activity_at": last_activity_at,
    }


def _message_text_preview(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = _clean_str(item.get("text"))
            if text:
                parts.append(text)
    elif isinstance(content, str):
        parts.append(content)
    text = " ".join(" ".join(parts).split())
    if not text:
        return None
    return text[:500]


def _read_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_datetime(value: object) -> str | None:
    text = _clean_str(value)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
