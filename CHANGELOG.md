# Changelog

## 0.4.0

* hjs-tplugin: a pure-stdlib rendering plugin for both SDKs. Hand-written PNG
  encoder, PDF writer, and 5x7 bitmap font (no Pillow, reportlab,
  wkhtmltopdf, or Chromium). Adds screenshot (content snapshot), pdf,
  print_/PrintText, reader, and a Viewer with scroll + tap link hit-testing.
* Go SDK tplugin package; `*hjs.Page` satisfies `tplugin.Page` directly
  (Link is a shared alias, no conversion layer).
* Forms and login: `submit()` in both SDKs, urlencoded or JSON, session
  cookies and referer carried. New engine flags `--method` and `--body`;
  `CURLOPT_COPYPOSTFIELDS` fixes request bodies being read after free.
* MCP server now exposes 17 tools (adds hjs_submit, hjs_screenshot, hjs_pdf,
  hjs_print, hjs_reader, hjs_scroll, hjs_tap). Self-test verifies the PNG and
  PDF actually land on disk.
* Benchmarks section + scaling chart: 19 MB / ~50 fetches/s at 1 worker up to
  118 MB / ~205 fetches/s at 8 workers, measured on a local test server.

## 0.3.0

* Python and Go SDKs with a Playwright-style API: `Browser`, `Page`,
  session state, referer chaining, parallel `scrape()`.
* Fingerprint rotation: pass `profiles=[...]` and the browser identity
  cycles on every request.
* Structured extraction in both SDKs: Open Graph, Twitter cards, meta,
  JSON-LD, canonical, links with anchor text (`parse_html` / `ParseHTML`).
* robots.txt parsing with allow/disallow precedence and a
  `respect_robots` session mode that refuses disallowed URLs.
* Sitemap reader (regular and sitemap-index files).
* Codegen: `Record("python"|"go")` captures a session and emits a
  runnable script, the Playwright-inspector trick minus the browser.
* MCP server (`sdk-python/mcp_server.py`) exposing 10 tools over stdio:
  goto, extract, html, links, click, wait_for, scrape, sitemap, robots,
  session.
* `wait_for` polling for JS-rendered pages.
* hjs binary: captcha/bot-wall detection now reported in JSON; referer,
  cookies, proxy, TLS floor/ciphers, delay, retries with linear backoff.
* Five fingerprint profiles shipped in `browser_profiles.txt`.
* Cookie jar session persistence verified end to end.

## 0.2.0

* First stealth build: HTTP/2, TLS 1.2/1.3 floor, proxy support, politeness
  delay, retry/backoff, captcha detection, Netscape cookie read/write,
  browser profiles with UA + client hints + Sec-Fetch + cipher sets.
* JS engine stabilized (QuickJS promise capability fix; session loop
  handles fetch()/XHR through the host).

## 0.1.0

* hbrowser: 120 KB headless fetch + text/JSON extraction (no JS).
* hjs: QuickJS added via a 470-line C shim, still ~21 MB RSS.
