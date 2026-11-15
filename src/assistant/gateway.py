from __future__ import annotations

import json
import time
from typing import Any
import httpx

from assistant.config import settings
from assistant.observability import SpendTracker, get_tracer, log_event


class GatewayError(RuntimeError):
    """Ошибка при обращении к LLM-шлюзу."""


class LLMGatewayClient:
    def __init__(
            self,
            base_url: str | None = None,
            api_key: str | None = None,
            timeout: float = 120.0,
            spend_tracker: SpendTracker | None = None,
            channel: str = "cli",
            session_id: str = "default",
    ) -> None:
        self.base_url = (base_url or settings.litellm_base_url).rstrip("/")
        self.api_key = api_key or settings.litellm_api_key
        if not self.api_key:
            raise ValueError("Не задан LITELLM_VIRTUAL_KEY в .env")

        self.spend_tracker = spend_tracker
        self.channel = channel
        self.session_id = session_id
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _extract_cost(self, response: httpx.Response) -> float:
        raw = response.headers.get("x-litellm-response-cost")
        try:
            return float(raw) if raw is not None else 0.0
        except ValueError:
            return 0.0

    def chat_completion(
            self,
            messages: list[dict[str, Any]],
            model: str | None = None,
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.2,
    ) -> dict[str, Any]:
        used_model = model or settings.default_model
        payload: dict[str, Any] = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        tracer = get_tracer()
        t0 = time.perf_counter()
        with tracer.start_as_current_span("llm.chat_completion") as span:
            span.set_attribute("openinference.span.kind", "LLM")
            span.set_attribute("llm.model_name", used_model)
            span.set_attribute("input.value", json.dumps(messages[-2:], ensure_ascii=False, default=str)[:2000])

            response = self._client.post("/v1/chat/completions", json=payload)
            latency_ms = int((time.perf_counter() - t0) * 1000)

            if response.status_code >= 400:
                span.set_attribute("error", True)
                log_event(
                    "llm.chat_error",
                    level="ERROR",
                    model=used_model,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    detail=response.text[:500],
                )
                raise GatewayError(
                    f"LiteLLM вернул {response.status_code}: {response.text}"
                )

            data = response.json()
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            total_tokens = int(usage.get("total_tokens", 0))
            cost_usd = self._extract_cost(response)

            span.set_attribute("llm.token_count.prompt", prompt_tokens)
            span.set_attribute("llm.token_count.completion", completion_tokens)
            span.set_attribute("llm.token_count.total", total_tokens)
            span.set_attribute("llm.cost.total", cost_usd)

            msg = data["choices"][0]["message"]
            span.set_attribute(
                "output.value",
                json.dumps(msg.get("content") or msg.get("tool_calls") or "", ensure_ascii=False)[:2000],
            )

            log_event(
                "llm.chat_completion",
                model=used_model,
                channel=self.channel,
                session_id=self.session_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )

            if self.spend_tracker is not None:
                self.spend_tracker.record(
                    model=used_model,
                    operation="chat",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    channel=self.channel,
                    session_id=self.session_id,
                )

            return data

    def create_embedding(
            self,
            text: str,
            model: str | None = None,
    ) -> list[float]:
        used_model = model or settings.embed_model
        payload = {"model": used_model, "input": text}

        tracer = get_tracer()
        t0 = time.perf_counter()
        with tracer.start_as_current_span("llm.embedding") as span:
            span.set_attribute("openinference.span.kind", "EMBEDDING")
            span.set_attribute("embedding.model_name", used_model)
            span.set_attribute("input.value", text[:500])

            response = self._client.post("/v1/embeddings", json=payload)
            latency_ms = int((time.perf_counter() - t0) * 1000)

            if response.status_code >= 400:
                log_event(
                    "llm.embedding_error",
                    level="ERROR",
                    model=used_model,
                    status_code=response.status_code,
                    detail=response.text[:500],
                )
                raise GatewayError(
                    f"LiteLLM embeddings вернул {response.status_code}: {response.text}"
                )

            data = response.json()
            usage = data.get("usage") or {}
            total_tokens = int(usage.get("total_tokens", 0))
            cost_usd = self._extract_cost(response)

            log_event(
                "llm.embedding",
                model=used_model,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )

            if self.spend_tracker is not None:
                self.spend_tracker.record(
                    model=used_model,
                    operation="embedding",
                    prompt_tokens=total_tokens,
                    completion_tokens=0,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    channel=self.channel,
                    session_id=self.session_id,
                )

            return data["data"][0]["embedding"]

    def close(self) -> None:
        self._client.close()
