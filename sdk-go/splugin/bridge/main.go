// sbridge is the Splugin TLS-impersonation bridge for hjs.
//
// It speaks newline-delimited JSON over stdin/stdout so the Python and Go
// SDKs can drive a Chrome/Firefox/Safari-grade TLS fingerprint (utls based:
// real Chrome ClientHello, ALPN, GREASE, extensions order, session ticket
// behaviour) without pulling a browser into the process.
//
// Ops:
//   {"op":"ping"}                          -> {"ok":true,"version":...}
//   {"op":"profiles"}                      -> {"profiles":[...]}
//   {"op":"fetch","url":...,"profile":...,  "method":...,"body":...,
//    "headers":{...},"timeout_ms":...,     "insecure":bool,"http2":bool}
//      -> {"status":...,"body_b64":...,"headers":{...},"final_url":...,
//          "elapsed_ms":...}
//      when "fp":true the same request additionally hits tls.peet.ws and the
//      result carries "ja3"/"ja4" strings from the real handshake.
//   {"op":"cookies"}                       -> {"cookies":[{name,value,domain,path},...]}
//   {"op":"shutdown"}
//
// One process keeps one cookie jar, so a login followed by fetches just works
// (same as the hjs cookie-jar session).
package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

const version = "0.5.0"

// uaFor mirrors the hjs browser profiles onto the TLS profile so the HTTP
// layer and the TLS layer agree on one identity.
func uaFor(profile string, methodless string) string {
	switch profile {
	case "firefox_133", "firefox_135", "firefox_132":
		return "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
	case "safari_16_0":
		return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"
	case "edge_131", "chrome_131", "chrome_133", "chrome_144", "chrome_150":
		return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
	}
	return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}

func buildClient(profile string, insecure, http2 bool, timeout time.Duration) (tls_client.HttpClient, error) {
	p, ok := profiles.MappedTLSClients[strings.ToLower(profile)]
	if !ok {
		p = profiles.Chrome_131
	}
	opts := []tls_client.HttpClientOption{
		tls_client.WithClientProfile(p),
		tls_client.WithTimeoutSeconds(int(timeout.Seconds()) + 1),
		tls_client.WithNotFollowRedirects(),
	}
	if insecure {
		opts = append(opts, tls_client.WithInsecureSkipVerify())
	}
	if !http2 {
		opts = append(opts, tls_client.WithForceHttp1())
	}
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), opts...)
}

type fetchReq struct {
	Op        string            `json:"op"`
	URL       string            `json:"url"`
	Profile   string            `json:"profile"`
	Method    string            `json:"method"`
	Body      string            `json:"body"`
	BodyB64   string            `json:"body_b64"`
	Headers   map[string]string `json:"headers"`
	TimeoutMS int               `json:"timeout_ms"`
	Insecure  bool              `json:"insecure"`
	HTTP2     *bool             `json:"http2"`
	FP        bool              `json:"fp"`
}

type cookie struct {
	Name   string `json:"name"`
	Value  string `json:"value"`
	Domain string `json:"domain"`
	Path   string `json:"path"`
}

func doFetch(c tls_client.HttpClient, jar tls_client.CookieJar, r fetchReq) map[string]any {
	method := r.Method
	if method == "" {
		method = "GET"
	}
	var body io.Reader
	if r.BodyB64 != "" {
		if b, err := base64.StdEncoding.DecodeString(r.BodyB64); err == nil {
			body = strings.NewReader(string(b))
		}
	} else if r.Body != "" {
		body = strings.NewReader(r.Body)
	}
	req, err := fhttp.NewRequestWithContext(context.Background(), method, r.URL, body)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	req.Header = fhttp.Header{}
	if ua := r.Headers["User-Agent"]; ua == "" {
		req.Header.Set("User-Agent", uaFor(r.Profile, method))
	}
	for k, v := range r.Headers {
		req.Header.Set(k, v)
	}
	t0 := time.Now()
	resp, err := c.Do(req)
	out := map[string]any{}
	if err != nil {
		out["error"] = err.Error()
		return out
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	out["status"] = resp.StatusCode
	out["elapsed_ms"] = time.Since(t0).Milliseconds()
	out["final_url"] = resp.Request.URL.String()
	hdr := map[string]string{}
	for k, v := range resp.Header {
		hdr[k] = strings.Join(v, ", ")
	}
	out["headers"] = hdr
	out["body_b64"] = base64.StdEncoding.EncodeToString(data)
	if r.FP {
		// Fetch the fingerprints the *server* saw, using the same client so
		// it is the same handshake shape.
		fp, err := peet(c, r.Profile)
		if err != nil {
			out["fp_error"] = err.Error()
		} else {
			for k, v := range fp {
				out[k] = v
			}
		}
	}
	// dump jar on fetch responses for callers that want persistence
	if jar != nil {
		if cookies, err := dumpCookies(jar); err == nil {
			out["cookies"] = cookies
		}
	}
	return out
}

func dumpCookies(jar tls_client.CookieJar) ([]cookie, error) {
	c := cookieDumper{jar: jar}
	return c.dump()
}

// cookieDumper reflects on the jar to extract cookies without depending on
// internal cookiejar types (the jar exposes Cookies(u) via http.CookieJar).
type cookieDumper struct{ jar tls_client.CookieJar }

func (d cookieDumper) dump() ([]cookie, error) {
	// http.CookieJar interface only allows Cookies(url); tls-client jar also
	// has ExportAsJson. Try that first via a tiny interface probe.
	if e, ok := d.jar.(interface{ ExportAsJson() string }); ok {
		var raw []struct {
			Name, Value, Domain, Path string
		}
		s := e.ExportAsJson()
		if err := json.Unmarshal([]byte(s), &raw); err == nil {
			out := make([]cookie, 0, len(raw))
			for _, c := range raw {
				out = append(out, cookie{c.Name, c.Value, c.Domain, c.Path})
			}
			return out, nil
		}
	}
	// fallback: empty list (jar still works for the session itself)
	return []cookie{}, nil
}

func main() {
	jar := tls_client.NewCookieJar()
	var client tls_client.HttpClient
	var curProfile = "chrome_131"
	var err error
	client, err = buildClient("chrome_131", false, true, 15*time.Second)
	if err != nil {
		fmt.Fprintln(os.Stderr, "init:", err)
		os.Exit(1)
	}
	_ = jar
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	out := json.NewEncoder(os.Stdout)

	serve := len(os.Args) < 2 || os.Args[1] != "fetch"

	if !serve {
		// one-shot: flags: fetch <profile> <url> [headers...]
		profile := "chrome_131"
		url := ""
		headers := map[string]string{}
		if len(os.Args) > 2 {
			profile = os.Args[2]
		}
		if len(os.Args) > 3 {
			url = os.Args[3]
		}
		for _, h := range os.Args[4:] {
			k, v, _ := strings.Cut(h, "=")
			headers[k] = v
		}
		c, e := buildClient(profile, false, true, 15*time.Second)
		if e != nil {
			fmt.Fprintln(os.Stderr, e)
			os.Exit(1)
		}
		res := doFetch(c, nil, fetchReq{URL: url, Profile: profile, Headers: headers, FP: true})
		_ = out.Encode(res)
		return
	}

	// serve loop
	var keep *tls_client.CookieJar
	_ = keep
	for in.Scan() {
		line := strings.TrimSpace(in.Text())
		if line == "" {
			continue
		}
		var req fetchReq
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			_ = out.Encode(map[string]any{"error": "bad json: " + err.Error()})
			continue
		}
		switch req.Op {
		case "ping":
			_ = out.Encode(map[string]any{"ok": true, "version": version})
		case "shutdown":
			_ = out.Encode(map[string]any{"ok": true})
			return
		case "profiles":
			var names []string
			for k := range profiles.MappedTLSClients {
				names = append(names, k)
			}
			sort.Strings(names)
			_ = out.Encode(map[string]any{"profiles": names})
		case "fetch":
			want := strings.ToLower(req.Profile)
			if want == "" {
				want = "chrome_131"
			}
			http2 := req.HTTP2 == nil || *req.HTTP2
			if want != curProfile || http2 != true {
				client, err = buildClient(want, req.Insecure, http2, 15*time.Second)
				if err != nil {
					_ = out.Encode(map[string]any{"error": err.Error()})
					continue
				}
				curProfile = want
			}
			res := doFetch(client, jar, req)
			_ = out.Encode(res)
		case "cookies":
			cs, err := dumpCookies(jar)
			if err != nil {
				_ = out.Encode(map[string]any{"error": err.Error()})
				continue
			}
			_ = out.Encode(map[string]any{"cookies": cs})
		default:
			_ = out.Encode(map[string]any{"error": "unknown op: " + req.Op})
		}
	}
}
