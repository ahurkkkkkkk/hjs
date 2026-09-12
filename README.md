# hjs

A tiny stealth web browser for scraping, written in [Mojo](https://mojolang.org)
with JavaScript execution via QuickJS. Built to do the 95% of scraping jobs
where Playwright and Selenium are wildly oversized.

The whole stack is about 4 MB and roughly 21 MB of RAM per fetch. Playwright
with Chromium wants ~300 MB and a 170 MB download just to boot. A single hjs
worker clears ~50 fetches/s on localhost and 8 of them hit ~205/s on a 6-core
box, all under 120 MB total. You drive it from Python, Go, or any MCP client
(Claude Desktop, ZCode, etc).

It is not a browser emulator. It fetches pages, runs the page's JavaScript in
QuickJS with a working `fetch()`/XHR/event loop, parses HTML down to readable
text and structured data, keeps cookie sessions, rotates browser
fingerprints, and flags captcha walls so your pipeline can react. It has no
layout engine, so it will never give you pixel-perfect screenshots. The
`hjs-tplugin` plugin closes most of that gap with PDF export, a PNG content
snapshot, reader text, print output, and a line-based scroll/touch model, all
still in pure stdlib and all still ~20 MB.

## Why bother

![Memory and install size comparison](docs/memory-chart.svg)

| | hjs | Playwright (Chromium) | Selenium (Chrome) |
|---|---|---|---|
| RAM per browser session | ~21 MB | ~300 MB | ~300 MB |
| RAM for 8 parallel workers | ~118 MB | ~2400 MB (8 Chromium) | ~2400 MB |
| Install size | 4 MB | ~170 MB (browser) + pip | ~15 MB + chromedriver |
| Process start to first fetch | ~22 ms warm pool / ~0.9 s cold one-shot (TLS+RTT dominated) | ~1 s browser launch | ~1 s + driver handshake |
| Runs JavaScript | Yes (QuickJS, ES2020) | Yes (V8, full DOM) | Yes (V8, full DOM) |
| JA3/JA4 TLS fingerprint | `Splugin` plugin: real Chrome/Firefox/Safari | real (it is Chrome) | real (it is Chrome) |
| PDF export | Built in (`hjs-tplugin`) | Yes | Yes |
| PNG screenshot | Content snapshot (no layout) | Full pixel render | Full pixel render |
| Scroll / tap links | Built in (line hit-test) | Yes (pixels) | Yes (pixels) |
| Form submit (POST/JSON) | Built in, session-aware | `fill`/`click`/`submit` | `find_element`/`click` |
| Structured extract (OG, JSON-LD) | Built in | DIY | DIY |
| Captcha/bot-wall detection | Built in, reported as data | DIY | DIY |
| Fingerprint profiles + rotation | Built in (UA, client hints, Sec-Fetch, ciphers) | DIY/stealth plugins | DIY |
| Cookie jar persistence | Built in (`curl -b/-c` style) | StorageState | Cookie API |
| robots.txt + sitemap helpers | Built in | No | No |
| MCP server (AI agent ready) | 17 tools included | Official server | No |
| Codegen (record + replay) | Included (Python/Go) | Inspector | Record-and-playback |
| Parallel scraping | Process pool, no per-tab daemon | Browser contexts | Drivers per session |

hjs RAM and timings were measured with `time -v` and a scrape benchmark on this
repo (see [Benchmarks](#benchmarks)). Playwright/Selenium numbers are their
documented headless-Chromium ranges.

### Why it is so much lighter

Playwright and Selenium both run a full Chrome. A "page" is a rendered
document: compositor, V8 isolate with DOM bindings, network stack, GPU
process. Most scrapers never touch any of that. They want the HTML after the
page's own JavaScript has done its work, plus a few structured fields.

hjs keeps exactly that and deletes the rest:

* QuickJS instead of V8. Single-digit MB, no DOM bindings, no JIT tiers, no
  isolate heap. It runs real ES2020 (async/await, classes, promises).
* libcurl instead of Chrome's network stack. HTTP/2, TLS, cookies, gzip/brotli.
* The HTML is never parsed into a tree. Extraction is a single byte scan. Text,
  title, links, Open Graph and JSON-LD come out of that scan.
* No rendering pipeline. No layout, no raster, no GPU process, no 100 MB of V8
  heap. That is where the 15x RAM difference comes from.

When your scraper ran 12 Chrome tabs on a 1-core box and pinned it at load 12,
this runs the same 12 URLs as 12 workers at ~21 MB each and the CPU mostly
sits idle waiting on the network.

## Benchmarks

Everything measured live on this repo (6-core WSL2, Ubuntu 24.04), with the
full raw data checked in at [docs/bench-results.json](docs/bench-results.json)
and the generator at `sdk-python/bench_splugin.py`, so every number below can
be reproduced or argued with.

### Memory and throughput (hjs, local test server, warm)

![hjs scaling](docs/scaling-chart.svg)

| hjs workers | peak RAM | fetches/s (local) |
|---|---|---|
| 1 | 19 MB | ~55 |
| 4 | 66 MB | ~187 |
| 8 | 118 MB | ~247 |
| 16 | 254 MB | ~57 (subprocess contention on 6 cores) |

A single hjs fetch peaks at ~21 MB RSS. Compare: one headless Chromium is
~300 MB. Fifty parallel Chromium contexts is not a configuration people run;
fifty hjs workers is ~1 GB. That gap is the whole argument for using this
instead, and it is a memory argument first and a CPU argument second.

### One-shot and warm cost (hjs vs Splugin, same machine)

| what | median ms | peak RSS |
|---|---|---|
| hjs, fresh process + fetch (cold) | ~900 | 21 MB |
| Splugin, fresh bridge + 1 fetch (cold) | ~930 | 17 MB |
| Splugin, warm bridge, each extra fetch | ~220 | 17 MB total |

Cold start parity is expected: both processes spawn fast, the ~900 ms is mostly
TLS+RTT to a public host. The interesting row is the last one: reuse one
long-lived bridge and Splugin gives browser-grade TLS at 220 ms/fetch on a live
site, and 17 MB total no matter how many profiles you rotate through. hjs's own
process-pool model amortizes the same way across workers.

### TLS fingerprints (JA4) as a server sees them

Measured by asking tls.peet.ws to echo back the real handshake of each tool on
this box. A JA4 starting `t13d1516h2_8daaf6152771` is what Chrome sends.

| client | JA4 seen by server | verdict |
|---|---|---|
| hjs (libcurl/OpenSSL) | `t13d3112h2_e8f1e7e78f70` | reads as a scraper |
| python requests (OpenSSL) | `t13d3112h1_e8f1e7e78f70` | reads as a scraper |
| **hjs Splugin** `chrome_131` | `t13d1516h2_8daaf6152771` | **identical to Chrome** |
| **hjs Splugin** `firefox_133` | `t13d1714h2_5b57614c22b0` | Firefox |
| **hjs Splugin** `safari_16_0` | `t13d2014h2_a09f3c656075` | Safari |
| curl_cffi `chrome131` (reference) | `t13d1516h2_8daaf6152771` | matches the same Chrome |

Splugin's Chrome JA4 is byte-identical to curl_cffi's impersonation of Chrome,
and curl_cffi is the library the Python scraping world trusts for this. The
difference is Splugin ships it inside the hjs stack: same SDK call style, same
cookie jar, and the result flows straight into the tplugin screenshot/pdf/touch
tools, which curl_cffi has no answer for.

### HTTP/2

hjs and Splugin both negotiate `h2` against ALPN-capable hosts; plain
`requests` falls to HTTP/1.1. Real browsers speak h2, so an h1.1-only client is
a weak but real fingerprint signal. (hjs: h2 via libcurl. Splugin: h2,
ALPN `h2,http/1.1` exactly as Chrome advertises.)

Notes, stated plainly so the numbers are not misread:

* The 55-250 fetches/s range is a localhost test server. Real scraping is
  bound by remote hosts and RTT, so being polite (per-host delay, retries)
  matters more than these CPU numbers. They say: hjs is never the bottleneck.
* The 20 MB single-fetch RSS is measured with `/usr/bin/time -v`, not estimated.
* Playwright/Selenium figures are their documented ranges for headless Chrome;
  the CDN was too slow on this box to install Chromium and measure a live
  side-by-side, so those cells are marked as ranges, not measurements.
* A single hjs process is fast to spawn but each fetch pays the TLS cost; the
  model is a pool of short-lived processes, not a browser you keep open. That
  is the whole design trade: cheap to start, ~20 MB each, no persistent DOM.

### How to reproduce

```sh
cd sdk-python
HJS_BIN=/usr/local/bin/hjs HJS_SPLUGIN=/path/to/sbridge \
  python3 bench_splugin.py > docs/bench-results.json
```

## Install

The binary is a Linux x86-64 ELF (WSL on Windows is fine). Three files:

```
hjs                   the binary (~160 KB)
libhjs.so             QuickJS engine + C shim (3.8 MB)
browser_profiles.txt  fingerprint profiles, edit freely
sbridge               Splugin TLS bridge (~16 MB; only needed for JA3/JA4
                      impersonation, point $HJS_SPLUGIN at it or put it on PATH)
```

```sh
mkdir -p /opt/hjs && cp hjs libhjs.so browser_profiles.txt /opt/hjs/
ln -s /opt/hjs/hjs /usr/local/bin/hjs
```

The `hjs` binary needs libcurl (present on every normal Linux) and, if you
built from source, `LD_LIBRARY_PATH` pointed at the Mojo runtime libs (see
`engine/BUILDING.md`).

Python SDK (no third-party dependencies, stdlib only):

```sh
pip install ./sdk-python
```

Go SDK:

```sh
go get github.com/ahurkkkkkkk/hjs/sdk-go        # client
# tplugin ships in the same module:
import "github.com/ahurkkkkkkk/hjs/sdk-go/tplugin"
```

## Quick start

### Python

```python
from hjs import Browser

with Browser(profile="chrome131") as b:
    page = b.goto("https://example.com")
    print(page.status, page.title)
    print(page.text[:300])

    # structured data: OG tags, meta, JSON-LD, links with anchor text
    data = page.structured()
    print(data["og"]["og:title"], len(data["json_ld"]))

    # follow a link; session cookies and referer chain follow automatically
    p2 = b.goto(data["links"][0]["href"])

    # scrape 4 URLs in parallel, one shared session
    pages = b.scrape(["https://a.com", "https://b.com"], workers=4)

    # robots.txt and sitemaps
    robots = b.robots("https://example.com")
    if robots.allowed("https://example.com/page"):
        for url in b.sitemap("https://example.com"):
            print(url)
```

### Go

```go
b, err := hjs.New(hjs.Options{Profile: "chrome131", Session: true})
if err != nil { log.Fatal(err) }
defer b.Close()

p, err := b.Goto("https://example.com", nil)
fmt.Println(p.Status, p.Title, len(p.Links))

pages, errs := b.ScrapeAll(ctx, urls, 4)
```

### Forms and login (submit)

The Playwright way is to fill fields and click. hjs does it as a direct form or
JSON post that carries your session cookies and referer, so login-then-scrape
works without a DOM:

```python
b = Browser(profile="chrome131")
b.goto("https://site.example/login")
b.submit("https://site.example/login",
         data={"user": "me", "pass": "secret"})   # cookies + CSRF token now live
page = b.goto("https://site.example/dashboard")  # authenticated
```

```go
b.Goto(loginURL, nil)
b.Submit(loginURL, map[string]string{"user":"me","pass":"secret"}, nil, nil)
p, _ := b.Goto("https://site.example/dashboard", nil)
```

### hjs-tplugin: PDF, PNG, reader, print, touch and scroll

A pure-stdlib rendering plugin (Python `hjs_tplugin.py`, Go `tplugin` package).
No Pillow, no reportlab, no wkhtmltopdf, no Chromium: it ships its own 5x7
bitmap font, a hand-written PNG encoder and a hand-written PDF writer.

```python
from hjs import Browser
import hjs_tplugin as t

b = Browser(profile="chrome131")
page = b.goto("https://news.ycombinator.com")

open("page.pdf", "wb").write(t.pdf(page))        # paginated, valid PDF 1.4
open("shot.png", "wb").write(t.screenshot(page)) # text rendered to an image

print(t.reader(page)[:200])                      # main text, boilerplate out
print(t.print_(page))                             # form-feed paginated plain text

v = t.Viewer(page)                                # scroll + touch over the text
print(v.scroll(20))                              # 20 rows down, visible lines
target = v.tap_text("Show more")                 # hit-test a link by text
if target:
    b.goto(target)
```

```go
import "github.com/ahurkkkkkkk/hjs/sdk-go/tplugin"

pdf  := tplugin.PDF(p, 56)
png  := tplugin.Screenshot(p, 2)
main := tplugin.Reader(p)
v    := tplugin.NewViewer(p, 24)
v.Scroll(20)
if url := v.TapText("Show more"); url != "" { b.Goto(url, nil) }
```

`screenshot` is a content snapshot, not a pixel render. hjs has no layout
engine, so the PNG is the extracted text laid out on a grid with link lines
highlighted, good enough to eyeball a page or attach to a report. If you need
the exact pixels Chrome paints, that is the one job Playwright is built for and
this does not fake it.

### Splugin: real browser TLS fingerprints (JA3/JA4)

The one thing a browser gives a scraper that libcurl cannot: the TLS handshake.
TLS-terminating anti-bot systems read the ClientHello (cipher order, extension
order, GREASE, ALPN, curves) and hash it into JA3/JA4. OpenSSL always looks
like OpenSSL, no matter what headers you put on top.

Splugin fixes exactly that. It is a companion plugin: a small Go bridge process
built on `bogdanfinn/tls-client` (a maintained fork of utls, the same
impersonation tech curl-impersonate and curl_cffi use) that exposes 79 real
browser and app TLS identities. The Python and Go SDKs talk to it over stdin
with newline JSON, keep a cookie jar across requests, and the results plug
straight into everything else in this repo (tplugin screenshot/pdf/Viewer,
captcha detection, structured extraction).

```python
from hjs_splugin import Session
import hjs_tplugin as t

s = Session(profile="chrome_131")            # 79 profiles in s.profiles()
r = s.fetch("https://protected-site.example", fp=True)
print(r.status, r.title, r.ja4)              # t13d1516h2... = real Chrome JA4
open("shot.png", "wb").write(t.screenshot(r))
```

```go
s, _ := splugin.New(splugin.Options{Profile: "chrome_131"})
defer s.Close()
r, _ := s.Fetch("https://protected-site.example", splugin.FetchOpts{FP: true})
fmt.Println(r.Status, r.JA4)
```

`fp=True` is a nice debugging trick: the fetch also asks tls.peet.ws what the
server actually saw, and returns `.ja3`, `.ja4`, `.peetprint` on the result, so
you can prove your fingerprint is clean without leaving your REPL.

When to use which: hjs for volume scraping (lowest overhead), Splugin when a
specific site gates on TLS. Both share the same result shape, so a pipeline can
start on hjs and fall back to Splugin on the exact URLs that return 403s.

![JA4 fingerprints measured live](docs/fingerprint-chart.svg)

### Fingerprint rotation

```python
b = Browser(profiles=["chrome131", "firefox133", "safari17", "edge131"])
for url in urls:
    b.goto(url)  # cycles to the next browser identity every request
```

### Parallel with per-host politeness

```python
b = Browser(profile="chrome131", per_host_delay_ms=1500, retries=3)
pages = b.scrape(urls, workers=8)   # 8 in flight, max 1 req/1.5s per host
```

## Stealth and anti-block features

* **Browser profiles**: five ready-made identities (Chrome 131 desktop/mobile,
  Firefox 133, Safari 17, Edge 131). Each bundles a real User-Agent, `sec-ch-ua`
  client hints, `Sec-Fetch-*` headers, `Accept`/`Accept-Language` and a
  browser-matching TLS cipher list. Profiles are plain text in
  `browser_profiles.txt`; add your own by copying a block.
* **Cookies**: `--cookies=FILE` and `--cookie-jar=FILE` give persistent,
  Netscape-format sessions across runs. The SDK keeps a session jar per Browser.
* **Referer chaining**: every `goto` inside a session sends the previous page as
  the Referer, like clicking through a site.
* **TLS**: HTTP/2 with fallback, TLS floor 1.2 or 1.3, custom cipher lists, and,
  with the Splugin plugin, genuine browser TLS handshakes (JA3/JA4). See the
  fingerprint benchmark above.
* **Proxy**: any curl proxy URL (`socks5://`, `http://`) via `--proxy=`.
* **Rate limiting**: `--delay-ms` per request, `per_host_delay_ms` for the pool,
  `--retries` + linear `--backoff-ms` on 429/5xx/timeouts.
* **Captcha/bot-wall detection**: every response is checked for Cloudflare,
  PerimeterX, reCAPTCHA, hCaptcha, DataDome and Incapsula markers. Detected
  walls come back as `page.captcha == "cloudflare"` so your code can route the
  URL to a solver or drop it. Detection only; nothing solves captchas.
* **Stall kill**: transfers below 1 KB/s for 30 s abort on their own so a dead
  connection never hangs a job.

### Honest limits

Core hjs speaks OpenSSL TLS: HTTP-level headers are browser-shaped, but the
JA3/JA4 fingerprint will not match real Chrome on its own. That is exactly the
gap the Splugin plugin closes (see the measured JA4 table), so a site that
blocks hjs on TLS can be handled from the same pipeline by swapping the fetch
call, not the whole stack. Even with Splugin: if you get `blocked: cloudflare`
back and retries with fresh cookies do not clear it, the vendor is doing
browser-attestation beyond fingerprints (JS challenges, TLS timing, HTTP
fingerprinting); that needs a real browser or a solver service.

External `<script src>` files are not fetched, only inline scripts run. Pages
that build their whole DOM with React/Vue and never set body text still extract
thin, because there is no layout engine to reconstruct what the script painted.

## MCP server (AI agents)

`sdk-python/mcp_server.py` speaks Model Context Protocol over stdio. Add it to
Claude Desktop or ZCode:

```json
{
  "mcpServers": {
    "hjs": {
      "command": "python3",
      "args": ["/opt/hjs/mcp_server.py"],
      "env": {
        "HJS_BIN": "/usr/local/bin/hjs",
        "HJS_SPLUGIN": "/opt/hjs/sbridge"
      }
    }
  }
}
```

21 tools (the four TLS ones only appear when the Splugin bridge is found):
`hjs_goto`, `hjs_submit`, `hjs_extract`, `hjs_html`, `hjs_links`,
`hjs_click_link`, `hjs_tap`, `hjs_scroll`, `hjs_screenshot`, `hjs_pdf`,
`hjs_print`, `hjs_reader`, `hjs_wait_for`, `hjs_scrape`, `hjs_sitemap`,
`hjs_robots`, `hjs_session`, plus Splugin's `hjs_tls_fetch`, `hjs_tls_post`,
`hjs_tls_profiles`, `hjs_tls_session`. An agent gets text plus numbered links,
can click or tap through a site, take a snapshot or PDF, fetch through a real
browser TLS handshake when the target is picky, and one session (cookies plus
referer chain) is preserved across calls. `python3 mcp_server.py --self-test`
drives the whole protocol end to end and checks a real screenshot, a real PDF,
and a real Chrome JA4 all come back.

## CLI

```
hjs <url> [--mode=text|html|json] [--timeout=15] [--max=2097152]
          [--profile=chrome131] [--ua=...] [--cookies=F] [--cookie-jar=F]
          [--referer=URL] [--proxy=URL] [--http1] [--min-tls=1.2|1.3]
          [--ciphers=LIST] [--delay-ms=N] [--retries=N] [--backoff-ms=N]
          [--method=GET|POST|...] [--body=DATA]
          [--js/--no-js] [--js-budget-ms=3000] [--links] [--no-meta] [--quiet]
```

Exit codes: 0 ok, 1 usage error, 2 network error, 3 HTTP error status.

## Repo layout

```
binary/            prebuilt hjs + libhjs.so + browser_profiles.txt + sbridge
engine/            hjs.mojo, hjs_shim.c, build-from-source guide
sdk-python/        hjs.py, hjs_tplugin.py, hjs_splugin.py, mcp_server.py,
                   bench_splugin.py, tests, examples
sdk-go/            hjs.go module, tplugin/ package, splugin/ package + bridge,
                   tests
sibling-hbrowser/  hbrowser.mojo, the no-JS 120 KB variant
docs/              memory/scaling/fingerprint charts, bench-results.json,
                   feature and deployment guides
```

The Splugin bridge (`sdk-go/splugin/bridge`) is the only piece with a third-party
dependency (utls via `github.com/bogdanfinn/tls-client`); it builds with a plain
`go build -o sbridge .` and stays a separate small process, so the hjs core and
tplugin remain dependency-free.

## Building from source

`engine/BUILDING.md` has the full toolchain recipe (Mojo 1.0.0 wheels, QuickJS,
the config files that trip people up) and the gotchas list. Short version on
Linux/WSL:

```sh
# engine/hjs_shim.c + quickjs -> libhjs.so
gcc -O2 -fPIC -shared -Iquickjs -o libhjs.so hjs_shim.c libquickjs.a -lm -ldl -lpthread
# engine/hjs.mojo -> hjs
MODULAR_MOJO_MAX_SYSTEM_LIBS=/usr/lib/x86_64-linux-gnu/libcurl.so.4 \
  mojo build -I <mojo-home>/lib/mojo hjs.mojo -o hjs
```

## Tests

The SDK suites are deterministic against a local test server, no live network
needed once the binary exists. The Splugin suites do reach tls.peet.ws (that is
the point, a server has to report what it saw) and the fingerprint benchmark
likewise.

```sh
cd sdk-python && HJS_BIN=/usr/local/bin/hjs python3 test_hjs_sdk.py   # 42 checks
cd sdk-python && python3 test_tplugin.py                              # 23 checks
cd sdk-python && HJS_SPLUGIN=/path/to/sbridge python3 test_splugin.py # 22 checks
cd sdk-go     && HJS_BIN=/usr/local/bin/hjs go test ./...             # core + tplugin
cd sdk-go     && HJS_SPLUGIN=/path/to/sbridge go test ./splugin       # Splugin client
python3 sdk-python/mcp_server.py --self-test                          # MCP, 21 tools
python3 sdk-python/bench_splugin.py                                   # rebuild docs/bench-results.json
```

## License

MIT. See LICENSE.
