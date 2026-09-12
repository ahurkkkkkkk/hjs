package splugin

import (
	"os"
	"strings"
	"testing"
)

func requireBridge(t *testing.T) {
	t.Helper()
	if os.Getenv("HJS_SPLUGIN") == "" {
		for _, c := range []string{"/root/hjs/sbridge", "sbridge"} {
			if _, err := os.Stat(c); err == nil {
				os.Setenv("HJS_SPLUGIN", c)
				break
			}
		}
	}
	if os.Getenv("HJS_SPLUGIN") == "" {
		t.Skip("sbridge not available (set HJS_SPLUGIN)")
	}
}

func TestFetchAndJA4(t *testing.T) {
	requireBridge(t)
	s, err := New(Options{Profile: "chrome_131"})
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	r, err := s.Fetch("https://example.com", FetchOpts{FP: true})
	if err != nil {
		t.Fatal(err)
	}
	if r.Status != 200 {
		t.Fatalf("status %d", r.Status)
	}
	if r.GetTitle() != "Example Domain" {
		t.Fatalf("title %q", r.GetTitle())
	}
	if !strings.Contains(r.GetText(), "Example") {
		t.Fatalf("text %q", r.GetText())
	}
	if !strings.HasPrefix(r.JA4, "t13d1516h2") {
		t.Fatalf("chrome_131 ja4 = %q", r.JA4)
	}
	if len(r.JA3Hash) != 32 {
		t.Fatalf("ja3_hash %q", r.JA3Hash)
	}
	if !strings.Contains(r.Peetprint, "GREASE") {
		t.Fatalf("peetprint %q", r.Peetprint)
	}
	if r.HTTPVer != "h2" {
		t.Logf("http_version %q (peet reported)", r.HTTPVer)
	}
}

func TestJA4DiffersAcrossProfiles(t *testing.T) {
	requireBridge(t)
	sc, _ := New(Options{Profile: "chrome_131"})
	defer sc.Close()
	sf, _ := New(Options{Profile: "firefox_133"})
	defer sf.Close()

	a, err := sc.Fetch("https://example.com", FetchOpts{FP: true})
	if err != nil {
		t.Fatal(err)
	}
	b, err := sf.Fetch("https://example.com", FetchOpts{FP: true})
	if err != nil {
		t.Fatal(err)
	}
	if a.JA4 == "" || a.JA4 == b.JA4 {
		t.Fatalf("profiles produced ja4 %q vs %q", a.JA4, b.JA4)
	}
}

func TestRotation(t *testing.T) {
	requireBridge(t)
	s, _ := New(Options{Profiles: []string{"chrome_131", "firefox_133"}})
	defer s.Close()
	a, _ := s.Fetch("https://example.com", FetchOpts{FP: true})
	b, _ := s.Fetch("https://example.com", FetchOpts{FP: true})
	if a.JA4 == b.JA4 {
		t.Fatalf("rotation did not change fingerprint: %q", a.JA4)
	}
}

func TestCookiesAndPost(t *testing.T) {
	requireBridge(t)
	s, _ := New(Options{Profile: "chrome_131"})
	defer s.Close()
	if _, err := s.Fetch("https://httpbin.org/cookies/set/gs/1", FetchOpts{}); err != nil {
		t.Fatal(err)
	}
	r, err := s.Fetch("https://httpbin.org/cookies", FetchOpts{})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(r.GetText(), `"gs": "1"`) && !strings.Contains(r.GetText(), "gs") {
		t.Fatalf("cookie not carried: %q", r.GetText())
	}
	if len(s.Cookies()) == 0 {
		t.Fatal("cookie jar empty")
	}
	p, err := s.Post("https://httpbin.org/post", map[string]string{"user": "me"}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(p.GetText(), "me") {
		t.Fatalf("post echo %q", p.GetText())
	}
}

func TestProfilesList(t *testing.T) {
	requireBridge(t)
	s, _ := New(Options{})
	defer s.Close()
	ps, err := s.Profiles()
	if err != nil {
		t.Fatal(err)
	}
	if len(ps) < 40 {
		t.Fatalf("only %d profiles", len(ps))
	}
}

func TestCaptchaDetection(t *testing.T) {
	// offline check of the marker scanner
	r := &Result{raw: []byte("<html><body>cf-challenge checking your browser</body></html>")}
	r.text = cleanText(r.raw)
	low := strings.ToLower(string(r.raw))
	found := ""
	for _, m := range captchaMarkers {
		if strings.Contains(low, m.key) {
			found = m.name
			break
		}
	}
	if found != "cloudflare" {
		t.Fatalf("captcha=%q", found)
	}
}
