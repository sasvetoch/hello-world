"""Цифровой двойник: отвечает в выбранных чатах Telegram с вашего аккаунта.

Запуск: python -m twin [путь к config.yaml]
"""

import asyncio
import logging
import random
import sys
import time
from collections import deque
from datetime import datetime

from telethon import TelegramClient, events
from telethon.tl.custom import Message
from telethon.utils import get_display_name

from .config import Config, ChatRule, load_config
from .llm import ReplyGenerator, build_system_prompt

log = logging.getLogger("twin")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

HELP = (
    "Команды (пишите в «Избранное»):\n"
    "/twin on — включить\n"
    "/twin off — выключить\n"
    "/twin status — состояние"
)


def describe_media(msg: Message) -> str:
    if msg.sticker:
        alt = next((a.alt for a in msg.sticker.attributes if hasattr(a, "alt")), "")
        return f"[стикер {alt}]".replace(" ]", "]")
    if msg.voice:
        return "[голосовое сообщение]"
    if msg.video_note:
        return "[видеокружок]"
    if msg.photo:
        return "[фото]"
    if msg.video:
        return "[видео]"
    if msg.gif:
        return "[гифка]"
    if msg.document:
        return "[файл]"
    if msg.poll:
        return "[опрос]"
    if msg.geo:
        return "[геопозиция]"
    return ""


class Twin:
    def __init__(self, config: Config):
        self.cfg = config
        self.client = TelegramClient(config.session_name, config.api_id, config.api_hash)
        self.generator = ReplyGenerator(
            config.llm,
            build_system_prompt(config.persona_file, config.examples_file),
            config.anthropic_api_key,
        )
        self.enabled = True
        self.rules: dict[int, ChatRule] = {}
        self.pending: dict[int, asyncio.Task] = {}
        self.sent_log: dict[int, deque[float]] = {}
        self.manual_until: dict[int, float] = {}
        self.own_message_ids: set[int] = set()
        self.sending: set[int] = set()
        self.me_id = 0

    async def resolve_chats(self) -> None:
        for rule in self.cfg.chats:
            try:
                entity = await self.client.get_entity(rule.chat)
            except ValueError:
                log.error("Не удалось найти чат %s — проверьте config.yaml", rule.chat)
                continue
            peer_id = await self.client.get_peer_id(entity)
            self.rules[peer_id] = rule
            log.info("Слежу за чатом «%s» (%s), режим %s", get_display_name(entity), peer_id, rule.mode)
        if not self.rules:
            raise SystemExit("Ни один чат из config.yaml не найден")

    # ---------- обработчики ----------

    async def on_incoming(self, event: events.NewMessage.Event) -> None:
        if not self.enabled:
            return
        rule = self.rules.get(event.chat_id)
        if rule is None:
            return
        msg: Message = event.message
        sender = await msg.get_sender()
        if self.cfg.behavior.ignore_bots and getattr(sender, "bot", False):
            return

        if rule.mode == "mentions" and not msg.mentioned:
            return

        # Собеседник часто пишет несколько сообщений подряд — ждём, пока закончит.
        old = self.pending.pop(event.chat_id, None)
        if old:
            old.cancel()
        self.pending[event.chat_id] = asyncio.create_task(
            self.respond_later(event.chat_id, event.is_private, msg.mentioned)
        )

    async def on_outgoing(self, event: events.NewMessage.Event) -> None:
        msg: Message = event.message
        # Свои же отправленные ботом сообщения не считаем ручным ответом.
        if event.chat_id in self.sending or msg.id in self.own_message_ids:
            self.own_message_ids.discard(msg.id)
            return
        if event.chat_id == self.me_id and msg.raw_text.startswith("/twin"):
            await self.handle_command(event)
            return
        if event.chat_id in self.rules:
            # Вы ответили сами — бот уступает и не вмешивается какое-то время.
            minutes = self.cfg.behavior.pause_after_manual_minutes
            self.manual_until[event.chat_id] = time.time() + minutes * 60
            task = self.pending.pop(event.chat_id, None)
            if task:
                task.cancel()
            log.info("Вы пишете в чат %s сами — бот молчит %s мин", event.chat_id, minutes)

    async def handle_command(self, event: events.NewMessage.Event) -> None:
        arg = event.message.raw_text.removeprefix("/twin").strip().lower()
        if arg == "on":
            self.enabled = True
            text = "Двойник включён"
        elif arg == "off":
            self.enabled = False
            for task in self.pending.values():
                task.cancel()
            self.pending.clear()
            text = "Двойник выключен"
        elif arg == "status":
            now = time.time()
            paused = [str(c) for c, t in self.manual_until.items() if t > now]
            text = (
                f"Состояние: {'включён' if self.enabled else 'выключен'}\n"
                f"Чатов: {len(self.rules)}\n"
                f"На паузе после ваших ответов: {', '.join(paused) or 'нет'}\n"
                f"Тестовый режим: {'да' if self.cfg.dry_run else 'нет'}"
            )
        else:
            text = HELP
        await event.reply(text)

    # ---------- ответ ----------

    def rate_limited(self, chat_id: int) -> bool:
        log_ = self.sent_log.setdefault(chat_id, deque())
        hour_ago = time.time() - 3600
        while log_ and log_[0] < hour_ago:
            log_.popleft()
        return len(log_) >= self.cfg.behavior.max_replies_per_hour

    async def respond_later(self, chat_id: int, is_private: bool, mentioned: bool) -> None:
        b = self.cfg.behavior
        try:
            await asyncio.sleep(b.debounce_seconds)
            if time.time() < self.manual_until.get(chat_id, 0):
                return
            if self.rate_limited(chat_id):
                log.warning("Чат %s: достигнут лимит ответов в час", chat_id)
                return

            context = await self.build_context(chat_id, is_private, mentioned)
            replies = await self.generator.generate(context)
            if not replies:
                return

            await asyncio.sleep(random.uniform(b.min_delay_seconds, b.max_delay_seconds))
            for text in replies:
                # Пока «думали», вы могли ответить сами или выключить бота.
                if not self.enabled or time.time() < self.manual_until.get(chat_id, 0):
                    return
                if self.cfg.dry_run:
                    log.info("[тест] в чат %s было бы отправлено: %s", chat_id, text)
                    continue
                typing_time = min(len(text) / b.typing_chars_per_second, 20)
                async with self.client.action(chat_id, "typing"):
                    await asyncio.sleep(typing_time)
                self.sending.add(chat_id)
                try:
                    sent = await self.client.send_message(chat_id, text)
                    self.own_message_ids.add(sent.id)
                finally:
                    self.sending.discard(chat_id)
                self.sent_log.setdefault(chat_id, deque()).append(time.time())
                log.info("Чат %s ← %s", chat_id, text)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Ошибка при ответе в чат %s", chat_id)
        finally:
            if self.pending.get(chat_id) is asyncio.current_task():
                del self.pending[chat_id]

    async def build_context(self, chat_id: int, is_private: bool, mentioned: bool) -> str:
        chat = await self.client.get_entity(chat_id)
        history = await self.client.get_messages(chat_id, limit=self.cfg.behavior.history_limit)
        names: dict[int, str] = {}
        lines = []
        for msg in reversed(history):
            if msg.out:
                author = "Я"
            else:
                sid = msg.sender_id or 0
                if sid not in names:
                    sender = await msg.get_sender()
                    names[sid] = get_display_name(sender) if sender else "Неизвестный"
                author = names[sid]
            body = " ".join(filter(None, [describe_media(msg), msg.raw_text or ""])).strip()
            if not body:
                continue
            reply_note = f" (в ответ на #{msg.reply_to_msg_id})" if msg.reply_to_msg_id else ""
            stamp = msg.date.astimezone().strftime("%d.%m %H:%M")
            lines.append(f"#{msg.id} [{stamp}] {author}{reply_note}: {body}")

        now = datetime.now().astimezone()
        kind = "личная переписка" if is_private else "групповой чат"
        hint = ""
        if not is_private:
            hint = (
                "\nВ последнем сообщении тебя упомянули или ответили тебе — скорее всего, нужен ответ."
                if mentioned else
                "\nТебя не упоминали. Отвечай, только если без тебя разговор явно не обойдётся."
            )
        return (
            f"Чат: «{get_display_name(chat)}», {kind}.\n"
            f"Сейчас: {WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')}.\n"
            f"«Я» в переписке — это ты.{hint}\n\n"
            "Последние сообщения:\n" + "\n".join(lines) + "\n\n"
            "Реши, нужно ли ответить, и если да — напиши ответ."
        )

    # ---------- запуск ----------

    async def run(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        self.me_id = me.id
        log.info("Вошли как %s", get_display_name(me))
        await self.resolve_chats()

        self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        log.info("Двойник запущен%s. %s", " в тестовом режиме" if self.cfg.dry_run else "", HELP.replace("\n", " "))
        await self.client.run_until_disconnected()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    asyncio.run(Twin(config).run())


if __name__ == "__main__":
    main()
