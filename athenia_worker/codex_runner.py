from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from .config import WorkerConfig, with_codex_permission_level


class CodexRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexResult:
    content: str
    stdout: str
    stderr: str
    streamed_frames: int = 0
    codex_session_id: str | None = None


class CodexRunner:
    def __init__(self, config: WorkerConfig, *, timeout_seconds: int = 60 * 60) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        prompt: str,
        *,
        local_session_id: str | None = None,
        on_output: Callable[[str, str], None] | None = None,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> CodexResult:
        workspace = Path(self.config.workspace).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        prompt_text = self._artifact_prompt(prompt)

        with tempfile.TemporaryDirectory(prefix="athenia-worker-") as tmpdir:
            output_path = Path(tmpdir) / "codex-last-message.txt"
            command = self._build_command(prompt_text, output_path, local_session_id=local_session_id)
            if self._uses_codex_exec(command):
                return self._run_codex_json(
                    command,
                    workspace=workspace,
                    output_path=output_path,
                    on_output=on_output,
                    on_event=on_event,
                )

            expects_last_message = self._expects_last_message(command)
            completed = subprocess.run(
                command,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            last_message = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()

        if completed.returncode != 0:
            detail = stderr or stdout or f"Codex exited with {completed.returncode}."
            raise CodexRunError(detail)
        if expects_last_message and not last_message:
            detail = stderr or stdout
            if detail:
                detail = detail[:2000]
                raise CodexRunError(f"Codex completed without a final message. Captured output: {detail}")
            raise CodexRunError("Codex completed without a final message.")

        content = last_message or stdout or stderr
        return CodexResult(content=content, stdout=stdout, stderr=stderr)

    def _run_codex_json(
        self,
        command: list[str],
        *,
        workspace: Path,
        output_path: Path,
        on_output: Callable[[str, str], None] | None,
        on_event: Callable[[str, dict[str, object]], None] | None,
    ) -> CodexResult:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        streamed_frames = 0
        final_message = ""
        codex_session_id: str | None = None

        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                stdout_lines.append(raw_line)
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if on_event:
                        on_event("codex.stdout", {"line": line[:500]})
                    continue

                event_type = str(event.get("type", "unknown"))
                if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
                    codex_session_id = str(event["thread_id"])
                if on_event:
                    on_event(event_type, self._event_metadata(event))

                text, mode = self._streamable_text(event)
                if text:
                    final_message = text if mode == "replace" else f"{final_message}{text}"
                    if on_output:
                        on_output(text, mode)
                        streamed_frames += 1
        except Exception:
            process.kill()
            raise

        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise exc
        stderr_thread.join(timeout=5)

        stdout = "".join(stdout_lines).strip()
        stderr = "".join(stderr_lines).strip()
        last_message = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        content = last_message or final_message

        if returncode != 0:
            detail = stderr or stdout or f"Codex exited with {returncode}."
            raise CodexRunError(detail)
        if not content:
            detail = stderr or stdout
            if detail:
                raise CodexRunError(f"Codex completed without a final message. Captured output: {detail[:2000]}")
            raise CodexRunError("Codex completed without a final message.")

        if last_message and last_message != final_message and on_output:
            on_output(last_message, "replace")
            streamed_frames += 1

        return CodexResult(
            content=content,
            stdout=stdout,
            stderr=stderr,
            streamed_frames=streamed_frames,
            codex_session_id=codex_session_id,
        )

    def _build_command(
        self,
        prompt: str,
        output_path: Path,
        *,
        local_session_id: str | None,
    ) -> list[str]:
        command = with_codex_permission_level(
            list(self.config.codex_command),
            self.config.codex_permission_level,
        )
        if command[:2] == ["codex", "exec"]:
            if "--json" not in command:
                command.append("--json")
            if "--output-last-message" not in command and "-o" not in command:
                command.extend(["--output-last-message", str(output_path)])
            codex_session_id = self._codex_session_id(local_session_id)
            if codex_session_id:
                command = self._resume_command(command, codex_session_id)
        command.append(prompt)
        return command

    def _codex_session_id(self, local_session_id: str | None) -> str | None:
        if not local_session_id:
            return None
        return self.config.codex_sessions.get(local_session_id)

    def _resume_command(self, command: list[str], codex_session_id: str) -> list[str]:
        resume = command[:2] + ["resume"]
        bypass_sandbox = False
        index = 2
        while index < len(command):
            part = command[index]
            if part in {"--sandbox", "-s"}:
                if index + 1 < len(command) and command[index + 1] == "danger-full-access":
                    bypass_sandbox = True
                index += 2
                continue
            if part == "--sandbox=danger-full-access":
                bypass_sandbox = True
                index += 1
                continue
            if part.startswith("--sandbox="):
                index += 1
                continue
            resume.append(part)
            if part in {"-c", "--config", "-m", "--model", "-p", "--profile", "--profile-v2"} and index + 1 < len(command):
                index += 1
                resume.append(command[index])
            index += 1
        if bypass_sandbox and "--dangerously-bypass-approvals-and-sandbox" not in resume:
            resume.insert(3, "--dangerously-bypass-approvals-and-sandbox")
        resume.append(codex_session_id)
        return resume

    def _expects_last_message(self, command: list[str]) -> bool:
        return command[:2] == ["codex", "exec"] and (
            "--output-last-message" in command or "-o" in command
        )

    def _uses_codex_exec(self, command: list[str]) -> bool:
        return command[:2] == ["codex", "exec"] and "--json" in command

    def _artifact_prompt(self, prompt: str) -> str:
        return (
            "Athenia artifact delivery: if you create useful output files for the user, write only "
            "the final user-facing deliverables into the `athenia_artifacts/` directory in the "
            "current workspace. Do not put helper scripts, source code, notebooks, caches, logs, or "
            "intermediate files there unless the user explicitly asks to receive those files. "
            "Athenia will upload files from `athenia_artifacts/` as downloadable chat attachments "
            "after the task.\n\n"
            f"{prompt}"
        )

    def _event_metadata(self, event: dict[str, object]) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if "thread_id" in event:
            metadata["thread_id"] = event["thread_id"]
        if "usage" in event:
            metadata["usage"] = event["usage"]
        item = event.get("item")
        if isinstance(item, dict):
            metadata["item_type"] = item.get("type", "unknown")
            if "id" in item:
                metadata["item_id"] = item["id"]
        return metadata

    def _streamable_text(self, event: dict[str, object]) -> tuple[str, str]:
        event_type = str(event.get("type", ""))
        if event_type.endswith(".delta"):
            delta = event.get("delta")
            if isinstance(delta, str):
                return delta, "append"
            if isinstance(delta, dict):
                text = delta.get("text") or delta.get("content")
                if isinstance(text, str):
                    return text, "append"

        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                return text, "replace"

        return "", "append"
