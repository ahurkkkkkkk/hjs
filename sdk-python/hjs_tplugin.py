"""hjs-tplugin: rendering plugins for the hjs browser.

Everything here is pure Python stdlib. No Pillow, no reportlab, no wkhtmltopdf,
no Chromium. The fonts and encoders are hand-built:

* a 5x7 bitmap font (classic public-domain glyph set) scaled up for rendering
* a minimal PNG writer (zlib + struct, RGB, one filter byte per scanline)
* a minimal PDF writer (Helvetica built-in font, page tree + text lines)

What you get on top of an hjs Page:

* screenshot(page)  -> PNG bytes: the page text rendered as a tall image,
  link lines highlighted. A content snapshot, not a pixel-perfect layout
  render (hjs has no layout engine by design).
* pdf(page)         -> PDF bytes: paginated text with title and link list.
* print_(page)      -> plain paginated text (reader-style), no HTML noise.
* reader(page)      -> readability-lite main text (density scoring).
* Viewer(page)      -> touch/scroll model: the text is laid out into lines;
  scroll(n) / scroll_to(y) move a virtual viewport; tap(row) or tap_text(s)
  hit-test links and return the target URL (then browser.goto it, cookies and
  referer intact).

Usage:

    from hjs import Browser
    from hjs_tplugin import screenshot, pdf, Viewer, print_, reader

    b = Browser(profile="chrome131")
    page = b.goto("https://news.ycombinator.com")
    open("shot.png", "wb").write(screenshot(page))
    open("page.pdf", "wb").write(pdf(page))

    v = Viewer(page)
    print(v.scroll(25))            # visible text after scrolling 25 rows
    target = v.tap_text("Ask HN")  # hit-test a link by visible text
    if target:
        b.goto(target)
"""
from __future__ import annotations

import re
import struct
import zlib
from typing import Iterable, Optional

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# 5x7 bitmap font (public-domain glyph data, column-major, bit0 = top row)
# ---------------------------------------------------------------------------

_GLYPHS: dict[str, tuple[int, ...]] = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00),
    "!": (0x00, 0x00, 0x5F, 0x00, 0x00),
    '"': (0x00, 0x07, 0x00, 0x07, 0x00),
    "#": (0x14, 0x7F, 0x14, 0x7F, 0x14),
    "$": (0x24, 0x2A, 0x7F, 0x2A, 0x12),
    "%": (0x23, 0x13, 0x08, 0x64, 0x62),
    "&": (0x36, 0x49, 0x55, 0x22, 0x50),
    "'": (0x00, 0x05, 0x03, 0x00, 0x00),
    "(": (0x00, 0x1C, 0x22, 0x41, 0x00),
    ")": (0x00, 0x41, 0x22, 0x1C, 0x00),
    "*": (0x14, 0x08, 0x3E, 0x08, 0x14),
    "+": (0x08, 0x08, 0x3E, 0x08, 0x08),
    ",": (0x00, 0x50, 0x30, 0x00, 0x00),
    "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ".": (0x00, 0x60, 0x60, 0x00, 0x00),
    "/": (0x20, 0x10, 0x08, 0x04, 0x02),
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E),
    "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x42, 0x61, 0x51, 0x49, 0x46),
    "3": (0x21, 0x41, 0x45, 0x4B, 0x31),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10),
    "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30),
    "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00),
    ";": (0x00, 0x56, 0x36, 0x00, 0x00),
    "<": (0x08, 0x14, 0x22, 0x41, 0x00),
    "=": (0x14, 0x14, 0x14, 0x14, 0x14),
    ">": (0x00, 0x41, 0x22, 0x14, 0x08),
    "?": (0x02, 0x01, 0x51, 0x09, 0x06),
    "@": (0x32, 0x49, 0x79, 0x41, 0x3E),
    "A": (0x7E, 0x11, 0x11, 0x11, 0x7E),
    "B": (0x7F, 0x49, 0x49, 0x49, 0x36),
    "C": (0x3E, 0x41, 0x41, 0x41, 0x22),
    "D": (0x7F, 0x41, 0x41, 0x22, 0x1C),
    "E": (0x7F, 0x49, 0x49, 0x49, 0x41),
    "F": (0x7F, 0x09, 0x09, 0x09, 0x01),
    "G": (0x3E, 0x41, 0x49, 0x49, 0x7A),
    "H": (0x7F, 0x08, 0x08, 0x08, 0x7F),
    "I": (0x00, 0x41, 0x7F, 0x41, 0x00),
    "J": (0x20, 0x40, 0x41, 0x3F, 0x01),
    "K": (0x7F, 0x08, 0x14, 0x22, 0x41),
    "L": (0x7F, 0x40, 0x40, 0x40, 0x40),
    "M": (0x7F, 0x02, 0x0C, 0x02, 0x7F),
    "N": (0x7F, 0x04, 0x08, 0x10, 0x7F),
    "O": (0x3E, 0x41, 0x41, 0x41, 0x3E),
    "P": (0x7F, 0x09, 0x09, 0x09, 0x06),
    "Q": (0x3E, 0x41, 0x51, 0x21, 0x5E),
    "R": (0x7F, 0x09, 0x19, 0x29, 0x46),
    "S": (0x46, 0x49, 0x49, 0x49, 0x31),
    "T": (0x01, 0x01, 0x7F, 0x01, 0x01),
    "U": (0x3F, 0x40, 0x40, 0x40, 0x3F),
    "V": (0x1F, 0x20, 0x40, 0x20, 0x1F),
    "W": (0x3F, 0x40, 0x38, 0x40, 0x3F),
    "X": (0x63, 0x14, 0x08, 0x14, 0x63),
    "Y": (0x07, 0x08, 0x70, 0x08, 0x07),
    "Z": (0x61, 0x51, 0x49, 0x45, 0x43),
    "[": (0x00, 0x7F, 0x41, 0x41, 0x00),
    "\\": (0x02, 0x04, 0x08, 0x10, 0x20),
    "]": (0x00, 0x41, 0x41, 0x7F, 0x00),
    "^": (0x04, 0x02, 0x01, 0x02, 0x04),
    "_": (0x40, 0x40, 0x40, 0x40, 0x40),
    "`": (0x00, 0x01, 0x02, 0x04, 0x00),
    "a": (0x20, 0x54, 0x54, 0x54, 0x78),
    "b": (0x7F, 0x48, 0x44, 0x44, 0x38),
    "c": (0x38, 0x44, 0x44, 0x44, 0x20),
    "d": (0x38, 0x44, 0x44, 0x48, 0x7F),
    "e": (0x38, 0x54, 0x54, 0x54, 0x18),
    "f": (0x08, 0x7E, 0x09, 0x01, 0x02),
    "g": (0x0C, 0x52, 0x52, 0x52, 0x3E),
    "h": (0x7F, 0x08, 0x04, 0x04, 0x78),
    "i": (0x00, 0x44, 0x7D, 0x40, 0x00),
    "j": (0x20, 0x40, 0x44, 0x3D, 0x00),
    "k": (0x7F, 0x10, 0x28, 0x44, 0x00),
    "l": (0x00, 0x41, 0x7F, 0x40, 0x00),
    "m": (0x7C, 0x04, 0x18, 0x04, 0x78),
    "n": (0x7C, 0x08, 0x04, 0x04, 0x78),
    "o": (0x38, 0x44, 0x44, 0x44, 0x38),
    "p": (0x7C, 0x14, 0x14, 0x14, 0x08),
    "q": (0x08, 0x14, 0x14, 0x18, 0x7C),
    "r": (0x7C, 0x08, 0x04, 0x04, 0x08),
    "s": (0x48, 0x54, 0x54, 0x54, 0x20),
    "t": (0x04, 0x3F, 0x44, 0x40, 0x20),
    "u": (0x3C, 0x40, 0x40, 0x20, 0x7C),
    "v": (0x1C, 0x20, 0x40, 0x20, 0x1C),
    "w": (0x3C, 0x40, 0x3C, 0x40, 0x3C),
    "x": (0x44, 0x28, 0x10, 0x28, 0x44),
    "y": (0x0C, 0x50, 0x50, 0x50, 0x3C),
    "z": (0x44, 0x64, 0x54, 0x4C, 0x44),
    "{": (0x00, 0x08, 0x36, 0x41, 0x00),
    "|": (0x00, 0x00, 0x7F, 0x00, 0x00),
    "}": (0x00, 0x41, 0x36, 0x08, 0x00),
    "~": (0x08, 0x04, 0x08, 0x10, 0x08),
}


def glyph(ch: str) -> tuple[int, ...]:
    return _GLYPHS.get(ch, _GLYPHS[" "])


# ---------------------------------------------------------------------------
# PNG writer (RGB8, no interlace)
# ---------------------------------------------------------------------------

def _png(width: int, height: int, rgb_rows: Iterable[bytes]) -> bytes:
    """Build a PNG from rows of length width*3."""
    raw = b"".join(b"\x00" + row for row in rgb_rows)
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


WHITE = (255, 255, 255)
BLACK = (24, 24, 24)
BLUE = (26, 74, 153)
GRAY_BG = (232, 236, 242)
LINK_BG = (220, 232, 255)


def _draw_text(img: bytearray, width: int, x: int, y: int, text: str,
               color: tuple[int, int, int], scale: int = 2,
               bg: Optional[tuple[int, int, int]] = None) -> int:
    """Blit a line of text at (x, y). Returns advance in pixels."""
    cx = x
    for ch in text:
        cols = glyph(ch)
        for ci, colbits in enumerate(cols):
            for ry in range(7):
                if colbits >> ry & 1:
                    for sy in range(scale):
                        for sx in range(scale):
                            px = cx + ci * scale + sx
                            py = y + ry * scale + sy
                            if 0 <= px < width and py * width + px < len(img) // 3:
                                o = (py * width + px) * 3
                                img[o:o + 3] = bytes(color)
                elif bg is not None:
                    for sy in range(scale):
                        for sx in range(scale):
                            px = cx + ci * scale + sx
                            py = y + ry * scale + sy
                            if 0 <= px < width:
                                o = (py * width + px) * 3
                                img[o:o + 3] = bytes(bg)
        cx += 6 * scale
    return cx - x


def _fill_row(img: bytearray, width: int, y0: int, y1: int, x0: int, x1: int,
              color: tuple[int, int, int]) -> None:
    for y in range(y0, y1):
        for x in range(x0, max(x0, min(x1, width))):
            o = (y * width + x) * 3
            img[o:o + 3] = bytes(color)


# ---------------------------------------------------------------------------
# Page -> lines model (shared by screenshot and viewer)
# ---------------------------------------------------------------------------

_WORDS_PER_LINE = 80  # rough column budget before wrapping

_TAG_ONLY = re.compile(r"<[^>]+>")


def _page_lines(page) -> list[dict]:
    """Turn a Page into layout lines: [{text, link_index, is_title}].

    Lines that start with a link's anchor text are tagged with the link index
    so tap() can hit-test them.
    """
    lines: list[dict] = []
    title = (page.title or "").strip()
    if title:
        lines.append({"text": title, "link_index": None, "is_title": True})
        lines.append({"text": "", "link_index": None, "is_title": False})

    anchors = []
    try:
        anchors = page.structured().get("links", []) or []
    except Exception:
        pass

    # Map resolved URLs to the best anchor text we have. page.links are already
    # resolved to absolute by the SDK; anchor hrefs may be relative, so resolve
    # them the same way to match up.
    import urllib.parse as _up
    base = _up.urlsplit(getattr(page, "url", "") or "")
    href_to_text: dict[str, str] = {}
    for a in anchors:
        h = a.get("href")
        if a.get("text") and h:
            try:
                resolved = base and _up.urljoin(page.url, h) or h
            except Exception:
                resolved = h
            href_to_text.setdefault(resolved, a["text"])

    body = page.text or ""
    for chunk in _split_wrap(body, _WORDS_PER_LINE):
        lines.append({"text": chunk, "link_index": None, "is_title": False})

    if page.links:
        lines.append({"text": "", "link_index": None, "is_title": False})
        lines.append({"text": "LINKS", "link_index": None, "is_title": True})
        for i, href in enumerate(page.links):
            txt = href_to_text.get(href) or href
            for j, chunk in enumerate(_split_wrap(txt, 70)):
                lines.append({"text": chunk,
                              "link_index": i if j == 0 else None,
                              "is_title": False})
    return lines


def _split_wrap(text: str, cols: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines():
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > cols:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            out.append(cur)
    return out


# ---------------------------------------------------------------------------
# screenshot -> PNG
# ---------------------------------------------------------------------------

def screenshot(page, scale: int = 2, cols: int = _WORDS_PER_LINE) -> bytes:
    """Render the page text as a PNG image (content snapshot).

    White background, black text, link lines on a light blue tint, headings
    double-spaced. No layout engine: hjs has none by design, this is a readable
    rasterization of the extracted content, not a pixel-perfect browser render.
    """
    lines = _page_lines(page)
    line_h = 7 * scale + scale
    pad = scale * 4
    width = (cols * 6 + 8) * scale
    if width > 2400:
        width = 2400
    height = pad * 2 + line_h * len(lines)
    img = bytearray(bytes(WHITE) * (width * height))

    y = pad
    for ln in lines:
        color = BLUE if ln["link_index"] is not None else (
            (9, 47, 107) if ln["is_title"] else BLACK)
        bg = LINK_BG if ln["link_index"] is not None else None
        if bg:
            _fill_row(img, width, y - scale, y + 7 * scale + scale, 0, width, (220, 232, 255))
        _draw_text(img, width, pad, y, ln["text"], color, scale)
        if ln["is_title"]:
            y += line_h // 2
        y += line_h
    return _png(width, height, (img[r * width * 3:(r + 1) * width * 3]
                                for r in range(height)))


# ---------------------------------------------------------------------------
# pdf -> bytes (hand written, Helvetica built-in, WinAnsi)
# ---------------------------------------------------------------------------

def _pdf_escape(s: str) -> bytes:
    b = s.encode("latin-1", "replace")
    out = bytearray()
    for ch in b:
        if ch in (0x28, 0x29, 0x5C):
            out.append(0x5C)
        out.append(ch)
    return bytes(out)


def pdf(page, rows_per_page: int = 56) -> bytes:
    """Paginate the page text into a PDF document. Built-in Helvetica,
    link lines in blue via a second font slot is skipped (plain text only);
    valid uncompressed PDF 1.4."""
    lines = _page_lines(page)
    pages: list[list[str]] = [[]]
    for ln in lines:
        pages[-1].append(ln["text"])
        if len(pages[-1]) >= rows_per_page:
            pages.append([])
    if not pages[-1]:
        pages.pop()

    total_pages = max(len(pages), 1) or 1
    catalog_id, pages_id = 1, 2
    kids = [4 + 2 * i for i in range(total_pages)]
    font_id = 3 + 2 * total_pages + 1

    objs: list[bytes] = []

    def push(o: bytes):
        objs.append(o)

    push(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    kidstr = " ".join(f"{k} 0 R" for k in kids)
    push(f"<< /Type /Pages /Count {total_pages} /Kids [{kidstr}] >>".encode())
    for i in range(total_pages):
        content = pages[i] if i < len(pages) else []
        stream_lines = ["BT", "/F1 9 Tf", "12 TL", "50 780 Td"]
        for t in content:
            esc = _pdf_escape(t).decode("latin-1")
            stream_lines.append(f"({esc}) Tj T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", "replace")
        cid = 3 + 2 * i
        push(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        push((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
              f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
              f"/Contents {cid} 0 R >>").encode())
    push(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
         b"/Encoding /WinAnsiEncoding >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# print: plain paginated text (reader output for paper)
# ---------------------------------------------------------------------------

def print_(page, cols: int = 100, rows: int = 50, form_feed: str = "\x0c") -> str:
    lines = [ln["text"] for ln in _page_lines(page)]
    out: list[str] = []
    for i in range(0, max(len(lines), 1), rows):
        out.append("\n".join(lines[i:i + rows]))
    return form_feed.join(out)


# ---------------------------------------------------------------------------
# reader: main-content extraction (density heuristic, stdlib only)
# ---------------------------------------------------------------------------

def reader(page) -> str:
    """Pick the densest contiguous run of non-link text lines. Roughly
    what readability does, without the dependency."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", page.text or "")]
    scored = []
    for p in paras:
        if not p:
            continue
        words = len(p.split())
        punct = p.count(".") + p.count(",") + p.count(";")
        scored.append((words + punct * 2, p))
    if not scored:
        return page.text or ""
    scored.sort(key=lambda x: -x[0])
    keep = [p for s, p in scored if s >= 40]
    if not keep:
        keep = [scored[0][1]]
    return "\n\n".join(keep)


# ---------------------------------------------------------------------------
# Viewer: scroll + touch model over the laid-out lines
# ---------------------------------------------------------------------------

class Viewer:
    """Virtual scroll position over a laid-out page, with link hit-testing.

    No pixels are kept; the model is line based (a link occupies one line
    row, first row of a wrapped run). tap() returns the resolved URL so you
    can hand it to Browser.goto() and keep the session cookies/referer.
    """

    def __init__(self, page, viewport_rows: int = 24):
        self.page = page
        self.lines = _page_lines(page)
        self.rows = viewport_rows
        self.top = 0
        self.total = len(self.lines)

    # -- scrolling --------------------------------------------------------

    def scroll(self, delta_rows: int) -> list[str]:
        """Move the viewport by n rows (negative up). Returns visible text."""
        self.top = max(0, min(self.top + delta_rows, max(0, self.total - self.rows)))
        return self.visible()

    def scroll_to(self, row: int) -> list[str]:
        self.top = max(0, min(row, max(0, self.total - self.rows)))
        return self.visible()

    def scroll_to_text(self, needle: str) -> list[str]:
        low = needle.lower()
        for i, ln in enumerate(self.lines):
            if low in ln["text"].lower():
                return self.scroll_to(i)
        raise ValueError(f"text not found: {needle!r}")

    def scroll_to_fraction(self, frac: float) -> list[str]:
        """0.0 = top, 1.0 = bottom (a swipe-to-end)."""
        frac = max(0.0, min(1.0, float(frac)))
        return self.scroll_to(int(frac * max(0, self.total - 1)))

    def visible(self) -> list[str]:
        return [ln["text"] for ln in self.lines[self.top:self.top + self.rows]]

    # -- touch ------------------------------------------------------------

    def link_rows(self) -> list[tuple[int, int]]:
        """(row, link_index) pairs, for hit-testing."""
        return [(i, ln["link_index"]) for i, ln in enumerate(self.lines)
                if ln["link_index"] is not None]

    def tap(self, row: int) -> Optional[str]:
        """Hit-test an absolute row index; returns the link URL or None."""
        ln = self.lines[row] if 0 <= row < self.total else None
        if ln and ln["link_index"] is not None:
            return self.page.links[ln["link_index"]]
        return None

    def tap_visible(self, y: int) -> Optional[str]:
        """Tap a coordinate inside the viewport (0 = first visible row)."""
        if 0 <= y < self.rows and self.top + y < self.total:
            return self.tap(self.top + y)
        return None

    def tap_text(self, needle: str) -> Optional[str]:
        """Tap the first line containing needle; returns the link URL or None
        (a match on non-link text still scrolls it into view)."""
        low = needle.lower()
        for i, ln in enumerate(self.lines):
            if low in ln["text"].lower():
                self.scroll_to(i)
                if ln["link_index"] is not None:
                    return self.page.links[ln["link_index"]]
                return None
        return None
