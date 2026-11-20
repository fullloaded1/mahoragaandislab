"""Google Gemini REST API + Multi-Provider Failover runtime adapter for Harvey.

Supports:
1. Multiple comma-separated GEMINI_API_KEY values (round-robin + automatic failover)
2. Automatic fallback to GROQ_API_KEY, OPENROUTER_API_KEY, COHERE_API_KEY, and GROK_API_KEY
   when Gemini keys hit rate limits or errors, ensuring 24/7 uninterrupted operation.
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("harvey.gemini")

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiError(Exception):
    """A Gemini API call failed. ``kind`` drives retry and operator reporting."""

    def __init__(self, message: str, kind: str = "request", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class GeminiResult:
    text: str
    model: str = ""
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)


class GeminiQuotaClient:
    """Gemini quota client returning None (API keys don't expose subscription windows)."""

    async def get_utilization(self) -> dict | None:
        return None


class GeminiClient:
    """Async client for Google Gemini REST API v1beta with multi-key & multi-provider failover."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = DEFAULT_BASE_URL,
    ):
        raw_keys = [k.strip() for k in (api_key or "").split(",") if k.strip()]
        self.api_keys = raw_keys
        self.api_key = raw_keys[0] if raw_keys else ""
        self._key_cycle = itertools.cycle(raw_keys) if raw_keys else None
        self.model = model.strip().removeprefix("models/") or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")

    def _fallbacks(self) -> list[tuple[str, str]]:
        """Configured fallback providers from environment variables."""
        out: list[tuple[str, str]] = []
        if self.base_url != DEFAULT_BASE_URL:
            # Skip external fallbacks during isolated unit tests with a fake base_url
            return out
        groq = os.getenv("GROQ_API_KEY", "").strip()
        if groq:
            out.append(("groq", groq))
        openrouter = os.getenv("OPENROUTER_API_KEY", "").strip()
        if openrouter:
            out.append(("openrouter", openrouter))
        cohere = os.getenv("COHERE_API_KEY", "").strip()
        if cohere:
            out.append(("cohere", cohere))
        for nk in (os.getenv("NVIDIA_API_KEY", "") or "").split(","):
            if nk.strip():
                out.append(("nvidia", nk.strip()))
        return out

    def fingerprint(self) -> tuple:
        """A hashable representation of configuration for cache invalidation."""
        return (tuple(self.api_keys), self.model, self.base_url, tuple(self._fallbacks()))

    async def check_preflight(self) -> None:
        """Verify API key and connectivity without consuming inference tokens."""
        if not self.api_keys and not self._fallbacks():
            raise GeminiError("GEMINI_API_KEY is not configured in .env.", kind="setup", retryable=False)

        last_err: Exception | None = None
        for key in self.api_keys:
            url = f"{self.base_url}/v1beta/models/{self.model}"
            params = {"key": key}
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        return
                    self._handle_http_error(resp)
            except Exception as e:
                last_err = e

        if self._fallbacks():
            return

        if isinstance(last_err, GeminiError):
            raise last_err
        if isinstance(last_err, httpx.TimeoutException):
            raise GeminiError(
                f"Gemini preflight timed out contacting {self.base_url}.",
                kind="timeout",
                retryable=True,
            )
        if isinstance(last_err, httpx.NetworkError):
            raise GeminiError(
                f"Gemini preflight network error: {last_err}",
                kind="network",
                retryable=True,
            )
        raise GeminiError(
            f"Gemini preflight failed unexpectedly ({type(last_err).__name__}).",
            kind="request",
            retryable=False,
        )

    async def preflight(self) -> str:
        """Verify API key and connectivity without consuming inference tokens."""
        try:
            await self.check_preflight()
            return ""
        except GeminiError as e:
            return str(e)
        except Exception as e:
            return f"Gemini preflight failed unexpectedly ({type(e).__name__})."

    async def run(self, prompt: str, timeout: float = 300.0) -> GeminiResult:
        """Execute a text-only, tool-less completion turn with automatic multi-provider failover."""
        if not self.api_keys and not self._fallbacks():
            raise GeminiError("GEMINI_API_KEY is missing.", kind="auth", retryable=False)

        last_err: GeminiError | None = None

        # 0. Try Tokenizer.id primary token package if configured
        tok_key = os.getenv("TOKENIZER_API_KEY", "").strip() if self.base_url == DEFAULT_BASE_URL else ""
        if tok_key:
            try:
                return await self._run_fallback("tokenizer", tok_key, prompt, timeout)
            except Exception as e:
                logger.warning(f"Tokenizer.id failed ({e}); failing over to Gemini/fallback pool...")

        # 1. Try Gemini keys (starting from next key in round-robin cycle)
        n_keys = len(self.api_keys)
        ordered_keys = [next(self._key_cycle) for _ in range(n_keys)] if self._key_cycle else []
        for key in ordered_keys:
            try:
                return await self._run_gemini_key(key, prompt, timeout)
            except GeminiError as e:
                last_err = e
                logger.warning(f"Gemini key failed ({e.kind}): {e}; trying next provider in pool...")

        # 2. Try fallback providers (Groq -> OpenRouter -> Cohere -> Grok)
        for provider_name, key in self._fallbacks():
            try:
                return await self._run_fallback(provider_name, key, prompt, timeout)
            except Exception as e:
                logger.warning(f"Fallback provider {provider_name} failed: {e}")

        if last_err is not None:
            raise last_err
        raise GeminiError("All configured LLM providers failed.", kind="request", retryable=True)

    async def _run_gemini_key(self, key: str, prompt: str, timeout: float) -> GeminiResult:
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        params = {"key": key}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, params=params, json=payload)
                duration_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code != 200:
                    self._handle_http_error(resp)

                data = resp.json()
        except GeminiError:
            raise
        except httpx.TimeoutException:
            raise GeminiError(
                f"Gemini request timed out after {timeout:.0f}s.",
                kind="timeout",
                retryable=True,
            )
        except httpx.NetworkError as e:
            raise GeminiError(
                f"Gemini network error: {e}",
                kind="network",
                retryable=True,
            )
        except Exception as e:
            raise GeminiError(
                f"Gemini request failed unexpectedly ({type(e).__name__}): {e}",
                kind="request",
                retryable=False,
            )

        text = self._extract_text(data)
        usage = self._extract_usage(data)
        model = data.get("modelVersion") or self.model

        return GeminiResult(
            text=text,
            model=model,
            duration_ms=duration_ms,
            usage=usage,
        )

    async def _run_fallback(self, provider: str, key: str, prompt: str, timeout: float) -> GeminiResult:
        start = time.monotonic()
        sys_msg = (
            "Kamu adalah asisten eksekutif B2B PT Andis Sentral Laboratorium (AndisLab). "
            "PATUHI KONTEKS PERCAKAPAN, TAHAP DEAL, DAN INSTRUKSI DI BAWAH DENGAN PRESISI MUTLAK. "
            "DILARANG KERAS mengarang/menghalusinasi nama produk, merk, atau harga yang tidak tertulis eksplisit di prompt. "
            "Jika diminta format JSON, kembalikan HANYA objek JSON valid."
        )
        msgs = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ]
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider == "tokenizer":
                tok_base = (os.getenv("TOKENIZER_BASE_URL", "https://tokenizer.id/v1") or "https://tokenizer.id/v1").rstrip("/")
                tok_model = (os.getenv("TOKENIZER_MODEL", "gpt-6.1-sol") or "gpt-6.1-sol").strip()
                resp = await client.post(
                    f"{tok_base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": tok_model, "messages": msgs, "temperature": 0.15},
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or tok_model,
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

            if provider == "groq":
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "openai/gpt-oss-20b",
                        "messages": msgs,
                        "temperature": 0.15,
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or "groq/gpt-oss-20b",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

            if provider == "openrouter":
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "openrouter/auto",
                        "messages": msgs,
                        "temperature": 0.15,
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or "openrouter/auto",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

            if provider == "cohere":
                resp = await client.post(
                    "https://api.cohere.com/v2/chat",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "command-a-03-2025",
                        "messages": msgs,
                        "temperature": 0.15,
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                content_list = (data.get("message") or {}).get("content") or []
                text = "".join(c.get("text", "") for c in content_list if isinstance(c, dict))
                u = (data.get("usage") or {}).get("tokens") or {}
                return GeminiResult(
                    text=text,
                    model="cohere/command-a-03-2025",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("input_tokens") or 0),
                        "output_tokens": int(u.get("output_tokens") or 0),
                    },
                )

            if provider == "nvidia":
                resp = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "minimaxai/minimax-m3.1-flash",
                        "messages": msgs,
                        "temperature": 0.15,
                        "max_tokens": 2048,
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or "minimaxai/minimax-m3.1-flash",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

            if provider == "deepseek":
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or "deepseek-chat",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

            if provider == "grok":
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "grok-2-latest",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                u = data.get("usage") or {}
                return GeminiResult(
                    text=text,
                    model=data.get("model") or "grok-2-latest",
                    duration_ms=duration_ms,
                    usage={
                        "input_tokens": int(u.get("prompt_tokens") or 0),
                        "output_tokens": int(u.get("completion_tokens") or 0),
                    },
                )

        raise GeminiError(f"Unknown fallback provider: {provider}")

    def _extract_text(self, data: dict) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            block_reason = feedback.get("blockReason")
            if block_reason:
                raise GeminiError(
                    f"Gemini blocked prompt: {block_reason}",
                    kind="request",
                    retryable=False,
                )
            return ""

        first = candidates[0]
        content = first.get("content") or {}
        parts = content.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        finish_reason = first.get("finishReason")
        if not text and finish_reason and finish_reason != "STOP":
            raise GeminiError(
                f"Gemini response stopped: {finish_reason}",
                kind="request",
                retryable=False,
            )
        return text

    def _extract_usage(self, data: dict) -> dict:
        meta = data.get("usageMetadata") or {}
        usage = {
            "input_tokens": int(meta.get("promptTokenCount") or 0),
            "output_tokens": int(meta.get("candidatesTokenCount") or 0),
        }
        if "cachedContentTokenCount" in meta:
            usage["cache_read_tokens"] = int(meta["cachedContentTokenCount"] or 0)
        return usage

    def _handle_http_error(self, resp: httpx.Response) -> None:
        status = resp.status_code
        err_msg = ""
        err_status = ""
        try:
            err_data = resp.json().get("error", {})
            err_msg = err_data.get("message", "")
            err_status = err_data.get("status", "")
        except Exception:
            err_msg = resp.text[:200]

        if status == 429 or "RESOURCE_EXHAUSTED" in err_msg or err_status == "RESOURCE_EXHAUSTED":
            raise GeminiError(
                f"Gemini rate limit or quota exceeded (429): {err_msg}",
                kind="usage_limit",
                retryable=False,
            )
        if (
            status in (401, 403)
            or err_status in ("UNAUTHENTICATED", "PERMISSION_DENIED")
            or (status == 400 and ("API_KEY" in err_msg or "API key" in err_msg or "API_KEY_INVALID" in err_status))
        ):
            raise GeminiError(
                f"Gemini authentication failed ({status}): {err_msg or 'check GEMINI_API_KEY'}",
                kind="auth",
                retryable=False,
            )
        if status in (500, 502, 503, 504) or err_status in ("UNAVAILABLE", "INTERNAL"):
            raise GeminiError(
                f"Gemini server error ({status}): {err_msg}",
                kind="server",
                retryable=True,
            )
        raise GeminiError(
            f"Gemini request failed ({status}): {err_msg}",
            kind="request",
            retryable=False,
        )
