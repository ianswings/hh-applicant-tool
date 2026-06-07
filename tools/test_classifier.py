#!/usr/bin/env python3
"""Оценка локального классификатора сообщений работодателя.

Прогоняет набор формулировок по 4 классам (external_link / scheduling /
question / other) через наш LocalClassifier и сравнивает с ожиданием.

Запуск (из корня репо, в poetry-окружении):
    poetry run python tools/test_classifier.py --model gemma4:e4b
    poetry run python tools/test_classifier.py --model gemma4:26b
    poetry run python tools/test_classifier.py --only fail   # показать только провалы

Требует поднятый Ollama и скачанную модель (ollama pull gemma4:e4b).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Чтобы запускать без установки пакета: добавляем src/ в путь.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hh_applicant_tool.ai.local import ClassifierError, LocalClassifier  # noqa: E402

# (сообщение, ожидаемый_класс). Для неоднозначных — кортеж допустимых меток.
CASES: list[tuple] = [
    # --- external_link: просят перейти/заполнить форму/бота по ссылке ---
    ("Здравствуйте! Пройдите, пожалуйста, тестовое по ссылке https://forms.gle/abc123", "external_link"),
    ("Заполните анкету кандидата: https://docs.google.com/forms/d/xyz/viewform", "external_link"),
    ("Для продолжения пройдите короткий опрос по ссылке на нашем сайте.", "external_link"),
    ("Пообщайтесь с нашим ботом-рекрутёром и ответьте на вопросы: https://t.me/hr_bot", "external_link"),
    ("Перейдите по ссылке и заполните форму отклика, пожалуйста.", "external_link"),
    ("Пройдите оценку на нашей платформе по ссылке: https://hr.company.ru/assessment", "external_link"),

    # --- test_task: просьба выполнить задание и прислать ---
    # явные (со словами «тестовое»/«демо»)
    ("Выполните, пожалуйста, тестовое задание и пришлите решение.", "test_task"),
    ("Просим пройти небольшое тестовое перед интервью.", "test_task"),
    ("Сделайте демо-проект на FastAPI и отправьте ссылку на GitHub.", "test_task"),
    # неочевидные (без слов «тестовое»/«демо»)
    ("Нужно будет решить небольшое оплачиваемое задание.", "test_task"),
    ("Перед собеседованием напишите маленький сервис по нашему ТЗ и пришлите код.", "test_task"),
    ("Дадим задачку на пару часов — реализуете и покажете решение.", "test_task"),
    ("Реализуйте, пожалуйста, прототип парсера и отправьте нам на проверку.", "test_task"),
    ("Предлагаем выполнить домашнее задание, потом обсудим на встрече.", "test_task"),

    # --- неоднозначные: ок любой «отказной» маршрут (test_task ИЛИ external_link) ---
    ("Просьба пройти тестирование на нашей платформе по ссылке: https://hr.company.ru/test/start", ("test_task", "external_link")),
    ("Заполните профиль и пройдите оценку навыков по ссылке.", ("external_link", "test_task")),

    # --- employment_proof: подтвердить официальность стажа НА СЛОВАХ ---
    ("Весь ли ваш опыт оформлен по ТК РФ?", "employment_proof"),
    ("Нам важен официальный стаж — он у вас белый, в трудовой?", "employment_proof"),
    ("Подтвердите, пожалуйста, что трудоустройство было официальное.", "employment_proof"),
    # --- document_request: просят ПРИСЛАТЬ документы → эскалация ---
    ("Пришлите, пожалуйста, скан трудовой книжки.", "document_request"),
    ("Можете прислать копии договоров, подтверждающих стаж?", "document_request"),
    ("Нужны фото паспорта и диплома для оформления.", "document_request"),

    # --- scheduling: согласование времени / собеседование / созвон ---
    ("Готовы пригласить вас на собеседование. Когда вам удобно созвониться на этой неделе?", "scheduling"),
    ("Давайте назначим интервью. Какие слоты вам подходят?", "scheduling"),
    ("Предлагаю созвон в Zoom завтра в 15:00, подойдёт?", "scheduling"),
    ("Вот ссылка на видеовстречу https://meet.google.com/abc-defg — выберите время.", "scheduling"),
    ("Можем пообщаться голосом? Скиньте удобное время для звонка.", "scheduling"),
    ("Приглашаем на финальное интервью с технической командой.", "scheduling"),

    # --- question: вопросы по сути (в т.ч. бот спрашивает) ---
    ("Какой у вас опыт работы с Kafka и асинхронным Python?", "question"),
    ("Подскажите ваши зарплатные ожидания?", "question"),
    ("Готовы ли вы работать в офисе?", "question"),
    ("Расскажите коротко, почему вам интересна наша вакансия?", "question"),
    ("Сколько лет коммерческого опыта на Python?", "question"),
    ("Вы рассматриваете только удалёнку или возможен гибрид?", "question"),
    ("Какими фреймворками и базами данных владеете?", "question"),

    # --- other: подтверждения, приветствия, офферы, неясное ---
    ("Спасибо за отклик! Изучим резюме и вернёмся к вам.", "other"),
    ("Здравствуйте!", "other"),
    ("Благодарим за интерес к нашей вакансии.", "other"),
    ("Мы получили ваше резюме, оно на рассмотрении.", "other"),
    ("Готовы сделать вам оффер, давайте обсудим условия.", "other"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="gemma4:e4b", help="Ollama-модель")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument(
        "--only",
        choices=["all", "fail"],
        default="all",
        help="Показывать все кейсы или только провалы",
    )
    args = ap.parse_args()

    clf = LocalClassifier(model=args.model, base_url=args.base_url)
    try:
        clf.health_check()
    except ClassifierError as ex:
        print(f"❌ {ex}")
        return 1

    print(f"🧪 Модель: {args.model}\n")
    correct = 0
    per_class: dict[str, list[int]] = {}  # class -> [correct, total]
    failures: list[tuple[str, str, str]] = []
    t0 = time.monotonic()

    for msg, expected in CASES:
        # expected может быть строкой или кортежем допустимых меток (неоднозначные).
        acceptable = (expected,) if isinstance(expected, str) else tuple(expected)
        primary = acceptable[0]
        exp_label = "|".join(acceptable)
        start = time.monotonic()
        got = clf.classify(msg)
        dt = (time.monotonic() - start) * 1000
        ok = got in acceptable
        correct += ok
        c = per_class.setdefault(primary, [0, 0])
        c[1] += 1
        c[0] += ok
        if not ok:
            failures.append((msg, exp_label, got))
        if args.only == "all" or not ok:
            mark = "✅" if ok else "❌"
            print(f"{mark} [{got:<13}] ожид [{exp_label:<20}] {dt:5.0f}ms  {msg[:64]}")

    total = len(CASES)
    elapsed = time.monotonic() - t0
    print(f"\n=== ИТОГ: {correct}/{total} ({100*correct/total:.0f}%), {elapsed:.1f}s всего ===")
    print("По классам:")
    for cls, (cc, ct) in sorted(per_class.items()):
        print(f"  {cls:<13} {cc}/{ct}")
    if failures:
        print("\nПровалы (что подкрутить в промпте классификатора):")
        for msg, exp, got in failures:
            print(f"  ожид {exp} → получил {got}: {msg[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
