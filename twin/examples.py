"""Извлечение ваших реплик из экспорта Telegram Desktop (result.json) как примеров стиля."""

import random


def _flatten(text) -> str:
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)


def _iter_chats(data: dict):
    if "messages" in data:
        yield data
    for chat in data.get("chats", {}).get("list", []):
        yield chat


def extract_examples(data: dict, me: str | None = None, count: int = 60,
                     context: int = 3, seed: int = 42) -> tuple[str, int]:
    """Возвращает текст файла с примерами и общее число найденных ответов.

    me — ваш from_id в экспорте (например, user123456); в полном экспорте определяется сам.
    """
    if not me and "personal_information" in data:
        me = f"user{data['personal_information']['user_id']}"
    if not me:
        raise ValueError("Не удалось определить ваш id в экспорте. Сделайте полный экспорт "
                         "(с разделом «Информация об аккаунте») или укажите from_id вручную.")

    samples = []
    for chat in _iter_chats(data):
        msgs = [m for m in chat.get("messages", []) if m.get("type") == "message"]
        for i, msg in enumerate(msgs):
            if msg.get("from_id") != me or msg.get("forwarded_from"):
                continue
            text = _flatten(msg.get("text", "")).strip()
            if not (2 <= len(text) <= 400):
                continue
            prev = msgs[max(0, i - context):i]
            if not prev or prev[-1].get("from_id") == me:
                continue  # нужна именно реакция на чужое сообщение
            lines = []
            for p in prev:
                p_text = _flatten(p.get("text", "")).strip() or "[медиа]"
                author = "Я" if p.get("from_id") == me else "Собеседник"
                lines.append(f"{author}: {p_text[:300]}")
            lines.append(f"Я: {text}")
            samples.append("\n".join(lines))

    if not samples:
        raise ValueError("Ваших ответов в экспорте не нашлось.")
    random.Random(seed).shuffle(samples)
    return "\n\n---\n\n".join(samples[:count]) + "\n", len(samples)
