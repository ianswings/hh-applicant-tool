"""Клиент Anthropic для нативного SDK с опциональным прокси и prompt caching.

Назначение:
- `complete(message)` — drop-in замена для `ChatOpenAI.complete`, чтобы хук-точки
  в их коде, ожидающие `cover_letter_ai.complete(msg)`, продолжали работать.
- `complete_with_caching(system, user, ...)` — наш метод для слот-шаблона:
  принимает список system-блоков с `cache_control: ephemeral` (резюме + правила
  кэшируются), что даёт экономию 6-10x на повторных откликах.

Прокси берём из ANTHROPIC_PROXY_URL (http:// или socks5://). Нужен из РФ, т.к.
Anthropic блокирует РФ-IP. hh.ru при этом ходит напрямую — прокси только для AI.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import KW_ONLY, dataclass, field
from typing import Any

from .base import AIError

logger = logging.getLogger(__package__)

DEFAULT_MODEL = "claude-haiku-4-5"


class AnthropicError(AIError):
    pass


@dataclass
class ChatAnthropic:
    api_key: str

    _: KW_ONLY

    model: str = DEFAULT_MODEL
    system_prompt: str | None = None
    temperature: float = 0.0
    max_tokens: int = 1000
    timeout: float = 60.0
    # Прокси только для Anthropic. None → берём из ANTHROPIC_PROXY_URL, иначе прямое
    # соединение. hh.ru это поле не затрагивает.
    proxy_url: str | None = None

    _client: Any = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "ChatAnthropic | None":
        """Собрать клиент из переменных окружения.

        Возвращает None, если ANTHROPIC_API_KEY не задан — вызывающий код тогда
        деградирует на fallback (нейтральное письмо / пропуск анкеты).
        """
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            logger.warning(
                "ANTHROPIC_API_KEY не задан — ChatAnthropic отключён"
            )
            return None
        proxy = (
            os.environ.get("PROXY_URL")
            or os.environ.get("ANTHROPIC_PROXY_URL")
            or None
        )
        return cls(api_key=key, proxy_url=proxy, **kwargs)

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as ex:
                raise AnthropicError(
                    "anthropic SDK не установлен (poetry add anthropic)"
                ) from ex

            proxy = (
                self.proxy_url
                or os.environ.get("PROXY_URL")
                or os.environ.get("ANTHROPIC_PROXY_URL")
                or None
            )
            if proxy:
                import httpx

                http_client = httpx.Client(
                    proxy=proxy,
                    timeout=httpx.Timeout(self.timeout, read=self.timeout),
                )
                self._client = anthropic.Anthropic(
                    api_key=self.api_key, http_client=http_client
                )
            else:
                self._client = anthropic.Anthropic(
                    api_key=self.api_key, timeout=self.timeout
                )
        return self._client

    def complete(self, message: str) -> str:
        """Генерация текста по одному сообщению (drop-in для ChatOpenAI.complete)."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": message}],
        }
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        return self._create(kwargs)

    def complete_with_caching(
        self,
        system: list[dict],
        user: str,
        *,
        prefill: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        schema: dict | None = None,  # для drop-in совместимости с OllamaChat; не используется
    ) -> str:
        """Генерация со структурированным system-промптом и prompt caching.

        `system` — список блоков вида
        ``{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}``.
        `prefill` — затравка ответа ассистента (например "{" для JSON); если задана,
        возвращаемая строка уже включает её в начало.
        """
        messages: list[dict] = [{"role": "user", "content": user}]
        if prefill is not None:
            messages.append({"role": "assistant", "content": prefill})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "temperature": (
                temperature if temperature is not None else self.temperature
            ),
            "system": system,
            "messages": messages,
        }
        text = self._create(kwargs)
        return (prefill or "") + text

    def solve_captcha(
        self, image_data: bytes, *, media_type: str = "image/png"
    ) -> str:
        """Распознаёт текст на картинке капчи (vision).

        Drop-in по сигнатуре с ChatOpenAI.solve_captcha. Параметры генерации
        фиксированы (temperature=0, короткий max_tokens) — для OCR инстансные
        дефолты не нужны.
        """
        image_base64 = base64.b64encode(image_data).decode("utf-8")
        logger.debug(
            "Anthropic запрос на распознавание капчи: %d bytes",
            len(image_data),
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 64,
            "temperature": 0.0,
            "system": (
                "Ты должен распознать текст на изображении. Верни ТОЛЬКО "
                "текст, без каких-либо объяснений или дополнительных символов."
            ),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Распознай текст на изображении. Верни "
                            "только результат распознавания.",
                        },
                    ],
                }
            ],
        }
        return self._create(kwargs).strip()

    def _create(self, kwargs: dict[str, Any]) -> str:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Anthropic запрос: %s", kwargs.get("messages"))
        try:
            resp = self.client.messages.create(**kwargs)
        except AnthropicError:
            raise
        except Exception as ex:
            raise AnthropicError(f"Anthropic request failed: {ex}") from ex

        try:
            parts = [
                block.text
                for block in resp.content
                if getattr(block, "type", None) == "text"
            ]
            return "".join(parts)
        except (AttributeError, IndexError, TypeError) as ex:
            raise AnthropicError(f"Invalid response format: {ex}") from ex
