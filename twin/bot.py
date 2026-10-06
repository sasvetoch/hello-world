"""Цифровой двойник — отдельный Telegram-бот, который общается в манере владельца.

Запуск: python -m twin [путь к config.yaml]
"""

import asyncio
import html
import json
import logging
import random
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from .config import Config, load_config
from .examples import extract_examples
from .llm import ReplyGenerator
from .storage import Storage

log = logging.getLogger("twin")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

OWNER_HELP = """\
<b>Обучение</b>
/learn <i>факт</i> — запомнить факт («до 20 октября я в отпуске»)
/memory — что я запомнил
/forget <i>номер</i> — забыть факт (12) или исправление (и12)
/fix <i>текст</i> — ответом на моё сообщение: «надо было ответить так»
/reload — перечитать persona.md, примеры и папку knowledge

<b>Файлы</b> — просто пришлите их мне:
• <code>persona.md</code> — заменить описание личности
• <code>result.json</code> — экспорт Telegram, я выберу из него примеры вашего стиля
• <code>examples.md</code> — заменить примеры стиля (например, после правки)
• любой другой <code>.md</code> или <code>.txt</code> — добавить в базу знаний
/files — что сейчас загружено, /get — прислать persona.md и examples.md
/delete <i>имя</i> — удалить файл из базы знаний

<b>Чаты</b>
/chats — список чатов
/allow <i>id</i>, /block <i>id</i> — разрешить или запретить чат
/mode <i>id</i> all|mentions — отвечать на всё или только на обращения
В группе: /enable или /enable mentions — включить меня там, /disable — выключить

<b>Работа</b>
/on, /off — включить или выключить меня везде
/status — состояние

На мои отчёты и вопросы просто отвечайте (reply): отчёт — правильным вариантом ответа, \
вопрос — ответом, который я передам собеседнику.
Всё остальное, что вы мне пишете, — тестовый диалог: так можно проверить, как я отвечаю."""


def describe(message: Message) -> str:
    """Текст сообщения для модели, с пометками о медиа."""
    media = ""
    if message.sticker:
        media = f"[стикер {message.sticker.emoji or ''}]".replace(" ]", "]")
    elif message.voice:
        media = "[голосовое сообщение]"
    elif message.video_note:
        media = "[видеокружок]"
    elif message.photo:
        media = "[фото]"
    elif message.video:
        media = "[видео]"
    elif message.animation:
        media = "[гифка]"
    elif message.document:
        media = "[файл]"
    elif message.poll:
        media = f"[опрос: {message.poll.question}]"
    elif message.location:
        media = "[геопозиция]"
    text = message.text or message.caption or ""
    return " ".join(filter(None, [media, text])).strip()


class Twin:
    def __init__(self, config: Config):
        self.cfg = config
        self.bot = Bot(config.bot_token)
        self.store = Storage(config.db_file)
        self.generator = ReplyGenerator(
            config.llm, config.owner_name, config.persona_file,
            config.examples_file, config.knowledge_dir, config.anthropic_api_key,
        )
        self.enabled = True
        self.me_id = 0
        self.me_username = ""
        self.pending: dict[int, asyncio.Task] = {}
        self.sent_log: dict[int, deque[float]] = {}
        self.dp = Dispatcher()
        self.dp.include_router(self._build_router())

    # ---------- роутинг ----------

    def _build_router(self) -> Router:
        r = Router()
        owner = F.from_user.id == self.cfg.owner_id
        private = F.chat.type == ChatType.PRIVATE
        group = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

        r.message.register(self.cmd_help, Command("start", "help"), owner, private)
        r.message.register(self.cmd_learn, Command("learn"), owner, private)
        r.message.register(self.cmd_memory, Command("memory"), owner, private)
        r.message.register(self.cmd_forget, Command("forget"), owner, private)
        r.message.register(self.cmd_reload, Command("reload"), owner, private)
        r.message.register(self.cmd_chats, Command("chats"), owner, private)
        r.message.register(self.cmd_set_status, Command("allow", "block"), owner, private)
        r.message.register(self.cmd_mode, Command("mode"), owner, private)
        r.message.register(self.cmd_on_off, Command("on", "off"), owner, private)
        r.message.register(self.cmd_status, Command("status"), owner, private)
        r.message.register(self.cmd_fix, Command("fix"), owner)
        r.message.register(self.cmd_enable_here, Command("enable", "disable"), owner, group)
        r.message.register(self.cmd_files, Command("files"), owner, private)
        r.message.register(self.cmd_get, Command("get"), owner, private)
        r.message.register(self.cmd_delete, Command("delete"), owner, private)
        r.message.register(self.on_owner_document, F.document, owner, private)
        r.message.register(self.on_owner_private, owner, private)
        r.message.register(self.on_private, private)
        r.message.register(self.on_group, group)
        r.callback_query.register(self.on_access_button, F.data.startswith("access:"), owner)
        r.my_chat_member.register(self.on_added_to_chat)
        return r

    async def notify_owner(self, text: str, **kwargs) -> Message | None:
        try:
            return await self.bot.send_message(self.cfg.owner_id, text, parse_mode="HTML", **kwargs)
        except TelegramAPIError as e:
            log.error("Не удалось написать владельцу (нажмите /start в чате с ботом): %s", e)
            return None

    # ---------- команды владельца ----------

    async def cmd_help(self, message: Message) -> None:
        await message.answer(OWNER_HELP, parse_mode="HTML")

    async def cmd_learn(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Напишите факт после команды: /learn до 20 октября я в отпуске")
            return
        fact_id = self.store.add_fact(command.args.strip())
        await message.answer(f"Запомнил (№{fact_id})")

    async def cmd_memory(self, message: Message) -> None:
        facts = self.store.facts()
        corrections = self.store.corrections()
        lines = ["<b>Факты</b>"] + [f"{i}. {html.escape(t)}" for i, t in facts]
        if not facts:
            lines.append("пока нет")
        lines.append("\n<b>Исправления</b>")
        lines += [f"и{i}. {html.escape(bad[:60])} → {html.escape(good[:80])}" for i, _, bad, good in corrections]
        if not corrections:
            lines.append("пока нет")
        await self._answer_long(message, "\n".join(lines))

    async def _answer_long(self, message: Message, text: str) -> None:
        for start in range(0, len(text), 4000):
            await message.answer(text[start:start + 4000], parse_mode="HTML")

    async def cmd_forget(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Укажите номер: /forget 12 (факт) или /forget и12 (исправление)")
            return
        ok = self.store.forget(command.args)
        await message.answer("Забыл" if ok else "Такой записи нет — номера можно посмотреть в /memory")

    # ---------- файлы ----------

    async def on_owner_document(self, message: Message) -> None:
        doc = message.document
        name = Path(doc.file_name or "file.txt").name
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.answer("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. "
                                 "Для экспорта выберите меньше чатов или экспортируйте их по одному.")
            return
        if not name.lower().endswith((".md", ".txt", ".json")):
            await message.answer("Я понимаю только .md, .txt и экспорт Telegram в .json")
            return
        buf = await self.bot.download(doc)
        raw = buf.read() if buf else b""
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("Не могу прочитать файл: нужен текст в кодировке UTF-8")
            return

        lower = name.lower()
        if lower == "persona.md":
            self.cfg.persona_file.write_text(text, encoding="utf-8")
            reply = "Обновил описание личности"
        elif lower == "examples.md":
            self._write(self.cfg.examples_file, text)
            reply = "Обновил примеры стиля"
        elif lower.endswith(".json"):
            try:
                examples, total = extract_examples(json.loads(text))
            except (ValueError, KeyError, TypeError) as e:
                await message.answer(f"Не получилось разобрать экспорт: {e}")
                return
            self._write(self.cfg.examples_file, examples)
            await message.answer_document(
                BufferedInputFile(examples.encode("utf-8"), "examples.md"),
                caption=(f"Нашёл {total} ваших ответов, взял {min(total, 60)} в примеры. "
                         "Просмотрите файл: если там есть лишнее (адреса, телефоны, личное), "
                         "удалите это и пришлите файл обратно с тем же именем."),
            )
            reply = ""
        else:
            self._write(self.cfg.knowledge_dir / name, text)
            reply = f"Добавил «{name}» в базу знаний"
        try:
            self.generator.reload()
        except SystemExit as e:
            await message.answer(str(e))
            return
        if reply:
            await message.answer(reply)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    async def cmd_files(self, message: Message) -> None:
        def size(path: Path) -> str:
            return f"{path.stat().st_size // 1024 + 1} КБ" if path.exists() else "нет"
        lines = [
            f"Описание (persona.md): {size(self.cfg.persona_file)}",
            f"Примеры стиля (examples.md): {size(self.cfg.examples_file)}",
            "\n<b>База знаний</b>",
        ]
        kd = self.cfg.knowledge_dir
        files = sorted(p for p in kd.rglob("*") if p.is_file()) if kd.is_dir() else []
        lines += [f"• {html.escape(str(p.relative_to(kd)))} — {size(p)}" for p in files] or ["пусто"]
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_get(self, message: Message) -> None:
        for path in (self.cfg.persona_file, self.cfg.examples_file):
            if path.exists():
                await message.answer_document(BufferedInputFile(path.read_bytes(), path.name))

    async def cmd_delete(self, message: Message, command: CommandObject) -> None:
        name = Path((command.args or "").strip()).name
        path = self.cfg.knowledge_dir / name
        if not name or not path.is_file():
            await message.answer("Такого файла нет — список в /files")
            return
        path.unlink()
        self.generator.reload()
        await message.answer(f"Удалил «{name}» из базы знаний")

    async def cmd_reload(self, message: Message) -> None:
        try:
            self.generator.reload()
        except SystemExit as e:
            await message.answer(str(e))
            return
        await message.answer("Перечитал описание, примеры и базу знаний")

    async def cmd_chats(self, message: Message) -> None:
        chats = self.store.list_chats()
        if not chats:
            await message.answer("Чатов пока нет. Добавьте меня в группу или дайте ссылку на меня собеседнику.")
            return
        icons = {"allowed": "✅", "pending": "⏳", "blocked": "⛔"}
        lines = [
            f"{icons[c.status]} {html.escape(c.title)} — <code>{c.chat_id}</code>"
            + (" (только обращения)" if c.mode == "mentions" else "")
            for c in chats
        ]
        await self._answer_long(message, "\n".join(lines))

    async def cmd_set_status(self, message: Message, command: CommandObject) -> None:
        chat = self._chat_from_args(command.args)
        if not chat:
            await message.answer("Укажите id чата из /chats")
            return
        status = "allowed" if command.command == "allow" else "blocked"
        self.store.upsert_chat(chat.chat_id, chat.title, chat.kind, status=status)
        await message.answer(f"{'Разрешил' if status == 'allowed' else 'Запретил'}: {chat.title}")

    async def cmd_mode(self, message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        chat = self._chat_from_args(parts[0] if parts else None)
        if not chat or len(parts) < 2 or parts[1] not in ("all", "mentions"):
            await message.answer("Формат: /mode <id> all или /mode <id> mentions")
            return
        self.store.upsert_chat(chat.chat_id, chat.title, chat.kind, mode=parts[1])
        await message.answer(f"Режим для «{chat.title}»: {parts[1]}")

    def _chat_from_args(self, args: str | None):
        if not args or not args.strip().lstrip("-").isdigit():
            return None
        return self.store.get_chat(int(args.strip()))

    async def cmd_on_off(self, message: Message, command: CommandObject) -> None:
        self.enabled = command.command == "on"
        if not self.enabled:
            for task in self.pending.values():
                task.cancel()
            self.pending.clear()
        await message.answer("Включён" if self.enabled else "Выключен — не отвечаю нигде, кроме этого чата")

    async def cmd_status(self, message: Message) -> None:
        chats = self.store.list_chats()
        allowed = sum(c.status == "allowed" for c in chats)
        pending = sum(c.status == "pending" for c in chats)
        await message.answer(
            f"Состояние: {'включён' if self.enabled else 'выключен'}\n"
            f"Чатов разрешено: {allowed}, ждут решения: {pending}\n"
            f"Фактов: {len(self.store.facts())}, исправлений: {len(self.store.corrections())}"
        )

    async def cmd_enable_here(self, message: Message, command: CommandObject) -> None:
        enable = command.command == "enable"
        mode = "mentions" if (command.args or "").strip() == "mentions" else "all"
        self.store.upsert_chat(
            message.chat.id, message.chat.title or "группа", "group",
            status="allowed" if enable else "blocked", mode=mode if enable else None,
        )
        await self._try_delete(message)
        await self.notify_owner(
            f"{'Включил' if enable else 'Выключил'} себя в «{html.escape(message.chat.title or '')}»"
            + (" — отвечаю только на обращения" if enable and mode == "mentions" else "")
        )

    async def cmd_fix(self, message: Message, command: CommandObject) -> None:
        good = (command.args or "").strip()
        target = message.reply_to_message
        if not good or not target:
            await self.notify_owner("/fix нужно отправить ответом на моё сообщение: /fix как надо было ответить")
            return
        if message.chat.id == self.cfg.owner_id:
            thread = self.store.get_owner_thread(target.message_id)
            if thread and thread[0] == "report":
                await self._apply_correction(thread[1], thread[2], good, thread[3])
                await message.answer("Исправил и запомнил")
                return
        if not target.from_user or target.from_user.id != self.me_id:
            await self.notify_owner("/fix работает только ответом на моё сообщение")
            return
        context = self._format_history(message.chat.id, before_msg_id=target.message_id)
        await self._apply_correction(message.chat.id, target.message_id, good, context)
        if message.chat.id != self.cfg.owner_id:
            await self._try_delete(message)
        else:
            await message.answer("Исправил и запомнил")

    async def _apply_correction(self, chat_id: int, bot_msg_id: int | None, good: str, context: str) -> None:
        bad = ""
        if bot_msg_id:
            hist = [m for m in self.store.history(chat_id, 1, before_msg_id=bot_msg_id + 1) if m.msg_id == bot_msg_id]
            bad = hist[0].text if hist else ""
            try:
                await self.bot.edit_message_text(text=good, chat_id=chat_id, message_id=bot_msg_id)
                self.store.update_message_text(chat_id, bot_msg_id, good)
            except TelegramAPIError as e:
                log.warning("Не удалось исправить сообщение %s в чате %s: %s", bot_msg_id, chat_id, e)
        self.store.add_correction(context, bad or "(не помню)", good)

    async def _try_delete(self, message: Message) -> None:
        try:
            await message.delete()
        except TelegramAPIError:
            pass  # без прав администратора удалить чужое сообщение нельзя

    # ---------- доступ ----------

    async def on_added_to_chat(self, update: ChatMemberUpdated) -> None:
        if update.chat.type == ChatType.PRIVATE:
            return
        status = update.new_chat_member.status
        title = update.chat.title or "группа"
        if status in ("left", "kicked"):
            self.store.upsert_chat(update.chat.id, title, "group", status="blocked")
            return
        if status not in ("member", "administrator"):
            return
        if update.from_user.id == self.cfg.owner_id:
            self.store.upsert_chat(update.chat.id, title, "group", status="allowed")
            await self.notify_owner(
                f"Вы добавили меня в «{html.escape(title)}» — отвечаю там. "
                f"Чтобы я видел все сообщения, а не только обращения, отключите мне режим приватности "
                f"в @BotFather (/setprivacy → Disable) или сделайте администратором."
            )
            return
        self.store.upsert_chat(update.chat.id, title, "group", status="pending")
        await self._ask_access(update.chat.id, f"Меня добавили в группу «{html.escape(title)}» "
                                               f"(добавил: {html.escape(update.from_user.full_name)}).")

    async def _ask_access(self, chat_id: int, text: str) -> None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Разрешить", callback_data=f"access:allow:{chat_id}"),
            InlineKeyboardButton(text="Запретить", callback_data=f"access:block:{chat_id}"),
        ]])
        await self.notify_owner(text + "\nОтвечать там?", reply_markup=kb)

    async def on_access_button(self, query: CallbackQuery) -> None:
        _, action, chat_id = query.data.split(":")
        chat = self.store.get_chat(int(chat_id))
        if not chat:
            await query.answer("Чат не найден")
            return
        status = "allowed" if action == "allow" else "blocked"
        self.store.upsert_chat(chat.chat_id, chat.title, chat.kind, status=status)
        await query.answer("Готово")
        if query.message:
            verdict = "✅ разрешён" if status == "allowed" else "⛔ запрещён"
            await query.message.edit_text(f"{html.escape(chat.title)}: {verdict}", parse_mode="HTML")

    # ---------- сообщения ----------

    async def on_owner_private(self, message: Message) -> None:
        target = message.reply_to_message
        thread = self.store.get_owner_thread(target.message_id) if target else None
        text = describe(message)
        if thread and text:
            kind, chat_id, target_id, payload = thread
            if kind == "report":
                await self._apply_correction(chat_id, target_id, text, payload)
                await message.answer("Исправил и запомнил")
                return
            if kind == "question":
                self.store.add_fact(f"Вопрос: {payload} Ответ: {text}")
                await message.answer("Спасибо, передаю и запоминаю")
                note = (f"{self.cfg.owner_name} ответил(а) на твой вопрос «{payload}»: «{text}». "
                        f"Передай это собеседнику своими словами.")
                chat = self.store.get_chat(chat_id)
                self._schedule(chat_id, is_private=bool(chat and chat.kind == "private"),
                               mentioned=True, note=note, delay=False)
                return
        # Всё остальное — тестовый диалог владельца с двойником.
        self._record(message, role="owner")
        self._schedule(message.chat.id, is_private=True, mentioned=True)

    async def on_private(self, message: Message) -> None:
        user = message.from_user
        if not user or user.is_bot:
            return
        chat = self.store.get_chat(message.chat.id)
        if chat is None:
            chat = self.store.upsert_chat(message.chat.id, user.full_name, "private", status="pending")
            username = f" @{user.username}" if user.username else ""
            await self._ask_access(
                message.chat.id,
                f"Мне написал(а) {html.escape(user.full_name)}{username}:\n"
                f"«{html.escape(describe(message)[:300])}»",
            )
            if self.cfg.stranger_reply:
                await message.answer(self.cfg.stranger_reply)
        if chat.status != "allowed":
            return
        self._record(message, role="other")
        self._schedule(message.chat.id, is_private=True, mentioned=True)

    async def on_group(self, message: Message) -> None:
        chat = self.store.get_chat(message.chat.id)
        if not chat or chat.status != "allowed":
            return
        user = message.from_user
        if not user:
            return
        is_owner = user.id == self.cfg.owner_id
        if user.is_bot and self.cfg.behavior.ignore_bots:
            return
        self._record(message, role="owner" if is_owner else "other")
        if is_owner:
            # Вы сами в разговоре — двойник не перебивает и отменяет запланированный ответ.
            task = self.pending.pop(message.chat.id, None)
            if task:
                task.cancel()
            return
        mentioned = self._is_addressed(message)
        if chat.mode == "mentions" and not mentioned:
            return
        self._schedule(message.chat.id, is_private=False, mentioned=mentioned)

    def _is_addressed(self, message: Message) -> bool:
        reply = message.reply_to_message
        if reply and reply.from_user and reply.from_user.id == self.me_id:
            return True
        text = (message.text or message.caption or "").lower()
        return bool(self.me_username) and f"@{self.me_username.lower()}" in text

    def _record(self, message: Message, role: str) -> None:
        text = describe(message)
        if not text:
            return
        if role == "owner":
            author = f"{self.cfg.owner_name} [владелец]"
        else:
            author = message.from_user.full_name if message.from_user else "Неизвестный"
        reply_to = message.reply_to_message.message_id if message.reply_to_message else None
        self.store.add_message(message.chat.id, message.message_id, author, role, text,
                               reply_to, message.date.timestamp())

    # ---------- ответ ----------

    def _schedule(self, chat_id: int, is_private: bool, mentioned: bool,
                  note: str = "", delay: bool = True) -> None:
        if not self.enabled and chat_id != self.cfg.owner_id:
            return
        # Собеседник часто пишет несколько сообщений подряд — ждём, пока закончит.
        old = self.pending.pop(chat_id, None)
        if old:
            old.cancel()
        self.pending[chat_id] = asyncio.create_task(self._respond(chat_id, is_private, mentioned, note, delay))

    def _rate_limited(self, chat_id: int) -> bool:
        sent = self.sent_log.setdefault(chat_id, deque())
        hour_ago = time.time() - 3600
        while sent and sent[0] < hour_ago:
            sent.popleft()
        return len(sent) >= self.cfg.behavior.max_replies_per_hour

    async def _respond(self, chat_id: int, is_private: bool, mentioned: bool, note: str, delay: bool) -> None:
        b = self.cfg.behavior
        try:
            if delay:
                await asyncio.sleep(b.debounce_seconds)
            if not is_private and not mentioned:
                owner_idle = time.time() - self.store.last_owner_message_time(chat_id)
                if owner_idle < b.owner_active_minutes * 60:
                    return
            if self._rate_limited(chat_id):
                log.warning("Чат %s: достигнут лимит ответов в час", chat_id)
                return

            context = self._build_context(chat_id, is_private, mentioned, note)
            decision = await self.generator.decide(context, self.store.facts(), self.store.corrections())

            if decision.ask_owner and chat_id != self.cfg.owner_id:
                await self._ask_owner(chat_id, decision.ask_owner)
            if not decision.messages:
                return

            if delay:
                await asyncio.sleep(random.uniform(b.min_delay_seconds, b.max_delay_seconds))
            # Что написал собеседник после последнего ответа двойника — для отчёта владельцу.
            recent = self.store.history(chat_id, 10)
            last_twin = max((i for i, m in enumerate(recent) if m.role == "twin"), default=-1)
            last_incoming = recent[last_twin + 1:]
            for text in decision.messages:
                if not self.enabled and chat_id != self.cfg.owner_id:
                    return
                await self._type(chat_id, len(text))
                sent = await self.bot.send_message(chat_id, text)
                self.store.add_message(chat_id, sent.message_id, "Ты (двойник)", "twin", text)
                self.sent_log.setdefault(chat_id, deque()).append(time.time())
                log.info("Чат %s ← %s", chat_id, text)
            if is_private and chat_id != self.cfg.owner_id and self.cfg.report_private:
                await self._report(chat_id, sent.message_id, last_incoming, decision.messages, context)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Ошибка при ответе в чат %s", chat_id)
        finally:
            if self.pending.get(chat_id) is asyncio.current_task():
                del self.pending[chat_id]

    async def _type(self, chat_id: int, length: int) -> None:
        remaining = min(length / self.cfg.behavior.typing_chars_per_second, 15)
        while remaining > 0:
            await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
            step = min(4.5, remaining)  # статус «печатает…» держится около 5 секунд
            await asyncio.sleep(step)
            remaining -= step

    async def _ask_owner(self, chat_id: int, question: str) -> None:
        chat = self.store.get_chat(chat_id)
        last = self.store.history(chat_id, 3)
        quoted = "\n".join(f"{html.escape(m.author)}: {html.escape(m.text[:300])}" for m in last)
        sent = await self.notify_owner(
            f"❓ <b>{html.escape(chat.title if chat else str(chat_id))}</b>\n{quoted}\n\n"
            f"Нужна ваша помощь: {html.escape(question)}\n\n"
            f"<i>Ответьте на это сообщение — передам собеседнику и запомню.</i>"
        )
        if sent:
            self.store.add_owner_thread(sent.message_id, "question", chat_id, None, question)

    async def _report(self, chat_id: int, bot_msg_id: int, incoming, replies: list[str], context: str) -> None:
        chat = self.store.get_chat(chat_id)
        said = "\n".join(f"{html.escape(m.author)}: {html.escape(m.text[:300])}" for m in incoming) \
            or "(по вашему ответу на вопрос)"
        answer = "\n".join(html.escape(r) for r in replies)
        sent = await self.notify_owner(
            f"💬 <b>{html.escape(chat.title if chat else str(chat_id))}</b>\n{said}\n\n🤖 {answer}\n\n"
            f"<i>Не так? Ответьте на это сообщение правильным вариантом — исправлю и запомню.</i>"
        )
        if sent:
            self.store.add_owner_thread(sent.message_id, "report", chat_id, bot_msg_id, context)

    def _format_history(self, chat_id: int, before_msg_id: int | None = None) -> str:
        lines = []
        for m in self.store.history(chat_id, self.cfg.behavior.history_limit, before_msg_id):
            stamp = datetime.fromtimestamp(m.date).astimezone().strftime("%d.%m %H:%M")
            reply_note = f" (в ответ на #{m.reply_to})" if m.reply_to else ""
            lines.append(f"#{m.msg_id} [{stamp}] {m.author}{reply_note}: {m.text}")
        return "\n".join(lines)

    def _build_context(self, chat_id: int, is_private: bool, mentioned: bool, note: str) -> str:
        chat = self.store.get_chat(chat_id)
        now = datetime.now().astimezone()
        if chat_id == self.cfg.owner_id:
            where = f"личный чат с самим владельцем ({self.cfg.owner_name}) — он(а) тестирует, как ты отвечаешь"
        elif is_private:
            where = f"личная переписка с «{chat.title}»"
        else:
            where = f"групповой чат «{chat.title}»"
        hint = ""
        if not is_private:
            hint = ("\nК тебе обратились напрямую — скорее всего, нужен ответ." if mentioned else
                    "\nК тебе не обращались. Отвечай, только если без тебя разговор явно не обойдётся.")
        if note:
            hint += f"\n{note}"
        return (
            f"Где ты: {where}.\n"
            f"Сейчас: {WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')}.\n"
            f"«Ты (двойник)» в переписке — это ты.{hint}\n\n"
            "Последние сообщения:\n" + self._format_history(chat_id) + "\n\n"
            "Реши, нужно ли ответить, и если да — напиши ответ."
        )

    # ---------- запуск ----------

    async def run(self) -> None:
        me = await self.bot.get_me()
        self.me_id, self.me_username = me.id, me.username or ""
        log.info("Бот @%s запущен. Владелец: %s", self.me_username, self.cfg.owner_id)
        await self.notify_owner("Я запущен. /help — список команд")
        if "Скопируйте в persona.md" in self.cfg.persona_file.read_text(encoding="utf-8"):
            await self.notify_owner(
                "Описание личности ещё не заполнено. Скачайте шаблон командой /get, "
                "заполните его и пришлите мне файл <code>persona.md</code>."
            )
        await self.dp.start_polling(self.bot, allowed_updates=["message", "callback_query", "my_chat_member"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    asyncio.run(Twin(config).run())


if __name__ == "__main__":
    main()
