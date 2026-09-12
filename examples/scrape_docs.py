"""Example: scrape a docs site, follow the nav links, collect structured data.

Run:  HJS_BIN=/usr/local/bin/hjs python3 scrape_docs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hjs import Browser

START = os.environ.get("START", "https://docs.python.org/3/")

# one chrome identity, polite (1.5s between requests), session cookies held
b = Browser(profile="chrome131", per_host_delay_ms=1500, retries=3)
try:
    page = b.goto(START)
    print(f"landed: {page.title}  ({page.status})")

    data = page.structured()
    print(f"og:title: {data['og'].get('og:title', '-')}")
    print(f"links found: {len(page.links)}")

    # follow the first few same-host links, report each title
    from urllib.parse import urlsplit
    base = urlsplit(START).netloc
    queue = [l for l in page.links if urlsplit(l).netloc == base][:5]
    for u in queue:
        p = b.goto(u)
        flag = f"  [{p.captcha}]" if p.captcha else ""
        print(f"{p.status} {p.title}{flag}  <-  {u}")
finally:
    b.close()
    b.delete_jar()
