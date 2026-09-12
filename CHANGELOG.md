# Changelog

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
