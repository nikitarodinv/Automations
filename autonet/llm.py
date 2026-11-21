"""Text model calls (Claude) for the two places the spec uses one: pulling facts out of public
pages (Stage 4) and the optional polish (Stage 6). Both ask for JSON that must fit a schema
(structured outputs), so there is no free text to re-parse.

Credentials resolve the usual Anthropic way (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
`ant auth login` profile). Tests pass in a fake client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import AuthError, DataError, TransientError

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class TextModel:
    model: str = DEFAULT_MODEL
    # Server-side refusal fallback: on a policy decline the API re-runs the request on a fallback
    # model inside the same call. "default" routes by refusal category; None turns it off.
    fallbacks: str | None = "default"
    client: Any = None
    calls: int = 0
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})

    def _client(self) -> Any:
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic(max_retries=2, timeout=120.0)
        return self.client

    def json(self, *, system: str, user: str, schema: dict[str, Any], effort: str = "medium",
             max_tokens: int = 16000) -> dict[str, Any]:
        """One request; returns the parsed JSON object. Raises AuthError / TransientError / DataError."""
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        }
        client = self._client()
        try:
            if self.fallbacks:
                resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise AuthError("claude", str(getattr(e, "message", e))) from e
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise TransientError(f"claude: {type(e).__name__}") from e
        except anthropic.BadRequestError as e:
            raise DataError(f"claude: request rejected ({getattr(e, 'message', e)})") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientError(f"claude: HTTP {e.status_code}") from e
            raise DataError(f"claude: HTTP {e.status_code}") from e
        self.calls += 1
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
            self.usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
        if resp.stop_reason == "refusal":
            raise DataError("claude declined the request")
        if resp.stop_reason == "max_tokens":
            raise DataError("claude's answer was cut off (max_tokens)")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise DataError("claude returned no text block")
        try:
            return json.loads(text)
        except ValueError as e:
            raise DataError("claude returned invalid JSON") from e


def from_config(cfg: Any, client: Any = None) -> TextModel:
    c = cfg.data.get("llm") or {}
    fb = c.get("fallbacks", "default")
    return TextModel(model=c.get("model", DEFAULT_MODEL), fallbacks=None if fb in (None, False, "off") else fb, client=client)
