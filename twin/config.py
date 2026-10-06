"""Загрузка настроек из config.yaml и переменных окружения."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ChatRule:
    chat: str | int          # @username, числовой id или ссылка
    mode: str = "all"        # all — модель сама решает, отвечать ли; mentions — только на упоминания и ответы вам


@dataclass
class Behavior:
    history_limit: int = 30            # сколько последних сообщений чата отдавать модели
    debounce_seconds: float = 8.0      # ждать, пока собеседник допишет серию сообщений
    min_delay_seconds: float = 4.0     # пауза «прочитал и задумался»
    max_delay_seconds: float = 25.0
    typing_chars_per_second: float = 7.0
    max_replies_per_hour: int = 20     # на один чат
    pause_after_manual_minutes: int = 30  # если вы написали в чат сами, бот молчит
    ignore_bots: bool = True


@dataclass
class LLM:
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_tokens: int = 16000


@dataclass
class Config:
    api_id: int
    api_hash: str
    anthropic_api_key: str | None
    session_name: str = "twin"
    persona_file: Path = Path("persona.md")
    examples_file: Path = Path("data/examples.md")
    chats: list[ChatRule] = field(default_factory=list)
    behavior: Behavior = field(default_factory=Behavior)
    llm: LLM = field(default_factory=LLM)
    dry_run: bool = False


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config.yaml") -> Config:
    _load_dotenv(Path(".env"))
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Задайте TG_API_ID и TG_API_HASH в .env (берутся на my.telegram.org)")

    chats = []
    for item in raw.get("chats", []):
        if isinstance(item, (str, int)):
            chats.append(ChatRule(chat=item))
        else:
            chats.append(ChatRule(chat=item["chat"], mode=item.get("mode", "all")))
    if not chats:
        raise SystemExit("В config.yaml не указан ни один чат в разделе chats")
    for rule in chats:
        if rule.mode not in ("all", "mentions"):
            raise SystemExit(f"Неизвестный mode «{rule.mode}» для чата {rule.chat}")

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        session_name=raw.get("session_name", "twin"),
        persona_file=Path(raw.get("persona_file", "persona.md")),
        examples_file=Path(raw.get("examples_file", "data/examples.md")),
        chats=chats,
        behavior=Behavior(**raw.get("behavior", {})),
        llm=LLM(**raw.get("llm", {})),
        dry_run=bool(raw.get("dry_run", False)),
    )
