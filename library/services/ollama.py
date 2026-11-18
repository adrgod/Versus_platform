"""Minimal client for a local Ollama server (https://ollama.com), used for structured extraction.

The server can run on this machine or on another one on the network: set
``insights.ollama_url`` and ``insights.model`` in config.json.
"""
import json
import urllib.error
import urllib.request

from library import repository

DEFAULTS = {
    "ollama_url": "http://localhost:11434",
    "model": "qwen3:8b",
    "context_tokens": 16384,
    "chunk_chars": 24000,
    "timeout_seconds": 600,
}


class OllamaError(Exception):
    pass


def settings():
    return {**DEFAULTS, **repository.get_config().get("insights", {})}


class OllamaClient:
    def __init__(self, url=None, model=None, context_tokens=None, timeout=None):
        config = settings()
        self.url = (url or config["ollama_url"]).rstrip("/")
        self.model = model or config["model"]
        self.context_tokens = context_tokens or config["context_tokens"]
        self.timeout = timeout or config["timeout_seconds"]

    def _request(self, path, payload=None, timeout=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}", data=data, headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise OllamaError(f"Ollama answered {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise OllamaError(f"Could not reach Ollama at {self.url}: {error}") from error

    def status(self):
        """{"reachable", "model_ready", "models", "error"} without raising."""
        try:
            models = [item.get("name", "") for item in self._request("/api/tags", timeout=5).get("models", [])]
        except OllamaError as error:
            return {"reachable": False, "model_ready": False, "models": [], "error": str(error)}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return {"reachable": True, "model_ready": wanted in models, "models": models, "error": ""}

    def extract(self, system, text, schema):
        """The model's answer to ``text`` as a dict that follows the JSON ``schema``."""
        answer = self._request("/api/chat", {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.context_tokens},
        })
        content = (answer.get("message") or {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise OllamaError(f"The model did not answer in JSON: {content[:120]}") from error
        if not isinstance(data, dict):
            raise OllamaError("The model's answer was not a JSON object")
        return data
