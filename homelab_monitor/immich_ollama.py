"""The only module that talks to Ollama."""

import base64
import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any

Opener = Callable[..., Any]


class OllamaClient:
    """Ask a local vision model a question about one image; `opener` is injectable."""

    def __init__(self, base_url: str, model: str, opener: Opener = urllib.request.urlopen) -> None:
        self.base_url = base_url
        self.model = model
        self.opener = opener

    def _payload(self, prompt: str, image: bytes) -> dict:
        return {
            "model": self.model,
            "prompt": prompt,
            "images": [base64.b64encode(image).decode()],
            "stream": False,
            "format": "json",
            "think": False,  # qwen3-vl otherwise spends its whole budget thinking
            # num_ctx: a large photo is 4000+ image tokens, over Ollama's default 4096 window (HTTP 400)
            "options": {"temperature": 0.1, "num_predict": 200, "num_ctx": 8192},
        }

    def ask_json(self, prompt: str, image: bytes) -> dict:
        """Return the JSON object the model answers with, or raise ValueError.

        Example: ask_json("Reply {\\"a\\": 1}", jpeg_bytes) -> {"a": 1}
        """
        payload = self._payload(prompt, image)
        req = urllib.request.Request(
            self.base_url + "/api/generate", json.dumps(payload).encode(), method="POST"
        )
        with self.opener(req, timeout=300) as resp:
            reply = json.load(resp)
        # Ollama's qwen3-vl template sometimes puts the answer in "thinking"
        return extract_json(reply.get("response") or reply.get("thinking") or "")


def extract_json(text: str) -> dict:
    """First JSON object in `text`; models sometimes wrap it in prose or <think> tags."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"model did not return a JSON object: {text[:200]!r}")
