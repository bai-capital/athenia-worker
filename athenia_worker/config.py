from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import uuid


DEFAULT_CONFIG_PATH = Path(os.getenv("ATHENIA_WORKER_CONFIG", "~/.athenia-worker/config.json")).expanduser()
DEFAULT_CODEX_PERMISSION_LEVEL = "workspace-write"
CODEX_PERMISSION_LEVELS = ("read-only", "workspace-write", "danger-full-access")
CODEX_PERMISSION_RANK = {level: index for index, level in enumerate(CODEX_PERMISSION_LEVELS)}
CODEX_PERMISSION_ALIASES = {
    "readonly": "read-only",
    "read-only": "read-only",
    "read_only": "read-only",
    "restricted": "read-only",
    "workspace": "workspace-write",
    "workspace-write": "workspace-write",
    "workspace_write": "workspace-write",
    "write": "workspace-write",
    "full": "danger-full-access",
    "danger": "danger-full-access",
    "danger-full-access": "danger-full-access",
    "danger_full_access": "danger-full-access",
    "network": "danger-full-access",
}


def _default_name() -> str:
    return f"{socket.gethostname()} Codex Worker"


def _default_codex_command() -> list[str]:
    command = os.getenv("ATHENIA_CODEX_COMMAND")
    if command:
        return with_codex_permission_level(shlex.split(command), default_codex_permission_level())
    return with_codex_permission_level([
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-c",
        'model_reasoning_effort="low"',
    ], default_codex_permission_level())


def _split_model_list(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if values is None:
        return []
    raw_values = [values] if isinstance(values, str) else list(values)
    models: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in str(raw).replace("\n", ",").split(","):
            model = part.strip()
            if model and model not in seen:
                seen.add(model)
                models.append(model)
    return models


def _codex_binary(command: list[str] | None = None) -> str:
    if command and len(command) >= 2 and Path(command[0]).name == "codex" and command[1] == "exec":
        return command[0]
    return "codex"


def _configured_model_from_command(command: list[str]) -> str | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part in {"-m", "--model"} and index + 1 < len(command):
            return command[index + 1].strip() or None
        if part.startswith("--model="):
            return part.split("=", 1)[1].strip() or None
        if part in {"-c", "--config"} and index + 1 < len(command):
            value = command[index + 1]
            if value.startswith("model="):
                return value.split("=", 1)[1].strip().strip('"').strip("'") or None
            index += 2
            continue
        index += 1
    return None


def discover_codex_models(command: list[str] | None = None) -> list[str]:
    try:
        completed = subprocess.run(
            [_codex_binary(command), "debug", "models"],
            capture_output=True,
            check=False,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []

    slugs: list[str] = []
    seen: set[str] = set()
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        visibility = str(entry.get("visibility") or "")
        if not slug or slug in seen or visibility == "hidden":
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def default_available_models(command: list[str] | None = None) -> list[str]:
    env_models = _split_model_list(
        os.getenv("ATHENIA_CODEX_MODELS") or os.getenv("ATHENIA_AVAILABLE_MODELS")
    )
    if env_models:
        return env_models

    command = command or _default_codex_command()
    models = discover_codex_models(command)
    configured_model = _configured_model_from_command(command)
    if configured_model and configured_model not in models:
        models.insert(0, configured_model)
    return models


def default_codex_permission_level() -> str:
    return normalize_codex_permission_level(
        os.getenv("ATHENIA_CODEX_PERMISSION_LEVEL") or os.getenv("ATHENIA_CODEX_SANDBOX")
    )


def normalize_codex_permission_level(value: str | None) -> str:
    if value is None or not value.strip():
        return DEFAULT_CODEX_PERMISSION_LEVEL
    key = value.strip().lower().replace(" ", "-")
    if key in CODEX_PERMISSION_ALIASES:
        return CODEX_PERMISSION_ALIASES[key]
    valid = ", ".join(CODEX_PERMISSION_LEVELS)
    raise ValueError(f"Invalid Codex permission level {value!r}. Expected one of: {valid}.")


def with_codex_permission_level(command: list[str], permission_level: str) -> list[str]:
    if command[:2] != ["codex", "exec"]:
        return command

    normalized_permission = normalize_codex_permission_level(permission_level)
    cleaned: list[str] = command[:2]
    index = 2
    while index < len(command):
        part = command[index]
        if part in {"--sandbox", "-s"}:
            index += 2
            continue
        if part.startswith("--sandbox="):
            index += 1
            continue
        if part == "--dangerously-bypass-approvals-and-sandbox":
            index += 1
            continue
        cleaned.append(part)
        index += 1

    cleaned[2:2] = ["--sandbox", normalized_permission]
    return cleaned


def bounded_codex_permission_level(requested: str | None, maximum: str) -> str:
    maximum_level = normalize_codex_permission_level(maximum)
    if requested is None or not requested.strip():
        return maximum_level
    requested_level = normalize_codex_permission_level(requested)
    if CODEX_PERMISSION_RANK[requested_level] > CODEX_PERMISSION_RANK[maximum_level]:
        return maximum_level
    return requested_level


def default_resource_permissions(workspace: str, permission_level: str) -> dict[str, object]:
    normalized_permission = normalize_codex_permission_level(permission_level)
    return {
        "roots": [workspace],
        "shell": True,
        "codex_permission_level": normalized_permission,
        "network": normalized_permission == "danger-full-access",
    }


@dataclass
class WorkerConfig:
    server_url: str = field(default_factory=lambda: os.getenv("ATHENIA_SERVER_URL", "https://api.athenia.cc"))
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4()}")
    pairing_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    worker_token: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    worker_type: str = "codex-cli"
    name: str = field(default_factory=lambda: os.getenv("ATHENIA_WORKER_NAME", _default_name()))
    workspace: str = field(default_factory=lambda: os.getenv("ATHENIA_WORKSPACE", os.getcwd()))
    codex_permission_level: str = field(default_factory=default_codex_permission_level)
    codex_command: list[str] = field(default_factory=_default_codex_command)
    poll_interval_seconds: float = 2.0
    capabilities: list[str] = field(default_factory=lambda: ["shell", "python", "browser"])
    available_models: list[str] = field(default_factory=default_available_models)
    resource_permissions: dict[str, object] = field(default_factory=dict)
    codex_sessions: dict[str, str] = field(default_factory=dict)
    codex_session_runtime_configs: dict[str, str] = field(default_factory=dict)
    transport: str = field(default_factory=lambda: os.getenv("ATHENIA_WORKER_TRANSPORT", "websocket"))
    websocket_reconnect_seconds: float = 2.0
    artifact_output_dir: str = "athenia_artifacts"
    artifact_max_files: int = 20
    artifact_max_bytes: int = 25 * 1024 * 1024
    artifact_result_extensions: list[str] = field(
        default_factory=lambda: [
            ".csv",
            ".tsv",
            ".xlsx",
            ".xls",
            ".pdf",
            ".txt",
            ".md",
            ".json",
            ".jsonl",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".svg",
            ".html",
            ".xml",
            ".parquet",
            ".zip",
        ]
    )
    artifact_code_extensions: list[str] = field(
        default_factory=lambda: [
            ".py",
            ".ipynb",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".swift",
            ".java",
            ".kt",
            ".go",
            ".rs",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".sh",
            ".bash",
            ".zsh",
            ".ps1",
            ".sql",
        ]
    )
    artifact_exclude_dirs: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
        ]
    )

    def rotate_identity(self) -> None:
        self.worker_id = f"worker-{uuid.uuid4()}"
        self.pairing_secret = secrets.token_urlsafe(32)
        self.worker_token = secrets.token_urlsafe(48)

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> WorkerConfig:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(**data)

    @classmethod
    def load_or_create(
        cls,
        path: Path = DEFAULT_CONFIG_PATH,
        *,
        server_url: str | None = None,
        name: str | None = None,
        workspace: str | None = None,
        codex_permission_level: str | None = None,
        codex_command: list[str] | None = None,
        available_models: list[str] | None = None,
    ) -> WorkerConfig:
        if path.exists():
            config = cls.load(path)
        else:
            config = cls()

        if server_url:
            config.server_url = server_url.rstrip("/")
        else:
            config.server_url = config.server_url.rstrip("/")
        if name:
            config.name = name
        workspace_changed = False
        if workspace:
            config.workspace = str(Path(workspace).expanduser())
            workspace_changed = True
        if codex_permission_level:
            config.codex_permission_level = normalize_codex_permission_level(codex_permission_level)
        else:
            config.codex_permission_level = normalize_codex_permission_level(config.codex_permission_level)
        if codex_command:
            config.codex_command = codex_command
        elif config.codex_command in (
            ["codex", "exec"],
            ["codex", "exec", "--skip-git-repo-check"],
        ):
            config.codex_command = _default_codex_command()
        config.codex_command = with_codex_permission_level(
            config.codex_command,
            config.codex_permission_level,
        )
        if available_models:
            config.available_models = _split_model_list(available_models)
        else:
            env_models = _split_model_list(
                os.getenv("ATHENIA_CODEX_MODELS") or os.getenv("ATHENIA_AVAILABLE_MODELS")
            )
            if env_models:
                config.available_models = env_models
            elif not config.available_models:
                config.available_models = default_available_models(config.codex_command)
        if not config.resource_permissions:
            config.resource_permissions = default_resource_permissions(
                config.workspace,
                config.codex_permission_level,
            )
        else:
            config.resource_permissions["codex_permission_level"] = config.codex_permission_level
            config.resource_permissions["network"] = config.codex_permission_level == "danger-full-access"
            if workspace_changed and isinstance(config.resource_permissions.get("roots"), list):
                config.resource_permissions["roots"] = [config.workspace]
        env_transport = os.getenv("ATHENIA_WORKER_TRANSPORT")
        if env_transport:
            config.transport = env_transport.strip().lower()
        if config.transport not in {"websocket", "poll"}:
            config.transport = "websocket"

        config.save(path)
        return config

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, indent=2, sort_keys=True)
            handle.write("\n")
        path.chmod(0o600)
