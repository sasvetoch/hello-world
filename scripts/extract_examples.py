"""Достаёт ваши реплики из экспорта Telegram Desktop и сохраняет их как примеры стиля.

Экспорт: Telegram Desktop → Настройки → Продвинутые → «Экспорт данных Telegram»,
формат «Машиночитаемый JSON». Подходит и полный экспорт, и экспорт одного чата.

Пример:
    python scripts/extract_examples.py ~/Downloads/Telegram/result.json --count 60
"""

import argparse
import json
import random
from pathlib import Path


def flatten_text(text) -> str:
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)


def iter_chats(data: dict):
    if "messages" in data:
        yield data
    for chat in data.get("chats", {}).get("list", []):
        yield chat


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", type=Path, help="путь к result.json")
    parser.add_argument("--me", help="ваш from_id в экспорте, например user123456 (в полном экспорте определяется сам)")
    parser.add_argument("--count", type=int, default=60, help="сколько диалогов-примеров сохранить")
    parser.add_argument("--context", type=int, default=3, help="сколько чужих реплик показывать перед вашей")
    parser.add_argument("--out", type=Path, default=Path("data/examples.md"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data = json.loads(args.export.read_text(encoding="utf-8"))
    me = args.me
    if not me and "personal_information" in data:
        me = f"user{data['personal_information']['user_id']}"
    if not me:
        raise SystemExit("Не удалось определить ваш id — передайте его через --me (поле from_id ваших сообщений)")

    samples = []
    for chat in iter_chats(data):
        msgs = [m for m in chat.get("messages", []) if m.get("type") == "message"]
        for i, msg in enumerate(msgs):
            if msg.get("from_id") != me or msg.get("forwarded_from"):
                continue
            text = flatten_text(msg.get("text", "")).strip()
            if not (2 <= len(text) <= 400):
                continue
            prev = msgs[max(0, i - args.context):i]
            if not prev or prev[-1].get("from_id") == me:
                continue  # нужна именно реакция на чужое сообщение
            lines = []
            for p in prev:
                p_text = flatten_text(p.get("text", "")).strip() or "[медиа]"
                author = "Я" if p.get("from_id") == me else "Собеседник"
                lines.append(f"{author}: {p_text[:300]}")
            lines.append(f"Я: {text}")
            samples.append("\n".join(lines))

    if not samples:
        raise SystemExit("Ваших ответов в экспорте не нашлось — проверьте --me")

    random.Random(args.seed).shuffle(samples)
    chosen = samples[:args.count]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n\n---\n\n".join(chosen) + "\n", encoding="utf-8")
    print(f"Найдено {len(samples)} ответов, сохранено {len(chosen)} в {args.out}")
    print("Просмотрите файл и уберите то, что не должно попасть к модели (личное, пароли, адреса).")


if __name__ == "__main__":
    main()
