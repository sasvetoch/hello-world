"""Загрузка настроек из config.yaml и переменных окружения."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Behavior:
    history_limit: int = 40            # сколько последних сообщений чата отдавать модели
    debounce_seconds: float = 6.0      # ждать, пока собеседник допишет серию сообщений
    min_delay_seconds: float = 2.0     # пауза «прочитал и задумался»
    max_delay_seconds: float = 10.0
    typing_chars_per_second: float = 12.0
    max_replies_per_hour: int = 30     # на один чат
    owner_active_minutes: int = 10     # если вы сами пишете в группе, двойник не вмешивается
    ignore_bots: bool = True


@dataclass
class LLM:
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_tokens: int = 16000


@dataclass
class Config:
    bot_token: str
    owner_id: int
    owner_name: str
    anthropic_api_key: str | None
    persona_file: Path = Path("persona.md")
    examples_file: Path = Path("data/examples.md")
    knowledge_dir: Path = Path("knowledge")
    db_file: Path = Path("data/twin.db")
    # Что двойник отвечает незнакомцу, написавшему в личку, пока вы не разрешили диалог.
    stranger_reply: str = ""
    # Присылать вам копии диалогов двойника в личных чатах, чтобы их можно было поправить.
    report_private: bool = True
    behavior: Behavior = field(default_factory=Behavior)
    llm: LLM = field(default_factory=LLM)


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

    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        raise SystemExit("Задайте TG_BOT_TOKEN в .env (выдаёт @BotFather)")
    owner_id = os.environ.get("TG_OWNER_ID")
    if not owner_id or not owner_id.lstrip("-").isdigit():
        raise SystemExit("Задайте TG_OWNER_ID в .env — ваш числовой id в Telegram (подскажет @userinfobot)")

    owner_name = raw.get("owner_name")
    if not owner_name:
        raise SystemExit("Укажите owner_name в config.yaml — как вас зовут в переписке")

    return Config(
        bot_token=token,
        owner_id=int(owner_id),
        owner_name=owner_name,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        persona_file=Path(raw.get("persona_file", "persona.md")),
        examples_file=Path(raw.get("examples_file", "data/examples.md")),
        knowledge_dir=Path(raw.get("knowledge_dir", "knowledge")),
        db_file=Path(raw.get("db_file", "data/twin.db")),
        stranger_reply=raw.get("stranger_reply", ""),
        report_private=bool(raw.get("report_private", True)),
        behavior=Behavior(**raw.get("behavior", {})),
        llm=LLM(**raw.get("llm", {})),
    )
