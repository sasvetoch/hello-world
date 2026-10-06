"""Достаёт ваши реплики из экспорта Telegram Desktop и сохраняет их как примеры стиля.

Экспорт: Telegram Desktop → Настройки → Продвинутые → «Экспорт данных Telegram»,
формат «Машиночитаемый JSON». Подходит и полный экспорт, и экспорт одного чата.
Того же результата можно добиться, просто прислав result.json боту в личку.

Пример:
    python scripts/extract_examples.py ~/Downloads/Telegram/result.json --count 60
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from twin.examples import extract_examples  # noqa: E402


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
    try:
        text, total = extract_examples(data, args.me, args.count, args.context, args.seed)
    except ValueError as e:
        raise SystemExit(str(e))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Найдено {total} ответов, сохранено {min(total, args.count)} в {args.out}")
    print("Просмотрите файл и уберите то, что не должно попасть к модели (личное, пароли, адреса).")


if __name__ == "__main__":
    main()
