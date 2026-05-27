from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import WorkerConfig


@dataclass(frozen=True)
class PairingPayload:
    worker_id: str
    pairing_secret: str
    server_url: str

    def to_url(self) -> str:
        query = urlencode(
            {
                "worker_id": self.worker_id,
                "secret": self.pairing_secret,
                "server": self.server_url,
            }
        )
        return f"athenia-worker://pair?{query}"


class AtheniaClient:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        timeout: float = 60.0,
        max_attempts: int = 4,
        backoff_base_seconds: float = 0.5,
    ) -> None:
        self.config = config
        self._client = httpx.Client(base_url=config.server_url, timeout=timeout, trust_env=False)
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_seconds = max(0.0, backoff_base_seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AtheniaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def pairing_payload(self) -> PairingPayload:
        return PairingPayload(
            worker_id=self.config.worker_id,
            pairing_secret=self.config.pairing_secret,
            server_url=self.config.server_url,
        )

    def bootstrap(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/worker-runtime/bootstrap",
            json={
                "worker_id": self.config.worker_id,
                "pairing_secret": self.config.pairing_secret,
                "worker_token": self.config.worker_token,
                "worker_type": self.config.worker_type,
                "name": self.config.name,
                "capabilities": self.config.capabilities,
                "available_models": self.config.available_models,
                "resource_permissions": self.config.resource_permissions,
            },
        )

    def heartbeat(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/worker-runtime/heartbeat",
            auth=True,
            json={"available_models": self.config.available_models},
        )

    def log(self, level: str, message: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/worker-runtime/logs",
            auth=True,
            json={"level": level, "message": message, "metadata": metadata or {}},
        )

    def next_task(self) -> dict[str, Any] | None:
        return self._request("GET", "/v1/worker-runtime/tasks/next", auth=True)

    def send_frame(
        self,
        task_id: str,
        payload: bytes,
        content_type: str = "application/octet-stream",
        *,
        mode: str = "append",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/worker-runtime/tasks/{task_id}/frames",
            auth=True,
            content=payload,
            headers={"Content-Type": content_type, "X-Athenia-Frame-Mode": mode},
        )

    def upload_artifact(
        self,
        task_id: str,
        path: Path,
        *,
        relative_path: str,
        content_type: str,
    ) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self._request(
                "POST",
                f"/v1/worker-runtime/tasks/{task_id}/artifacts",
                auth=True,
                files={"file": (path.name, handle, content_type)},
                data={"relative_path": relative_path, "title": relative_path},
            )

    def complete_task(
        self,
        task_id: str,
        *,
        status: str,
        result_text: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/worker-runtime/tasks/{task_id}/complete",
            auth=True,
            json={"status": status, "result_text": result_text, "error": error},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = f"Bearer {self.config.worker_token}"

        attempts = self.max_attempts
        for attempt in range(1, attempts + 1):
            self._rewind_files(kwargs.get("files"))
            try:
                response = self._client.request(method, path, headers=request_headers, **kwargs)
                response.raise_for_status()
                if not response.content:
                    return None
                return response.json()
            except httpx.HTTPStatusError as exc:
                if not self._is_retryable_status(exc.response.status_code) or attempt == attempts:
                    raise
                self._sleep_before_retry(attempt)
            except httpx.RequestError:
                if attempt == attempts:
                    raise
                self._sleep_before_retry(attempt)

        raise RuntimeError("Athenia request retry loop exhausted unexpectedly.")

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.backoff_base_seconds <= 0:
            return
        time.sleep(min(self.backoff_base_seconds * (2 ** (attempt - 1)), 8.0))

    @staticmethod
    def _rewind_files(files: object) -> None:
        if not isinstance(files, dict):
            return
        for value in files.values():
            if isinstance(value, tuple) and len(value) >= 2:
                handle = value[1]
                if hasattr(handle, "seek"):
                    try:
                        handle.seek(0)
                    except OSError:
                        pass
