"""hjs_splugin - TLS fingerprint impersonation plugin for hjs.

The Splugin adds what libcurl cannot: a real Chrome/Firefox/Safari TLS
handshake. It drives a companion bridge process (Go, utls-based) over stdin
and stdout, so requests leave the machine with genuine browser JA3, JA4 and
Peetprint fingerprints, matching ALPN and GREASE, and the HTTP header order
the browser uses, while keeping a hjs-like API: sessions, cookies, one
identity per request, and the whole hjs-tplugin stack (screenshot, pdf,
reader, touch) works on the returned result.

Why a bridge: the fingerprint engine is Go (crypto/tls-fork utls), the
browser engine is Mojo, and the glue is a newline-delimited JSON protocol.
One long-lived bridge keeps a cookie jar across requests, so
``session.fetch(login)`` then ``session.fetch(dashboard)`` just works.

Usage::

    from hjs_splugin import Session
    import hjs_tplugin as t

    s = Session(profile="chrome_131")   # also firefox_133, safari_16_0, ...
    r = s.fetch("https://protected-site.example", fp=True)
    print(r.status, r.title)
    print("JA4:", r.ja4)                 # real Chrome JA4, verified by peet.ws
    print("cookies:", r.cookies())
    png = t.screenshot(r)                # tplugin works on Splugin results

List profiles with ``s.profiles()``. Set the bridge binary with the
HSJS_SPLUGIN env var or the ``bridge=`` argument; otherwise it is looked up
next to this file, then on PATH.

The result type (``FetchResult``) duck-types hjs.Page, so hjs_tplugin.Viewer
and the other plugin calls accept it directly.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

__version__ = "0.1.0"
__all__ = ["Session", "FetchResult", "SpluginError", "KNOWN_PROFILES"]

KNOWN_PROFILES = [
    "chrome_131",
    "chrome_133",
    "chrome_144",
    "chrome_150",
    "chrome_152",
    "firefox_133",
    "firefox_135",
    "safari_16_0",
    "edge_131",
    "okhttp4_android_13",
    "brave_146",
    "opera_90",
]

_DEFAULT_UA = {
    "chrome_131": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "firefox_133": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "safari_16_0": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
}

_BRIDGE_CANDIDATES = [
    os.environ.get("HJS_SPLUGIN", ""),
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "sbridge" + (".exe" if os.name == "nt" else "")),
    shutil.which("sbridge") or "",
]


def _find_bridge() -> str:
    for cand in _BRIDGE_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise SpluginError(
        "sbridge not found. Build it (sdk-go/splugin/bridge, `go build -o "
        "sbridge .`) and set HJS_SPLUGIN=/path/to/sbridge"
    )


class SpluginError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# text extraction (same approach as the Mojo engine: raw byte scan)
# ---------------------------------------------------------------------------

_TAG_STR = re.compile(r"<[^>]*>")
_SCRIPT_STR = re.compile(r"<script\b.*?</script>", re.I | re.S)
_STYLE_STR = re.compile(r"<style\b.*?</style>", re.I | re.S)
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(
    rb"""<meta[^>]+name=["']description["'][^>]+content=["'](.*?)["']""",
    re.I,
)


def _clean_text(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace")
    s = _SCRIPT_STR.sub(" ", s)
    s = _STYLE_STR.sub(" ", s)
    s = _TAG_STR.sub(" ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'")
         .replace("&nbsp;", " ").replace("\u00a0", " "))
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------

class FetchResult:
    """Result of a fetch. Mirrors the hjs.Page attributes so hjs-tplugin
    (screenshot/pdf/reader/Viewer) accepts it."""

    def __init__(self, d: dict[str, Any], session: "Session"):
        self.url: str = d.get("url") or d.get("final_url", "")
        self.status: int = int(d.get("status", 0))
        self.elapsed_ms: int = int(d.get("elapsed_ms", 0))
        self._raw: bytes = base64.b64decode(d.get("body_b64", ""))
        self.headers: dict[str, str] = {k.lower(): v for k, v in
                                        (d.get("headers") or {}).items()}
        self.captcha: Optional[str] = d.get("captcha") or _detect_captcha(
            self.html)
        self.ja3: Optional[str] = d.get("ja3")
        self.ja3_hash: Optional[str] = d.get("ja3_hash")
        self.ja4: Optional[str] = d.get("ja4")
        self.ja4_r: Optional[str] = d.get("ja4_r")
        self.peetprint: Optional[str] = d.get("peetprint")
        self.alpn: Optional[str] = d.get("alpn")
        self.http_version: Optional[str] = d.get("http_version")
        self.error: Optional[str] = d.get("error")
        self._session = session
        # link index used by hjs-tplugin Viewer
        self._anchors = _extract_anchors(self.url, self.html)

    # -- hjs.Page compatibility -------------------------------------------

    @property
    def html(self) -> str:
        return self._raw.decode("utf-8", "replace")

    @property
    def title(self) -> str:
        m = _TITLE.search(self._raw)
        return _clean_text(m.group(1)) if m else ""

    @property
    def description(self) -> str:
        m = _META_DESC.search(self._raw)
        return _clean_text(m.group(1)) if m else ""

    @property
    def text(self) -> str:
        return _clean_text(self._raw)

    @property
    def bytes(self) -> int:
        return len(self._raw)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.captcha

    @property
    def blocked(self) -> bool:
        return self.captcha is not None

    @property
    def links(self) -> list[str]:
        import urllib.parse
        seen, out = set(), []
        for href, _t in self._anchors:
            j = urllib.parse.urljoin(self.url, href)
            if j not in seen:
                seen.add(j)
                out.append(j)
        return sorted(out)

    def structured(self) -> dict[str, Any]:
        from hjs import parse_html  # reuse the shared extractor
        d = parse_html(self.html)
        d["url"] = self.url
        if not d["description"]:
            d["description"] = self.description
        return d

    def cookies(self) -> list[dict[str, str]]:
        return self._session.cookies()

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.html)
        except Exception:
            return {}

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<FetchResult {self.status} {self.url!r} {len(self.text)}"
                f" chars ja4={self.ja4 or '-'}>")


_CAPTCHA_MARKERS = {
    "cloudflare": [b"cf-challenge", b"checking your browser",
                   b"cf-browser-verification"],
    "perimeterx": [b"captcha-delivery.com", b"px-captcha"],
    "recaptcha": [b"recaptcha"],
    "hcaptcha": [b"hcaptcha.com", b"h-captcha"],
    "datadome": [b"datadome"],
    "incapsula": [b"incapsula", b"_incap_"],
}


def _detect_captcha(html: str) -> Optional[str]:
    low = html.lower()
    for name, marks in _CAPTCHA_MARKERS.items():
        if any(m.decode().lower() in low for m in marks):
            return name
    return None


_HREF = re.compile(r"<a\b[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
                   re.I | re.S)


def _extract_anchors(base: str, html: str) -> list[tuple[str, str]]:
    out = []
    for m in _HREF.finditer(html):
        href, txt = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        out.append((href, txt.strip()))
    return out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class Session:
    """A persistent Splugin session backed by one bridge process.

    Args mirror hjs.Browser: ``profile`` sets the TLS + UA identity for every
    request (rotation by passing ``profiles`` if you want per-request
    cycling). ``timeout`` is per request. ``insecure`` disables cert
    verification (off by default; use for broken test chains only).
    ``bridge`` overrides the sbridge path.
    """

    def __init__(
        self,
        profile: str = "chrome_131",
        *,
        profiles: Optional[list[str]] = None,
        timeout: int = 15,
        insecure: bool = False,
        http2: bool = True,
        headers: Optional[dict[str, str]] = None,
        bridge: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self.profile = profile
        self._profile_pool = list(profiles) if profiles else None
        self._rot = 0
        self.timeout = timeout
        self.insecure = insecure
        self.http2 = http2
        self.headers = dict(headers or {})
        if user_agent:
            self.headers.setdefault("User-Agent", user_agent)
        self._bridge_path = bridge or _find_bridge()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._err_sink: list[str] = []

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                [self._bridge_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
            )
        return self._proc

    def _next_profile(self) -> str:
        if self._profile_pool and len(self._profile_pool) > 1:
            p = self._profile_pool[self._rot % len(self._profile_pool)]
            self._rot += 1
            return p
        if self._profile_pool:
            return self._profile_pool[0]
        return self.profile

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        proc = self._ensure_proc()
        with self._lock:
            assert proc.stdin and proc.stdout
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise SpluginError("bridge closed")
                return json.loads(line)
            except (BrokenPipeError, OSError) as e:
                self._proc = None
                raise SpluginError(f"bridge lost: {e}")

    def fetch(
        self,
        url: str,
        *,
        method: Optional[str] = None,
        body: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        profile: Optional[str] = None,
        fp: bool = False,
        timeout: Optional[int] = None,
    ) -> FetchResult:
        """Fetch a URL with the session's browser TLS identity.

        ``fp=True`` also queries tls.peet.ws on the same handshake and fills
        ``.ja3`` / ``.ja4`` / ``.peetprint`` on the result (the fingerprints
        a server would actually see)."""
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        payload: dict[str, Any] = {
            "op": "fetch",
            "url": url,
            "profile": profile or self._next_profile(),
            "method": method or "GET",
            "body": body or "",
            "headers": merged,
            "timeout_ms": int((timeout or self.timeout) * 1000),
            "insecure": self.insecure,
            "http2": self.http2,
            "fp": fp,
        }
        d = self._call(payload)
        if d.get("error"):
            raise SpluginError(d["error"])
        d.setdefault("url", url)
        return FetchResult(d, self)

    def post(
        self,
        url: str,
        *,
        data: Optional[dict[str, str]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        fp: bool = False,
        timeout: Optional[int] = None,
    ) -> FetchResult:
        import urllib.parse
        body = ""
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body)
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode(data)
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return self.fetch(url, method="POST", body=body,
                          headers=hdrs, fp=fp, timeout=timeout)

    def cookies(self) -> list[dict[str, str]]:
        d = self._call({"op": "cookies"})
        return list(d.get("cookies") or [])

    def profiles(self) -> list[str]:
        d = self._call({"op": "profiles"})
        return list(d.get("profiles") or [])

    def close(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                self._call({"op": "shutdown"})
            except Exception:
                pass
            proc.wait(timeout=3)
        self._proc = None
