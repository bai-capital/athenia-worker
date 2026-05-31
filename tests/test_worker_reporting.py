from __future__ import annotations

import threading
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from athenia_worker.client import AtheniaClient
from athenia_worker.config import WorkerConfig, default_available_models
from athenia_worker.codex_sessions import discover_codex_sessions
from athenia_worker.codex_runner import CodexResult, CodexRunError, CodexRunner
from athenia_worker.main import handle_next_task


def test_default_available_models_prefers_env(monkeypatch):
    monkeypatch.setenv("ATHENIA_CODEX_MODELS", "gpt-5.5,gpt-5-codex")

    assert default_available_models(["codex", "exec"]) == ["gpt-5.5", "gpt-5-codex"]


def test_default_available_models_uses_codex_catalog(monkeypatch):
    monkeypatch.delenv("ATHENIA_CODEX_MODELS", raising=False)
    monkeypatch.delenv("ATHENIA_AVAILABLE_MODELS", raising=False)
    monkeypatch.setattr(
        "athenia_worker.config.discover_codex_models",
        lambda command: ["gpt-5.5", "gpt-5-codex"],
    )

    assert default_available_models(["codex", "exec"]) == ["gpt-5.5", "gpt-5-codex"]


def test_load_or_create_reports_available_models_from_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("ATHENIA_CODEX_MODELS", raising=False)
    monkeypatch.delenv("ATHENIA_AVAILABLE_MODELS", raising=False)

    config = WorkerConfig.load_or_create(
        tmp_path / "worker.json",
        codex_command=["codex", "exec"],
        available_models=["gpt-5.5,gpt-5-codex"],
    )

    assert config.available_models == ["gpt-5.5", "gpt-5-codex"]


def test_client_reports_available_models_on_bootstrap_and_heartbeat():
    config = WorkerConfig(
        server_url="https://api.example",
        worker_id="worker-test-01",
        pairing_secret="pairing-secret",
        worker_token="worker-token",
        available_models=["gpt-5.5", "gpt-5-codex"],
    )
    calls: list[dict[str, object]] = []

    class CapturingClient(AtheniaClient):
        def _request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"method": method, "path": path, **kwargs})
            return {}

    with CapturingClient(config) as client:
        client.bootstrap()
        client.heartbeat()

    assert calls[0]["path"] == "/v1/worker-runtime/bootstrap"
    assert calls[0]["json"]["available_models"] == ["gpt-5.5", "gpt-5-codex"]  # type: ignore[index]
    assert calls[1]["path"] == "/v1/worker-runtime/heartbeat"
    assert calls[1]["json"]["available_models"] == ["gpt-5.5", "gpt-5-codex"]  # type: ignore[index]


def test_client_syncs_codex_session_catalog():
    config = WorkerConfig(
        server_url="https://api.example",
        worker_id="worker-test-01",
        pairing_secret="pairing-secret",
        worker_token="worker-token",
    )
    calls: list[dict[str, object]] = []

    class CapturingClient(AtheniaClient):
        def _request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"method": method, "path": path, **kwargs})
            return [{"id": "catalog-1"}]

    with CapturingClient(config) as client:
        assert client.sync_codex_sessions([{"codex_session_id": "codex-1"}]) == [{"id": "catalog-1"}]

    assert calls[0]["path"] == "/v1/worker-runtime/codex-sessions"
    assert calls[0]["auth"] is True
    assert calls[0]["json"] == {"sessions": [{"codex_session_id": "codex-1"}]}


def test_discovers_codex_sessions_from_index_and_rollout(tmp_path):
    codex_home = tmp_path / ".codex"
    rollout_dir = codex_home / "sessions" / "2026" / "05" / "31"
    rollout_dir.mkdir(parents=True)
    (codex_home / "session_index.jsonl").write_text(
        '{"id":"019e74bd-ced8-7812-8f0d-3f238d5f2747","thread_name":"Ghost trader","updated_at":"2026-05-31T02:00:00Z"}\n',
        encoding="utf-8",
    )
    (rollout_dir / "rollout-2026-05-31T10-00-00-019e74bd-ced8-7812-8f0d-3f238d5f2747.jsonl").write_text(
        "\n".join(
            [
                '{"timestamp":"2026-05-31T02:00:01Z","type":"session_meta","payload":{"cwd":"/tmp/ghost-trader","originator":"Codex CLI","model":"gpt-5-codex"}}',
                '{"timestamp":"2026-05-31T02:00:02Z","type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"Analyze exp70 and later."}]}}',
                '{"timestamp":"2026-05-31T02:00:03Z","type":"response_item","payload":{"role":"assistant","content":[{"type":"output_text","text":"Done"}]}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = discover_codex_sessions(codex_home)

    assert sessions == [
        {
            "codex_session_id": "019e74bd-ced8-7812-8f0d-3f238d5f2747",
            "title": "Ghost trader",
            "last_activity_at": "2026-05-31T02:00:03Z",
            "source": "Codex CLI",
            "metadata": {
                "indexed": True,
                "rollout_path": str(rollout_dir / "rollout-2026-05-31T10-00-00-019e74bd-ced8-7812-8f0d-3f238d5f2747.jsonl"),
            },
            "cwd": "/tmp/ghost-trader",
            "model": "gpt-5-codex",
            "preview": "Analyze exp70 and later.",
            "message_count": 2,
        }
    ]


def test_pairing_payload_targets_exact_worker_for_binding():
    config = WorkerConfig(
        server_url="https://api.example",
        worker_id="worker-test-01",
        pairing_secret="pairing-secret",
        worker_token="worker-token",
    )

    payload = AtheniaClient(config).pairing_payload.to_url()
    parsed = urlparse(payload)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "athenia-worker"
    assert parsed.netloc == "pair"
    assert query == {
        "worker_id": ["worker-test-01"],
        "secret": ["pairing-secret"],
        "server": ["https://api.example"],
    }


def test_codex_exec_reads_prompt_from_stdin_not_command_args(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class CapturingStdin:
        def __init__(self) -> None:
            self.text = ""
            self.closed = False

        def write(self, text: str) -> int:
            self.text += text
            return len(text)

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self, command, **kwargs):  # type: ignore[no-untyped-def]
            self.stdin = CapturingStdin()
            self.stdout = iter(
                [
                    '{"type":"thread.started","thread_id":"thread-1"}\n',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
                ]
            )
            self.stderr = iter([])
            captured["command"] = command
            captured["kwargs"] = kwargs
            captured["stdin"] = self.stdin

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            return 0

        def kill(self):  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    config = WorkerConfig(
        workspace=str(tmp_path),
        codex_command=["codex", "exec", "--skip-git-repo-check"],
        available_models=["gpt-test"],
    )
    result = CodexRunner(config).run("hello from task")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[-1] == "-"
    assert "hello from task" not in command
    stdin = captured["stdin"]
    assert isinstance(stdin, CapturingStdin)
    assert "hello from task" in stdin.text
    assert stdin.closed is True
    assert result.content == "done"


def test_client_retries_transient_http_failures(monkeypatch):
    config = WorkerConfig(
        server_url="https://api.example",
        worker_id="worker-test-01",
        pairing_secret="pairing-secret",
        worker_token="worker-token",
    )
    attempts = 0

    class FlakyTransport:
        def request(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            request = httpx.Request(method, f"https://api.example{path}")
            if attempts == 1:
                raise httpx.ConnectError("temporary network failure", request=request)
            return httpx.Response(200, json={"ok": True}, request=request)

        def close(self):  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr("athenia_worker.client.time.sleep", lambda _: None)

    client = AtheniaClient(config, max_attempts=2)
    client._client = FlakyTransport()  # type: ignore[assignment]

    assert client.heartbeat() == {"ok": True}
    assert attempts == 2


def test_client_does_not_retry_auth_failures(monkeypatch):
    config = WorkerConfig(
        server_url="https://api.example",
        worker_id="worker-test-01",
        pairing_secret="pairing-secret",
        worker_token="worker-token",
    )
    attempts = 0

    class AuthFailureTransport:
        def request(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            request = httpx.Request(method, f"https://api.example{path}")
            return httpx.Response(403, json={"detail": "revoked"}, request=request)

        def close(self):  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr("athenia_worker.client.time.sleep", lambda _: None)

    client = AtheniaClient(config, max_attempts=3)
    client._client = AuthFailureTransport()  # type: ignore[assignment]

    try:
        client.heartbeat()
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("Expected HTTPStatusError")
    assert attempts == 1


def test_handle_next_task_does_not_fail_completed_task_when_completion_log_fails(tmp_path):
    config = WorkerConfig(workspace=str(tmp_path))
    client = ReportingClient(
        task={"id": "task-1", "input_text": "hello", "local_session_id": "chat-1"},
        fail_log_message="Worker task completed.",
    )
    runner = SuccessfulRunner(tmp_path)

    assert handle_next_task(client, runner, config, tmp_path / "worker.json", threading.Lock()) is True

    completions = [call for call in client.calls if call[0] == "complete_task"]
    assert completions == [("complete_task", "task-1", "completed", "done", None)]


def test_handle_next_task_keeps_failure_path_best_effort_when_failure_report_fails(tmp_path):
    config = WorkerConfig(workspace=str(tmp_path))
    client = ReportingClient(
        task={"id": "task-1", "input_text": "hello"},
        fail_complete_status="failed",
    )
    runner = FailingRunner(tmp_path)

    assert handle_next_task(client, runner, config, tmp_path / "worker.json", threading.Lock()) is True

    assert ("complete_task", "task-1", "failed", None, "codex exploded") in client.calls
    assert ("log", "error", "Worker task failed.") in client.calls


class ReportingClient:
    def __init__(
        self,
        *,
        task: dict[str, object],
        fail_log_message: str | None = None,
        fail_complete_status: str | None = None,
    ) -> None:
        self.task = task
        self.fail_log_message = fail_log_message
        self.fail_complete_status = fail_complete_status
        self.calls: list[tuple[object, ...]] = []

    def next_task(self) -> dict[str, object] | None:
        self.calls.append(("next_task",))
        return self.task

    def send_frame(self, task_id: str, payload: bytes, content_type: str, *, mode: str):  # type: ignore[no-untyped-def]
        self.calls.append(("send_frame", task_id, payload.decode("utf-8"), content_type, mode))
        return {}

    def complete_task(
        self,
        task_id: str,
        *,
        status: str,
        result_text: str | None = None,
        error: str | None = None,
    ):  # type: ignore[no-untyped-def]
        self.calls.append(("complete_task", task_id, status, result_text, error))
        if status == self.fail_complete_status:
            raise RuntimeError("complete failed")
        return {}

    def log(self, level: str, message: str, metadata: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(("log", level, message))
        if message == self.fail_log_message:
            raise RuntimeError("log failed")
        return {}

    def upload_artifact(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("upload_artifact", args, kwargs))
        return {}


class SuccessfulRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def workspace_from_runtime(self, runtime_config: dict[str, object]) -> Path:
        return self.workspace

    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return CodexResult(content="done", stdout="", stderr="", streamed_frames=0, codex_session_id="codex-1")


class FailingRunner(SuccessfulRunner):
    def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise CodexRunError("codex exploded")
