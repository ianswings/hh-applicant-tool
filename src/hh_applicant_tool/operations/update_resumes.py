# Этот модуль можно использовать как образец для других
from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..api import ApiError, datatypes
from ..main import BaseNamespace, BaseOperation
from ..our_extensions.notify import TelegramNotifier
from ..utils.string import shorten

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    pass


class Operation(BaseOperation):
    """Обновить все резюме"""

    __aliases__ = ["update"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, tool: HHApplicantTool, args: BaseNamespace) -> None:
        resumes: list[datatypes.Resume] = tool.get_resumes()
        # Там вызов API меняет поля
        tool.storage.resumes.save_batch(resumes)

        updated: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for resume in resumes:
            title = shorten(resume["title"])
            if not resume.get("can_publish_or_update"):
                logger.warning(f"Не могу обновить: {resume['alternate_url']}")
                skipped.append(title)
                continue
            try:
                r = tool.api_client.post(
                    f"/resumes/{resume['id']}/publish",
                )
                assert {} == r
                print("✅ Обновлено", resume["alternate_url"], "-", title)
                updated.append(title)
            except ApiError as ex:
                logger.error(f"Ошибка при обновлении резюме: {ex}")
                failed.append(title)

        self._notify_summary(updated, skipped, failed)

    def _notify_summary(
        self, updated: list[str], skipped: list[str], failed: list[str]
    ) -> None:
        # Отчёт в Telegram — только когда есть о чём (подняли или упали).
        # Если все резюме просто пропущены (4ч ещё не прошло) — не спамим.
        if not updated and not failed:
            return
        notifier = TelegramNotifier.from_env()
        if notifier is None:
            return
        lines = [f"🔄 update: поднято {len(updated)} резюме"]
        lines += [f"✅ {t}" for t in updated]
        if failed:
            lines.append(f"⚠️ ошибок: {len(failed)}")
            lines += [f"❌ {t}" for t in failed]
        if skipped:
            lines.append(f"⏭️ пропущено (рано поднимать): {len(skipped)}")
        notifier.send("\n".join(lines))
