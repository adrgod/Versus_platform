"""Client for Google's Gemini API (https://ai.google.dev), an online alternative to the local Ollama model.

Switch on with ``"provider": "gemini"`` under ``insights`` in config.json. The API key comes from the
GEMINI_API_KEY environment variable, or ``insights.gemini_api_key`` in config.json (never commit a key).
It offers the same ``status()`` / ``extract()`` as OllamaClient, so the rest of the app does not care which one runs.

The free tier is rate limited (a few requests a minute), so calls are spaced out and a 429/503 answer is retried.
"""
import json
import os
import time
import urllib.error
import urllib.request

from library.services.ollama import OllamaError, settings

GEMINI_DEFAULTS = {
    "gemini_model": "gemini-3.6-flash",
    "gemini_min_interval": 4.5,  # seconds between calls; 15 requests a minute is the usual free limit
    "gemini_retries": 4,
}
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(OllamaError):
    """An OllamaError, so the code that already handles model failures handles these too."""


def configured_key():
    return (os.environ.get("GEMINI_API_KEY") or settings().get("gemini_api_key") or "").strip()


class GeminiClient:
    def __init__(self, model=None, api_key=None, timeout=None, sleep=time.sleep):
        config = {**GEMINI_DEFAULTS, **settings()}
        self.model = model or config["gemini_model"]
        self.api_key = configured_key() if api_key is None else api_key
        self.timeout = timeout or config["timeout_seconds"]
        self.min_interval = config["gemini_min_interval"]
        self.retries = config["gemini_retries"]
        self.url = "Google Gemini API"
        self._sleep = sleep
        self._last_call = 0.0

    def status(self):
        """{"reachable", "model_ready", "models", "error"} without raising or spending a request."""
        if not self.api_key:
            return {"reachable": False, "model_ready": False, "models": [], "error":
                    "Gemini needs an API key: set the GEMINI_API_KEY environment variable "
                    '(or "gemini_api_key" under "insights" in config.json).'}
        return {"reachable": True, "model_ready": True, "models": [self.model], "error": ""}

    def _post(self, payload):
        data = json.dumps(payload).encode()
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            request = urllib.request.Request(
                f"{BASE_URL}/models/{self.model}:generateContent", data=data, method="POST",
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
            try:
                self._last_call = time.monotonic()
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:300]
                if error.code in (429, 500, 503) and attempt < self.retries:
                    self._sleep(15 * (attempt + 1))  # rate limited or busy: wait and try again
                    continue
                if error.code == 429:
                    raise GeminiError("Gemini's rate limit was reached (the free tier allows few requests per "
                                      "minute and per day); try again later") from error
                raise GeminiError(f"Gemini answered {error.code}: {detail}") from error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                raise GeminiError(f"Could not reach the Gemini API: {error}") from error

    def extract(self, system, text, schema):
        """The model's answer to ``text`` as a dict that follows the JSON ``schema``."""
        answer = self._post({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema},
        })
        candidates = answer.get("candidates") or []
        if not candidates:
            reason = (answer.get("promptFeedback") or {}).get("blockReason", "no answer")
            raise GeminiError(f"Gemini returned no answer ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        content = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise GeminiError(f"The model did not answer in JSON: {content[:120]}") from error
        if not isinstance(data, dict):
            raise GeminiError("The model's answer was not a JSON object")
        return data
