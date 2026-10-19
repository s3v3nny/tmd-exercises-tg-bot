"""Напоминания об упражнениях для челюсти по материалу wikiHow."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE = "https://www.wikihow.com/Treat-Temporomandibular-Joint-Disorder-(TMD)-with-Jaw-Exercises"
SESSION_INTERVAL = 150  # Статья рекомендует 6 подходов в день, но не задаёт интервал.
SESSIONS_PER_DAY = 6
DELIVERY_GRACE = timedelta(minutes=5)
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Samara")
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class User:
    chat_id: int
    start_minute: int
    timezone_name: str
    enabled: bool


def parse_time(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError("Время нужно указать в формате ЧЧ:ММ, например 08:00.")
    hour, minute = map(int, parts)
    if hour > 23 or minute > 59:
        raise ValueError("Время нужно указать в формате ЧЧ:ММ, например 08:00.")
    return hour * 60 + minute


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def session_times(start_minute: int) -> list[tuple[str, bool]]:
    result = []
    for slot in range(SESSIONS_PER_DAY):
        minutes = start_minute + slot * SESSION_INTERVAL
        result.append((format_time(minutes % 1440), minutes >= 1440))
    return result


def due_sessions(user: User, now: datetime) -> list[tuple[str, int]]:
    """Return (anchor date, slot) for recent sessions in the user's local time."""
    local_now = now.astimezone(ZoneInfo(user.timezone_name))
    due = []
    for anchor in (local_now.date() - timedelta(days=1), local_now.date()):
        start = datetime.combine(anchor, datetime.min.time()) + timedelta(minutes=user.start_minute)
        for slot in range(SESSIONS_PER_DAY):
            scheduled = start + timedelta(minutes=slot * SESSION_INTERVAL)
            elapsed = local_now.replace(tzinfo=None) - scheduled
            if timedelta() <= elapsed < DELIVERY_GRACE:
                due.append((anchor.isoformat(), slot))
    return due


def reminder_text(slot: int) -> str:
    """Original Russian descriptions based on the cited article."""
    common = (
        "1. Открывание с сопротивлением — положите два пальца под подбородок. "
        "Мягко удерживайте его и медленно откройте рот. 6 раз.\n\n"
        "2. Закрывание с сопротивлением — откройте рот, положите два пальца под нижнюю губу. "
        "Слегка давите вниз, пока медленно закрываете рот. 6 раз.\n\n"
        "3. Открывание с языком у нёба — коснитесь языком нёба сразу за верхними зубами. "
        "Не отрывая язык, плавно опускайте и поднимайте челюсть, расслабляя мышцы. 6 раз.\n\n"
        "4. «Золотая рыбка» — положите два пальца на сустав перед ухом, "
        "палец другой руки — на подбородок. Слегка поддерживая сустав, откройте рот без сопротивления подбородку. 6 раз."
    )
    extras = (
        "Дополнительно сегодня: выпрямитесь, отведите подбородок прямо назад, словно делаете «двойной подбородок». "
        "Задержитесь на 3 секунды, расслабьтесь. Всего 10 повторений за день.",
        "В течение дня держите зубы немного разомкнутыми. Для расслабления можно медленно вдохнуть носом "
        "за 5 секунд и выдохнуть за 5 секунд, постепенно отпуская напряжение в челюсти. Повторяйте по желанию.",
        "Если нужна большая подвижность: поместите между зубами безопасную для рта палочку толщиной примерно 0,5–1,3 см, "
        "выведя её конец вперёд. Осторожно подайте нижнюю челюсть вперёд, направляя конец палочки вверх. "
        "Увеличивайте толщину только когда прежняя даётся комфортно. Делайте по необходимости, например перед едой.",
        "Если нужна боковая подвижность: поместите безопасную для рта палочку толщиной примерно 0,5–1,3 см "
        "между зубами горизонтально. Плавно перемещайте нижнюю челюсть из стороны в сторону. Делайте по необходимости.",
        "Следите за осанкой: встаньте у стены, сведите лопатки и отведите подбородок назад и чуть к груди. "
        "Не выводите голову вперёд при ходьбе.",
        "Расслабьте челюсть: зубы слегка разомкнуты. Медленно вдохните носом за 5 секунд, "
        "затем выдохните за 5 секунд, расслабляя жевательные мышцы. Дыхание можно повторять по желанию.",
    )
    return (
        f"Упражнения для челюсти · подход {slot + 1}/6\n\n{common}\n\n{extras[slot]}\n\n"
        "Движения должны быть комфортными. При боли прекратите упражнение и обратитесь к стоматологу или врачу.\n"
        f"Источник: {SOURCE}"
    )


class Storage:
    def __init__(self, path: str):
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                start_minute INTEGER NOT NULL,
                timezone_name TEXT NOT NULL,
                enabled INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                chat_id INTEGER NOT NULL,
                anchor_date TEXT NOT NULL,
                slot INTEGER NOT NULL,
                PRIMARY KEY(chat_id, anchor_date, slot)
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def get_user(self, chat_id: int) -> User | None:
        row = self.connection.execute(
            "SELECT chat_id, start_minute, timezone_name, enabled FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return User(row[0], row[1], row[2], bool(row[3])) if row else None

    def save_user(self, user: User) -> None:
        self.connection.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "start_minute=excluded.start_minute, timezone_name=excluded.timezone_name, enabled=excluded.enabled",
            (user.chat_id, user.start_minute, user.timezone_name, int(user.enabled)),
        )
        self.connection.commit()

    def active_users(self) -> list[User]:
        rows = self.connection.execute(
            "SELECT chat_id, start_minute, timezone_name, enabled FROM users WHERE enabled = 1"
        ).fetchall()
        return [User(*row) for row in rows]

    def was_sent(self, chat_id: int, anchor_date: str, slot: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE chat_id = ? AND anchor_date = ? AND slot = ?",
            (chat_id, anchor_date, slot),
        ).fetchone() is not None

    def mark_sent(self, chat_id: int, anchor_date: str, slot: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)", (chat_id, anchor_date, slot)
        )
        self.connection.commit()

    def get_offset(self) -> int:
        row = self.connection.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int) -> None:
        self.connection.execute(
            "INSERT INTO state VALUES ('offset', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(offset),),
        )
        self.connection.commit()

    def cleanup(self, today: date) -> None:
        self.connection.execute(
            "DELETE FROM deliveries WHERE anchor_date < ?", ((today - timedelta(days=2)).isoformat(),)
        )
        self.connection.commit()


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, data: dict) -> dict:
        request = Request(
            self.base_url + method,
            data=urlencode(data).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=40) as response:
                payload = json.load(response)
        except HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError):
                raise TelegramError(exc.code, str(exc)) from exc
        if not payload.get("ok"):
            raise TelegramError(payload.get("error_code", 0), payload.get("description", "Ошибка Telegram API"))
        return payload["result"]

    def send_message(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})


def status_text(user: User) -> str:
    times = ", ".join(
        time_text + (" (+1 день)" if tomorrow else "")
        for time_text, tomorrow in session_times(user.start_minute)
    )
    state = "включены" if user.enabled else "приостановлены"
    return (
        f"Напоминания {state}.\nНачало: {format_time(user.start_minute)}. "
        f"Часовой пояс: {user.timezone_name}.\nПодходы: {times}.\n\n"
        "В статье рекомендовано 6 подходов в день, но интервал не указан. "
        "Здесь они распределены через 2 часа 30 минут.\n"
        "Изменить: /time ЧЧ:ММ, /timezone Регион/Город."
    )


HELP = (
    "Я напоминаю об упражнениях для челюсти по статье wikiHow.\n\n"
    "/start [ЧЧ:ММ] — включить напоминания (по умолчанию 08:00)\n"
    "/time ЧЧ:ММ — изменить время первого подхода\n"
    "/timezone Регион/Город — указать часовой пояс, например Europe/Samara\n"
    "/status — показать расписание\n"
    "/exercises — показать все упражнения и частоту\n"
    "/pause — приостановить, /resume — возобновить\n\n"
    "Если движение вызывает боль, остановитесь и обратитесь к стоматологу или врачу."
)


def exercises_text() -> str:
    return (
        "План по статье wikiHow:\n\n"
        "6 раз в день по 6 повторений: открывание и закрывание с лёгким сопротивлением пальцев, "
        "медленное открывание и закрывание с языком у нёба, «золотая рыбка» с мягкой поддержкой сустава. "
        "Подробная техника приходит в каждом напоминании.\n\n"
        "10 раз в день: отведение подбородка назад с удержанием 3 секунды (описание в первом подходе).\n\n"
        "По желанию: дыхание с расслаблением челюсти (вдох 5 секунд, выдох 5 секунд); "
        "держать зубы слегка разомкнутыми.\n\n"
        "По необходимости для подвижности: движение нижней челюсти вперёд и из стороны в сторону "
        "с безопасной палочкой между зубами. Описание приходит в третьем и четвёртом подходах.\n\n"
        "Следите за осанкой: отводите подбородок назад, расправляйте плечи.\n"
        f"Источник: {SOURCE}"
    )


def handle_message(storage: Storage, api: TelegramAPI, message: dict) -> None:
    chat = message.get("chat", {})
    if chat.get("type") != "private" or not isinstance(message.get("text"), str):
        return
    chat_id = chat["id"]
    raw = message["text"].strip().split(maxsplit=1)
    if not raw:
        return
    command = raw[0].split("@")[0].lower()
    argument = raw[1].strip() if len(raw) > 1 else ""
    user = storage.get_user(chat_id)
    try:
        if command == "/start":
            start = parse_time(argument) if argument else (user.start_minute if user else 8 * 60)
            user = User(chat_id, start, user.timezone_name if user else DEFAULT_TIMEZONE, True)
            storage.save_user(user)
            answer = "Напоминания включены.\n\n" + status_text(user)
        elif command == "/help":
            answer = HELP
        elif command == "/exercises":
            answer = exercises_text()
        elif user is None:
            answer = "Сначала включите напоминания командой /start ЧЧ:ММ.\n\n" + HELP
        elif command == "/time":
            if not argument:
                raise ValueError("Укажите время: /time 08:00")
            user = User(chat_id, parse_time(argument), user.timezone_name, user.enabled)
            storage.save_user(user)
            answer = status_text(user)
        elif command == "/timezone":
            if not argument:
                raise ValueError("Укажите пояс, например /timezone Europe/Samara")
            try:
                ZoneInfo(argument)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Неизвестный часовой пояс. Пример: Europe/Samara") from None
            user = User(chat_id, user.start_minute, argument, user.enabled)
            storage.save_user(user)
            answer = status_text(user)
        elif command == "/status":
            answer = status_text(user)
        elif command == "/pause":
            user = User(chat_id, user.start_minute, user.timezone_name, False)
            storage.save_user(user)
            answer = "Напоминания приостановлены. Возобновить: /resume"
        elif command == "/resume":
            user = User(chat_id, user.start_minute, user.timezone_name, True)
            storage.save_user(user)
            answer = "Напоминания возобновлены.\n\n" + status_text(user)
        else:
            answer = HELP
    except ValueError as exc:
        answer = str(exc)
    api.send_message(chat_id, answer)


def send_due(storage: Storage, api: TelegramAPI, now: datetime) -> None:
    for user in storage.active_users():
        for anchor_date, slot in due_sessions(user, now):
            if storage.was_sent(user.chat_id, anchor_date, slot):
                continue
            try:
                api.send_message(user.chat_id, reminder_text(slot))
            except TelegramError as exc:
                if exc.code == 403:
                    storage.save_user(User(user.chat_id, user.start_minute, user.timezone_name, False))
                else:
                    LOG.warning("Не удалось отправить напоминание chat_id=%s: %s", user.chat_id, exc)
                break
            except (URLError, TimeoutError, OSError) as exc:
                LOG.warning("Сетевая ошибка при отправке chat_id=%s: %s", user.chat_id, exc)
                break
            else:
                storage.mark_sent(user.chat_id, anchor_date, slot)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в окружении")
    try:
        ZoneInfo(DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Неверный DEFAULT_TIMEZONE: {DEFAULT_TIMEZONE}") from exc
    database = Path(os.getenv("BOT_DB_PATH", "bot.sqlite3"))
    storage = Storage(str(database))
    api = TelegramAPI(token)
    api.call("deleteWebhook", {"drop_pending_updates": "false"})
    LOG.info("Бот запущен")
    last_cleanup = None
    while True:
        now = datetime.now(timezone.utc)
        if last_cleanup != now.date():
            storage.cleanup(now.date())
            last_cleanup = now.date()
        send_due(storage, api, now)
        try:
            updates = api.call(
                "getUpdates",
                {"offset": storage.get_offset(), "timeout": 20, "allowed_updates": json.dumps(["message"])},
            )
            for update in updates:
                try:
                    handle_message(storage, api, update.get("message", {}))
                except (TelegramError, URLError, TimeoutError, OSError) as exc:
                    LOG.warning("Ошибка обработки update_id=%s: %s", update["update_id"], exc)
                    break
                storage.set_offset(update["update_id"] + 1)
        except (TelegramError, URLError, TimeoutError, OSError) as exc:
            LOG.warning("Ошибка получения сообщений: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    run()
