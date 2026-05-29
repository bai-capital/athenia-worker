# Athenia Worker

Private execution worker for Athenia agentic tasks.

The worker runs on a user's machine, registers a one-time pairing secret with the
Athenia backend, prints a QR payload, then keeps a WebSocket runtime connection
open for tasks assigned to its paired worker identity. Tasks are executed through
the local Codex CLI in the configured workspace, and results are relayed back to
Athenia over the existing authenticated HTTP reporting endpoints.

While tasks run, the worker prints local lifecycle logs and streams Codex JSON
events back to Athenia as text frames. The backend can use those frames to update
the in-chat assistant message before final task completion.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Pair And Run

```bash
athenia-worker init --server https://api.athenia.cc --workspace ~/workspace/project
athenia-worker serve
```

If worker tasks need to make network requests or run commands that the default
Codex workspace sandbox blocks, choose a broader permission level:

```bash
athenia-worker init --permission-level danger-full-access
athenia-worker serve
```

Available permission levels are:

- `read-only`: read files only.
- `workspace-write`: read and write inside the configured workspace. This is the
  default.
- `danger-full-access`: run without workspace sandbox restrictions. Use this only
  for workers and workspaces you trust.

`init` creates `~/.athenia-worker/config.json`, bootstraps the worker with the
server, and prints an `athenia-worker://pair?...` QR payload. In Athenia, open
Worker management and bind using that payload.

If the worker was revoked or the saved runtime token is no longer accepted,
`init` generates a fresh local worker identity and prints a new pairing payload.
You can force that behavior with `athenia-worker init --reset`.

## Security Model

- The pairing secret is high entropy, one-time, and server-side hashed.
- The runtime token is separate from the QR secret and is never shown in the QR
  payload.
- The backend authenticates runtime calls with `Authorization: Bearer <token>`.
- Revoking a worker invalidates future task polling and detaches it from chats.
- Local execution is scoped by the configured workspace and the Codex CLI's own
  permission model.

## Commands

```bash
athenia-worker init
athenia-worker init --reset
athenia-worker pairing-payload
athenia-worker serve
athenia-worker run-once
```

Configuration can also be set with environment variables:

- `ATHENIA_SERVER_URL`
- `ATHENIA_WORKER_NAME`
- `ATHENIA_WORKSPACE`
- `ATHENIA_CODEX_PERMISSION_LEVEL`
- `ATHENIA_CODEX_COMMAND`
- `ATHENIA_CODEX_MODELS`
- `ATHENIA_WORKER_MAX_CONCURRENCY`
- `ATHENIA_WORKER_TRANSPORT` (`websocket` by default, or `poll` for the legacy
  HTTP polling loop)
- `ATHENIA_WORKER_CONFIG`

By default the worker runs
`codex exec --sandbox workspace-write --skip-git-repo-check` with low reasoning
effort so a private workspace does not need to be a Git repository and short
worker commands return promptly. Set `ATHENIA_CODEX_PERMISSION_LEVEL` or pass
`--permission-level` to choose `read-only`, `workspace-write`, or
`danger-full-access`.

The worker reports available Codex model slugs to Athenia during bootstrap and
heartbeat. By default it reads the local Codex catalog with `codex debug models`.
To pin the app picker to a specific set, pass `--available-model` one or more
times or set `ATHENIA_CODEX_MODELS` to comma-separated slugs:

```bash
athenia-worker init --available-model gpt-5.5 --available-model gpt-5-codex
ATHENIA_CODEX_MODELS=gpt-5.5,gpt-5-codex athenia-worker serve
```

The worker stores a local mapping from Athenia chat session IDs to Codex session
IDs in `~/.athenia-worker/config.json`, then uses `codex exec resume` for later
messages in the same chat.

## Concurrent Tasks

By default one worker runs one Codex task at a time. To let one worker run
several Athenia chats in parallel, start it with `--max-concurrency`:

```bash
athenia-worker serve --max-concurrency 3
```

or set:

```bash
ATHENIA_WORKER_MAX_CONCURRENCY=3 athenia-worker serve
```

Use separate working directories for parallel sessions whenever possible.
Concurrent Codex tasks in the same directory can still edit the same files and
create normal source-control conflicts.

## Per-Chat Sessions

One worker can serve multiple Athenia chats. Each attached chat gets its own
local session ID, and the worker maps that ID to the Codex thread ID after the
first successful run. Later messages in the same Athenia chat resume the same
Codex session.

Athenia can send per-chat runtime settings with each task:

- `working_dir`: task working directory passed to Codex with `--cd`.
- `permission_level`: `read-only`, `workspace-write`, or `danger-full-access`.
  The worker clamps this to the maximum permission configured when the worker was
  started.
- `codex_model`: Codex CLI model passed with `--model`.
- `reasoning_effort`: `minimal`, `low`, `medium`, or `high`, passed as
  `model_reasoning_effort`.

The worker only accepts per-chat working directories under its configured
`resource_permissions.roots`, unless the worker itself is configured for
`danger-full-access`.

If a chat's runtime settings change after a Codex thread has already been
created, the worker starts a fresh Codex thread for that Athenia chat. This
avoids resuming a thread with stale sandbox or working-directory settings.

## Artifacts

When a task creates useful output files, write only final user-facing deliverables
inside `athenia_artifacts/` in the configured workspace. After Codex finishes,
the worker uploads changed files from that directory as attachments on the
assistant message. The app can then render and open those files.

Do not put helper scripts, source code, notebooks, caches, logs, or intermediate
files in `athenia_artifacts/` unless the user explicitly asks to receive those
files. If Codex does not use `athenia_artifacts/`, the worker falls back to a
small allowlist of result file extensions such as CSV, PDF, images, text, JSON,
and archives while skipping common source-code extensions.

This keeps the Athenia runtime token inside the worker process instead of
exposing upload credentials to Codex. A future MCP or function-call interface can
wrap the same backend endpoint, but the current contract is intentionally simple:
produce files in the workspace and let the worker relay them.

Artifact limits are configured in `~/.athenia-worker/config.json`:

- `artifact_max_files`: maximum changed files uploaded per task.
- `artifact_max_bytes`: maximum size for each uploaded file.
- `artifact_output_dir`: preferred directory for final deliverables.
- `artifact_result_extensions`: fallback result-file allowlist.
- `artifact_code_extensions`: fallback source-code denylist.
- `artifact_exclude_dirs`: workspace directories ignored by artifact scanning.
