import os
import tempfile
import unittest
from pathlib import Path


class RoomServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["ROOM_DATABASE_PATH"] = str(Path(self.temp_dir.name) / "messages.sqlite3")
        os.environ["ROOM_MESSAGE_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        from app import init_db
        init_db()
        import app
        self.app = app

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_persistence_and_duplicate_client_id(self):
        first, created = self.app.persist_message(7, "alice", "client-1", "hello")
        duplicate, duplicate_created = self.app.persist_message(7, "alice", "client-1", "hello again")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["message_id"], duplicate["message_id"])
        self.assertEqual(duplicate["body"], "hello")

    def test_ticket_is_room_scoped(self):
        ticket = self.app.ticket_serializer.dumps({"aud": "room-server", "room_id": self.app.ROOM_ID, "user_id": 7, "username": "alice"})
        self.assertEqual(self.app.parse_auth('{"type":"authenticate","ticket":"' + ticket + '"}') ["user_id"], 7)
        wrong = self.app.ticket_serializer.dumps({"aud": "room-server", "room_id": "other", "user_id": 7, "username": "alice"})
        self.assertIsNone(self.app.parse_auth('{"type":"authenticate","ticket":"' + wrong + '"}'))


if __name__ == "__main__":
    unittest.main()
