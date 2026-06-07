"""Фабрика чат-клиента по llm.provider из config.yaml.

provider: local    → OllamaChat (локально, без прокси) — наш дефолт сейчас;
provider: anthropic→ ChatAnthropic (облако, через ANTHROPIC_PROXY_URL).

Anthropic-путь остаётся рабочим — переключение одной строкой в конфиге.
"""

from __future__ import annotations

from .anthropic import ChatAnthropic
from .local import OllamaChat


def make_chat(llm_cfg: dict):
    provider = (llm_cfg.get("provider") or "anthropic").lower()
    if provider == "local":
        return OllamaChat(
            model=llm_cfg.get("local_model", "gemma4:e4b"),
            base_url=llm_cfg.get("local_base_url", "http://localhost:11434"),
            num_predict=int(llm_cfg.get("num_predict", 4096)),
            num_ctx=int(llm_cfg.get("num_ctx", 12288)),
            keep_alive=str(llm_cfg.get("keep_alive", "30s")),
            think=llm_cfg.get("think"),
        )
    return ChatAnthropic.from_env(
        model=llm_cfg.get("model", "claude-haiku-4-5"),
        max_tokens=llm_cfg.get("max_tokens", 1000),
        temperature=llm_cfg.get("temperature", 0.0),
    )
