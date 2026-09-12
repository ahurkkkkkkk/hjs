package hjs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func testServer(t *testing.T) *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/setcookie"):
			http.SetCookie(w, &http.Cookie{Name: "sid", Value: "test123", Path: "/"})
			w.Write([]byte("<html><title>Cookie Set</title><body>done</body></html>"))
		case strings.HasPrefix(r.URL.Path, "/echo"):
			hj, _ := json.Marshal(map[string]string{
				"useragent": r.UserAgent(),
				"referer":   r.Referer(),
				"cookie":    r.Header.Get("Cookie"),
			})
			w.Write([]byte("<html><title>Echo</title><body>" + string(hj) + "</body></html>"))
		case strings.HasPrefix(r.URL.Path, "/linkpage"):
			w.Write([]byte(`<html><title>Links</title><body>` +
				`<a href="/page1">one</a><a href="sub/page2">two</a>` +
				`<a href="https://ext.example/x">ext</a></body></html>`))
		case strings.HasPrefix(r.URL.Path, "/structured"):
			w.Write([]byte(`<html><head><title>Structured Page</title>` +
				`<meta name="description" content="A test page">` +
				`<meta property="og:title" content="OG Title">` +
				`<meta property="og:type" content="article">` +
				`<link rel="canonical" href="/structured">` +
				`<script type="application/ld+json">{"@type":"Article","headline":"Hello"}</script>` +
				`</head><body>article body</body></html>`))
		case strings.HasPrefix(r.URL.Path, "/cf"):
			w.WriteHeader(403)
			w.Write([]byte("<html><title>Just a moment</title><body>cf-challenge checking your browser</body></html>"))
		case strings.HasPrefix(r.URL.Path, "/robots.txt"):
			w.Write([]byte("User-agent: *\nDisallow: /private/\nAllow: /private/public/\nSitemap: " + hostOf(r) + "/sitemap.xml\n"))
		case strings.HasPrefix(r.URL.Path, "/sitemap.xml"):
			w.Write([]byte(`<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">` +
				`<url><loc>` + hostOf(r) + `/a</loc></url>` +
				`<url><loc>` + hostOf(r) + `/b</loc></url></urlset>`))
		default:
			w.Write([]byte("<html><title>Home</title><body>hello world</body></html>"))
		}
	})
	return httptest.NewServer(mux)
}

func hostOf(r *http.Request) string { return "http://" + r.Host }

func newTestBrowser(t *testing.T, opts Options) *Browser {
	if opts.Binary == "" {
		opts.Binary = os.Getenv("HJS_BIN")
	}
	b, err := NewBrowser(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	return b
}

func TestBasicGoto(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	p, err := b.Goto(srv.URL+"/", nil)
	if err != nil {
		t.Fatal(err)
	}
	if p.Status != 200 || p.Title != "Home" {
		t.Fatalf("status=%d title=%q", p.Status, p.Title)
	}
	if !strings.Contains(p.Text, "hello world") || !p.OK() {
		t.Fatalf("text=%q ok=%v", p.Text, p.OK())
	}
}

func TestProfileRotation(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{Profiles: []string{"chrome131", "firefox133"}})
	defer b.DeleteJar()
	for i, want := range []string{"Chrome/131", "Firefox/133", "Chrome/131"} {
		p, err := b.Goto(srv.URL+"/echo", nil)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(p.Text, want) {
			t.Fatalf("req %d expected %s in %s", i, want, p.Text)
		}
	}
}

func TestRefererChain(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	if _, err := b.Goto(srv.URL+"/", nil); err != nil {
		t.Fatal(err)
	}
	p, _ := b.Goto(srv.URL+"/echo", nil)
	if !strings.Contains(p.Text, `referer":"`+srv.URL+`/"`) {
		t.Fatalf("referer not chained: %s", p.Text)
	}
}

func TestCookies(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	jar := t.TempDir() + "/jar.txt"
	b := newTestBrowser(t, Options{CookieJar: jar})
	if _, err := b.Goto(srv.URL+"/setcookie", nil); err != nil {
		t.Fatal(err)
	}
	p, _ := b.Goto(srv.URL+"/echo", nil)
	if !strings.Contains(p.Text, "sid=test123") {
		t.Fatalf("cookie not replayed in-session: %s", p.Text)
	}
	b.Close()
	b2 := newTestBrowser(t, Options{CookieJar: jar})
	defer b2.Close()
	p2, _ := b2.Goto(srv.URL+"/echo", nil)
	if !strings.Contains(p2.Text, "sid=test123") {
		t.Fatalf("cookie not persisted across sessions: %s", p2.Text)
	}
	if ck, _ := b2.Cookies(); !strings.Contains(ck, "sid") {
		t.Fatal("Cookies() missing sid")
	}
}

func TestLinkResolution(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	p, _ := b.Goto(srv.URL+"/linkpage", nil)
	if len(p.Links) != 3 {
		t.Fatalf("links=%v", p.Links)
	}
	for _, want := range []string{srv.URL + "/page1", srv.URL + "/sub/page2", "https://ext.example/x"} {
		found := false
		for _, l := range p.Links {
			if l == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("missing %q in %v", want, p.Links)
		}
	}
	if got := p.FindLinks("/page", ""); len(got) != 2 {
		t.Fatalf("FindLinks=%v", got)
	}
}

func TestCaptcha(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	p, _ := b.Goto(srv.URL+"/cf", nil)
	if p.Captcha != "cloudflare" || !p.Blocked() {
		t.Fatalf("captcha=%q blocked=%v", p.Captcha, p.Blocked())
	}
}

func TestStructured(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	p, _ := b.Goto(srv.URL+"/structured", nil)
	s := p.Structured()
	if s.OG["og:title"] != "OG Title" {
		t.Fatalf("og=%v", s.OG)
	}
	if s.Description != "A test page" {
		t.Fatalf("desc=%q", s.Description)
	}
	if s.Canonical != "/structured" {
		t.Fatalf("canon=%q", s.Canonical)
	}
	if len(s.JSONLD) == 0 {
		t.Fatal("jsonld empty")
	}
	m := s.JSONLD[0].(map[string]any)
	if m["headline"] != "Hello" {
		t.Fatalf("jsonld=%v", s.JSONLD)
	}
}

func TestRobots(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	r, err := b.Robots(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Disallow) != 1 || r.Disallow[0] != "/private/" {
		t.Fatalf("disallow=%v", r.Disallow)
	}
	if len(r.Sitemaps) != 1 {
		t.Fatalf("sitemaps=%v", r.Sitemaps)
	}
	if r.Allowed(srv.URL + "/private/secret") {
		t.Fatal("private should be disallowed")
	}
	if !r.Allowed(srv.URL + "/about") {
		t.Fatal("about should be allowed")
	}
	if !r.Allowed(srv.URL + "/private/public/x") {
		t.Fatal("public-under-private should be allowed")
	}
}

func TestRespectRobotsBlocks(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{RespectRobots: true})
	defer b.DeleteJar()
	if _, err := b.Goto(srv.URL+"/private/secret", nil); err == nil {
		t.Fatal("expected robots-blocked error")
	}
}

func TestSitemap(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	locs, err := b.Sitemap(srv.URL + "/sitemap.xml")
	if err != nil {
		t.Fatal(err)
	}
	if len(locs) != 2 {
		t.Fatalf("locs=%v", locs)
	}
}

func TestScrape(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{PerHostDelayMS: 10})
	defer b.DeleteJar()
	urls := make([]string, 6)
	for i := range urls {
		urls[i] = srv.URL + "/"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pages, errs := b.ScrapeAll(ctx, urls, 3)
	for i, err := range errs {
		if err != nil {
			t.Fatalf("url %d: %v", i, err)
		}
	}
	for i, p := range pages {
		if p == nil || p.Status != 200 {
			t.Fatalf("page %d bad", i)
		}
	}
}

func TestHTMLAndWaitFor(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{})
	defer b.DeleteJar()
	raw, err := b.GotoHTML(srv.URL+"/", nil)
	if err != nil || !strings.Contains(raw, "hello world") {
		t.Fatalf("html mode broken: %q %v", raw, err)
	}
	if p, err := b.WaitFor(context.Background(), srv.URL+"/", "hello world", 200, 20*time.Second); err != nil {
		t.Fatal(err)
	} else if p.Status != 200 {
		t.Fatal("waitfor bad status")
	}
}

func TestCodegen(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{Profile: "chrome131"})
	defer b.DeleteJar()
	rec := b.Record("go")
	if _, err := b.Goto(srv.URL+"/", nil); err != nil {
		t.Fatal(err)
	}
	code := rec.Code()
	if !strings.Contains(code, "b.Goto(") {
		t.Fatalf("codegen=%s", code)
	}
	if !strings.Contains(code, "chrome131") {
		t.Fatal("codegen missing profile")
	}
	prec := b.Record("python")
	if _, err := b.Goto(srv.URL+"/", nil); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(prec.Code(), "b.goto(") {
		t.Fatal("python codegen missing goto")
	}
}

func TestMaxPages(t *testing.T) {
	srv := testServer(t)
	defer srv.Close()
	b := newTestBrowser(t, Options{MaxPages: 2})
	defer b.DeleteJar()
	for i := 0; i < 2; i++ {
		if _, err := b.Goto(srv.URL+"/", nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := b.Goto(srv.URL+"/", nil); err == nil {
		t.Fatal("expected max_pages error")
	}
}

func TestBinaryNotFound(t *testing.T) {
	t.Setenv("HJS_BIN", "")
	t.Setenv("PATH", "/nonexistent")
	if _, err := NewBrowser(Options{Binary: "/no/such/hjs"}); err == nil {
		t.Fatal("expected error for missing binary")
	}
}
