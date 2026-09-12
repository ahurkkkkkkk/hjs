# hbrowser - tiny headless browser engine in Mojo
#
# Fetches a URL over HTTP/HTTPS using libcurl, extracts readable text,
# links, title, and metadata from HTML, and prints a JSON document to
# stdout. Designed to be driven from Go or Python as a zero-dependency,
# low-RAM subprocess (no V8, no DOM tree, no rendering).
#
# Usage:
#   hbrowser <url> [--mode=text|html|json] [--timeout=15] [--max=2097152]
#                  [--header="Name: value"]... [--links] [--no-meta]
#                  [--quiet] [--ua=<string>]
#
# Exit codes: 0 ok, 1 usage error, 2 network error, 3 HTTP error status.

from std.ffi import (
    OwnedDLHandle,
    external_call,
    c_int,
    c_long,
    c_size_t,
    c_char,
    c_uchar,
)
from std.collections import List, Span
from std.collections.string import String
from std.time import perf_counter_ns
from std.sys import argv
from std.sys.info import size_of


# ---------------------------------------------------------------------------
# libcurl option codes (from curl.h)
# ---------------------------------------------------------------------------

comptime CURLOPT_URL = c_int(10002)
comptime CURLOPT_WRITEFUNCTION = c_int(20011)
comptime CURLOPT_WRITEDATA = c_int(10001)
comptime CURLOPT_FOLLOWLOCATION = c_int(52)
comptime CURLOPT_MAXFILESIZE = c_int(115)
comptime CURLOPT_TIMEOUT = c_int(13)
comptime CURLOPT_CONNECTTIMEOUT = c_int(78)
comptime CURLOPT_USERAGENT = c_int(10018)
comptime CURLOPT_ACCEPT_ENCODING = c_int(10102)
comptime CURLOPT_SSL_VERIFYPEER = c_int(64)
comptime CURLOPT_SSL_VERIFYHOST = c_int(81)
comptime CURLOPT_HTTPHEADER = c_int(10023)
comptime CURLOPT_MAXREDIRS = c_int(68)
comptime CURLOPT_NOSIGNAL = c_int(99)

comptime CURLE_OK = c_int(0)


# ---------------------------------------------------------------------------
# curl easy handle wrapper. The handle is an opaque pointer (CURL *).
# ---------------------------------------------------------------------------

comptime CURLPtr = Pointer[NoneType, MutUntrackedOrigin]
comptime RecvBufPtr = Pointer[RecvBuf, MutUntrackedOrigin]


@fieldwise_init
struct RecvBuf(Copyable, Movable):
    var data: Pointer[UInt8, MutUntrackedOrigin]
    var size: c_size_t
    var cap: c_size_t


@fieldwise_init
struct CurlSList(Copyable, Movable):
    # struct curl_slist { char *data; struct curl_slist *next; }
    var data: Pointer[c_char, MutUntrackedOrigin]
    var next: Pointer[CurlSList, MutUntrackedOrigin]



def ptr_to_int(p: Pointer[RecvBuf, MutUntrackedOrigin]) -> Int:
    # Address of the RecvBuf struct itself (not its first field).
    return Int(p.unsafe_bitcast[Pointer[UInt64, MutUntrackedOrigin]]())


def recv_callback(
    ptr: Pointer[c_uchar, MutUntrackedOrigin],
    sz: c_size_t,
    nmemb: c_size_t,
    userdata: RecvBufPtr,
) abi("C") -> c_size_t:
    var total = Int(sz) * Int(nmemb)
    var used = Int(userdata[].size)
    var room = Int(userdata[].cap) - used
    var take = total
    if take > room:
        take = room
    for i in range(take):
        userdata[].data[unsafe_offset=used + i] = ptr[unsafe_offset=i]
    userdata[].size = c_size_t(used + take)
    return c_size_t(total)


struct CurlEasy:
    var lib: OwnedDLHandle

    def __init__(out self) raises:
        self.lib = OwnedDLHandle("libcurl.so.4")

    def setopt_long(self, easy: CURLPtr, opt: c_int, value: c_long) raises:
        var curl_fn = self.lib.get_function[c_int]("curl_easy_setopt")
        var rc = curl_fn(easy, opt, value)
        if rc != CURLE_OK:
            raise Error("setopt_long failed opt=" + String(opt))

    def setopt_str(mut self, easy: CURLPtr, opt: c_int, value: String) raises:
        var curl_fn = self.lib.get_function[c_int]("curl_easy_setopt")
        var cs = value
        var slice = cs.as_c_string_slice()
        var rc = curl_fn(easy, opt, slice.unsafe_ptr())
        if rc != CURLE_OK:
            raise Error("setopt failed (str/callback)")

    def setopt_ptr(self, easy: CURLPtr, opt: c_int, addr: Int) raises:
        var curl_fn = self.lib.get_function[c_int]("curl_easy_setopt")
        var rc = curl_fn(easy, opt, addr)
        if rc != CURLE_OK:
            raise Error("setopt failed (str/callback)")

    def setopt_callback(self, easy: CURLPtr, opt: c_int) raises:
        # external_call resolves curl_easy_setopt at link time; passing the
        # Mojo function directly lets the compiler emit the C pointer.
        var rc = external_call[
            "curl_easy_setopt", c_int
        ](easy, opt, recv_callback)
        if rc != CURLE_OK:
            raise Error("setopt failed (str/callback)")


# ---------------------------------------------------------------------------
# Small char helpers (ASCII byte comparisons on UTF-8 strings)
# ---------------------------------------------------------------------------

def is_ws_byte(ch: StringSpan) -> Bool:
    return ch == " " or ch == "\t" or ch == "\n" or ch == "\r"


# ---------------------------------------------------------------------------
# HTML helpers (byte-oriented, allocation-light)
# ---------------------------------------------------------------------------


def html_decode_entities(text: String) -> String:
    """Decode the common HTML entities."""
    var out = text
    out = out.replace("&amp;", "&")
    out = out.replace("&lt;", "<")
    out = out.replace("&gt;", ">")
    out = out.replace("&quot;", "\"")
    out = out.replace("&#39;", "'")
    out = out.replace("&apos;", "'")
    out = out.replace("&nbsp;", " ")
    return out


def strip_tags(html: String) -> String:
    """Remove tags, collapse whitespace, decode entities.

    Drops the contents of <script> and <style> blocks entirely.

    Scans raw UTF-8 bytes: structural ASCII (<, >, whitespace) is handled
    directly; bytes >= 0x80 (inside a multi-byte sequence) are copied
    through untouched. Multi-byte UTF-8 never contains ASCII bytes, so
    byte-wise scanning cannot split a codepoint. find() is used for the
    script/style open and close lookups because slicing at an arbitrary
    byte offset may land mid-codepoint and abort.
    """
    var out = String()
    var in_tag = False
    var skip_until = String("")
    var last_space = True
    var i = 0
    var n = html.byte_length()
    var lower_all = String(html).lower()
    var lower_bytes = lower_all.as_bytes()
    var src_bytes = html.as_bytes()

    while i < n:
        # Raw byte read: ASCII structural chars are single bytes, and
        # multi-byte UTF-8 sequences never contain ASCII bytes, so a
        # byte-wise scan cannot split a codepoint.
        var b = Int(src_bytes[i])
        if skip_until != "":
            var close = html.find(skip_until, i)
            if close < 0:
                break
            i = close + skip_until.byte_length()
            skip_until = ""
            in_tag = False
            continue
        if not in_tag:
            if b == 0x3C:
                if html.find("<script", i) == i:
                    skip_until = "</script>"
                elif html.find("<style", i) == i:
                    skip_until = "</style>"
                in_tag = True
            elif b == 0x20 or b == 0x09 or b == 0x0A or b == 0x0D:
                if not last_space:
                    out.write_string(" ")
                    last_space = True
            else:
                # Copy the full codepoint starting at i (1 byte for ASCII,
                # up to 4 bytes for multi-byte UTF-8) from the source bytes.
                var cp_len = 1
                if b >= 0xF0:
                    cp_len = 4
                elif b >= 0xE0:
                    cp_len = 3
                elif b >= 0x80:
                    cp_len = 2
                var stop = i + cp_len
                if stop > n:
                    stop = n
                var seg = Span(
                    unsafe_ptr=src_bytes.unsafe_ptr().unsafe_offset(i),
                    length=stop - i,
                )
                out.write_string(String(from_utf8_lossy=seg))
                last_space = False
                i += cp_len - 1
        else:
            if b == 0x3E:
                in_tag = False
        i += 1
    return html_decode_entities(out)


def extract_tag_content(html: String, tag: String) -> String:
    """Extract inner content of the first <tag>...</tag> (case-insensitive)."""
    var lower = String(html).lower()
    var open_tag = String("<") + tag
    var close_tag = String("</") + tag + ">"
    var start = lower.find(open_tag)
    if start < 0:
        return String("")
    var gt = html.find(">", start)
    if gt < 0:
        return String("")
    var end = lower.find(close_tag, gt)
    if end < 0:
        return String("")
    return String(lower[byte=gt + 1:end])


def attr_value_after(html: String, html_lower: String, pos: Int, name: String) -> String:
    """Given a position inside a tag where attr `name` is expected, return
    its quoted value. `pos` points at `name="`. Empty string if absent."""
    var search_len = name.byte_length() + 2
    var qpos = pos + search_len
    if qpos >= html.byte_length():
        return String("")
    var quote = html[byte=qpos]
    var qb = Int(quote.as_bytes()[0])
    if qb != 34 and qb != 39:
        return String("")
    var end = html.find(chr_byte(qb), qpos + 1)
    if end < 0:
        return String("")
    return String(html[byte=qpos + 1:end])


def extract_meta_description(html: String) -> String:
    var lower = String(html).lower()
    var marker = "name=\"description\""
    var pos = lower.find(marker)
    if pos < 0:
        return String("")
    var cpos = lower.find("content=", pos)
    if cpos < 0:
        return String("")
    return attr_value_after(html, lower, cpos, String("content"))


def extract_links(html: String, mut links: List[String]) raises:
    """Collect all href="..." values into `links` (in document order)."""
    var lower = String(html).lower()
    var search = "href=\""
    var i = 0
    while True:
        var pos = lower.find(search, i)
        if pos < 0:
            break
        var vstart = pos + search.byte_length()
        var end = html.find("\"", vstart)
        if end < 0:
            break
        var url = String(html[byte=vstart:end])
        if url.byte_length() > 0 and not url.startswith("#"):
            links.append(url)
        i = end + 1


# ---------------------------------------------------------------------------
# JSON output helpers
# ---------------------------------------------------------------------------

comptime HEX_CHARS = "0123456789abcdef"


def chr_byte(b: Int) -> String:
    # Build a 1-byte ASCII string from a byte value.
    var arr = Array[Byte, 1](uninitialized=True)
    arr[0] = Byte(b)
    return String(from_utf8_lossy=Span(unsafe_ptr=arr.unsafe_ptr(), length=1))


def json_escape(text: String) -> String:
    # Raw-byte scan: JSON structural escapes are ASCII; multi-byte UTF-8
    # sequences (never containing ASCII bytes) are copied in one span.
    var out = String("\"")
    var tbytes = text.as_bytes()
    var i = 0
    var n = text.byte_length()
    while i < n:
        var b = Int(tbytes[i])
        if b == 0x22:
            out.write_string("\\\"")
            i += 1
        elif b == 0x5C:
            out.write_string("\\\\")
            i += 1
        elif b == 0x0A:
            out.write_string("\\n")
            i += 1
        elif b == 0x0D:
            out.write_string("\\r")
            i += 1
        elif b == 0x09:
            out.write_string("\\t")
            i += 1
        elif b < 0x20:
            # Control char: emit \u00XX
            out.write_string("\\u00")
            out.write_string(chr_byte(Int(HEX_CHARS[byte=b // 16].as_bytes()[0])))
            out.write_string(chr_byte(Int(HEX_CHARS[byte=b % 16].as_bytes()[0])))
            i += 1
        else:
            # Copy the full UTF-8 sequence starting at this lead byte.
            var cp_len = 1
            if b >= 0xF0:
                cp_len = 4
            elif b >= 0xE0:
                cp_len = 3
            elif b >= 0x80:
                cp_len = 2
            var stop = i + cp_len
            if stop > n:
                stop = n
            var seg = Span(
                unsafe_ptr=tbytes.unsafe_ptr().unsafe_offset(i),
                length=stop - i,
            )
            out.write_string(String(from_utf8_lossy=seg))
            i += cp_len
    out.write_string("\"")
    return out


# ---------------------------------------------------------------------------
# Fetch via libcurl
# ---------------------------------------------------------------------------

def fetch_url(
    mut curl: CurlEasy,
    url: String,
    timeout_sec: Int,
    max_bytes: Int,
    extra_headers: List[String],
    user_agent: String,
    mut headers_out: List[String],
) raises -> String:
    """Fetch a URL and return the body. A "status: N" line is appended to
    headers_out. Raises Error on network failure."""
    var easy_opt = external_call[
        "curl_easy_init", Optional[CURLPtr]
    ]()
    if not easy_opt:
        raise Error("curl_easy_init failed")
    var easy = easy_opt.value()

    # Receive buffer: allocated on the Mojo side, capped at max_bytes.
    var data_opt = external_call[
        "malloc", Optional[Pointer[UInt8, MutUntrackedOrigin]]
    ](c_size_t(max_bytes))
    if not data_opt:
        external_call["curl_easy_cleanup", NoneType](easy)
        raise Error("malloc failed")
    var data_ptr = data_opt.value()
    var bufmem = external_call[
        "malloc", Optional[Pointer[RecvBuf, MutUntrackedOrigin]]
    ](c_size_t(size_of[RecvBuf]()))
    if not bufmem:
        external_call["curl_easy_cleanup", NoneType](easy)
        raise Error("malloc buf failed")
    var buf_ptr = bufmem.value()
    buf_ptr[unsafe_offset=0] = RecvBuf(data_ptr, c_size_t(0), c_size_t(max_bytes))

    curl.setopt_str(easy, CURLOPT_URL, url)
    curl.setopt_callback(easy, CURLOPT_WRITEFUNCTION)
    curl.setopt_ptr(easy, CURLOPT_WRITEDATA, ptr_to_int(buf_ptr))
    curl.setopt_long(easy, CURLOPT_FOLLOWLOCATION, c_long(1))
    curl.setopt_long(easy, CURLOPT_MAXREDIRS, c_long(10))
    curl.setopt_long(easy, CURLOPT_TIMEOUT, c_long(timeout_sec))
    curl.setopt_long(easy, CURLOPT_CONNECTTIMEOUT, c_long(10))
    curl.setopt_long(easy, CURLOPT_NOSIGNAL, c_long(1))
    curl.setopt_long(easy, CURLOPT_SSL_VERIFYPEER, c_long(1))
    curl.setopt_long(easy, CURLOPT_SSL_VERIFYHOST, c_long(2))
    if user_agent.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_USERAGENT, user_agent)
    curl.setopt_str(easy, CURLOPT_ACCEPT_ENCODING, String(""))

    # Extra headers via curl_slist.
    comptime NULLLIST = Int(0)
    var have_headers = False
    var header_addr = NULLLIST
    var slist_append = curl.lib.get_function[Int]("curl_slist_append")
    var slist_free_all_addr = curl.lib.get_function[Int]("curl_slist_free_all")
    for h in extra_headers:
        var hs = h
        var slice = hs.as_c_string_slice()
        header_addr = slist_append(header_addr, slice.unsafe_ptr())
        have_headers = True
    if have_headers:
        curl.setopt_ptr(easy, CURLOPT_HTTPHEADER, header_addr)

    # Perform the transfer.
    var perform = curl.lib.get_function[c_int]("curl_easy_perform")
    var rc = perform(easy)

    var status = c_long(0)
    if rc == CURLE_OK:
        var getinfo = curl.lib.get_function[c_int]("curl_easy_getinfo")
        # CURLINFO_RESPONSE_CODE = CURLINFO_LONG + 2 -> 0x200002
        _ = getinfo(easy, c_int(0x200002), Pointer(to=status))
        headers_out.append(String("status: ") + String(status))
    else:
        var strerror = curl.lib.get_function[
            Optional[Pointer[c_char, MutUntrackedOrigin]]
        ]("curl_easy_strerror")
        var msg_opt = strerror(rc)
        var msg_str = String("curl error")
        if msg_opt:
            var msg = msg_opt.value()
            var mlen = external_call["strlen", c_size_t](msg)
            var mspan = Span(unsafe_ptr=msg.unsafe_bitcast[Byte](), length=Int(mlen))
            msg_str = String("curl error: ") + String(from_utf8_lossy=mspan)
        headers_out.append(msg_str)

    if have_headers:
        _ = slist_free_all_addr(header_addr)
    external_call["curl_easy_cleanup", NoneType](easy)

    if rc != CURLE_OK:
        external_call["free", NoneType](data_ptr.unsafe_bitcast[NoneType]())
        raise Error("fetch failed")

    var body_span = Span(
        unsafe_ptr=buf_ptr[unsafe_offset=0].data.unsafe_bitcast[Byte](),
        length=Int(buf_ptr[unsafe_offset=0].size),
    )
    var body = String(from_utf8_lossy=body_span)
    external_call["free", NoneType](data_ptr.unsafe_bitcast[NoneType]())
    external_call["free", NoneType](buf_ptr.unsafe_bitcast[NoneType]())
    return body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_help():
    print("hbrowser - tiny headless browser (Mojo + libcurl)")
    print("")
    print("Usage: hbrowser <url> [options]")
    print("")
    print("Options:")
    print("  --mode=text|html|json   Output format (default: json)")
    print("  --timeout=N             Total transfer timeout seconds (default: 15)")
    print("  --max=N                 Max body bytes (default: 2097152)")
    print("  --header=K: V           Extra request header (repeatable)")
    print("  --ua=string             User-Agent override")
    print("  --links                 Include extracted links (json mode)")
    print("  --no-meta               Omit title/description (json mode)")
    print("  --quiet                 Suppress the trailing newline")
    print("  --help                  Show this help")


def main() raises:
    var args = argv()
    if len(args) < 2:
        print_help()
        raise Error("no URL given")

    var url = String("")
    var mode = String("json")
    var timeout_sec = 15
    var max_bytes = 2097152
    var extra_headers = List[String]()
    var user_agent = String("hbrowser/0.1 (Mojo headless; +libcurl)")
    var want_links = False
    var want_meta = True
    var quiet = False

    var i = 1
    while i < len(args):
        var a = String(args[i])
        if a == "--help" or a == "-h":
            print_help()
            return
        elif a.startswith("--mode="):
            mode = String(a.removeprefix("--mode="))
        elif a.startswith("--timeout="):
            timeout_sec = Int(a.removeprefix("--timeout="))
        elif a.startswith("--max="):
            max_bytes = Int(a.removeprefix("--max="))
        elif a.startswith("--header="):
            extra_headers.append(String(a.removeprefix("--header=")))
        elif a.startswith("--ua="):
            user_agent = String(a.removeprefix("--ua="))
        elif a == "--links":
            want_links = True
        elif a == "--no-meta":
            want_meta = False
        elif a == "--quiet":
            quiet = True
        elif a.startswith("--"):
            # Unknown flag: ignore for forward compatibility.
            pass
        elif url.byte_length() == 0:
            url = a
        i += 1

    if url.byte_length() == 0:
        print_help()
        raise Error("no URL given")

    var curl = CurlEasy()
    var headers_out = List[String]()
    var t0 = perf_counter_ns()
    var body = fetch_url(
        curl, url, timeout_sec, max_bytes, extra_headers, user_agent, headers_out
    )
    var elapsed_ms = (perf_counter_ns() - t0) // 1_000_000

    if mode == "html":
        print(body)
        return

    var text = strip_tags(body)
    if mode == "text":
        print(text)
        return

    # json mode (default)
    var status = String("0")
    if len(headers_out) > 0 and headers_out[0].startswith("status: "):
        status = String(headers_out[0].removeprefix("status: "))
    print("{")
    print("  \"url\": " + json_escape(url) + ",")
    print("  \"status\": " + status + ",")
    print("  \"bytes\": " + String(body.byte_length()) + ",")
    print("  \"elapsed_ms\": " + String(elapsed_ms) + ",")
    if want_meta:
        var title = strip_tags(extract_tag_content(body, "title"))
        var desc = extract_meta_description(body)
        print("  \"title\": " + json_escape(title) + ",")
        print("  \"description\": " + json_escape(desc) + ",")
    if want_links:
        var links = List[String]()
        extract_links(body, links)
        print("  \"links\": [")
        for j in range(len(links)):
            var comma = String(",")
            if j == len(links) - 1:
                comma = String("")
            print("    " + json_escape(links[j]) + comma)
        print("  ],")
    print("  \"text\": " + json_escape(text))
    print("}")
