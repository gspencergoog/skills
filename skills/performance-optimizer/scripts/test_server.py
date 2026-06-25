import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import io

# Append sidecar folder
script_dir = os.path.dirname(os.path.abspath(__file__))
sidecar_dir = os.path.abspath(os.path.join(script_dir, "../sidecar"))
if sidecar_dir not in sys.path:
    sys.path.append(sidecar_dir)

import server

class TestServer(unittest.TestCase):
    @patch("builtins.open", new_callable=mock_open, read_data=b"<html>Dashboard</html>")
    def test_get_dashboard_root(self, mock_file):
        # Instantiate a mock handler
        handler = MagicMock(spec=server.DashboardHandler)
        handler.path = "/"
        handler.wfile = io.BytesIO()
        
        # Run handle get
        server.DashboardHandler.do_GET(handler)
        
        # Check output
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/html")
        handler.end_headers.assert_called()
        self.assertIn(b"Dashboard", handler.wfile.getvalue())

    @patch("builtins.open", new_callable=mock_open, read_data="workspace_abc")
    def test_get_workspace_path(self, mock_file):
        handler = MagicMock(spec=server.DashboardHandler)
        path = server.DashboardHandler.get_workspace_path(handler)
        self.assertEqual(path, "workspace_abc")
        mock_file.assert_called_with("workspace_path.txt", "r")

    @patch("builtins.open", side_effect=Exception("Read error"))
    def test_get_workspace_path_exception(self, mock_file):
        handler = MagicMock(spec=server.DashboardHandler)
        path = server.DashboardHandler.get_workspace_path(handler)
        self.assertEqual(path, "")

    @patch("os.path.exists", return_value=True)
    @patch("builtins.open")
    def test_get_history_api(self, mock_file, mock_exists):
        # Mock file read of optimization_history.json
        mock_file.return_value.__enter__.return_value.read.return_value = '[{"step": 1}]'
        
        handler = MagicMock(spec=server.DashboardHandler)
        handler.path = "/api/history"
        handler.wfile = io.BytesIO()
        handler.get_workspace_path.return_value = "/workspace"
        
        server.DashboardHandler.do_GET(handler)
        
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json")
        handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")
        handler.end_headers.assert_called()
        self.assertEqual(handler.wfile.getvalue(), b'[{"step": 1}]')

    @patch("server.DashboardHandler.get_workspace_path", return_value="/workspace")
    @patch("os.path.exists", return_value=False)
    def test_get_history_api_not_exists(self, mock_exists, mock_get_workspace):
        handler = MagicMock(spec=server.DashboardHandler)
        handler.path = "/api/history"
        handler.wfile = io.BytesIO()
        
        server.DashboardHandler.do_GET(handler)
        
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json")
        self.assertEqual(handler.wfile.getvalue(), b"[]")

    @patch("server.DashboardHandler.get_workspace_path", return_value="/workspace")
    @patch("os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=Exception("Read failure"))
    def test_get_history_api_exception(self, mock_file, mock_exists, mock_get_workspace):
        handler = MagicMock(spec=server.DashboardHandler)
        handler.path = "/api/history"
        handler.wfile = io.BytesIO()
        
        server.DashboardHandler.do_GET(handler)
        
        handler.send_error_json.assert_called_with(500, "Error reading optimization history: Read failure")

    def test_get_history_api_unconfigured(self):
        handler = MagicMock(spec=server.DashboardHandler)
        handler.path = "/api/history"
        handler.wfile = io.BytesIO()
        handler.get_workspace_path.return_value = ""
        
        server.DashboardHandler.do_GET(handler)
        
        handler.send_error_json.assert_called_with(400, "Workspace path not configured in sidecar.")

    def test_do_options(self):
        handler = MagicMock(spec=server.DashboardHandler)
        server.DashboardHandler.do_OPTIONS(handler)
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Access-Control-Allow-Origin", "*")

    @patch("http.server.HTTPServer")
    @patch("os.environ.get", return_value="9999")
    def test_run(self, mock_env, mock_server):
        server.run()
        mock_server.assert_called_once_with(("", 9999), server.DashboardHandler)
        mock_server.return_value.serve_forever.assert_called_once()

if __name__ == '__main__':
    unittest.main()
