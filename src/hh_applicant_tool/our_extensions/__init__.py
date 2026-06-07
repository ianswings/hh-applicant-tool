"""Наши расширения поверх форка s3rgeym/hh-applicant-tool.

Содержит слот-шаблон сопроводительного письма (cover.py), резолвер анкет
(questions.py) и их конфиг (config.yaml + resume.md). Подключаются через
Operation-сабкласс в operations/ (см. MIGRATION_CONTEXT.md, Этап 2).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent
DEFAULT_CONFIG_PATH = _HERE / "config.yaml"
DEFAULT_RESUME_PATH = _HERE / "resume.md"


def load_config(path: str | Path | None = None) -> dict:
    """Загрузить наш YAML-конфиг (слоты + rules). По умолчанию — config.yaml рядом."""
    import yaml

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_resume(path: str | Path | None = None) -> str:
    """Прочитать CV для контекста LLM. Возвращает "" если файла нет."""
    resume_path = Path(path) if path else DEFAULT_RESUME_PATH
    if not resume_path.exists():
        return ""
    return resume_path.read_text(encoding="utf-8")
