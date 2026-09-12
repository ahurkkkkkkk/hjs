import os, threading, http.server, socketserver, subprocess, json, time

# Restart the test server with the /api/data endpoint
os.makedirs("/root/hjs/testsite", exist_ok=True)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/data":
            body = b'{"message":"hello from api","price":42.5}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            fn = "/root/hjs/testsite" + self.path
            if not os.path.exists(fn):
                fn = "/root/hjs/testsite/spa.html"
            body = open(fn, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

try:
    srv = socketserver.TCPServer(("127.0.0.1", 8931), H)
except OSError:
    print("server already running; continuing")
    srv = None
if srv:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

env = dict(os.environ, LD_LIBRARY_PATH="/root/hjs:/root/hbrowser/mojo-home/lib")
for page in ["spa.html", "timer.html", "xhr.html"]:
    print("===", page)
    r = subprocess.run(["/root/hjs/hjs", "http://127.0.0.1:8931/" + page,
                        "--mode=json", "--timeout=10", "--no-meta"],
                       capture_output=True, text=True, env=env, timeout=60)
    print("rc:", r.returncode)
    if r.returncode == 0:
        d = json.loads(r.stdout)
        print("status:", d["status"], "| text:", d["text"][:130])
    else:
        print(r.stderr[:300])
if srv:
    srv.shutdown()
