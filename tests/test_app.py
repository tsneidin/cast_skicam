import json
import unittest
from unittest.mock import patch, MagicMock
from app import Handler, STATE, set_duration, next_camera, prev_camera
from io import BytesIO


class DummyRequest:
    def __init__(self, method, path, headers=None, body=b""):
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()


class AppHttpTests(unittest.TestCase):
    def setUp(self):
        STATE["active"] = False
        STATE["rotation_seconds"] = 45

    def test_set_duration(self):
        self.assertEqual(set_duration(60), 60)
        self.assertEqual(STATE["rotation_seconds"], 60)
        # Test boundaries (10 <= duration <= 600)
        self.assertEqual(set_duration(5), 10)
        self.assertEqual(STATE["rotation_seconds"], 10)
        self.assertEqual(set_duration(1000), 600)
        self.assertEqual(STATE["rotation_seconds"], 600)
        self.assertIsNone(set_duration("invalid"))

    @patch("app.Handler.__init__", lambda self, *args, **kwargs: None)
    def test_get_index(self):
        handler = Handler()
        handler.path = "/"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        output = handler.wfile.getvalue().decode()
        self.assertIn("Start App", output)
        self.assertIn("Next", output)
        self.assertIn("Prev", output)
        self.assertIn("Viewing duration", output)

    @patch("app.Handler.__init__", lambda self, *args, **kwargs: None)
    def test_get_status(self):
        handler = Handler()
        handler.path = "/status"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        data = json.loads(handler.wfile.getvalue().decode())
        self.assertIn("active", data)
        self.assertIn("rotation_seconds", data)
        self.assertEqual(data["rotation_seconds"], 45)

    @patch("app.Handler.__init__", lambda self, *args, **kwargs: None)
    def test_post_duration(self):
        handler = Handler()
        handler.path = "/duration"
        body = json.dumps({"seconds": 90}).encode()
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()
        self.assertEqual(STATE["rotation_seconds"], 90)
        res = json.loads(handler.wfile.getvalue().decode())
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("rotation_seconds"), 90)

    @patch("app.Handler.__init__", lambda self, *args, **kwargs: None)
    @patch("app.next_camera")
    def test_post_next(self, mock_next):
        handler = Handler()
        handler.path = "/next"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()
        mock_next.assert_called_once()
        res = json.loads(handler.wfile.getvalue().decode())
        self.assertTrue(res.get("ok"))

    @patch("app.Handler.__init__", lambda self, *args, **kwargs: None)
    @patch("app.prev_camera")
    def test_post_prev(self, mock_prev):
        handler = Handler()
        handler.path = "/prev"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()
        mock_prev.assert_called_once()
        res = json.loads(handler.wfile.getvalue().decode())
        self.assertTrue(res.get("ok"))


if __name__ == "__main__":
    unittest.main()
