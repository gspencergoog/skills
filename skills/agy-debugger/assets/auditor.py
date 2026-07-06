import sys
import json
import time
import os
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs, quote
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading
import queue
import re
import signal
import webbrowser

try:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except AttributeError:
    pass

FLAG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pause.flag"))
PORT = 5555

# Thread-safe list of active SSE client queues
sse_clients = []
sse_lock = threading.Lock()
last_disconnect_time = None
active_conversation_id = None

DEFAULT_BRAIN_PATHS = [
    "~/.gemini/jetski/brain",
    "~/.antigravity/brain",
    "~/.gemini/config/brain",
    "~/.gemini/brain",
]
conversation_brain_dirs = {}

def get_brain_dir(conv_id=None):
    if conv_id and conv_id in conversation_brain_dirs:
        return conversation_brain_dirs[conv_id]
    
    for path in DEFAULT_BRAIN_PATHS:
        resolved = os.path.expanduser(path)
        if os.path.exists(resolved):
            if conv_id:
                if os.path.exists(os.path.join(resolved, conv_id)):
                    return resolved
            else:
                return resolved
    
    return os.path.expanduser(DEFAULT_BRAIN_PATHS[0])

def extract_brain_dir_and_conv_id(payload):
    conv_id = None
    for key in ['conversationId', 'conversation_id', 'session_id', 'sessionId']:
        if key in payload and payload[key]:
            conv_id = str(payload[key])
            break
            
    payload_str = json.dumps(payload)
    match = re.search(r'(/[^\'"]+)/brain/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', payload_str)
    if match:
        base_dir = match.group(1)
        extracted_conv_id = match.group(2)
        return os.path.join(base_dir, "brain"), conv_id or extracted_conv_id
        
    match_rel = re.search(r'brain/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', payload_str)
    if match_rel:
        return None, conv_id or match_rel.group(1)
        
    return None, conv_id or "default-session"

def extract_conversation_id(payload):
    _, conv_id = extract_brain_dir_and_conv_id(payload)
    return conv_id

def get_flag_path():
    return FLAG_FILE

def clear_pause_flag():
    flag = get_flag_path()
    if os.path.exists(flag):
        try:
            os.remove(flag)
            print("Cleared pause flag due to client disconnection.", flush=True)
        except Exception as e:
            print(f"Error clearing pause flag: {e}", file=sys.stderr, flush=True)

def monitor_clients():
    global last_disconnect_time
    while True:
        time.sleep(1.0)
        with sse_lock:
            client_count = len(sse_clients)
        
        if client_count == 0:
            if last_disconnect_time is not None:
                if time.time() - last_disconnect_time > 5.0:
                    clear_pause_flag()
                    last_disconnect_time = None
        else:
            last_disconnect_time = None

def get_conversation_title(conv_id):
    if conv_id in ['session-A', 'default-session']:
        return conv_id
        
    brain_dir = get_brain_dir(conv_id)
    filepath = os.path.join(brain_dir, conv_id, ".system_generated", "logs", "transcript_full.jsonl")
    if not os.path.exists(filepath):
        return conv_id
        
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            if first_line:
                step = json.loads(first_line)
                content = step.get('content', '')
                match = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', content, re.DOTALL)
                if match:
                    title = match.group(1).strip()
                else:
                    title = content.strip()
                
                title = title.replace('\n', ' ')
                title = re.sub(r'\s+', ' ', title)
                if len(title) > 60:
                    title = title[:57] + "..."
                return title if title else conv_id
    except Exception:
        pass
    return conv_id

def get_all_conversations():
    brain_dir = get_brain_dir()
    if not os.path.exists(brain_dir):
        return []
    convs = []
    try:
        for item in os.listdir(brain_dir):
            item_path = os.path.join(brain_dir, item)
            if os.path.isdir(item_path):
                transcript_path = os.path.join(item_path, ".system_generated", "logs", "transcript_full.jsonl")
                if os.path.exists(transcript_path):
                    convs.append(item)
    except Exception as e:
        print(f"Error scanning conversations: {e}", file=sys.stderr)
    
    # Sort by last modified time of transcript file (most recent first)
    try:
        convs.sort(key=lambda c: os.path.getmtime(os.path.join(brain_dir, c, ".system_generated", "logs", "transcript_full.jsonl")), reverse=True)
    except Exception:
        pass
    return convs

def read_transcript(conv_id):
    brain_dir = get_brain_dir(conv_id)
    filepath = os.path.join(brain_dir, conv_id, ".system_generated", "logs", "transcript_full.jsonl")
    if not os.path.exists(filepath):
        return []
    logs = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"Error reading transcript: {e}", file=sys.stderr)
    return logs

class AuditorHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global last_disconnect_time
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        if path == '/' or path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            html_path = os.path.join(os.path.dirname(__file__), 'index.html')
            if os.path.exists(html_path):
                with open(html_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"HTML UI file not found. Place index.html next to auditor.py.")
        
        elif path == '/stream':
            conv_id = query.get('conversationId', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)
                print(f"Client connected to stream (Conv: {conv_id}). Active clients: {len(sse_clients)}", flush=True)
            
            stop_event = threading.Event()
            
            def tail_file(filepath, client_queue, stop_evt):
                # Wait for file to exist
                while not os.path.exists(filepath) and not stop_evt.is_set():
                    time.sleep(0.5)
                if stop_evt.is_set():
                    return
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        f.seek(0, os.SEEK_END)
                        while not stop_evt.is_set():
                            line = f.readline()
                            if not line:
                                time.sleep(0.3)
                                continue
                            client_queue.put(line.strip())
                except Exception as e:
                    print(f"Watcher error: {e}", file=sys.stderr)

            if conv_id:
                brain_dir = get_brain_dir(conv_id)
                filepath = os.path.join(brain_dir, conv_id, ".system_generated", "logs", "transcript_full.jsonl")
                watcher_thread = threading.Thread(target=tail_file, args=(filepath, q, stop_event), daemon=True)
                watcher_thread.start()

            try:
                self.wfile.write(b"data: {\"type\": \"connected\"}\n\n")
                self.wfile.flush()
                
                while True:
                    try:
                        event_data = q.get(timeout=1.0)
                        self.wfile.write(f"data: {event_data}\n\n".encode('utf-8'))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, Exception) as e:
                pass
            finally:
                stop_event.set()
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
                    print(f"Client disconnected from stream. Active clients: {len(sse_clients)}", flush=True)
                    if len(sse_clients) == 0:
                        last_disconnect_time = time.time()
        
        elif path == '/conversations':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                limit = int(query.get('limit', ['10'])[0])
            except ValueError:
                limit = 10
            convs = get_all_conversations()[:limit]
            conv_items = [{"id": c, "title": get_conversation_title(c)} for c in convs]
            self.wfile.write(json.dumps(conv_items).encode('utf-8'))
        
        elif path == '/logs':
            conv_id = query.get('conversationId', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            logs = read_transcript(conv_id) if conv_id else []
            self.wfile.write(json.dumps(logs).encode('utf-8'))
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self):
        print(f"do_POST called: {self.path}", flush=True)
        global active_conversation_id
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            print(f"Content-Length: {content_length}", flush=True)
            post_data = self.rfile.read(content_length)
            print("Post data read complete", flush=True)
        except Exception as e:
            print(f"Error reading post data: {e}", flush=True)
            return
        
        if self.path.startswith('/register'):
            query = parse_qs(urlparse(self.path).query)
            conv_id = query.get('conversationId', [''])[0]
            brain_dir_arg = query.get('brainDir', [''])[0]
            if conv_id:
                active_conversation_id = conv_id
                if brain_dir_arg:
                    conversation_brain_dirs[conv_id] = brain_dir_arg
                print(f"Registered active conversation: {conv_id} (Brain dir: {brain_dir_arg})", flush=True)
                
                title = get_conversation_title(conv_id)
                # Broadcast global active conversation event
                event_str = json.dumps({"type": "active_conversation", "conversationId": conv_id, "title": title})
                with sse_lock:
                    for q in sse_clients:
                        q.put(event_str)
                        
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b"{\"status\": \"registered\"}")
            else:
                self.send_error(400, "Missing conversationId")

        elif self.path == '/pause':
            try:
                with open(get_flag_path(), 'w') as f:
                    f.write("paused")
                print("Debugger Paused", flush=True)
                state_event = json.dumps({"type": "state_change", "state": "paused"})
                with sse_lock:
                    for q in sse_clients:
                        q.put(state_event)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b"{\"status\": \"paused\"}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                sys.stderr.flush()
                self.send_error(500, f"Server Error: {e}")

        elif self.path == '/resume':
            try:
                clear_pause_flag()
                state_event = json.dumps({"type": "state_change", "state": "running"})
                with sse_lock:
                    for q in sse_clients:
                        q.put(state_event)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b"{\"status\": \"running\"}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                sys.stderr.flush()
                self.send_error(500, f"Server Error: {e}")
        else:
            self.send_error(404, "Not Found")

def open_browser():
    time.sleep(0.5)
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception as e:
        print(f"Failed to open browser automatically: {e}", file=sys.stderr, flush=True)

def start_server():
    server_address = ('', PORT)
    httpd = ThreadingHTTPServer(server_address, AuditorHTTPRequestHandler)
    print(f"Debugger server running on http://localhost:{PORT}", flush=True)
    
    monitor_thread = threading.Thread(target=monitor_clients, daemon=True)
    monitor_thread.start()
    
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        clear_pause_flag()

def is_server_alive():
    try:
        req = urllib.request.Request(f"http://localhost:{PORT}/", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as response:
            return response.status == 200
    except Exception:
        return False

def run_hook():
    try:
        input_data = sys.stdin.read()
        if not input_data:
            return
        payload = json.loads(input_data)
        brain_dir, conversation_id = extract_brain_dir_and_conv_id(payload)
    except Exception as e:
        print(f"Error parsing stdin: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        url = f"http://localhost:{PORT}/register?conversationId={conversation_id}"
        if brain_dir:
            url += f"&brainDir={quote(brain_dir)}"
        req = urllib.request.Request(url, method='POST')
        with urllib.request.urlopen(req, timeout=2) as response:
            response.read()
    except Exception as e:
        print(f"Debugger server not reachable: {e}", file=sys.stderr)

    flag = get_flag_path()
    check_counter = 0
    while os.path.exists(flag):
        if check_counter % 4 == 0:
            if not is_server_alive():
                print("Debugger server disconnected or stopped. Resuming agent automatically.", file=sys.stderr)
                clear_pause_flag()
                break
        check_counter += 1
        time.sleep(0.5)

    print(json.dumps({"allow_tool": True}))

if __name__ == '__main__':
    port_arg_index = -1
    for idx, arg in enumerate(sys.argv):
        if arg == '--port' and idx + 1 < len(sys.argv):
            try:
                PORT = int(sys.argv[idx + 1])
                port_arg_index = idx
            except ValueError:
                pass
    
    if port_arg_index != -1:
        sys.argv = sys.argv[:port_arg_index] + sys.argv[port_arg_index+2:]

    if len(sys.argv) > 1 and sys.argv[1] == '--server':
        start_server()
    elif len(sys.argv) > 1 and sys.argv[1] == '--hook':
        run_hook()
    else:
        print("Usage: python3 auditor.py [--server | --hook] [--port <port>]")
        sys.exit(1)
