import unittest
import os
import sys
import io
import json
import time
import queue
import urllib.request
import urllib.error
import runpy
from unittest.mock import patch, MagicMock, mock_open

# Ensure the assets directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets")))
import auditor

class TestAuditor(unittest.TestCase):
    def setUp(self):
        # Reset globals
        auditor.conversation_brain_dirs = {}
        auditor.sse_clients = []
        auditor.last_disconnect_time = None
        auditor.active_conversation_id = None
        auditor.PORT = 5555

    def test_extract_brain_dir_and_conv_id_keys(self):
        bdir, cid = auditor.extract_brain_dir_and_conv_id({'conversationId': 'id1'})
        self.assertIsNone(bdir)
        self.assertEqual(cid, 'id1')

    def test_extract_brain_dir_and_conv_id_regex_absolute(self):
        payload = {'some_path': '/Users/user/.gemini/jetski/brain/12345678-1234-1234-1234-1234567890ab/artifact.json'}
        bdir, cid = auditor.extract_brain_dir_and_conv_id(payload)
        self.assertEqual(bdir, '/Users/user/.gemini/jetski/brain')
        self.assertEqual(cid, '12345678-1234-1234-1234-1234567890ab')

    def test_extract_brain_dir_and_conv_id_regex_relative(self):
        payload = {'some_path': 'brain/12345678-1234-1234-1234-1234567890ab/artifact.json'}
        bdir, cid = auditor.extract_brain_dir_and_conv_id(payload)
        self.assertIsNone(bdir)
        self.assertEqual(cid, '12345678-1234-1234-1234-1234567890ab')

    def test_extract_brain_dir_and_conv_id_default(self):
        bdir, cid = auditor.extract_brain_dir_and_conv_id({'foo': 'bar'})
        self.assertIsNone(bdir)
        self.assertEqual(cid, 'default-session')

    def test_get_brain_dir_mapped(self):
        auditor.conversation_brain_dirs = {'conv1': '/custom/brain'}
        self.assertEqual(auditor.get_brain_dir('conv1'), '/custom/brain')

    def test_get_brain_dir_default_search(self):
        paths_exist = {
            os.path.expanduser('~/.antigravity/brain'): True
        }
        with patch('os.path.exists', side_effect=lambda p: paths_exist.get(p, False)):
            self.assertEqual(auditor.get_brain_dir(), os.path.expanduser('~/.antigravity/brain'))

    def test_extract_conversation_id_direct_keys(self):
        self.assertEqual(auditor.extract_conversation_id({'conversationId': 'id1'}), 'id1')
        self.assertEqual(auditor.extract_conversation_id({'conversation_id': 'id2'}), 'id2')
        self.assertEqual(auditor.extract_conversation_id({'session_id': 'id3'}), 'id3')
        self.assertEqual(auditor.extract_conversation_id({'sessionId': 'id4'}), 'id4')

    def test_extract_conversation_id_regex(self):
        payload = {'some_path': 'brain/12345678-1234-1234-1234-1234567890ab/artifact.json'}
        self.assertEqual(auditor.extract_conversation_id(payload), '12345678-1234-1234-1234-1234567890ab')

    def test_extract_conversation_id_default(self):
        self.assertEqual(auditor.extract_conversation_id({'foo': 'bar'}), 'default-session')

    def test_clear_pause_flag_exists(self):
        with patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=True), \
             patch('os.remove') as mock_remove:
            auditor.clear_pause_flag()
            mock_remove.assert_called_once_with('/dummy/pause.flag')

    def test_clear_pause_flag_not_exists(self):
        with patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=False), \
             patch('os.remove') as mock_remove:
            auditor.clear_pause_flag()
            mock_remove.assert_not_called()

    def test_clear_pause_flag_exception(self):
        with patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=True), \
             patch('os.remove', side_effect=OSError("Permission Denied")), \
             patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
            auditor.clear_pause_flag()
            self.assertIn("Error clearing pause flag: Permission Denied", mock_stderr.getvalue())

    def test_monitor_clients_no_clients_no_disconnect_time(self):
        class StopLoop(Exception): pass
        call_count = 0
        def mock_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise StopLoop()

        with patch('time.sleep', side_effect=mock_sleep):
            with self.assertRaises(StopLoop):
                auditor.monitor_clients()
            self.assertIsNone(auditor.last_disconnect_time)

    def test_monitor_clients_no_clients_with_disconnect_time_expired(self):
        auditor.last_disconnect_time = time.time() - 6.0
        class StopLoop(Exception): pass
        call_count = 0
        def mock_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise StopLoop()

        with patch('time.sleep', side_effect=mock_sleep), \
             patch('auditor.clear_pause_flag') as mock_clear:
            with self.assertRaises(StopLoop):
                auditor.monitor_clients()
            mock_clear.assert_called_once()
            self.assertIsNone(auditor.last_disconnect_time)

    def test_monitor_clients_no_clients_with_disconnect_time_not_expired(self):
        auditor.last_disconnect_time = time.time() - 2.0
        class StopLoop(Exception): pass
        call_count = 0
        def mock_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise StopLoop()

        with patch('time.sleep', side_effect=mock_sleep), \
             patch('auditor.clear_pause_flag') as mock_clear:
            with self.assertRaises(StopLoop):
                auditor.monitor_clients()
            mock_clear.assert_not_called()
            self.assertIsNotNone(auditor.last_disconnect_time)

    def test_monitor_clients_with_clients(self):
        auditor.sse_clients = [queue.Queue()]
        auditor.last_disconnect_time = time.time()
        class StopLoop(Exception): pass
        call_count = 0
        def mock_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise StopLoop()

        with patch('time.sleep', side_effect=mock_sleep):
            with self.assertRaises(StopLoop):
                auditor.monitor_clients()
            self.assertIsNone(auditor.last_disconnect_time)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_GET_root_file_exists(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=b"html contents")):
            handler.do_GET()
            
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_called_once_with('Content-Type', 'text/html')
        handler.end_headers.assert_called_once()
        self.assertEqual(handler.wfile.getvalue(), b"html contents")

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_GET_root_file_not_exists(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/index.html'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        with patch('os.path.exists', return_value=False):
            handler.do_GET()
            
        handler.send_response.assert_called_once_with(200)
        self.assertIn(b"HTML UI file not found", handler.wfile.getvalue())

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('auditor.get_all_conversations', return_value=['conv1', 'conv2'])
    @patch('auditor.get_conversation_title')
    def test_do_GET_conversations(self, mock_title, mock_all):
        mock_title.side_effect = lambda cid: cid + "-title"
        
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/conversations'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_called_once_with('Content-Type', 'application/json')
        handler.end_headers.assert_called_once()
        expected = [
            {"id": "conv1", "title": "conv1-title"},
            {"id": "conv2", "title": "conv2-title"}
        ]
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), expected)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('auditor.get_all_conversations', return_value=['conv1', 'conv2', 'conv3'])
    @patch('auditor.get_conversation_title')
    def test_do_GET_conversations_with_limit(self, mock_title, mock_all):
        mock_title.side_effect = lambda cid: cid + "-title"
        
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/conversations?limit=2'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        
        handler.send_response.assert_called_once_with(200)
        expected = [
            {"id": "conv1", "title": "conv1-title"},
            {"id": "conv2", "title": "conv2-title"}
        ]
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), expected)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('auditor.get_all_conversations', return_value=['conv1', 'conv2'])
    @patch('auditor.get_conversation_title')
    def test_do_GET_conversations_invalid_limit(self, mock_title, mock_all):
        mock_title.side_effect = lambda cid: cid + "-title"
        
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/conversations?limit=invalid'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        
        handler.send_response.assert_called_once_with(200)
        expected = [
            {"id": "conv1", "title": "conv1-title"},
            {"id": "conv2", "title": "conv2-title"}
        ]
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), expected)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('auditor.read_transcript')
    def test_do_GET_logs(self, mock_read):
        mock_read.return_value = [{'log': 1}]
        
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/logs?conversationId=conv1'
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        
        mock_read.assert_called_once_with('conv1')
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), [{'log': 1}])

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_GET_not_found(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/unknown'
        handler.send_error = MagicMock()

        handler.do_GET()
        handler.send_error.assert_called_once_with(404, "File Not Found")

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('threading.Thread')
    def test_do_GET_stream_heartbeat(self, mock_thread):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/stream'
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        
        mock_wfile = MagicMock()
        write_calls = []
        def mock_write(data):
            write_calls.append(data)
            if b"ping" in data:
                raise ConnectionResetError("client disconnected")
        mock_wfile.write.side_effect = mock_write
        handler.wfile = mock_wfile

        mock_queue = MagicMock()
        mock_queue.get.side_effect = queue.Empty()
        
        with patch('queue.Queue', return_value=mock_queue), \
             patch('time.time', return_value=12345.67):
            handler.do_GET()

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(auditor.last_disconnect_time, 12345.67)
        self.assertEqual(len(auditor.sse_clients), 0)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('threading.Thread')
    def test_do_GET_stream_send_event(self, mock_thread):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'GET'
        handler.path = '/stream'
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        
        mock_wfile = MagicMock()
        write_calls = []
        def mock_write(data):
            write_calls.append(data)
            if b"event_data" in data:
                raise ConnectionResetError("client disconnected")
        mock_wfile.write.side_effect = mock_write
        handler.wfile = mock_wfile

        mock_queue = MagicMock()
        mock_queue.get.return_value = "event_data"
        
        with patch('queue.Queue', return_value=mock_queue):
            handler.do_GET()

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(len(auditor.sse_clients), 0)

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    @patch('auditor.get_conversation_title', return_value='conv1-title')
    def test_do_POST_register_success(self, mock_title):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/register?conversationId=conv1'
        handler.headers = {'Content-Length': '0'}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        client_q = queue.Queue()
        auditor.sse_clients.append(client_q)
        auditor.active_conversation_id = None

        handler.do_POST()

        handler.send_response.assert_called_once_with(200)
        self.assertEqual(auditor.active_conversation_id, 'conv1')
        self.assertFalse(client_q.empty())
        event = json.loads(client_q.get())
        self.assertEqual(event['type'], 'active_conversation')
        self.assertEqual(event['conversationId'], 'conv1')
        self.assertEqual(event['title'], 'conv1-title')

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_register_missing_id(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/register'
        handler.headers = {'Content-Length': '0'}
        handler.rfile = io.BytesIO()
        handler.send_error = MagicMock()

        handler.do_POST()
        handler.send_error.assert_called_once_with(400, "Missing conversationId")

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_pause_success(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/pause'
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        client_q = queue.Queue()
        auditor.sse_clients.append(client_q)

        with patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('builtins.open', mock_open()) as mock_file:
            handler.do_POST()

        handler.send_response.assert_called_once_with(200)
        mock_file.assert_called_once_with('/dummy/pause.flag', 'w')
        mock_file().write.assert_called_once_with('paused')
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), {"status": "paused"})
        self.assertFalse(client_q.empty())
        self.assertEqual(json.loads(client_q.get())['state'], 'paused')

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_pause_error(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/pause'
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.send_error = MagicMock()

        with patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('builtins.open', side_effect=PermissionError("Permission Denied")):
            handler.do_POST()

        handler.send_error.assert_called_once_with(500, "Server Error: Permission Denied")

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_resume_success(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/resume'
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        client_q = queue.Queue()
        auditor.sse_clients.append(client_q)

        with patch('auditor.clear_pause_flag') as mock_clear:
            handler.do_POST()

        handler.send_response.assert_called_once_with(200)
        mock_clear.assert_called_once()
        self.assertEqual(json.loads(handler.wfile.getvalue().decode('utf-8')), {"status": "running"})
        self.assertFalse(client_q.empty())
        self.assertEqual(json.loads(client_q.get())['state'], 'running')

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_resume_error(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/resume'
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.send_error = MagicMock()

        with patch('auditor.clear_pause_flag', side_effect=PermissionError("Permission Denied")):
            handler.do_POST()

        handler.send_error.assert_called_once_with(500, "Server Error: Permission Denied")

    @patch('http.server.BaseHTTPRequestHandler.__init__', lambda *args, **kwargs: None)
    def test_do_POST_not_found(self):
        handler = auditor.AuditorHTTPRequestHandler()
        handler.command = 'POST'
        handler.path = '/unknown'
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.send_error = MagicMock()

        handler.do_POST()
        handler.send_error.assert_called_once_with(404, "Not Found")

    def test_run_hook_empty_stdin(self):
        with patch('sys.stdin.read', return_value=''):
            self.assertIsNone(auditor.run_hook())

    def test_run_hook_bad_json(self):
        with patch('sys.stdin.read', return_value='bad json'), \
             patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(SystemExit) as cm:
            auditor.run_hook()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Error parsing stdin", mock_stderr.getvalue())

    @patch('urllib.request.urlopen')
    def test_run_hook_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'registered'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch('sys.stdin.read', return_value=json.dumps({'conversationId': 'conv1'})), \
             patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=False), \
             patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            auditor.run_hook()
            
        mock_urlopen.assert_called_once()
        self.assertIn('{"allow_tool": true}', mock_stdout.getvalue())

    @patch('urllib.request.urlopen')
    def test_run_hook_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection Refused")

        with patch('sys.stdin.read', return_value=json.dumps({'conversationId': 'conv1'})), \
             patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=False), \
             patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            auditor.run_hook()
            
        mock_urlopen.assert_called_once()
        self.assertIn("Debugger server not reachable: <urlopen error Connection Refused>", mock_stderr.getvalue())
        self.assertIn('{"allow_tool": true}', mock_stdout.getvalue())

    @patch('urllib.request.urlopen')
    def test_run_hook_paused_then_resumed(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'registered'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        exists_returns = [True, False]
        def mock_exists(path):
            return exists_returns.pop(0)

        with patch('sys.stdin.read', return_value=json.dumps({'conversationId': 'conv1'})), \
             patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', side_effect=mock_exists), \
             patch('auditor.is_server_alive', return_value=True), \
             patch('time.sleep') as mock_sleep, \
             patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            auditor.run_hook()
            
        mock_sleep.assert_called_once_with(0.5)
        self.assertIn('{"allow_tool": true}', mock_stdout.getvalue())

    @patch('urllib.request.urlopen')
    def test_run_hook_paused_server_dies(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'registered'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch('sys.stdin.read', return_value=json.dumps({'conversationId': 'conv1'})), \
             patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=True), \
             patch('auditor.is_server_alive', return_value=False), \
             patch('auditor.clear_pause_flag') as mock_clear, \
             patch('sys.stdout', new_callable=io.StringIO) as mock_stdout, \
             patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
            auditor.run_hook()
            
        mock_clear.assert_called_once()
        self.assertIn("Debugger server disconnected or stopped", mock_stderr.getvalue())
        self.assertIn('{"allow_tool": true}', mock_stdout.getvalue())

    @patch('auditor.ThreadingHTTPServer')
    @patch('threading.Thread')
    def test_start_server(self, mock_thread, mock_server):
        server_instance = MagicMock()
        server_instance.serve_forever.side_effect = KeyboardInterrupt()
        mock_server.return_value = server_instance

        with patch('auditor.clear_pause_flag') as mock_clear:
            auditor.start_server()

        mock_server.assert_called_once_with(('', 5555), auditor.AuditorHTTPRequestHandler)
        self.assertEqual(mock_thread.call_count, 2)
        server_instance.serve_forever.assert_called_once()
        server_instance.server_close.assert_called_once()
        mock_clear.assert_called_once()

    @patch('http.server.ThreadingHTTPServer')
    @patch('threading.Thread')
    def test_main_server_arg(self, mock_thread, mock_server):
        server_instance = MagicMock()
        mock_server.return_value = server_instance
        server_instance.serve_forever.return_value = None

        with patch.object(sys, 'argv', ['auditor.py', '--server']):
            runpy.run_path(auditor.__file__, run_name='__main__')

        mock_server.assert_called_once()
        self.assertEqual(mock_thread.call_count, 2)
        server_instance.serve_forever.assert_called_once()
        server_instance.server_close.assert_called_once()

    @patch('http.server.ThreadingHTTPServer')
    @patch('threading.Thread')
    def test_main_server_arg_custom_port(self, mock_thread, mock_server):
        server_instance = MagicMock()
        mock_server.return_value = server_instance
        server_instance.serve_forever.return_value = None

        with patch.object(sys, 'argv', ['auditor.py', '--server', '--port', '8080']):
            res = runpy.run_path(auditor.__file__, run_name='__main__')

        self.assertEqual(res.get('PORT'), 8080)
        mock_server.assert_called_once()

    @patch('urllib.request.urlopen')
    @patch('sys.stdin.read', return_value='{"conversationId": "conv1"}')
    def test_main_hook_arg(self, mock_stdin, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'registered'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch.object(sys, 'argv', ['auditor.py', '--hook']), \
             patch('auditor.get_flag_path', return_value='/dummy/pause.flag'), \
             patch('os.path.exists', return_value=False):
            runpy.run_path(auditor.__file__, run_name='__main__')

        mock_urlopen.assert_called_once()

    def test_main_invalid_arg(self):
        with patch.object(sys, 'argv', ['auditor.py', '--invalid']), \
             patch('sys.stdout', new_callable=io.StringIO) as mock_stdout, \
             self.assertRaises(SystemExit) as cm:
            runpy.run_path(auditor.__file__, run_name='__main__')
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Usage: python3 auditor.py", mock_stdout.getvalue())

if __name__ == '__main__':
    unittest.main()
