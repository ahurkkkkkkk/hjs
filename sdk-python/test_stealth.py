import subprocess, os, json, re

env = dict(os.environ, LD_LIBRARY_PATH="/root/hjs:/root/hbrowser/mojo-home/lib")

def run(args):
    r = subprocess.run(["/root/hjs/hjs"] + args, capture_output=True, text=True, env=env, timeout=120)
    return r

print("=== 1. profile chrome131: UA + client hints echoed by httpbin")
r = run(["https://httpbin.org/headers", "--profile=chrome131", "--timeout=20", "--mode=text"])
if r.returncode == 0:
    ok_ua = "Chrome/131" in r.stdout
    ok_ch = "sec-ch-ua" in r.stdout.lower()
    ok_sf = "Sec-Fetch-Dest" in r.stdout
    print("UA chrome131 sent:", ok_ua, "| sec-ch-ua:", ok_ch, "| Sec-Fetch:", ok_sf)
else:
    print("rc", r.returncode, r.stdout[:100], r.stderr[:150])

print("=== 2. cookie jar persistence")
jar = "/tmp/hjs_cookies.txt"
if os.path.exists(jar):
    os.remove(jar)
# first request sets a cookie
r1 = run(["https://httpbin.org/cookies/set/hjs_token/abc123", "--cookie-jar=" + jar,
          "--timeout=20", "--no-js", "-L"])
r2 = run(["https://httpbin.org/cookies", "--cookies=" + jar, "--timeout=20", "--mode=text"])
print("cookie persisted + replayed:", "abc123" in r2.stdout, "| rc:", r2.returncode)

print("=== 3. captcha detection")
r3 = run(["https://httpbin.org/status/403", "--timeout=15"])
print("rc403:", r3.returncode)

print("=== 4. retry on 500")
r4 = run(["https://httpbin.org/status/500", "--retries=2", "--backoff-ms=100", "--timeout=15"])
print("500 after retries rc:", r4.returncode)

print("=== 5. proxy flag parse (bad proxy fails gracefully)")
r5 = run(["https://example.com", "--proxy=http://127.0.0.1:9", "--timeout=5"])
print("rc:", r5.returncode, "(nonzero expected, no crash)")
