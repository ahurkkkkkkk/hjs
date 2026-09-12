"""hjs - Python client for the hjs stealth headless browser.

A Playwright-flavoured wrapper around the hjs binary. One Browser object
is one session: it owns a cookie jar, chains the Referer header like a
real browser, resolves relative links, detects captcha/bot-wall pages,
and can scrape many URLs in parallel with per-host rate limiting and
fingerprint rotation.

Quick start:

    from hjs import Browser

    with Browser(profile="chrome131") as b:
        page = b.goto("https://example.com")
        print(page.title, page.status)
        print(page.text[:200])
        for link in page.links:
            print(link)

    # parallel scraping, session cookies shared
    with Browser(profile="chrome131", cookie_jar="session.txt") as b:
        pages = b.scrape(["https://a.com", "https://b.com"], workers=4)
        for p in pages:
            if p.captcha:
                print("blocked:", p.url, p.captcha)

    # structured extraction (Open Graph, meta tags, JSON-LD)
    page = b.goto("https://en.wikipedia.org/wiki/Go_(programming_language)")
    data = page.structured()
    print(data["title"], data["og"].get("og:description"))
    print(len(data["json_ld"]), "JSON-LD blocks")

    # fingerprint rotation: cycle profiles per request
    b = Browser(profiles=["chrome131", "firefox133", "safari17"], js=False)
    for url in urls:
        b.goto(url)

The hjs binary must be installed separately (HJS_BIN env var, PATH, or
the documented search paths). See the project README.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Iterator, Optional


__version__ = "0.3.0"
__all__ = ["Browser", "Page", "HJSError", "Recorder", "get", "parse_html"]

_DEFAULT_LD = "/root/hjs:/root/hbrowser/mojo-home/lib"
KNOWN_PROFILES = [
    "chrome131",
    "chrome131-mobile",
    "firefox133",
    "safari17",
    "edge131",
]


def _find_binary() -> str:
    for cand in (
        os.environ.get("HJS_BIN"),
        shutil.which("hjs"),
        "/usr/local/bin/hjs",
        "/root/hjs/hjs",
    ):
        if cand and os.path.exists(cand):
            return cand
    raise FileNotFoundError(
        "hjs binary not found; install it or set HJS_BIN=/path/to/hjs"
    )


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    if "LD_LIBRARY_PATH" not in env and os.path.isdir("/root/hjs"):
        env["LD_LIBRARY_PATH"] = _DEFAULT_LD
    return env


class HJSError(RuntimeError):
    """hjs exited with a non-zero code."""


# ---------------------------------------------------------------------------
# HTML parsing (structured extraction), stdlib only, no deps
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", re.DOTALL)
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"|([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*\'([^\']*)\'|([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([^\s"\'>]+)', re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\b([^>]*)>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b([^>]*)>", re.DOTALL | re.IGNORECASE)
_LD_RE = re.compile(
    r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        if m.group(1):
            out[m.group(1).lower()] = m.group(2)
        elif m.group(3):
            out[m.group(3).lower()] = m.group(4)
        elif m.group(5):
            out[m.group(5).lower()] = m.group(6)
    return out


def parse_html(html: str) -> dict[str, Any]:
    """Extract title, meta/OG tags, links, canonical, and JSON-LD blocks."""
    out: dict[str, Any] = {
        "title": "",
        "description": "",
        "canonical": "",
        "og": {},
        "meta": {},
        "links": [],
        "json_ld": [],
    }

    m = _TITLE_RE.search(html)
    if m:
        out["title"] = _clean_text(m.group(1))

    for m in _META_RE.finditer(html):
        a = _attrs(m.group(1))
        name = a.get("name") or a.get("property") or ""
        content = a.get("content", "")
        low = name.lower()
        if low == "description":
            out["description"] = content
        if low.startswith("og:") or low.startswith("twitter:"):
            out["og"][low] = content
        if name and low not in ("description", "keywords", "viewport", "robots"):
            out["meta"][name] = content

    for m in _LINK_RE.finditer(html):
        a = _attrs(m.group(1))
        if a.get("rel", "").lower() == "canonical":
            out["canonical"] = a.get("href", "")

    for m in _LD_RE.finditer(html):
        try:
            out["json_ld"].append(json.loads(m.group(1)))
        except Exception:
            pass

    for m in _A_RE.finditer(html):
        a = _attrs(m.group(1))
        href = a.get("href", "")
        text = _clean_text(re.sub(r"<[^>]+>", "", m.group(2)))
        if href:
            out["links"].append({"href": href, "text": text})

    return out


def _clean_text(s: str) -> str:
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# robots + sitemap
# ---------------------------------------------------------------------------

class Robots:
    """Parsed robots.txt for one host."""

    def __init__(self, text: str, user_agent: str = "*"):
        self.text = text
        self.user_agent = user_agent
        self.disallow: list[str] = []
        self.allow: list[str] = []
        self.sitemaps: list[str] = []
        self.crawl_delay: Optional[float] = None
        self._parse()

    def _parse(self):
        best_ua = None
        # pick the matching user-agent block (case-insensitive), else '*'
        blocks: dict[str, list[str]] = {}
        current: Optional[str] = None
        for line in self.text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                current = None
                continue
            low = line.lower()
            if low.startswith("user-agent:"):
                current = line.split(":", 1)[1].strip().lower()
                blocks.setdefault(current, [])
            elif low.startswith("sitemap:"):
                # Sitemap lines appear anywhere; check before the block rule
                self.sitemaps.append(line.split(":", 1)[1].strip())
            elif current is not None:
                blocks[current].append(low)
        for ua in (self.user_agent.lower(), "*"):
            if ua in blocks:
                best_ua = ua
                break
        if best_ua is None:
            return
        for line in blocks[best_ua]:
            if line.startswith("disallow:"):
                v = line.split(":", 1)[1].strip()
                if v:
                    self.disallow.append(v)
            elif line.startswith("allow:"):
                v = line.split(":", 1)[1].strip()
                if v:
                    self.allow.append(v)
            elif line.startswith("crawl-delay:"):
                try:
                    self.crawl_delay = float(line.split(":", 1)[1])
                except ValueError:
                    pass

    def allowed(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path or "/"
        # last matching rule wins, allow > disallow on equal length
        rules = [(p, True) for p in self.allow] + [(p, False) for p in self.disallow]
        rules.sort(key=lambda r: len(r[0]), reverse=True)
        for prefix, is_allow in rules:
            if _glob_match(path, prefix):
                return is_allow
        return True


def _glob_match(path: str, pattern: str) -> bool:
    rx = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.match(rx, path) is not None


def parse_sitemap(xml_text: str) -> list[str]:
    """Extract <loc> entries from a sitemap or sitemap index."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = ""
    tag = root.tag
    if tag.startswith("{"):
        ns = tag.split("}")[0] + "}"
    locs = []
    for el in root.iter(f"{ns}loc"):
        if el.text:
            locs.append(el.text.strip())
    return locs


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class Page:
    """One fetched page. Attributes mirror hjs JSON output."""

    def __init__(self, data: dict[str, Any], browser: "Browser",
                 html: str = ""):
        self._data = data
        self._browser = browser
        self.url: str = data.get("url", "")
        self.status: int = data.get("status", 0)
        self.attempts: int = data.get("attempts", 1)
        self.elapsed_ms: int = data.get("elapsed_ms", 0)
        self.bytes: int = data.get("bytes", 0)
        self.title: str = data.get("title", "")
        self.description: str = data.get("description", "")
        self.text: str = data.get("text", "")
        self.captcha: Optional[str] = data.get("captcha")
        self._html = html
        self.links: list[str] = sorted(
            {
                urllib.parse.urljoin(self.url, l)
                for l in data.get("links", [])
                if l
            }
        )

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.captcha

    @property
    def blocked(self) -> bool:
        return self.captcha is not None

    def structured(self) -> dict[str, Any]:
        """Parse the raw HTML (fetched on demand) for structured data:
        title, description, canonical, OG/Twitter tags, meta, JSON-LD,
        and links with anchor text. Returns cached if already fetched."""
        if not self._html:
            self._html = self.html()
        d = parse_html(self._html)
        d["url"] = self.url
        if not d["description"] and self.description:
            d["description"] = self.description
        return d

    def html(self) -> str:
        """Raw HTML source of this page."""
        if not self._html:
            self._html = self._browser.goto(self.url, mode="html")
        return self._html

    def json(self) -> dict[str, Any]:
        return dict(self._data)

    def find_links(self, contains: str = "", pattern: str = "",
                   full: bool = False) -> list[str] | list[dict]:
        """Filter links by substring or regex. ``full=True`` returns
        dicts with href+text (anchor text needs structured()/html)."""
        import re as _re

        out: list[Any] = []
        rx = _re.compile(pattern) if pattern else None
        if full:
            for item in parse_html(self.html())["links"]:
                href = urllib.parse.urljoin(self.url, item["href"])
                if contains and contains not in href and contains not in item["text"]:
                    continue
                if rx and not rx.search(href):
                    continue
                out.append({"href": href, "text": item["text"]})
            return out
        for l in self.links:
            if contains and contains not in l:
                continue
            if rx and not rx.search(l):
                continue
            out.append(l)
        return out  # type: ignore[return-value]

    def __repr__(self) -> str:  # pragma: no cover
        tag = f" captcha={self.captcha}" if self.captcha else ""
        return f"<Page {self.status} {self.url!r} {len(self.text)} chars{tag}>"


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

class Browser:
    """A browsing session: cookie jar, fingerprint profile, referer chain.

    Args mirror the hjs CLI flags. Pass ``profiles=[...]`` to rotate a
    different fingerprint on every request; ``profile`` stays sticky then
    and the list is cycled. ``session=True`` keeps a temp cookie jar for
    the browser's lifetime.
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        *,
        profiles: Optional[list[str]] = None,
        cookie_jar: Optional[str] = None,
        session: bool = True,
        proxy: Optional[str] = None,
        referer: Optional[str] = None,
        user_agent: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
        max_bytes: int = 2 * 1024 * 1024,
        js: bool = True,
        js_budget_ms: int = 3000,
        http1: bool = False,
        min_tls: str = "1.2",
        ciphers: Optional[str] = None,
        delay_ms: int = 0,
        retries: int = 2,
        backoff_ms: int = 500,
        binary: Optional[str] = None,
        per_host_delay_ms: int = 0,
        respect_robots: bool = False,
        max_pages: int = 0,
        capture_html: bool = False,
    ):
        self._profile_list = list(profiles) if profiles else (
            [profile] if profile else [])
        self._rot_i = 0
        self.cookie_jar = cookie_jar
        self._owns_jar = cookie_jar is None and session
        if self._owns_jar:
            import tempfile

            fd, self.cookie_jar = tempfile.mkstemp(prefix="hjs-jar-", suffix=".txt")
            os.close(fd)
        self.proxy = proxy
        self.referer = referer
        self.user_agent = user_agent
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.js = js
        self.js_budget_ms = js_budget_ms
        self.http1 = http1
        self.min_tls = min_tls
        self.ciphers = ciphers
        self.delay_ms = delay_ms
        self.retries = retries
        self.backoff_ms = backoff_ms
        self.binary = binary or _find_binary()
        self.per_host_delay_ms = per_host_delay_ms
        self.respect_robots = respect_robots
        self.max_pages = max_pages
        self.capture_html = capture_html

        self._last_url: Optional[str] = None
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._count = 0
        self._closed = False
        self._recorder: Optional["Recorder"] = None
        self._robots_cache: dict[str, Optional[Robots]] = {}

    # -- internals -----------------------------------------------------

    def _next_profile(self) -> Optional[str]:
        if not self._profile_list:
            return None
        if len(self._profile_list) == 1:
            return self._profile_list[0]
        with self._lock:
            p = self._profile_list[self._rot_i % len(self._profile_list)]
            self._rot_i += 1
        return p

    def _rate_limit(self, url: str):
        if self.per_host_delay_ms <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc
        with self._lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
            self._host_last.setdefault(host, 0.0)
        with lock:
            wait = self._host_last[host] + self.per_host_delay_ms / 1000.0 - time.time()
            if wait > 0:
                time.sleep(wait)
            self._host_last[host] = time.time()

    def _check_robots(self, url: str):
        if not self.respect_robots:
            return
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
        with self._lock:
            if base not in self._robots_cache:
                txt = self._run_raw_text(self._argv(base + "/robots.txt", "html", "", {}),
                                         timeout=8)
                ua = (self._next_profile_ua() or "*")
                self._robots_cache[base] = Robots(txt, ua) if txt else None
        rob = self._robots_cache.get(base)
        if rob and not rob.allowed(url):
            raise HJSError(f"hjs: disallowed by robots.txt: {url}")

    def _next_profile_ua(self) -> Optional[str]:
        # best-effort: read UA from the active profile name if we know it
        return None

    def _run_raw_text(self, argv, timeout: int) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  env=_base_env(), timeout=timeout)
            return proc.stdout if proc.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def _argv(self, url: str, mode: str, referer: Optional[str],
              extra_headers: Optional[dict[str, str]]) -> list[str]:
        prof = self._next_profile()
        args = [self.binary, url, f"--timeout={self.timeout}",
                f"--max={self.max_bytes}", f"--mode={mode}", "--links"]
        if prof:
            args.append(f"--profile={prof}")
        if self.cookie_jar:
            args += [f"--cookies={self.cookie_jar}",
                     f"--cookie-jar={self.cookie_jar}"]
        eff_referer = referer if referer is not None else (
            self.referer or (self._last_url if mode != "html" else None)
        )
        if eff_referer:
            args.append(f"--referer={eff_referer}")
        if self.proxy:
            args.append(f"--proxy={self.proxy}")
        if self.user_agent:
            args.append(f"--ua={self.user_agent}")
        if self.http1:
            args.append("--http1")
        if self.min_tls:
            args.append(f"--min-tls={self.min_tls}")
        if self.ciphers:
            args.append(f"--ciphers={self.ciphers}")
        if self.delay_ms:
            args.append(f"--delay-ms={self.delay_ms}")
        if self.retries != 2:
            args.append(f"--retries={self.retries}")
        if self.backoff_ms != 500:
            args.append(f"--backoff-ms={self.backoff_ms}")
        if mode == "json":
            if self.js:
                args += ["--js", f"--js-budget-ms={self.js_budget_ms}"]
            else:
                args.append("--no-js")
        merged = dict(self.headers)
        if extra_headers:
            merged.update(extra_headers)
        for k, v in merged.items():
            args.append(f"--header={k}: {v}")
        return args

    # -- public API ----------------------------------------------------

    def goto(
        self,
        url: str,
        *,
        mode: str = "json",
        referer: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Page | str:
        """Fetch a URL. Returns a Page (mode=json) or raw HTML string
        (mode=html). Referer defaults to the previous page in-session."""
        if self._closed:
            raise HJSError("browser session is closed")
        if self.max_pages and self._count >= self.max_pages:
            raise HJSError("max_pages reached for this session")
        self._check_robots(url)
        self._rate_limit(url)
        argv = self._argv(url, mode, referer, headers)
        if self._recorder:
            self._recorder._record(argv, mode)
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=_base_env(),
            timeout=max(self.timeout * (self.retries + 1) + 30, 60),
        )
        self._count += 1
        if proc.returncode != 0:
            raise HJSError(f"hjs failed ({proc.returncode}): {proc.stderr.strip()}")
        if mode == "html":
            self._last_url = url
            return proc.stdout
        data = json.loads(proc.stdout)
        page = Page(data, self)
        self._last_url = url
        return page

    def wait_for(
        self,
        url: str,
        text: str = "",
        *,
        status: Optional[int] = None,
        timeout: float = 20,
        interval: float = 1.0,
    ) -> Page:
        """Poll a URL until the rendered text contains ``text`` (or an
        HTTP status matches), like Playwright's wait_for_selector but for
        static snapshots. Raises HJSError on timeout."""
        deadline = time.time() + timeout
        last: Optional[Page] = None
        while time.time() < deadline:
            p = self.goto(url)
            last = p
            if isinstance(p, Page):
                if status is not None and p.status == status:
                    return p
                if not text or text.lower() in p.text.lower():
                    return p
            time.sleep(interval)
        raise HJSError(f"hjs: wait_for timed out on {url}")

    def scrape(
        self,
        urls: Iterable[str],
        *,
        workers: int = 4,
        stop_on_captcha: bool = False,
    ) -> list[Page]:
        """Fetch many URLs in parallel. Session cookies and the referer
        chain are shared; per-host politeness via per_host_delay_ms.
        Results keep input order; failures yield empty Pages."""
        url_list = list(urls)
        results: list[Optional[Page]] = [None] * len(url_list)

        def work(i: int, u: str):
            try:
                return i, self.goto(u)
            except HJSError:
                return i, None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, i, u) for i, u in enumerate(url_list)]
            for fut in as_completed(futs):
                i, page = fut.result()
                results[i] = page
                if stop_on_captcha and page and page.captcha:
                    for j in range(i, len(url_list)):
                        results[j] = results[j]  # keep as None
                    break
        out = []
        for i, u in enumerate(url_list):
            if results[i] is None:
                results[i] = Page({"url": u, "status": 0, "text": "",
                                   "links": []}, self)
            out.append(results[i])
        return out

    def sitemap(self, url: str = "") -> list[str]:
        """Read sitemap.xml for a host (or a given sitemap URL) and list
        every <loc>. Follows sitemap-index files one level deep."""
        base = url or (self._last_url or "")
        if not base:
            raise HJSError("sitemap(): pass a URL or goto() first")
        split = urllib.parse.urlsplit(base)
        if base.endswith(".xml"):
            xml_url = base
        else:
            xml_url = f"{split.scheme}://{split.netloc}/sitemap.xml"
        xml_text = self.goto(xml_url, mode="html")
        locs = parse_sitemap(xml_text)
        # sitemap index: recurse one level
        if "<sitemapindex" in xml_text:
            expanded: list[str] = []
            for child in locs[:200]:
                try:
                    sub = self.goto(child, mode="html")
                    expanded.extend(parse_sitemap(sub))
                except HJSError:
                    pass
            return expanded
        return locs

    def robots(self, url: str = "") -> Robots:
        """Fetch and parse robots.txt for a host."""
        base = url or self._last_url or ""
        split = urllib.parse.urlsplit(base)
        txt = self.goto(f"{split.scheme}://{split.netloc}/robots.txt",
                        mode="html")
        return Robots(txt)

    def cookies(self) -> str:
        """Current cookie jar contents (Netscape format), if any."""
        if self.cookie_jar and os.path.exists(self.cookie_jar):
            with open(self.cookie_jar, encoding="utf-8", errors="replace") as f:
                return f.read()
        return ""

    # -- recorder ------------------------------------------------------

    def record(self, lang: str = "python") -> "Recorder":
        """Attach a Recorder that captures every goto and can emit a
        runnable snippet (Playwright codegen-style)."""
        rec = Recorder(self, lang=lang)
        self._recorder = rec
        return rec

    # -- lifecycle -----------------------------------------------------

    def close(self):
        self._closed = True

    def delete_jar(self):
        if self._owns_jar and self.cookie_jar and os.path.exists(self.cookie_jar):
            os.remove(self.cookie_jar)

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Recorder (codegen)
# ---------------------------------------------------------------------------

class Recorder:
    """Captures goto() calls made through its Browser and emits a script.

    Usage::

        b = Browser(js=True)
        rec = b.record()
        b.goto("https://example.com")
        b.goto("https://example.com/about")
        print(rec.code())
    """

    def __init__(self, browser: "Browser", lang: str = "python"):
        self.browser = browser
        self.lang = lang
        self._calls: list[tuple[str, str]] = []

    def _record(self, argv: list[str], mode: str):
        url = argv[1] if len(argv) > 1 else ""
        self._calls.append((url, mode))

    @property
    def calls(self) -> list[tuple[str, str]]:
        return list(self._calls)

    def code(self) -> str:
        if self.lang == "python":
            return self._python()
        if self.lang == "go":
            return self._go()
        raise ValueError(f"unsupported recorder lang: {self.lang}")

    def _profile_comment(self) -> str:
        prof = self.browser._next_profile()
        return prof or ""

    def _python(self) -> str:
        o = self._python_opts()
        head = "from hjs import Browser\n\n"
        head += f"# captured by hjs codegen ({len(self._calls)} requests)\n"
        head += "b = Browser(\n"
        for k, v in o.items():
            head += f"    {k}={v!r},\n"
        head += ")\ntry:\n"
        body = "".join(
            f"    page = b.goto({url!r})\n    print(page.status, page.title)\n"
            if mode == "json"
            else f"    html = b.goto({url!r}, mode='html')\n"
            for url, mode in self._calls
        ) or "    pass  # no requests recorded\n"
        return head + body + "finally:\n    b.close()\n"

    def _python_opts(self) -> dict[str, Any]:
        prof = self.browser._profile_list[0] if self.browser._profile_list else None
        return {
            "profile": prof,
            "timeout": self.browser.timeout,
            "js": self.browser.js,
            "delay_ms": self.browser.delay_ms,
            "retries": self.browser.retries,
        }

    def _go(self) -> str:
        lines = [
            "package main",
            "",
            "import (",
            '\t"fmt"',
            '\t"log"',
            "",
            '\t"github.com/ahurkkkkkkk/hjs/sdk-go/hjs"',
            ")",
            "",
            "func main() {",
            f"\t// captured by hjs codegen ({len(self._calls)} requests)",
            f"\topts := hjs.Options{{JS: true, Retries: {self.browser.retries}}}",
        ]
        prof = self.browser._profile_list[0] if self.browser._profile_list else ""
        if prof:
            lines.append(f"\topts.Profile = {json.dumps(prof)}")
        lines += [
            "\tb, err := hjs.NewBrowser(opts)",
            "\tif err != nil {",
            "\t\tlog.Fatal(err)",
            "\t}",
            "\tdefer b.Close()",
        ]
        for url, mode in self._calls:
            if mode == "json":
                lines.append(f"\tp, err := b.Goto({json.dumps(url)}, nil)")
                lines.append("\tif err != nil {")
                lines.append("\t\tlog.Fatal(err)")
                lines.append("\t}")
                lines.append("\tfmt.Println(p.Status, p.Title)")
            else:
                lines.append(f"\traw, err := b.GotoHTML({json.dumps(url)}, nil)")
                lines.append("\tif err != nil {")
                lines.append("\t\tlog.Fatal(err)")
                lines.append("\t}")
                lines.append("\t_ = raw")
        lines.append("}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# one-shot helpers
# ---------------------------------------------------------------------------

def get(url: str, **kwargs: Any) -> Page:
    """One-shot fetch with a throwaway Browser."""
    with Browser(**kwargs) as b:
        return b.goto(url)  # type: ignore[return-value]
