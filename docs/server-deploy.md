# Running hjs on a server

The binary is a plain Linux x86-64 ELF, so it runs on any VPS (this project's
box is ahura.site, Ubuntu). The one dependency is libcurl, present on every
normal install.

## Copy the three files

```
hjs                 binary
libhjs.so           QuickJS engine + shim
browser_profiles.txt  edit as you like
```

into a directory, say `/opt/hjs`, then:

```sh
chmod +x /opt/hjs/hjs
ln -sf /opt/hjs/hjs /usr/local/bin/hjs
```

If you built from source, `hjs` also needs the Mojo runtime libs at run time.
Put them next to it or export the path:

```sh
export LD_LIBRARY_PATH=/opt/hjs:<mojo-toolchain>/lib
```

The prebuilt shipped binary has `libhjs.so` found via `HJS_ENGINE` and needs
only the Mojo runtime dir.

## Verify

```sh
hjs https://example.com --profile=chrome131 --no-js
```

You should see a JSON document with `"status": 200`. To watch memory, which
is the whole point:

```sh
/usr/bin/time -v hjs https://example.com --timeout=15 2>&1 | grep "Maximum resident"
```

Expect around 21 MB. Compare against one Playwright or Selenium tab, which is
several hundred.

## As a scraper backend

From Go or Python, set `HJS_BIN=/usr/local/bin/hjs` (and `HJS_LD` to the Mojo
lib dir for the Go wrapper when needed). Sessions, cookies and referer
chaining live in the SDK, so each worker process keeps its own jar.

Run a fleet of workers with per-host politeness to stay well-behaved and
avoid tripping rate limiters:

```python
b = Browser(profile="chrome131", per_host_delay_ms=1500, retries=3)
pages = b.scrape(urls, workers=8)
```

Do not store credentials, proxy passwords, or API keys in code or in this
repo. Use environment variables or a secrets manager on the server.
