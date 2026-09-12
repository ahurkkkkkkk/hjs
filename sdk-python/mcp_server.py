"""hjs MCP server: expose the stealth browser as Model Context Protocol tools.

Works over stdio (newline-delimited JSON-RPC 2.0) like @playwright/mcp, so
you can point Claude Desktop / ZCode / any MCP client at it:

    claude_desktop_config:
    {
        "mcpServers": {
            "hjs": {
                "command": "python3",
                "args": ["/path/to/mcp_server.py"],
                "env": {"HJS_BIN": "/usr/local/bin/hjs"}
            }
        }
    }

Run once to self-test the protocol layer:

    python3 mcp_server.py --self-test

Tools:
    hjs_goto       fetch a URL, returns status/title/text/links
    hjs_extract    structured data (OG, meta, JSON-LD, links+text)
    hjs_html       raw HTML of the current/last page (or a URL)
    hjs_click_link follow a link found on the current page (by number/text)
    hjs_links      list indexed links of the current page
    hjs_wait_for   poll a URL until text appears or status matches
    hjs_scrape     fetch many URLs in parallel
    hjs_sitemap    list sitemap <loc> entries for a host
    hjs_robots     check robots.txt allowance for a URL
    hjs_session    reset/inspect the browser session (profile, cookies)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjs import Browser, HJSError  # noqa: E402

try:
    import hjs_tplugin as tp  # optional: adds screenshot/pdf/tap/scroll tools
    _HAVE_TPLUGIN = True
except Exception:
    tp = None
    _HAVE_TPLUGIN = False

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hjs"
SERVER_VERSION = "0.3.0"


def _profile() -> str:
    return os.environ.get("HJS_PROFILE", "chrome131")


class Session:
    """One persistent Browser, recreated on reset."""

    def __init__(self):
        self.browser: Browser | None = None
        self.page = None
        self.history: list[str] = []
        self.viewer = None

    def get(self) -> Browser:
        if self.browser is None:
            self.browser = Browser(profile=_profile(), session=True,
                                   per_host_delay_ms=200)
        return self.browser

    def refresh_viewer(self):
        if _HAVE_TPLUGIN and self.page is not None:
            try:
                self.viewer = tp.Viewer(self.page)
            except Exception:
                self.viewer = None
        else:
            self.viewer = None

    def set_page(self, page):
        self.page = page
        if page is not None:
            self.history.append(page.url)
        self.refresh_viewer()

    def reset(self, profile: str = "") -> str:
        if self.browser:
            self.browser.close()
        self.browser = None
        self.page = None
        self.viewer = None
        self.history = []
        if profile:
            self._profile = profile
        return f"session reset (profile={profile or _profile()})"


def tool_defs() -> list[dict]:
    return [
        {
            "name": "hjs_goto",
            "description": ("Fetch a URL with the stealth headless browser. "
                            "Returns status, title, readable text, and a "
                            "numbered link list. Runs page JavaScript by "
                            "default. Optional method/body for non-GET."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "absolute URL"},
                    "js": {"type": "boolean",
                           "description": "execute page scripts (default true)"},
                    "method": {"type": "string",
                               "description": "HTTP verb (optional)"},
                    "body": {"type": "string",
                             "description": "request body (optional)"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "hjs_extract",
            "description": ("Extract structured data: title, description, "
                            "canonical, Open Graph, meta tags, JSON-LD, and "
                            "links with anchor text."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "URL to fetch (omit = current page)"},
                },
            },
        },
        {
            "name": "hjs_html",
            "description": "Raw HTML of the current page.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "hjs_links",
            "description": "Numbered links on the current page.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "hjs_click_link",
            "description": ("Navigate to a numbered link from hjs_links "
                            "(keeps the session and referer chain)."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "1-based link number"},
                    "contains": {"type": "string",
                                 "description": "or match by substring in URL/text"},
                },
            },
        },
        {
            "name": "hjs_wait_for",
            "description": ("Poll a URL until rendered text contains a string "
                            "or an HTTP status matches (for JS pages)."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "text": {"type": "string"},
                    "status": {"type": "integer"},
                    "timeout": {"type": "number", "description": "seconds, default 20"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "hjs_scrape",
            "description": "Fetch many URLs in parallel, shared session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "workers": {"type": "integer", "description": "default 4"},
                },
                "required": ["urls"],
            },
        },
        {
            "name": "hjs_sitemap",
            "description": "List sitemap <loc> URLs for a host.",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        {
            "name": "hjs_robots",
            "description": ("Check robots.txt: allowed(url) decision plus "
                            "disallow rules and sitemap entries."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "check": {"type": "string",
                              "description": "URL to test allowance for"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "hjs_session",
            "description": ("Inspect or reset the browser session. action="
                            "reset|info, optional profile name."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["reset", "info"]},
                    "profile": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "hjs_submit",
            "description": ("Send a form or JSON request (POST by default) and "
                            "return the response. No-JS analogue of filling a "
                            "Playwright form; session cookies carry over."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "data": {"type": "object",
                             "description": "urlencoded form fields"},
                    "json": {"type": "object",
                             "description": "JSON body (overrides data)"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "hjs_screenshot",
            "description": ("Render the current page (or url) to a PNG content "
                            "snapshot file (text + highlighted link lines). Not "
                            "a pixel layout render; hjs has no layout engine by "
                            "design."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "output .png file"},
                    "url": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "hjs_pdf",
            "description": ("Paginate the current page (or url) into a PDF "
                            "document and save it to path."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "output .pdf file"},
                    "url": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "hjs_print",
            "description": "Reader-mode paginated plain text of the current page.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "hjs_reader",
            "description": ("Extract the main article text (drop nav/boilerplate) "
                            "from the current page."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "hjs_scroll",
            "description": ("Scroll the virtual viewport of the current page by "
                            "n rows (negative up) and return the visible lines. "
                            "Pair with hjs_tap to touch links."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "delta": {"type": "integer",
                              "description": "rows to scroll, e.g. 10"},
                    "to": {"type": "number",
                           "description": "or jump to 0..1 fraction of page"},
                },
            },
        },
        {
            "name": "hjs_tap",
            "description": ("Touch the visible link at the given viewport row "
                            "(0 = first visible line) or by text; navigates "
                            "there keeping the session. This is Playwright's "
                            "click/tap without pixels: hit-test on laid-out "
                            "lines."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "y": {"type": "integer",
                          "description": "viewport row to tap"},
                    "text": {"type": "string",
                             "description": "or tap first line containing this"},
                },
            },
        },
    ]


SESSION = Session()


def _fmt_page(p) -> str:
    lines = [f"status: {p.status}  attempts: {p.attempts}  elapsed: {p.elapsed_ms}ms"]
    if p.captcha:
        lines.append(f"CAPTCHA DETECTED: {p.captcha}")
    lines.append(f"title: {p.title}")
    if p.description:
        lines.append(f"description: {p.description}")
    text = p.text
    if len(text) > 4000:
        text = text[:4000] + f"\n... [{len(p.text)} chars total, use hjs_extract for full]"
    lines.append("text:\n" + text)
    return "\n".join(lines)


def call_tool(name: str, args: dict) -> str:
    if name == "hjs_goto":
        b = SESSION.get()
        b.js = args.get("js", True)
        if args.get("method"):
            p = b.goto(args["url"], method=args["method"],
                       body=args.get("body"))
        else:
            p = b.goto(args["url"])
        SESSION.set_page(p)
        out = _fmt_page(p)
        if p.links:
            numbered = "\n".join(f"  {i+1}. {l}" for i, l in enumerate(p.links[:80]))
            out += f"\n\nlinks ({len(p.links)}):\n{numbered}"
        return out

    if name == "hjs_submit":
        b = SESSION.get()
        data = args.get("data")
        jb = args.get("json")
        if not data and jb is None:
            data = {"body": args.get("body", "")}
        p = b.submit(args["url"], data=data or None, json_body=jb)
        SESSION.set_page(p)
        return _fmt_page(p)

    if name == "hjs_extract":
        url = args.get("url")
        if url:
            p = SESSION.get().goto(url)
            SESSION.set_page(p)
        else:
            p = SESSION.page
            if p is None:
                return "no current page; call hjs_goto first"
        d = p.structured()
        return json.dumps(d, ensure_ascii=False, indent=2, default=str)

    if name == "hjs_html":
        p = SESSION.page
        if p is None:
            return "no current page; call hjs_goto first"
        html = p.html()
        if len(html) > 16000:
            html = html[:16000] + f"\n... [{p.bytes} bytes total]"
        return html

    if name == "hjs_links":
        p = SESSION.page
        if p is None:
            return "no current page; call hjs_goto first"
        return "\n".join(f"{i+1}. {l}" for i, l in enumerate(p.links))

    if name == "hjs_click_link":
        p = SESSION.page
        if p is None:
            return "no current page; call hjs_goto first"
        target = None
        idx = args.get("index")
        if idx:
            try:
                target = p.links[int(idx) - 1]
            except (IndexError, ValueError):
                return f"no link #{idx} (page has {len(p.links)} links)"
        else:
            needle = args.get("contains", "")
            for l in p.links:
                if needle and needle.lower() in l.lower():
                    target = l
                    break
            if target is None:
                return f"no link matching {needle!r}"
        np = SESSION.get().goto(target)
        SESSION.set_page(np)
        return _fmt_page(np)

    if name == "hjs_wait_for":
        page = SESSION.get().wait_for(
            args["url"], args.get("text", ""),
            status=args.get("status"), timeout=args.get("timeout", 20))
        SESSION.set_page(page)
        return _fmt_page(page)

    if name == "hjs_scrape":
        pages = SESSION.get().scrape(args["urls"], workers=args.get("workers", 4))
        rows = []
        for pp in pages:
            rows.append({"url": pp.url, "status": pp.status,
                         "title": pp.title, "captcha": pp.captcha,
                         "chars": len(pp.text)})
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "hjs_sitemap":
        locs = SESSION.get().sitemap(args["url"])
        head = locs[:200]
        txt = "\n".join(head)
        if len(locs) > 200:
            txt += f"\n... [{len(locs)} total]"
        return txt

    if name == "hjs_robots":
        r = SESSION.get().robots(args["url"])
        check = args.get("check") or args["url"]
        verdict = "ALLOWED" if r.allowed(check) else "DISALLOWED"
        return (f"{verdict} for {check}\n"
                f"disallow: {r.disallow}\nallow: {r.allow}\n"
                f"sitemaps: {r.sitemaps}\n"
                f"crawl_delay: {r.crawl_delay}")

    if name == "hjs_session":
        if args.get("action") == "reset":
            return SESSION.reset(args.get("profile", ""))
        prof = SESSION.browser._profile_list if SESSION.browser else []
        cookies = SESSION.browser.cookies() if SESSION.browser else ""
        n_cookies = sum(1 for ln in cookies.splitlines()
                        if ln and not ln.startswith("#"))
        return (f"profile={prof or '(none)'}\n"
                f"visited={len(SESSION.history)} pages\n"
                f"cookies_in_jar={n_cookies}")

    # -- tplugin-backed tools (need the rendering plugin) --------------------
    if _HAVE_TPLUGIN and name in ("hjs_screenshot", "hjs_pdf", "hjs_print",
                                  "hjs_reader", "hjs_scroll", "hjs_tap"):
        return _call_tplugin(name, args)

    raise ValueError(f"unknown tool: {name}")


def _resolve_page(args: dict):
    url = args.get("url")
    if url:
        p = SESSION.get().goto(url)
        SESSION.set_page(p)
    else:
        p = SESSION.page
    if p is None:
        raise HJSError("no current page; call hjs_goto first")
    return p


def _call_tplugin(name: str, args: dict) -> str:
    if name == "hjs_screenshot":
        p = _resolve_page(args)
        data = tp.screenshot(p)
        path = args.get("path") or "/tmp/hjs-shot.png"
        with open(path, "wb") as f:
            f.write(data)
        return f"wrote {len(data)} bytes to {path}"

    if name == "hjs_pdf":
        p = _resolve_page(args)
        data = tp.pdf(p)
        path = args.get("path") or "/tmp/hjs-page.pdf"
        with open(path, "wb") as f:
            f.write(data)
        return f"wrote {len(data)} bytes to {path}"

    if name == "hjs_print":
        p = _resolve_page(args)
        return tp.print_(p)

    if name == "hjs_reader":
        p = _resolve_page(args)
        return tp.reader(p)

    if name == "hjs_scroll":
        if SESSION.viewer is None:
            SESSION.refresh_viewer()
        if SESSION.viewer is None:
            return "no current page; call hjs_goto first"
        v = SESSION.viewer
        if args.get("to") is not None:
            visible = v.scroll_to_fraction(float(args["to"]))
        else:
            visible = v.scroll(int(args.get("delta", 5)))
        return "\n".join(f"  [{i}] {ln}" for i, ln in enumerate(visible))

    if name == "hjs_tap":
        if SESSION.viewer is None:
            SESSION.refresh_viewer()
        if SESSION.viewer is None:
            return "no current page; call hjs_goto first"
        v = SESSION.viewer
        target = None
        if args.get("text"):
            target = v.tap_text(args["text"])
        elif args.get("y") is not None:
            target = v.tap_visible(int(args["y"]))
        if not target:
            return "no link at that spot (check hjs_scroll to see link rows)"
        np = SESSION.get().goto(target)
        SESSION.set_page(np)
        return _fmt_page(np)

    raise ValueError(f"unknown tplugin tool: {name}")


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

def handle(req: dict) -> dict | None:
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_defs()}}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        try:
            text = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": str(text)}]}}
        except (HJSError, ValueError) as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def self_test() -> int:
    """Drive the protocol layer against a local URL to prove it works."""
    import subprocess
    cases = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "hjs_goto", "arguments": {"url": "https://example.com",
                                              "js": False}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "hjs_extract", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "hjs_session", "arguments": {"action": "info"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "hjs_screenshot", "arguments": {"path": "/tmp/_hjs_selftest.png"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "hjs_pdf", "arguments": {"path": "/tmp/_hjs_selftest.pdf"}}},
    ]
    stdin = "\n".join(json.dumps(c) for c in cases) + "\n"
    proc = subprocess.run([sys.executable, __file__], input=stdin,
                          capture_output=True, text=True, timeout=120)
    ok = True
    n_expected = len(tool_defs())
    seen_pdf = seen_png = False
    for line in proc.stdout.splitlines():
        msg = json.loads(line)
        if msg.get("id") == 2:
            n = len(msg["result"]["tools"])
            print(f"tools/list -> {n} tools")
            ok = ok and n == n_expected
        if msg.get("id") == 3:
            text = msg["result"]["content"][0]["text"]
            print("hjs_goto ->", text.splitlines()[0] if text else "EMPTY")
            ok = ok and "status: 200" in text
        if msg.get("id") == 4:
            d = json.loads(msg["result"]["content"][0]["text"])
            print("hjs_extract -> title:", d.get("title"))
            ok = ok and bool(d.get("title"))
        if msg.get("id") == 6:
            seen_png = os.path.exists("/tmp/_hjs_selftest.png")
            print("hjs_screenshot ->", "wrote file" if seen_png else "MISSING")
            ok = ok and seen_png
        if msg.get("id") == 7:
            seen_pdf = os.path.exists("/tmp/_hjs_selftest.pdf")
            print("hjs_pdf ->", "wrote file" if seen_pdf else "MISSING")
            ok = ok and seen_pdf
    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    serve()
