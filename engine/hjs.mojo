# hjs - headless browser with JavaScript execution, still feather-light.
#
# Architecture: hjs.mojo (this file, ~600 lines) owns HTTP + HTML
# extraction exactly like hbrowser, and drives a QuickJS engine
# (libhjs.so, C shim in hjs_shim.c) that runs the page's <script>
# code. JS sees: setTimeout/setInterval, a fetch()/XHR shim backed by
# __hjs_http (host performs the real HTTP), and a minimal DOM stub so
# scripts that read document.* do not crash. The event loop is fully
# cooperative: the host pumps until the page has no more timers,
# pending fetches, or microtasks.
#
# Usage (superset of hbrowser):
#   hjs <url> [--mode=text|html|json] [--timeout=15] [--max=2097152]
#        [--js/--no-js] [--header=K: V] [--ua=...] [--links] [--quiet]
#
# With --no-js (or for pages without <script>) it behaves exactly like
# hbrowser. With JS on, the fetch of subresources happens inside the
# engine loop: JS calls fetch()/XHR -> promise queued -> Mojo performs
# the HTTP -> resolves the promise -> JS continues.

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
from std.time import perf_counter_ns, sleep
from std.sys import argv
from std.sys.info import size_of


comptime CURLOPT_URL = c_int(10002)
comptime CURLOPT_WRITEFUNCTION = c_int(20011)
comptime CURLOPT_WRITEDATA = c_int(10001)
comptime CURLOPT_FOLLOWLOCATION = c_int(52)
comptime CURLOPT_TIMEOUT = c_int(13)
comptime CURLOPT_CONNECTTIMEOUT = c_int(78)
comptime CURLOPT_USERAGENT = c_int(10018)
comptime CURLOPT_ACCEPT_ENCODING = c_int(10102)
comptime CURLOPT_SSL_VERIFYPEER = c_int(64)
comptime CURLOPT_SSL_VERIFYHOST = c_int(81)
comptime CURLOPT_HTTPHEADER = c_int(10023)
comptime CURLOPT_MAXREDIRS = c_int(68)
comptime CURLOPT_NOSIGNAL = c_int(99)
comptime CURLOPT_CUSTOMREQUEST = c_int(10036)
comptime CURLOPT_POSTFIELDS = c_int(10015)
comptime CURLOPT_COPYPOSTFIELDS = c_int(10165)

comptime CURLOPT_COOKIE = c_int(10022)
comptime CURLOPT_COOKIEFILE = c_int(10031)
comptime CURLOPT_COOKIEJAR = c_int(10082)
comptime CURLOPT_COOKIELIST = c_int(10135)
comptime CURLOPT_REFERER = c_int(10016)
comptime CURLOPT_HTTP_VERSION = c_int(84)
comptime CURLOPT_SSLVERSION = c_int(32)
comptime CURLOPT_SSL_CIPHER_LIST = c_int(10083)
comptime CURLOPT_LOW_SPEED_LIMIT = c_int(19)
comptime CURLOPT_LOW_SPEED_TIME = c_int(20)
comptime CURLOPT_ACCEPT_ENCODING_VAL = c_int(10102)
comptime CURLOPT_TCP_FASTOPEN = c_int(244)
comptime CURLOPT_USERPWD = c_int(10005)

# CURL_HTTP_VERSION_2_0 = 3 (attempt 2.0, fall back allowed)
comptime CURL_HTTP_VERSION_2_0 = c_long(3)
# CURL_SSLVERSION_TLSv1_2 = 6
comptime CURL_SSLVERSION_TLSv1_2 = c_long(6)

comptime CURLE_OK = c_int(0)

comptime CURLPtr = Pointer[NoneType, MutUntrackedOrigin]


@fieldwise_init
struct RecvBuf(Copyable, Movable):
    var data: Pointer[UInt8, MutUntrackedOrigin]
    var size: c_size_t
    var cap: c_size_t


@fieldwise_init
struct CurlSList(Copyable, Movable):
    var data: Pointer[c_char, MutUntrackedOrigin]
    var next: Pointer[CurlSList, MutUntrackedOrigin]


def recv_callback(
    ptr: Pointer[c_uchar, MutUntrackedOrigin],
    sz: c_size_t,
    nmemb: c_size_t,
    userdata: Pointer[RecvBuf, MutUntrackedOrigin],
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
        var rc = external_call[
            "curl_easy_setopt", c_int
        ](easy, opt, recv_callback)
        if rc != CURLE_OK:
            raise Error("setopt failed (str/callback)")


def ptr_to_int(p: Pointer[RecvBuf, MutUntrackedOrigin]) -> Int:
    return Int(p.unsafe_bitcast[Pointer[UInt64, MutUntrackedOrigin]]())


def chr_byte(b: Int) -> String:
    var arr = Array[Byte, 1](uninitialized=True)
    arr[0] = Byte(b)
    return String(from_utf8_lossy=Span(unsafe_ptr=arr.unsafe_ptr(), length=1))


comptime HEX_CHARS = "0123456789abcdef"


# ---------------------------------------------------------------------------
# HTML helpers (shared logic with hbrowser, byte-oriented)
# ---------------------------------------------------------------------------


def is_ws_byte(ch: StringSpan) -> Bool:
    return ch == " " or ch == "\t" or ch == "\n" or ch == "\r"


def html_decode_entities(text: String) -> String:
    var out = text
    out = out.replace("&amp;", "&")
    out = out.replace("&lt;", "<")
    out = out.replace("&gt;", ">")
    out = out.replace("&quot;", "\"")
    out = out.replace("&#39;", "'")
    out = out.replace("&apos;", "'")
    out = out.replace("&nbsp;", " ")
    return out


def extract_scripts(html: String, mut scripts: List[String]) raises:
    """Collect inline <script> bodies (skip src= external ones; the
    engine will not fetch those - most scraping targets inline their
    bootstrap or the data is already in the DOM)."""
    var lower = String(html).lower()
    var i = 0
    while True:
        var pos = lower.find("<script", i)
        if pos < 0:
            break
        var gt = html.find(">", pos)
        if gt < 0:
            break
        var open_tag = String(html[byte=pos:gt + 1])
        var close = lower.find("</script>", gt)
        if close < 0:
            break
        if open_tag.find("src=") < 0:
            var body = String(html[byte=gt + 1:close])
            scripts.append(body)
        i = close + 9


def strip_tags(html: String) -> String:
    var out = String()
    var in_tag = False
    var skip_until = String("")
    var last_space = True
    var i = 0
    var n = html.byte_length()
    var src_bytes = html.as_bytes()

    while i < n:
        var b = Int(src_bytes[i])
        if skip_until != "":
            var close = html.find(skip_until, i)  # close tags matched verbatim
            if close < 0:
                break
            i = close + skip_until.byte_length()
            skip_until = ""
            in_tag = False
            continue
        if not in_tag:
            if b == 0x3C:
                # Compare a case-insensitive 7-byte tag window without
                # scanning a lowercased copy (its byte offsets can differ
                # from the original after .lower() on some Unicode).
                var win_stop = i + 7
                if win_stop > n:
                    win_stop = n
                var win = String(from_utf8_lossy=Span(
                    unsafe_ptr=src_bytes.unsafe_ptr().unsafe_offset(i),
                    length=win_stop - i,
                ))
                var wl = win.lower()
                if wl == "<script":
                    skip_until = "</script>"
                elif wl.startswith("<style"):
                    skip_until = "</style>"
                in_tag = True
            elif b == 0x20 or b == 0x09 or b == 0x0A or b == 0x0D:
                if not last_space:
                    out.write_string(" ")
                    last_space = True
            else:
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
    return String(html[byte=gt + 1:end])


def extract_links(html: String, mut links: List[String]) raises:
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


def json_escape(text: String) -> String:
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
            out.write_string("\\u00")
            out.write_string(chr_byte(Int(HEX_CHARS[byte=b // 16].as_bytes()[0])))
            out.write_string(chr_byte(Int(HEX_CHARS[byte=b % 16].as_bytes()[0])))
            i += 1
        else:
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
# Fetch via libcurl (same as hbrowser, plus method/body for XHR)
# ---------------------------------------------------------------------------

@fieldwise_init
struct FetchResult(Copyable, Movable):
    var status: Int
    var body: String


@fieldwise_init
struct FetchConfig(Copyable, Movable):
    # Stealth / behavior knobs shared by every request in a run.
    var profile: String          # browser profile name (empty = hjs default)
    var cookie_file: String      # read cookies from here ("" = none)
    var cookie_jar: String       # write cookies to here ("" = none)
    var referer: String          # Referer header ("" = none)
    var proxy: String            # proxy URL ("" = none)
    var use_http2: Bool          # try HTTP/2 (default true)
    var min_tls: String          # "1.2" or "1.3"
    var cipher_list: String      # TLS cipher list override ("" = curl default)
    var extra_headers: List[String]  # sec-ch-ua etc per profile
    var delay_ms: Int            # politeness delay before request
    var retries: Int             # retries on transient failure
    var backoff_ms: Int          # base backoff between retries

    @staticmethod
    def default() -> Self:
        var headers = List[String]()
        return Self(
            String(""),
            String(""),
            String(""),
            String(""),
            String(""),
            True,
            String("1.2"),
            String(""),
            headers^,
            0,
            2,
            500,
        )


def fetch_url(
    mut curl: CurlEasy,
    url: String,
    timeout_sec: Int,
    max_bytes: Int,
    user_agent: String,
    method: String,
    body: String,
    cfg: FetchConfig,
) raises -> FetchResult:
    """Returns status + body. Raises on network error."""
    var easy_opt = external_call[
        "curl_easy_init", Optional[CURLPtr]
    ]()
    if not easy_opt:
        raise Error("curl_easy_init failed")
    var easy = easy_opt.value()

    var data_opt = external_call[
        "malloc", Optional[Pointer[UInt8, MutUntrackedOrigin]]
    ](c_size_t(max_bytes))
    if not data_opt:
        external_call["curl_easy_cleanup", NoneType](easy)
        raise Error("malloc failed")
    var data_ptr = data_opt.value()
    var bufmem_opt = external_call[
        "malloc", Optional[Pointer[RecvBuf, MutUntrackedOrigin]]
    ](c_size_t(size_of[RecvBuf]()))
    if not bufmem_opt:
        external_call["curl_easy_cleanup", NoneType](easy)
        raise Error("malloc buf failed")
    var buf_ptr = bufmem_opt.value()
    buf_ptr[unsafe_offset=0] = RecvBuf(data_ptr, c_size_t(0), c_size_t(max_bytes))

    # Politeness delay before this request (rate limiting).
    if cfg.delay_ms > 0:
        sleep(Float64(cfg.delay_ms) / 1000.0)

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
    # Abort stalled transfers: below 1 KB/s for 30 s.
    curl.setopt_long(easy, c_int(19), c_long(1024))
    curl.setopt_long(easy, c_int(20), c_long(30))
    if user_agent.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_USERAGENT, user_agent)
    curl.setopt_str(easy, CURLOPT_ACCEPT_ENCODING, String(""))

    # HTTP/2 with fallback (real browsers speak h2).
    if cfg.use_http2:
        curl.setopt_long(easy, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2_0)
    # Floor TLS at 1.2 (or 1.3 only).
    if cfg.min_tls == "1.3":
        curl.setopt_long(easy, CURLOPT_SSLVERSION, c_long(7))
    else:
        curl.setopt_long(easy, CURLOPT_SSLVERSION, CURL_SSLVERSION_TLSv1_2)
    if cfg.cipher_list.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_SSL_CIPHER_LIST, cfg.cipher_list)

    # Cookie persistence (file read + jar write) and Referer.
    if cfg.cookie_file.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_COOKIEFILE, cfg.cookie_file)
    if cfg.cookie_jar.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_COOKIEJAR, cfg.cookie_jar)
    if cfg.referer.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_REFERER, cfg.referer)

    # Proxy support (http/https/socks5 URLs).
    if cfg.proxy.byte_length() > 0:
        curl.setopt_str(easy, c_int(10004), cfg.proxy)

    if method != "GET":
        curl.setopt_str(easy, CURLOPT_CUSTOMREQUEST, method)
    if body.byte_length() > 0:
        curl.setopt_str(easy, CURLOPT_COPYPOSTFIELDS, body)

    var have_headers = False
    var header_addr = Int(0)
    var slist_append = curl.lib.get_function[Int]("curl_slist_append")
    var slist_free_all_addr = curl.lib.get_function[Int]("curl_slist_free_all")
    for h in cfg.extra_headers:
        var hs = h
        var slice = hs.as_c_string_slice()
        header_addr = slist_append(header_addr, slice.unsafe_ptr())
        have_headers = True
    if have_headers:
        curl.setopt_ptr(easy, CURLOPT_HTTPHEADER, header_addr)

    var perform = curl.lib.get_function[c_int]("curl_easy_perform")
    var rc = perform(easy)

    var status = c_long(0)
    if rc == CURLE_OK:
        var getinfo = curl.lib.get_function[c_int]("curl_easy_getinfo")
        _ = getinfo(easy, c_int(0x200002), Pointer(to=status))

    if have_headers:
        _ = slist_free_all_addr(header_addr)
    external_call["curl_easy_cleanup", NoneType](easy)

    if rc != CURLE_OK:
        external_call["free", NoneType](data_ptr.unsafe_bitcast[NoneType]())
        external_call["free", NoneType](buf_ptr.unsafe_bitcast[NoneType]())
        raise Error("fetch failed")

    var body_span = Span(
        unsafe_ptr=buf_ptr[unsafe_offset=0].data.unsafe_bitcast[Byte](),
        length=Int(buf_ptr[unsafe_offset=0].size),
    )
    var body_out = String(from_utf8_lossy=body_span)
    external_call["free", NoneType](data_ptr.unsafe_bitcast[NoneType]())
    external_call["free", NoneType](buf_ptr.unsafe_bitcast[NoneType]())
    return FetchResult(Int(status), body_out)


# ---------------------------------------------------------------------------
# QuickJS engine bindings
# ---------------------------------------------------------------------------

comptime EnginePtr = Pointer[NoneType, MutUntrackedOrigin]


struct JSEngine:
    var lib: OwnedDLHandle

    def __init__(out self, path: String) raises:
        self.lib = OwnedDLHandle(path)

    def new(self) raises -> EnginePtr:
        var f = self.lib.get_function[EnginePtr]("hjs_new")
        return f()

    def free(self, eng: EnginePtr) raises:
        var f = self.lib.get_function[NoneType]("hjs_free")
        _ = f(eng)

    def eval(self, eng: EnginePtr, code: String) raises -> Int:
        var f = self.lib.get_function[c_int]("hjs_eval")
        var cs = code
        var clen = cs.byte_length()
        var slice = cs.as_c_string_slice()
        var rc = f(eng, slice.unsafe_ptr(), c_size_t(clen))
        return Int(rc)

    def eval_get(self, eng: EnginePtr, code: String) raises -> String:
        var f = self.lib.get_function[c_int]("hjs_eval_get")
        var cs = code
        var clen = cs.byte_length()
        var slice = cs.as_c_string_slice()
        var rc = f(eng, slice.unsafe_ptr(), c_size_t(clen))
        if rc != 0:
            return String("")
        var rf = self.lib.get_function[
            Optional[Pointer[c_char, MutUntrackedOrigin]]
        ]("hjs_result_ptr")
        var rp = rf(eng)
        if not rp:
            return String("")
        var ptr = rp.value()
        var slen = external_call["strlen", c_size_t](ptr)
        var span = Span(unsafe_ptr=ptr.unsafe_bitcast[Byte](), length=Int(slen))
        return String(from_utf8_lossy=span)

    def pending(self, eng: EnginePtr) raises -> Bool:
        var f = self.lib.get_function[c_int]("hjs_pending")
        return Int(f(eng)) != 0

    def has_work(self, eng: EnginePtr) raises -> Bool:
        var f = self.lib.get_function[c_int]("hjs_has_work")
        return Int(f(eng)) != 0

    def mark_done(self, eng: EnginePtr) raises:
        var f = self.lib.get_function[NoneType]("hjs_mark_done")
        _ = f(eng)

    def pump(self, eng: EnginePtr, budget_ms: Int) raises -> Int:
        var f = self.lib.get_function[c_int]("hjs_pump")
        return Int(f(eng, c_int(budget_ms)))

    def resolve_http(self, eng: EnginePtr, op_id: Int, status: Int, body: String) raises:
        var f = self.lib.get_function[c_int]("hjs_resolve_http")
        var bs = body
        var blen = bs.byte_length()
        var slice = bs.as_c_string_slice()
        var rc = f(eng, c_int(op_id), c_int(status), slice.unsafe_ptr(), c_size_t(blen))
        if rc != 0:
            raise Error("hjs_resolve_http: op not found")

    def pending_http(self, eng: EnginePtr) raises -> String:
        var f = self.lib.get_function[
            Optional[Pointer[c_char, MutUntrackedOrigin]]
        ]("hjs_pending_http_str")
        var rp = f(eng)
        if not rp:
            return String("")
        var ptr = rp.value()
        var slen = external_call["strlen", c_size_t](ptr)
        var span = Span(unsafe_ptr=ptr.unsafe_bitcast[Byte](), length=Int(slen))
        var out = String(from_utf8_lossy=span)
        var fs = self.lib.get_function[NoneType]("hjs_free_string")
        _ = fs(eng, ptr)
        return out

    def op_meta(self, eng: EnginePtr, op_id: Int, field: Int) raises -> String:
        """Read method/url/body of a pending op. field: 0=method, 1=url, 2=body."""
        var f = self.lib.get_function[
            Optional[Pointer[c_char, MutUntrackedOrigin]]
        ]("hjs_op_meta")
        var rp = f(eng, c_int(op_id), c_int(field))
        if not rp:
            return String("")
        var ptr = rp.value()
        var slen = external_call["strlen", c_size_t](ptr)
        var span = Span(unsafe_ptr=ptr.unsafe_bitcast[Byte](), length=Int(slen))
        var out = String(from_utf8_lossy=span)
        var fs = self.lib.get_function[NoneType]("hjs_free_string")
        _ = fs(eng, ptr)
        return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_help():
    print("hjs - headless browser with JS execution (Mojo + QuickJS)")
    print("")
    print("Usage: hjs <url> [options]")
    print("")
    print("Options:")
    print("  --mode=text|html|json   Output format (default: json)")
    print("  --timeout=N             Total transfer timeout seconds (default: 15)")
    print("  --max=N                 Max body bytes (default: 2097152)")
    print("  --js-budget-ms=N        Max JS event-loop time (default: 3000)")
    print("  --js/--no-js            Enable/disable script execution (default: --js)")
    print("  --header=K: V           Extra request header (repeatable)")
    print("  --ua=string             User-Agent override")
    print("")
    print("Stealth / scraping:")
    print("  --profile=NAME          Browser fingerprint profile (chrome131,")
    print("                          chrome131-mobile, firefox133, safari17, edge131)")
    print("  --cookies=FILE          Read cookies from Netscape-format file")
    print("  --cookie-jar=FILE       Write cookies to file after run (persist)")
    print("  --referer=URL           Set Referer header")
    print("  --proxy=URL             HTTP(S)/SOCKS5 proxy, e.g. socks5://host:1080")
    print("  --http1                 Force HTTP/1.1 (default: try HTTP/2)")
    print("  --min-tls=1.2|1.3       Minimum TLS version (default 1.2)")
    print("  --ciphers=LIST          TLS cipher list override")
    print("  --delay-ms=N            Politeness delay before each request")
    print("  --retries=N             Retries on 429/5xx/timeouts (default 2)")
    print("  --backoff-ms=N          Base backoff between retries (default 500)")
    print("  --links                 Include extracted links (json mode)")
    print("  --no-meta               Omit title/description (json mode)")
    print("  --method=GET|POST|...   HTTP verb for the request")
    print("  --body=DATA             Request body (with --method=POST etc.)")
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
    var js_budget_ms = 3000
    var want_js = True
    var extra_headers = List[String]()
    var user_agent = String("")
    var want_links = False
    var want_meta = True
    var quiet = False
    var http_method = String("GET")
    var post_body = String("")

    # Stealth knobs
    var profile = String("")
    var cookie_file = String("")
    var cookie_jar = String("")
    var referer = String("")
    var proxy = String("")
    var use_http2 = True
    var min_tls = String("1.2")
    var cipher_list = String("")
    var delay_ms = 0
    var retries = 2
    var backoff_ms = 500

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
        elif a.startswith("--js-budget-ms="):
            js_budget_ms = Int(a.removeprefix("--js-budget-ms="))
        elif a == "--js":
            want_js = True
        elif a == "--no-js":
            want_js = False
        elif a.startswith("--header="):
            extra_headers.append(String(a.removeprefix("--header=")))
        elif a.startswith("--ua="):
            user_agent = String(a.removeprefix("--ua="))
        elif a == "--links":
            want_links = True
        elif a == "--no-meta":
            want_meta = False
        elif a.startswith("--profile="):
            profile = String(a.removeprefix("--profile="))
        elif a.startswith("--cookies="):
            cookie_file = String(a.removeprefix("--cookies="))
        elif a.startswith("--cookie-jar="):
            cookie_jar = String(a.removeprefix("--cookie-jar="))
        elif a.startswith("--referer="):
            referer = String(a.removeprefix("--referer="))
        elif a.startswith("--proxy="):
            proxy = String(a.removeprefix("--proxy="))
        elif a == "--http1":
            use_http2 = False
        elif a.startswith("--min-tls="):
            min_tls = String(a.removeprefix("--min-tls="))
        elif a.startswith("--ciphers="):
            cipher_list = String(a.removeprefix("--ciphers="))
        elif a.startswith("--delay-ms="):
            delay_ms = Int(a.removeprefix("--delay-ms="))
        elif a.startswith("--retries="):
            retries = Int(a.removeprefix("--retries="))
        elif a.startswith("--backoff-ms="):
            backoff_ms = Int(a.removeprefix("--backoff-ms="))
        elif a.startswith("--method="):
            http_method = String(a.removeprefix("--method="))
        elif a.startswith("--body="):
            post_body = String(a.removeprefix("--body="))
        elif a.startswith("--"):
            pass
        elif url.byte_length() == 0:
            url = a
        i += 1

    if url.byte_length() == 0:
        print_help()
        raise Error("no URL given")

    # Build the stealth config; apply a browser profile if asked.
    var cfg = FetchConfig(
        profile,
        cookie_file,
        cookie_jar,
        referer,
        proxy,
        use_http2,
        min_tls,
        cipher_list,
        extra_headers^,
        delay_ms,
        retries,
        backoff_ms,
    )
    if cfg.profile.byte_length() > 0:
        var pname = cfg.profile
        var pua = load_profile(pname, cfg)
        if pua.byte_length() > 0 and user_agent.byte_length() == 0:
            user_agent = pua
    if user_agent.byte_length() == 0:
        user_agent = String("hjs/0.2 (Mojo+QuickJS headless)")

    var t0 = perf_counter_ns()

    var curl = CurlEasy()
    var status = 0
    var html = String("")
    var attempt = 0
    while True:
        var fetched = fetch_url(
            curl, url, timeout_sec, max_bytes, user_agent,
            http_method, post_body, cfg
        )
        status = fetched.status
        html = fetched.body
        if not looks_transient(status) or attempt >= cfg.retries:
            break
        sleep(Float64(cfg.backoff_ms * (attempt + 1)) / 1000.0)
        attempt += 1

    var captcha = detect_captcha(html)

    # Run page scripts if requested and any are present.
    var scripts = List[String]()
    if want_js:
        extract_scripts(html, scripts)

    if want_js and len(scripts) > 0:
        var engine_path = String(getenv_or("HJS_ENGINE", "/root/hjs/libhjs.so"))
        var js = JSEngine(engine_path)
        var eng = js.new()

        # Prepend the JS-side glue: fetch/XHR over __hjs_http + tiny DOM.
        var glue = String(get_glue())
        js.eval(eng, glue)
        for s in scripts:
            js.eval(eng, s)

        # Event loop: pump, servicing JS fetch() calls with real HTTP,
        # until the page settles or the JS budget runs out.
        var loop_deadline = js_budget_ms * 1_000_000
        var loop_start = perf_counter_ns()
        while js.pending(eng):
            if perf_counter_ns() - loop_start > loop_deadline:
                break
            var ops = js.pending_http(eng)
            if ops != "[]" and ops != "":
                # Resolve every pending op with a real fetch.
                var ids = parse_ids(ops)
                for op_id in ids:
                    var m = js.op_meta(eng, op_id, 0)
                    if m.byte_length() == 0:
                        m = String("GET")
                    var u = js.op_meta(eng, op_id, 1)
                    if u.byte_length() == 0:
                        continue
                    # Resolve relative URLs against the page URL.
                    u = resolve_url(url, u)
                    var b = js.op_meta(eng, op_id, 2)
                    var r2 = fetch_url(
                        curl, u, timeout_sec, max_bytes, user_agent,
                        m, b, cfg
                    )
                    js.resolve_http(eng, op_id, r2.status, r2.body)
                js.pump(eng, 10)
            else:
                js.pump(eng, 20)
        js.mark_done(eng)

        # Ask JS for the final DOM-ish state: the glue collects
        # document.body-like text if the page mutated it.
        var dom_text = js.eval_get(eng, "String(__hjs_dom_text())")

    var text = strip_tags(html)
    var elapsed_ms = (perf_counter_ns() - t0) // 1_000_000

    if mode == "html":
        print(html)
        return
    if mode == "text":
        print(text)
        return

    var status_str = String(status)
    print("{")
    print("  \"url\": " + json_escape(url) + ",")
    print("  \"status\": " + status_str + ",")
    print("  \"bytes\": " + String(html.byte_length()) + ",")
    print("  \"elapsed_ms\": " + String(elapsed_ms) + ",")
    if captcha.byte_length() > 0:
        print("  \"captcha\": " + json_escape(captcha) + ",")
    print("  \"attempts\": " + String(attempt + 1) + ",")
    if want_meta:
        var title = strip_tags(extract_tag_content(html, "title"))
        print("  \"title\": " + json_escape(title) + ",")
    if want_links:
        var links = List[String]()
        extract_links(html, links)
        print("  \"links\": [")
        for j in range(len(links)):
            var comma = String(",")
            if j == len(links) - 1:
                comma = String("")
            print("    " + json_escape(links[j]) + comma)
        print("  ],")
    print("  \"text\": " + json_escape(text))
    print("}")


def getenv_or(name: String, fallback: String) -> String:
    var nm = name
    var f = external_call[
        "getenv", Optional[Pointer[c_char, MutUntrackedOrigin]]
    ](nm.as_c_string_slice().unsafe_ptr())
    if not f:
        return fallback
    var ptr = f.value()
    var slen = external_call["strlen", c_size_t](ptr)
    var span = Span(unsafe_ptr=ptr.unsafe_bitcast[Byte](), length=Int(slen))
    return String(from_utf8_lossy=span)


def parse_ids(s: String) -> List[Int]:
    """Parse "[1,2,3]" into ids."""
    var ids = List[Int]()
    var cur = Int(0)
    var has = False
    var bytes = s.as_bytes()
    for i in range(s.byte_length()):
        var b = Int(bytes[i])
        if b >= 0x30 and b <= 0x39:
            cur = cur * 10 + (b - 0x30)
            has = True
        else:
            if has:
                ids.append(cur)
                cur = 0
                has = False
    if has:
        ids.append(cur)
    return ids^


def get_glue() -> String:
    return String(GLUE_JS)


comptime GLUE_JS = """
// hjs glue: fetch/XHR shims over __hjs_http + a tiny DOM stub.
var __hjs_text_parts = [];
var __hjs_doc = {
  title: "",
  body: { innerText: "", textContent: "", innerHTML: "" },
  documentElement: null,
  head: null,
  readyState: "loading"
};
var document = __hjs_doc;
document.documentElement = document;
document.head = document;
var location = { href: "", host: "", hostname: "", pathname: "/", search: "", hash: "", protocol: "https:" };
var window = this;
var navigator = { userAgent: "hjs" };
var self = this;

function __hjs_dom_text() {
  return __hjs_doc.body.innerText;
}

function fetch(url, opts) {
  opts = opts || {};
  var method = (opts.method || "GET").toUpperCase();
  var body = opts.body ? String(opts.body) : null;
  var headers = opts.headers ? JSON.stringify(opts.headers) : null;
  return __hjs_http(method, String(url), body, headers).then(function(r) {
    var o = JSON.parse(r);
    return {
      ok: o.status >= 200 && o.status < 300,
      status: o.status,
      statusText: "",
      text: function() { return Promise.resolve(o.body); },
      json: function() { return Promise.resolve(JSON.parse(o.body)); }
    };
  });
}

function XMLHttpRequest() {
  this.readyState = 0;
  this.status = 0;
  this.responseText = "";
  this._headers = {};
}
XMLHttpRequest.prototype.open = function(method, url) {
  this._method = method; this._url = url;
  this.readyState = 1;
};
XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
  this._headers[k] = v;
};
XMLHttpRequest.prototype.send = function(body) {
  var self = this;
  __hjs_http(this._method || "GET", this._url, body ? String(body) : null, null)
    .then(function(r) {
      var o = JSON.parse(r);
      self.status = o.status;
      self.responseText = o.body;
      self.readyState = 4;
      if (self.onload) self.onload();
      if (self.onreadystatechange) self.onreadystatechange();
    });
};

// innerHTML/innerText setters record text so extractors can read it.
Object.defineProperty(__hjs_doc.body, "innerHTML", {
  set: function(v) { __hjs_text_parts.push(String(v)); },
  get: function() { return __hjs_text_parts.join(""); }
});
Object.defineProperty(__hjs_doc.body, "innerText", {
  set: function(v) { __hjs_text_parts.push(String(v)); },
  get: function() { return __hjs_text_parts.join(""); }
});
Object.defineProperty(__hjs_doc.body, "textContent", {
  set: function(v) { __hjs_text_parts.push(String(v)); },
  get: function() { return __hjs_text_parts.join(""); }
});
// Mark page as loaded; host treats settled loop as done.
__hjs_doc.readyState = "complete";
""";

def resolve_url(base: String, rel: String) -> String:
    """Resolve a possibly-relative URL against the page URL.
    Handles: absolute (scheme://), protocol-relative (//), root-relative
    (/path), and same-directory paths. Query/hash kept as-is."""
    if rel.find("://") >= 0:
        return rel
    if rel.startswith("//"):
        # inherit scheme
        var scheme_end = base.find("://")
        if scheme_end < 0:
            return rel
        return String(base[byte=0:scheme_end]) + rel
    # Find origin: scheme://host[:port]
    var scheme_end = base.find("://")
    if scheme_end < 0:
        return rel
    var after_scheme = scheme_end + 3
    var path_start = base.find("/", after_scheme)
    var origin = base
    if path_start < 0:
        origin = base + "/"
        path_start = base.byte_length()
    var origin_part = String(base[byte=0:path_start])
    if rel.startswith("/"):
        return origin_part + rel
    # Same-directory: take directory part of the page path.
    var dir_end = base.rfind("/", path_start)
    if dir_end < 0:
        dir_end = path_start
    var dir_part = String(base[byte=0:dir_end + 1])
    return dir_part + rel

# ---------------------------------------------------------------------------
# Browser fingerprint profiles
# ---------------------------------------------------------------------------

comptime PROFILES_FILE = "/root/hjs/browser_profiles.txt"


def load_profile(name: String, mut cfg: FetchConfig) -> String:
    """Apply a browser profile's UA + headers + ciphers to cfg.
    Returns the profile's User-Agent (empty if profile unknown)."""
    var path = getenv_or("HJS_PROFILES", PROFILES_FILE)
    var f_opt = read_file(path)
    if f_opt == "":
        return String("")
    var ua = String("")
    var in_block = False
    var lines = f_opt.splitlines()
    for ln in lines:
        var line = String(ln).strip()
        if line.startswith("[") and line.endswith("]"):
            var blk = String(line[byte=1:line.byte_length() - 1])
            if in_block:
                # leaving the target block: stop if we found it
                pass
            in_block = blk == name
            continue
        if not in_block or line.byte_length() == 0:
            continue
        if line.startswith("ua = ") or line.startswith("ua="):
            ua = String(line.removeprefix("ua = ").removeprefix("ua="))
        elif line.startswith("ciphers = ") or line.startswith("ciphers="):
            cfg.cipher_list = String(line.removeprefix("ciphers = ").removeprefix("ciphers="))
        elif line.startswith("accept = ") or line.startswith("accept="):
            var acc = line.removeprefix("accept = ").removeprefix("accept=")
            cfg.extra_headers.append(String("Accept: ") + acc)
        elif line.startswith("lang = ") or line.startswith("lang="):
            var lang = line.removeprefix("lang = ").removeprefix("lang=")
            cfg.extra_headers.append(String("Accept-Language: ") + lang)
        elif line.startswith("ch = ") or line.startswith("ch="):
            var ch = line.removeprefix("ch = ").removeprefix("ch=")
            if ch.byte_length() > 0:
                cfg.extra_headers.append(String("sec-ch-ua: ") + ch)
                cfg.extra_headers.append(String("sec-ch-ua-mobile: ?0"))
                cfg.extra_headers.append(String("sec-ch-ua-platform: \"Windows\""))
        elif line.startswith("sec_fetch = ") or line.startswith("sec_fetch="):
            var sf = line.removeprefix("sec_fetch = ").removeprefix("sec_fetch=")
            cfg.extra_headers.append(String("Sec-Fetch-Dest: ") + sf)
            cfg.extra_headers.append(String("Sec-Fetch-Mode: navigate"))
            cfg.extra_headers.append(String("Sec-Fetch-Site: none"))
            cfg.extra_headers.append(String("Sec-Fetch-User: ?1"))
            cfg.extra_headers.append(String("Upgrade-Insecure-Requests: 1"))
    return ua


def read_file(path: String) -> String:
    """Read a whole file as a String ("" if missing).

    Implemented with popen+cat because the stdlib already declares a
    conflicting libc `fclose` external_call; declaring our own breaks
    the module. popen/pclose/fread have no such conflict.
    """
    var cmd = String("cat '") + path + String("' 2>/dev/null")
    var cs = cmd
    var mode = String("r")
    var pipe = external_call[
        "popen", Optional[Pointer[NoneType, MutUntrackedOrigin]]
    ](cs.as_c_string_slice().unsafe_ptr(), mode.as_c_string_slice().unsafe_ptr())
    if not pipe:
        return String("")
    var p = pipe.value()
    var out = String()
    var buf = Array[Byte, 65536](uninitialized=True)
    while True:
        var got = external_call[
            "fread", c_size_t
        ](buf.unsafe_ptr(), c_size_t(1), c_size_t(65536), p)
        if got == 0:
            break
        var seg = Span(unsafe_ptr=buf.unsafe_ptr(), length=Int(got))
        out.write_string(String(from_utf8_lossy=seg))
    _ = external_call["pclose", c_int](p)
    return out

def detect_captcha(html: String) -> String:
    """Return a captcha type hint if the page looks like a bot challenge,
    empty string otherwise. Checks common providers + generic markers."""
    var lower = String(html).lower()
    if lower.find("cf-challenge") >= 0 or lower.find("checking your browser") >= 0 or lower.find("cf-browser-verification") >= 0:
        return String("cloudflare")
    if lower.find("captcha-delivery.com") >= 0 or lower.find("px-captcha") >= 0:
        return String("perimeterx")
    if lower.find("recaptcha") >= 0 or lower.find("g-recaptcha") >= 0:
        return String("recaptcha")
    if lower.find("hcaptcha.com") >= 0 or lower.find("h-captcha") >= 0:
        return String("hcaptcha")
    if lower.find("datadome") >= 0:
        return String("datadome")
    if lower.find("incapsula") >= 0 or lower.find("_incap_") >= 0:
        return String("incapsula")
    if lower.find("just a moment") >= 0 and lower.find("enable") >= 0:
        return String("cloudflare-generick")
    return String("")


def looks_transient(status: Int) -> Bool:
    """Statuses worth retrying: 408, 425, 429, 5xx."""
    return status == 408 or status == 425 or status == 429 or (status >= 500 and status <= 599)

def fopen(path: String, mode: String) -> Optional[Pointer[NoneType, MutUntrackedOrigin]]:
    var p = path
    var m = mode
    var pslice = p.as_c_string_slice()
    var mslice = m.as_c_string_slice()
    var f = external_call[
        "fopen", Optional[Pointer[NoneType, MutUntrackedOrigin]]
    ](pslice.unsafe_ptr(), mslice.unsafe_ptr())
    return f


def fread(buf_addr: UInt, sz: Int, n: Int, f: Optional[Pointer[NoneType, MutUntrackedOrigin]]) -> c_size_t:
    if not f:
        return 0
    return external_call[
        "fread", c_size_t
    ](buf_addr, c_size_t(sz), c_size_t(n), f.value())


def fclose_file(f: Optional[Pointer[NoneType, MutUntrackedOrigin]]):
    if not f:
        return
    _ = external_call["fclose", c_int](f.value())
