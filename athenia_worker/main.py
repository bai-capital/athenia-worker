from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import shlex
import sys
import subprocess
import time
from pathlib import Path

import httpx
import qrcode

from .client import AtheniaClient
from .codex_runner import CodexRunError, CodexRunner, runtime_config_fingerprint
from .config import DEFAULT_CONFIG_PATH, WorkerConfig


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except httpx.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="athenia-worker")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    subparsers = parser.add_subparsers(required=True)

    init_parser = subparsers.add_parser("init")
    add_config_options(init_parser)
    init_parser.add_argument("--reset", action="store_true", help="Generate a new worker identity before bootstrapping.")
    init_parser.set_defaults(func=cmd_init)

    payload_parser = subparsers.add_parser("pairing-payload")
    payload_parser.set_defaults(func=cmd_pairing_payload)

    serve_parser = subparsers.add_parser("serve")
    add_config_options(serve_parser)
    serve_parser.add_argument("--run-once", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    run_once_parser = subparsers.add_parser("run-once")
    add_config_options(run_once_parser)
    run_once_parser.set_defaults(func=cmd_run_once)

    return parser


def add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", dest="server_url")
    parser.add_argument("--name")
    parser.add_argument("--workspace")
    parser.add_argument(
        "--permission-level",
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Codex sandbox permission level for worker tasks.",
    )
    parser.add_argument("--codex-command")
    parser.add_argument(
        "--available-model",
        action="append",
        dest="available_models",
        help="Codex model slug to report to Athenia. Repeat or pass comma-separated values.",
    )


def load_or_create_from_args(args: argparse.Namespace) -> WorkerConfig:
    codex_command = shlex.split(args.codex_command) if getattr(args, "codex_command", None) else None
    return WorkerConfig.load_or_create(
        args.config,
        server_url=getattr(args, "server_url", None),
        name=getattr(args, "name", None),
        workspace=getattr(args, "workspace", None),
        codex_permission_level=getattr(args, "permission_level", None),
        codex_command=codex_command,
        available_models=getattr(args, "available_models", None),
    )


def cmd_init(args: argparse.Namespace) -> int:
    config = load_or_create_from_args(args)
    if args.reset:
        config.rotate_identity()
        config.save(args.config)

    with AtheniaClient(config) as client:
        try:
            bootstrap = client.bootstrap()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            worker = None
            try:
                worker = client.heartbeat()
            except httpx.HTTPStatusError as heartbeat_exc:
                if heartbeat_exc.response.status_code not in {401, 403}:
                    raise

            if worker is None or worker.get("id") != config.worker_id or worker.get("status") == "revoked":
                config.rotate_identity()
                config.save(args.config)
                bootstrap = client.bootstrap()
                print("Previous worker identity is no longer usable; generated a new pairing identity.")
                print(f"Worker: {bootstrap['worker_id']}")
                print(f"Status: {bootstrap['status']}")
                print(f"Pairing expires at: {bootstrap['pairing_expires_at']}")
                print_pairing_payload(client)
                return 0

            print(f"Worker: {worker['id']}")
            print(f"Status: {worker['status']}")
            print("Already registered with this runtime token.")
            return 0
        print(f"Worker: {bootstrap['worker_id']}")
        print(f"Status: {bootstrap['status']}")
        print(f"Pairing expires at: {bootstrap['pairing_expires_at']}")
        print_pairing_payload(client)
    return 0


def cmd_pairing_payload(args: argparse.Namespace) -> int:
    config = WorkerConfig.load(args.config)
    with AtheniaClient(config) as client:
        print_pairing_payload(client)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config = load_or_create_from_args(args)
    with AtheniaClient(config) as client:
        try_bootstrap(client)
        emit_worker_log(client, "info", "Worker started.", {"workspace": config.workspace})
        runner = CodexRunner(config)
        if not args.run_once and config.transport == "websocket":
            return websocket_loop(client, runner, config, args.config)
        return polling_loop(client, runner, config, args.config, run_once=args.run_once)


def cmd_run_once(args: argparse.Namespace) -> int:
    args.run_once = True
    return cmd_serve(args)


def polling_loop(
    client: AtheniaClient,
    runner: CodexRunner,
    config: WorkerConfig,
    config_path: Path,
    *,
    run_once: bool,
) -> int:
    consecutive_errors = 0

    while True:
        try:
            handled = handle_next_task(client, runner, config, config_path)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            emit_local_log(
                "error",
                "Worker loop failed while contacting Athenia.",
                {"error": str(exc) or exc.__class__.__name__, "consecutive_errors": consecutive_errors},
            )
            if run_once:
                return 1
            time.sleep(min(config.poll_interval_seconds * (2 ** min(consecutive_errors - 1, 5)), 60.0))
            continue
        if run_once:
            return 0 if handled else 2
        if not handled:
            time.sleep(config.poll_interval_seconds)


def websocket_loop(
    client: AtheniaClient,
    runner: CodexRunner,
    config: WorkerConfig,
    config_path: Path,
) -> int:
    consecutive_errors = 0
    while True:
        try:
            asyncio.run(run_websocket_session(client, runner, config, config_path))
            consecutive_errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            consecutive_errors += 1
            emit_local_log(
                "warning",
                "Worker WebSocket session ended; reconnecting.",
                {"error": str(exc) or exc.__class__.__name__, "consecutive_errors": consecutive_errors},
            )
        time.sleep(min(config.websocket_reconnect_seconds * (2 ** min(consecutive_errors, 5)), 60.0))


async def run_websocket_session(
    client: AtheniaClient,
    runner: CodexRunner,
    config: WorkerConfig,
    config_path: Path,
) -> None:
    import websockets

    async with websockets.connect(
        client.websocket_url,
        additional_headers={"Authorization": f"Bearer {config.worker_token}"},
        ping_interval=30,
        ping_timeout=30,
    ) as websocket:
        emit_local_log("info", "Connected to Athenia worker runtime WebSocket.", {"url": client.websocket_url})
        await websocket.send(json.dumps({"type": "worker.hello", "available_models": config.available_models}))

        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                emit_local_log("warning", "Ignored non-JSON WebSocket message.")
                continue
            if not isinstance(message, dict):
                emit_local_log("warning", "Ignored non-object WebSocket message.")
                continue

            message_type = str(message.get("type") or "")
            if message_type == "task.assigned":
                task = message.get("task")
                if isinstance(task, dict):
                    handle_task(client, runner, config, config_path, task)
                    await websocket.send(json.dumps({"type": "task.ready"}))
                continue

            if message_type in {"worker.connected", "worker.ack"}:
                worker = message.get("worker") if isinstance(message.get("worker"), dict) else {}
                emit_local_log(
                    "info",
                    "Worker runtime acknowledged.",
                    {"status": worker.get("status"), "worker_id": worker.get("id")},
                )
                continue

            if message_type == "error":
                emit_local_log("warning", "Athenia worker runtime returned an error.", {"detail": message.get("detail")})
                continue

            emit_local_log("debug", "Ignored WebSocket message.", {"type": message_type})


def try_bootstrap(client: AtheniaClient) -> None:
    try:
        bootstrap = client.bootstrap()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        try:
            worker = client.heartbeat()
        except httpx.HTTPStatusError as heartbeat_exc:
            if heartbeat_exc.response.status_code in {401, 403}:
                raise RuntimeError(
                    "Worker identity is invalid or revoked. Run `athenia-worker init --reset` to pair a new worker."
                ) from heartbeat_exc
            raise
        if worker.get("status") == "revoked":
            raise RuntimeError(
                "Worker identity is revoked. Run `athenia-worker init --reset` to pair a new worker."
            )
        return

    if bootstrap.get("status") == "pending":
        print_pairing_payload(client)


def handle_next_task(
    client: AtheniaClient,
    runner: CodexRunner,
    config: WorkerConfig,
    config_path: Path,
) -> bool:
    task = client.next_task()
    if not task:
        return False
    handle_task(client, runner, config, config_path, task)
    return True


def handle_task(
    client: AtheniaClient,
    runner: CodexRunner,
    config: WorkerConfig,
    config_path: Path,
    task: dict,
) -> None:

    task_id = str(task["id"])
    task_completed = False
    try:
        local_session_id = task.get("local_session_id")
        runtime_config = task.get("runtime_config") if isinstance(task.get("runtime_config"), dict) else {}
        input_text = str(task["input_text"])
        emit_worker_log(
            client,
            "info",
            "Worker task started.",
            {
                "task_id": task_id,
                "local_session_id": local_session_id,
                "runtime_config": runtime_config,
                "input_preview": input_text[:160],
            },
        )

        def send_output_frame(text: str, mode: str) -> None:
            payload = text.encode("utf-8")
            client.send_frame(task_id, payload, "text/plain; charset=utf-8", mode=mode)
            emit_local_log(
                "info",
                "Worker streamed frame.",
                {"task_id": task_id, "bytes": len(payload), "mode": mode},
            )

        def log_codex_event(event_type: str, metadata: dict[str, object]) -> None:
            emit_local_log("info", f"Codex event: {event_type}", metadata)

        workspace = runner.workspace_from_runtime(runtime_config)
        before_artifacts = snapshot_workspace(workspace, config)
        result = runner.run(
            input_text,
            local_session_id=local_session_id,
            runtime_config=runtime_config,
            on_output=send_output_frame,
            on_event=log_codex_event,
        )
        streamed_frames = result.streamed_frames
        if result.streamed_frames == 0:
            send_output_frame(result.content, "replace")
            streamed_frames = 1
        uploaded_artifacts = upload_changed_artifacts(
            client,
            task_id,
            workspace,
            before_artifacts,
            config,
        )
        client.complete_task(task_id, status="completed", result_text=result.content)
        task_completed = True
        if local_session_id and result.codex_session_id:
            try:
                config.codex_sessions[str(local_session_id)] = result.codex_session_id
                config.codex_session_runtime_configs[str(local_session_id)] = runtime_config_fingerprint(runtime_config)
                config.save(config_path)
                emit_local_log(
                    "info",
                    "Saved Codex session mapping.",
                    {"local_session_id": local_session_id, "codex_session_id": result.codex_session_id},
                )
            except Exception as exc:
                emit_local_log(
                    "warning",
                    "Task completed, but failed to save Codex session mapping.",
                    {"task_id": task_id, "error": str(exc) or exc.__class__.__name__},
                )
        safe_emit_worker_log(
            client,
            "info",
            "Worker task completed.",
            {
                "task_id": task_id,
                "streamed_frames": streamed_frames,
                "uploaded_artifacts": uploaded_artifacts,
                "result_chars": len(result.content),
            },
        )
    except (CodexRunError, subprocess.TimeoutExpired) as exc:
        message = str(exc)
        if not task_completed:
            complete_task_failed(client, task_id, message)
        safe_emit_worker_log(client, "error", "Worker task failed.", {"task_id": task_id, "error": message})
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if not task_completed:
            complete_task_failed(client, task_id, message)
        safe_emit_worker_log(client, "error", "Worker task failed.", {"task_id": task_id, "error": message})


ArtifactSnapshot = dict[str, tuple[int, int]]


def snapshot_workspace(workspace: Path, config: WorkerConfig) -> ArtifactSnapshot:
    snapshot: ArtifactSnapshot = {}
    for path in iter_artifact_candidates(workspace, config):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.relative_to(workspace))] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def upload_changed_artifacts(
    client: AtheniaClient,
    task_id: str,
    workspace: Path,
    before: ArtifactSnapshot,
    config: WorkerConfig,
) -> int:
    changed: list[Path] = []
    for path in iter_artifact_candidates(workspace, config):
        try:
            stat = path.stat()
            relative_path = str(path.relative_to(workspace))
        except OSError:
            continue
        if stat.st_size <= 0 or stat.st_size > config.artifact_max_bytes:
            continue
        if before.get(relative_path) != (stat.st_size, stat.st_mtime_ns):
            changed.append(path)

    artifact_root = (workspace / config.artifact_output_dir).resolve()
    explicit_artifacts = [path for path in changed if is_relative_to(path, artifact_root)]
    upload_paths = explicit_artifacts or [path for path in changed if is_result_artifact(path, config)]
    upload_paths = sorted(upload_paths, key=mtime_ns)[: config.artifact_max_files]

    uploaded = 0
    for path in upload_paths:
        relative_path = artifact_relative_path(path, workspace, artifact_root)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            client.upload_artifact(
                task_id,
                path,
                relative_path=relative_path,
                content_type=content_type,
            )
        except Exception as exc:
            emit_local_log(
                "warning",
                "Failed to upload artifact.",
                {"task_id": task_id, "path": relative_path, "error": str(exc)},
            )
            continue
        uploaded += 1
        emit_local_log(
            "info",
            "Uploaded artifact.",
            {"task_id": task_id, "path": relative_path, "content_type": content_type},
        )
    return uploaded


def artifact_relative_path(path: Path, workspace: Path, artifact_root: Path) -> str:
    if is_relative_to(path, artifact_root):
        return str(path.relative_to(artifact_root))
    return str(path.relative_to(workspace))


def is_result_artifact(path: Path, config: WorkerConfig) -> bool:
    suffix = path.suffix.lower()
    code_extensions = {extension.lower() for extension in config.artifact_code_extensions}
    result_extensions = {extension.lower() for extension in config.artifact_result_extensions}
    if suffix in code_extensions:
        return False
    return suffix in result_extensions


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def iter_artifact_candidates(workspace: Path, config: WorkerConfig):
    excluded = set(config.artifact_exclude_dirs)
    try:
        paths = workspace.rglob("*")
    except OSError:
        return

    for path in paths:
        parts = set(path.relative_to(workspace).parts)
        if parts & excluded:
            continue
        if path.name.startswith(".") or path.name == ".DS_Store":
            continue
        if not path.is_file():
            continue
        yield path


def emit_local_log(
    level: str,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    details = f" {metadata}" if metadata else ""
    print(f"{level.upper()}: {message}{details}", flush=True)


def emit_worker_log(
    client: AtheniaClient,
    level: str,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    emit_local_log(level, message, metadata)
    client.log(level, message, metadata)


def safe_emit_worker_log(
    client: AtheniaClient,
    level: str,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    try:
        emit_worker_log(client, level, message, metadata)
    except Exception as exc:
        emit_local_log(
            "warning",
            "Failed to send worker log.",
            {"original_message": message, "error": str(exc) or exc.__class__.__name__},
        )


def complete_task_failed(client: AtheniaClient, task_id: str, message: str) -> None:
    try:
        client.complete_task(task_id, status="failed", error=message)
    except Exception as exc:
        emit_local_log(
            "error",
            "Worker task failed, and failure reporting also failed.",
            {"task_id": task_id, "task_error": message, "report_error": str(exc) or exc.__class__.__name__},
        )


def print_pairing_payload(client: AtheniaClient) -> None:
    payload = client.pairing_payload.to_url()
    print(payload)
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


if __name__ == "__main__":
    raise SystemExit(main())
