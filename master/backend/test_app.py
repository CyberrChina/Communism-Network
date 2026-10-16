import unittest

from app import app, clean_registration_payload, clean_server_payload


class ValidationTests(unittest.TestCase):
    def test_registration_validation(self):
        self.assertEqual(clean_registration_payload({"username": "alice", "password": "a"})[1], "password must be at least 12 characters")
        self.assertIsNotNone(clean_registration_payload({"username": "bad name", "password": "long-enough-password"})[1])
        user, error = clean_registration_payload({"username": "alice_01", "password": "long-enough-password"})
        self.assertIsNone(error)
        self.assertEqual(user["username"], "alice_01")

    def test_server_validation(self):
        server, error = clean_server_payload({"ip_address": "2001:db8::1", "port": 25565})
        self.assertIsNone(error)
        self.assertEqual(server, {"ip_address": "2001:db8::1", "port": 25565})
        self.assertIsNotNone(clean_server_payload({"ip_address": "not-an-ip", "port": 25565})[1])
        self.assertIsNotNone(clean_server_payload({"ip_address": "127.0.0.1", "port": 70000})[1])


class RouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="test-secret", SESSION_COOKIE_SECURE=False)
        self.client = app.test_client()

    def test_health_and_login_required(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.post("/servers", json={"ip_address": "127.0.0.1", "port": 25565}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
