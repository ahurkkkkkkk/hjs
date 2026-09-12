"""Tests for hjs_splugin. Needs the sbridge binary (HJS_SPLUGIN env var).

Runs standalone (no pytest) to match the repo's other suites, and also under
pytest if present.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjs_splugin import Session, SpluginError  # noqa: E402

BRIDGE = os.environ.get("HJS_SPLUGIN") or (
    "/root/hjs/sbridge" if os.path.exists("/root/hjs/sbridge") else None)
if not BRIDGE:
    print("SKIP: sbridge not available (set HJS_SPLUGIN=/path/to/sbridge)")
    raise SystemExit(0)
os.environ["HJS_SPLUGIN"] = BRIDGE

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


def test_fetch_basic():
    with Session(profile="chrome_131") as s:
        r = s.fetch("https://example.com", timeout=25)
        check("fetch status", r.status == 200, r.status)
        check("title", r.title == "Example Domain", r.title)
        check("text present", "example" in r.text.lower(), r.text[:40])
        check("ok flag", r.ok and not r.blocked)


def test_ja4_matches_chrome():
    with Session(profile="chrome_131") as s:
        r = s.fetch("https://example.com", fp=True, timeout=25)
        check("ja4 returned", bool(r.ja4), r.ja4)
        check("chrome_131 ja4 shape", bool(r.ja4) and r.ja4.startswith("t13d1516h2"),
              r.ja4)
        check("ja3_hash 32 hex", r.ja3_hash and len(r.ja3_hash) == 32,
              str(r.ja3_hash)[:16])
        check("peetprint has GREASE", bool(r.peetprint) and "GREASE" in r.peetprint)
        check("h2 negotiated", r.http_version == "h2", r.http_version)


def test_ja4_differs():
    with Session(profile="chrome_131") as sc, Session(profile="firefox_133") as sf:
        a = sc.fetch("https://example.com", fp=True, timeout=25).ja4
        b = sf.fetch("https://example.com", fp=True, timeout=25).ja4
    check("profiles give distinct ja4", a and b and a != b, f"{a} vs {b}")


def test_rotation():
    with Session(profiles=["chrome_131", "firefox_133"]) as s:
        a = s.fetch("https://example.com", fp=True, timeout=25).ja4
        b = s.fetch("https://example.com", fp=True, timeout=25).ja4
    check("rotation cycles ja4", a != b, f"{a} {b}")


def test_cookie_jar():
    with Session(profile="chrome_131") as s:
        s.fetch("https://httpbin.org/cookies/set/spl/yes42", timeout=25)
        r = s.fetch("https://httpbin.org/cookies", timeout=25)
        check("cookie carried across redirect", "yes42" in r.text, r.text[:80])
        check("jar export nonempty", len(s.cookies()) >= 1, s.cookies())


def test_post_form():
    with Session(profile="chrome_131") as s:
        r = s.post("https://httpbin.org/post", data={"user": "me", "pass": "x"},
                   timeout=25)
        check("post status", r.status == 200, r.status)
        check("form echoed", "me" in r.text and '"x"' in r.text, r.text[:120])


def test_tplugin_interop():
    import hjs_tplugin as t
    with Session(profile="chrome_131") as s:
        r = s.fetch("https://example.com", timeout=25)
        check("tplugin png", t.screenshot(r)[:8] == b"\x89PNG\r\n\x1a\n")
        check("tplugin pdf", t.pdf(r)[:8] == b"%PDF-1.4")
        check("tplugin viewer lines", t.Viewer(r).total > 0)


def test_profiles_listing():
    with Session() as s:
        p = s.profiles()
    check("many profiles", len(p) > 40, len(p))
    check("has chrome/firefox", "chrome_131" in p and "firefox_133" in p)


def test_structured():
    with Session(profile="chrome_131") as s:
        d = s.fetch("https://example.com", timeout=25).structured()
    check("structured title", d["title"] == "Example Domain", d["title"])
    check("structured links list", isinstance(d["links"], list))


if __name__ == "__main__":
    for fn in [test_fetch_basic, test_ja4_matches_chrome, test_ja4_differs,
               test_rotation, test_cookie_jar, test_post_form,
               test_tplugin_interop, test_profiles_listing, test_structured]:
        print("==", fn.__name__)
        try:
            fn()
        except SpluginError as e:
            check(f"{fn.__name__} no bridge error", False, str(e))
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
