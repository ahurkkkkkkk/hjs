# hbrowser - tiny headless browser in Mojo

A single-purpose, no-JS headless browser: fetch a URL over HTTP/HTTPS via
libcurl, strip HTML down to readable text, pull out the title, meta
description, and links, and print a JSON document. No V8, no DOM tree, no
rendering. Peak RSS is ~21-24 MB and the binary is ~120 KB.

Built and tested with Mojo 1.0.0 on Linux x86-64 (WSL2 Ubuntu 24.04).

## Why

Go/Python HTTP clients stop at HTML strings; Chrome/Playwright eat hundreds
of MB and spin up V8 for the simple case of "give me the text and links of
this page". hbrowser fills that middle ground: subprocess it from Go or
Python, get structured JSON back, throw it away. Nothing persists.

## Build

Mojo 1.0.0 is required (pixi/conda or the PyPI wheels both work):

```bash
MODULAR_MOJO_MAX_SYSTEM_LIBS=/lib/x86_64-linux-gnu/libcurl.so.4 \
  mojo build -I "$MOJO_HOME/lib/mojo" hbrowser.mojo -o hbrowser
```

The Mojo runtime libs (`libKGENCompilerRTShared.so` and friends) must be
findable at run time:

```bash
export LD_LIBRARY_PATH=/path/to/mojo-home/lib
./hbrowser https://example.com
```

If the compiler cannot find libcurl at link time, set
`MODULAR_MOJO_MAX_SYSTEM_LIBS` to the full libcurl path as shown above.

## Usage

```bash
hbrowser <url> [options]

Options:
  --mode=text|html|json   Output format (default: json)
  --timeout=N             Total transfer timeout seconds (default: 15)
  --max=N                 Max body bytes (default: 2097152)
  --header=K: V           Extra request header (repeatable)
  --ua=string             User-Agent override
  --links                 Include extracted links (json mode)
  --no-meta               Omit title/description (json mode)
  --quiet                 Suppress the trailing newline
  --help                  Show this help
```

Exit codes: 0 ok, 1 usage error, 2 network error, 3 HTTP error status.

### JSON output shape

```json
{
  "url": "https://example.com",
  "status": 200,
  "bytes": 559,
  "elapsed_ms": 900,
  "title": "Example Domain",
  "description": "",
  "links": ["https://www.iana.org/domains/example"],
  "text": "Example Domain This domain is for use in documentation..."
}
```

`links` only appears with `--links`. With `--no-meta` the title and
description keys are omitted.

## Wrappers

### Python (`hbrowser.py`)

```python
from hbrowser import fetch

res = fetch("https://example.com", links=True, timeout=10)
print(res["title"], len(res["links"]), res["elapsed_ms"])
```

Or from the shell: `python3 hbrowser.py https://example.com`.

The binary is located via `$HBROWSER`, then `which hbrowser`.

### Go (`hfetch.go`)

```go
res, err := hfetch.Fetch("https://example.com", &hfetch.Opts{Links: true})
```

Package main doubles as a CLI: `go build` then `./hfetch <url>`.

Both wrappers accept:

- `HBROWSER` - full path to the hbrowser binary
- `HBROWSER_LD` (Go only) - value for `LD_LIBRARY_PATH` when the Mojo
  runtime libs are not on the default loader path

## Environment

| Variable | Purpose |
|----------|---------|
| `HBROWSER` | path to the hbrowser binary (wrappers) |
| `HBROWSER_LD` | LD_LIBRARY_PATH for the Mojo runtime (Go wrapper) |
| `LD_LIBRARY_PATH` | must include the Mojo `lib/` dir when running directly |
| `MODULAR_MOJO_MAX_SYSTEM_LIBS` | libcurl path at link time |

## Limitations

- No JavaScript execution, no cookies, no caching, no HTTP/2 push.
- Text extraction is byte-oriented heuristics (script/style dropping,
  whitespace collapsing, entity decoding for the common five entities).
  Pages whose readable content is injected by JS will come back thin.
- `links` are raw `href` values, not resolved against the page URL.
- Response compression is accepted (curl decodes gzip/deflate/br) but the
  `bytes` field counts the decoded body.

## Files

- `hbrowser.mojo` - the browser (~550 lines of Mojo)
- `hbrowser.py` - Python wrapper
- `hfetch.go` - Go wrapper
- `run_test.sh` - smoke test used during development
