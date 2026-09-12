"""Tests for hjs-tplugin (renderer, PDF, touch/scroll, print, reader)."""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjs_tplugin import (screenshot, pdf, print_, reader, Viewer, _png,
                         _draw_text, _split_wrap)  # noqa: E402


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


class FakePage:
    """Mimics the slice of hjs.Page the plugin reads."""
    def __init__(self, title, text, links, anchors, url="https://example.com/page"):
        self.title = title
        self.text = text
        self.links = links
        self._anchors = anchors
        self.url = url

    def structured(self):
        return {"links": self._anchors}


page = FakePage(
    "Test Page",
    "The quick brown fox jumps over the lazy dog.\n\n"
    "Second paragraph with a bit more text so wrapping is exercised by the "
    "layout model and produces several lines of output.",
    ["https://example.com/a", "https://example.com/b"],
    [{"href": "/a", "text": "Link Alpha"}, {"href": "/b", "text": "Link Beta"}],
)

print("== PNG writer ==")
png = screenshot(page)
check("png signature", png[:8] == b"\x89PNG\r\n\x1a\n")
w, h = struct.unpack(">II", png[16:24])
check("png dimensions sane", 200 <= w <= 2600 and h > 100, f"{w}x{h}")
check("png has IEND", png[-8:-4] == b"IEND")
# validate IDAT decompresses to the expected raw size (filter byte + row)
idat = b""
i = 8
while i < len(png):
    ln = struct.unpack(">I", png[i:i+4])[0]
    tag = png[i+4:i+8]
    if tag == b"IDAT":
        idat += png[i+8:i+8+ln]
    i += 12 + ln
raw = zlib.decompress(idat)
check("png raw rows correct", len(raw) == (w * 3 + 1) * h,
      f"{len(raw)} vs {(w*3+1)*h}")

print("== PDF writer ==")
pdfb = pdf(page)
check("pdf header", pdfb[:8] == b"%PDF-1.4")
check("pdf eof", pdfb.rstrip().endswith(b"%%EOF"))
check("pdf has xref", b"\nxref\n" in pdfb)
check("pdf has catalog", b"/Type /Catalog" in pdfb)
check("pdf has font", b"/BaseFont /Helvetica" in pdfb)
# startxref offset must point at the real xref table
sx = int(pdfb.rsplit(b"startxref", 1)[1].split()[0])
check("pdf startxref valid", pdfb[sx:sx+4] == b"xref", str(sx))

print("== touch / scroll / tap ==")
v = Viewer(page, viewport_rows=5)
check("model built", v.total > 5, f"lines={v.total}")
vis = v.visible()
check("viewport rows", len(vis) <= 5)
top0 = v.top
v.scroll(3)
check("scroll down", v.top == top0 + 3, str(v.top))
v.scroll(-100)
check("clamp top", v.top == 0)
v.scroll_to_fraction(1.0)
check("scroll to end clamps", v.top == v.total - 5, f"{v.top}/{v.total}")
# find a link row and tap it
rows = v.link_rows()
check("link rows found", len(rows) == 2, str(rows))
row, idx = rows[0]
check("tap hits url", v.tap(row) == page.links[idx], f"row{row} idx{idx}")
check("tap_text hits url", v.tap_text("Link Alpha") == page.links[0],
      str(v.tap_text("Link Alpha")))

print("== print ==")
pt = print_(page, rows=3)
check("form feed pagination", "\x0c" in pt, repr(pt[:40]))

print("== reader ==")
r = reader(page)
# reader keeps the densest prose run; here that is the long second paragraph
check("reader picks main text", "paragraph" in r and "exercised" in r, repr(r[:80]))
check("reader drops link lines", "LINKS" not in r, repr(r[:80]))

print("== helpers ==")
wrap = _split_wrap("word " * 100, 40)
check("wrap respects cols", all(len(line) <= 40 + 4 for line in wrap),
      max(len(l) for l in wrap))

# standalone _png + _draw_text smoke test (no page involved)
small = bytearray(bytes((255, 255, 255)) * (20 * 9))
_draw_text(small, 20, 0, 0, "Hi", (0, 0, 0), scale=1)
img = _png(20, 9, (small[r*60:(r+1)*60] for r in range(9)))
check("tiny png built", img[:8] == b"\x89PNG\r\n\x1a\n")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
