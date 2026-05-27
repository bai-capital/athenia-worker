from __future__ import annotations

from athenia_worker.client import AtheniaClient
from athenia_worker.config import WorkerConfig, default_available_models


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
