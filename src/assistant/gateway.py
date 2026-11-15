from __future__ import annotations

from typing import Any
import httpx

from assistant.config import settings


class GatewayError(RuntimeError):
    """Ошибка при обращении к LLM-шлюзу."""


class LLMGatewayClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or settings.litellm_base_url).rstrip("/")
        self.api_key = api_key or settings.litellm_api_key
        if not self.api_key:
            raise ValueError("Не задан LITELLM_VIRTUAL_KEY в .env")

        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or settings.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = self._client.post("/v1/chat/completions", json=payload)
        if response.status_code >= 400:
            raise GatewayError(
                f"LiteLLM вернул {response.status_code}: {response.text}"
            )
        return response.json()

    def create_embedding(
            self,
            text: str,
            model: str | None = None,
    ) -> list[float]:
        payload = {
            "model": model or settings.embed_model,
            "input": text,
        }
        response = self._client.post("/v1/embeddings", json=payload)
        if response.status_code >= 400:
            raise GatewayError(
                f"LiteLLM embeddings вернул {response.status_code}: {response.text}"
            )
        data = response.json()
        return data["data"][0]["embedding"]

    def close(self) -> None:
        self._client.close()


if __name__ == "__main__":
    client = LLMGatewayClient()
    try:
        res = client.chat_completion(
            messages=[
                {"role": "system", "content": "Отвечай кратко и по делу."},
                {"role": "user", "content": "Привет! Подтверди, что шлюз работает."},
            ]
        )
        msg = res["choices"][0]["message"]
        usage = res.get("usage", {})
        print("Ответ модели:", msg.get("content"))
        print("Использовано токенов:", usage)
    finally:
        client.close()