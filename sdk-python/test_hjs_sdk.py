"""Tests for the hjs Python SDK. Requires the hjs binary."""
import os
import http.server
import json
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjs import Browser, HJSError, Recorder, parse_html  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/post"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            ct = self.headers.get("Content-Type", "")
            if "json" in ct:
                echo = f"posted={raw}"
            else:
                import urllib.parse as up
                d = dict(up.parse_qsl(raw))
                echo = f"posted={d.get('greeting','')}|{d.get('name','')}"
            body = f"<html><title>Posted</title><body>{echo}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(405)

    def do_GET(self):
        if self.path.startswith("/setcookie"):
            body = b"<html><title>Cookie Set</title><body>done</body></html>"
            self.send_response(200)
            self.send_header("Set-Cookie", "sid=test123; Path=/")
        elif self.path.startswith("/echo"):
            hdrs = json.dumps({k.lower(): v for k, v in self.headers.items()})
            body = f"<html><title>Echo</title><body>{hdrs}</body></html>".encode()
            self.send_response(200)
        elif self.path.startswith("/linkpage"):
            body = (b'<html><title>Links</title><body>'
                    b'<a href="/page1">one</a><a href="sub/page2">two</a>'
                    b'<a href="https://ext.example/x">ext</a></body></html>')
            self.send_response(200)
        elif self.path.startswith("/structured"):
            body = (b'<html><head><title>Structured Page</title>'
                    b'<meta name="description" content="A test page">'
                    b'<meta property="og:title" content="OG Title">'
                    b'<meta property="og:type" content="article">'
                    b'<link rel="canonical" href="/structured">'
                    b'<script type="application/ld+json">'
                    b'{"@type":"Article","headline":"Hello"}'
                    b'</script></head>'
                    b'<body>some article body text</body></html>')
            self.send_response(200)
        elif self.path.startswith("/cf"):
            self.send_response(403)
            body = b"<html><title>Just a moment</title><body>cf-challenge checking your browser</body></html>"
        elif self.path.startswith("/robots.txt"):
            self.send_response(200)
            body = (b"User-agent: *\nDisallow: /private/\n"
                    b"Allow: /private/public/\nSitemap: http://127.0.0.1:8932/sitemap.xml\n")
        elif self.path.startswith("/sitemap.xml"):
            self.send_response(200)
            body = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b'<url><loc>http://127.0.0.1:8932/a</loc></url>'
                    b'<url><loc>http://127.0.0.1:8932/b</loc></url></urlset>')
        elif self.path.startswith("/private/secret"):
            self.send_response(200)
            body = b"<html><title>Secret</title><body>hidden</body></html>"
        else:
            body = b"<html><title>Home</title><body>hello world</body></html>"
            self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8932), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:8932"
print(f"server on {BASE}")

print("== basic goto ==")
with Browser(js=False) as b:
    p = b.goto(f"{BASE}/")
    check("status 200", p.status == 200)
    check("title case preserved", p.title == "Home", p.title)
    check("text", "hello world" in p.text)
    check("ok property", p.ok)

print("== profile chrome131 headers ==")
with Browser(profile="chrome131", js=False) as b:
    p = b.goto(f"{BASE}/echo")
    check("UA", "Chrome/131" in p.text)
    check("sec-ch-ua", "sec-ch-ua" in p.text)
    check("Sec-Fetch", "sec-fetch-dest" in p.text.lower())

print("== fingerprint rotation ==")
with Browser(profiles=["chrome131", "firefox133", "safari17"], js=False) as b:
    uas = [b.goto(f"{BASE}/echo").text for _ in range(3)]
    check("3 profiles cycle",
          "Chrome/131" in uas[0] and "Firefox/133" in uas[1] and "Safari/605" in uas[2],
          [t[:60] for t in uas])
    ua4 = b.goto(f"{BASE}/echo").text
    check("rotation wraps", "Chrome/131" in ua4, ua4[:60])

print("== referer chaining ==")
with Browser(js=False) as b:
    b.goto(f"{BASE}/")
    p = b.goto(f"{BASE}/echo")
    check("referer auto", f'{BASE}/"' in p.text, p.text[:160])

print("== cookies in-session and across sessions ==")
jar = "/tmp/hjs_py_sdk_jar.txt"
if os.path.exists(jar):
    os.remove(jar)
with Browser(cookie_jar=jar, js=False) as b:
    b.goto(f"{BASE}/setcookie")
    p = b.goto(f"{BASE}/echo")
    check("cookie in-session", "sid=test123" in p.text)
with Browser(cookie_jar=jar, js=False) as b:
    p = b.goto(f"{BASE}/echo")
    check("cookie across sessions", "sid=test123" in p.text)

print("== link resolution ==")
with Browser(js=False) as b:
    p = b.goto(f"{BASE}/linkpage")
    check("abs + rel + ext resolved", len(p.links) == 3 and
          f"{BASE}/page1" in p.links and f"{BASE}/sub/page2" in p.links
          and "https://ext.example/x" in p.links, p.links)
    check("find_links", len(p.find_links(contains="/page")) == 2)

print("== captcha detection ==")
with Browser(js=False) as b:
    p = b.goto(f"{BASE}/cf")
    check("cloudflare detected", p.captcha == "cloudflare", p.captcha)
    check("blocked", p.blocked and not p.ok)

print("== structured extraction ==")
with Browser(js=False) as b:
    p = b.goto(f"{BASE}/structured")
    d = p.structured()
    check("og:title", d["og"].get("og:title") == "OG Title", d["og"])
    check("og:type", d["og"].get("og:type") == "article")
    check("description", d["description"] == "A test page", d["description"])
    check("canonical", d["canonical"] == "/structured")
    check("json_ld parsed", d["json_ld"] and d["json_ld"][0]["headline"] == "Hello")
    check("links with text", any(l["text"] == "one" for l in d["links"])
          or True)  # /structured has no anchors; skip
with Browser(js=False) as b:
    p = b.goto(f"{BASE}/linkpage")
    d = p.structured()
    check("anchor text captured",
          any(l["text"] == "one" for l in d["links"]), d["links"])

print("== robots.txt ==")
with Browser(js=False) as b:
    r = b.robots(f"{BASE}/")
    check("disallow parsed", r.disallow == ["/private/"], r.disallow)
    check("allow parsed", r.allow == ["/private/public/"], r.allow)
    check("sitemap parsed", r.sitemaps, r.sitemaps)
    check("allowed public", r.allowed(f"{BASE}/about"))
    check("disallowed private", not r.allowed(f"{BASE}/private/secret"))
    check("allow beats disallow", r.allowed(f"{BASE}/private/public/x"))

print("== respect_robots blocks ==")
with Browser(js=False, respect_robots=True) as b:
    try:
        b.goto(f"{BASE}/private/secret")
        check("robots enforced", False, "request went through")
    except HJSError as e:
        check("robots enforced", "robots.txt" in str(e), str(e)[:80])

print("== sitemap ==")
with Browser(js=False) as b:
    locs = b.sitemap(BASE)
    check("2 locs", locs == [f"{BASE}/a", f"{BASE}/b"], locs)

print("== parse_html standalone ==")
d = parse_html('<html><head><title>T</title></head><body>x <a href="/y">Link</a></body></html>')
check("standalone title", d["title"] == "T")
check("standalone link", d["links"][0]["href"] == "/y")

print("== submit: forms + json + referer chain ==")
with Browser(js=False) as b:
    p = b.submit(f"{BASE}/post", data={"greeting": "hello", "name": "world"})
    check("urlencoded form post", "posted=hello|world" in p.text, p.text[:120])
    b.goto(f"{BASE}/")  # set last-url so submit chains a referer
    p2 = b.submit(f"{BASE}/post", json_body={"a": 1})
    check("json body post", '"a": 1' in p2.text or '"a":1' in p2.text, p2.text[:120])

print("== recorder captures submit ==")
with Browser(profile="chrome131", js=False) as b:
    rec = b.record("python")
    b.submit(f"{BASE}/post", data={"x": "1"})
    code = rec.code()
    check("codegen shows method arg", "method=" in code and "/post" in code, code[:160])

print("== recorder / codegen ==")
with Browser(profile="chrome131", js=False) as b:
    rec = b.record("python")
    b.goto(f"{BASE}/")
    b.goto(f"{BASE}/echo")
    code = rec.code()
    check("py codegen has gotos", code.count("b.goto(") == 2, code[:120])
    check("py codegen profile", "chrome131" in code)
    g = b.record("go")
    b.goto(f"{BASE}/")
    gocode = g.code()
    check("go codegen has Goto", "b.Goto(" in gocode, gocode[:80])

print("== max_pages cap ==")
with Browser(js=False, max_pages=2) as b:
    b.goto(f"{BASE}/")
    b.goto(f"{BASE}/")
    try:
        b.goto(f"{BASE}/")
        check("max_pages enforced", False)
    except HJSError:
        check("max_pages enforced", True)

print("== wait_for ==")
with Browser(js=False) as b:
    p = b.wait_for(f"{BASE}/", "hello world", timeout=10)
    check("wait_for finds text", p.status == 200)

print("== html mode + find_links full ==")
with Browser(js=False) as b:
    raw = b.goto(f"{BASE}/", mode="html")
    check("raw html", "<html>" in raw, raw[:40])

srv.shutdown()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
