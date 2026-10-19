import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot import (
    Storage,
    User,
    due_sessions,
    handle_message,
    parse_time,
    reminder_text,
    send_due,
    session_times,
)


class FakeAPI:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "test.sqlite3"))
        self.api = FakeAPI()

    def tearDown(self):
        self.storage.connection.close()
        self.temp.cleanup()

    def command(self, text):
        handle_message(self.storage, self.api, {"chat": {"id": 123, "type": "private"}, "text": text})

    def test_time_validation_and_schedule(self):
        self.assertEqual(parse_time("09:30"), 570)
        for invalid in ("9:30", "24:00", "09:60", "09:3", "abc", "０９:３０"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_time(invalid)
        self.assertEqual(
            session_times(8 * 60),
            [("08:00", False), ("10:30", False), ("13:00", False),
             ("15:30", False), ("18:00", False), ("20:30", False)],
        )

    def test_cross_midnight_due_session_and_no_repeat(self):
        self.storage.save_user(User(123, 20 * 60, "Europe/Samara", True))
        now = datetime(2026, 10, 19, 21, 1, tzinfo=timezone.utc)  # 01:01 next day in Samara
        self.assertEqual(due_sessions(self.storage.get_user(123), now), [("2026-10-19", 2)])
        send_due(self.storage, self.api, now)
        send_due(self.storage, self.api, now)
        self.assertEqual(len(self.api.messages), 1)
        self.assertIn("подход 3/6", self.api.messages[0][1])
        self.assertTrue(self.storage.was_sent(123, "2026-10-19", 2))
        self.assertEqual(due_sessions(self.storage.get_user(123), now.replace(minute=6)), [])

    def test_commands_save_settings_and_pause(self):
        self.command("/start 09:30")
        self.command("/timezone Europe/Moscow")
        self.command("/time 10:00")
        self.command("/pause")
        self.assertEqual(self.storage.get_user(123), User(123, 600, "Europe/Moscow", False))
        self.command("/resume")
        self.assertTrue(self.storage.get_user(123).enabled)
        self.command("/time 25:00")
        self.assertIn("формате ЧЧ:ММ", self.api.messages[-1][1])
        self.assertEqual(self.storage.get_user(123).start_minute, 600)

    def test_each_notification_has_instructions_and_source(self):
        for slot in range(6):
            text = reminder_text(slot)
            self.assertIn("Открывание с сопротивлением", text)
            self.assertIn("Закрывание с сопротивлением", text)
            self.assertIn("Открывание с языком", text)
            self.assertIn("Золотая рыбка", text)
            self.assertIn("https://www.wikihow.com/", text)
            self.assertLess(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
