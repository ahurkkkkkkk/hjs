// Package splugin is the Go client for the Splugin TLS-impersonation bridge.
// It spawns the sbridge subprocess (built from ./bridge) and drives it over
// newline-delimited JSON, so one process keeps a cookie jar and rotates
// browser TLS identities (real Chrome/Firefox/Safari JA3/JA4 via utls) while
// you fetch. Results satisfy the tplugin.Page interface, so screenshot, PDF,
// reader and touch/scroll all work on them.
//
// Quick start:
//
//	s, _ := splugin.New(splugin.Options{Profile: "chrome_131"})
//	defer s.Close()
//	r, _ := s.Fetch("https://example.com", splugin.FetchOpts{FP: true})
//	fmt.Println(r.Status, r.Title, r.JA4)  // t13d1516h2... real Chrome JA4
//
// The bridge binary is found via Options.Bridge, $HJS_SPLUGIN, or PATH.
package splugin

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/ahurkkkkkkk/hjs/sdk-go/tplugin"
)

// Options configures a Session.
type Options struct {
	Profile  string            // chrome_131, firefox_133, safari_16_0, ...
	Profiles []string          // rotate per request when >1
	Timeout  time.Duration     // per request (default 15s)
	Insecure bool              // skip TLS verification (default false)
	HTTP2    *bool             // use h2 (default true)
	Headers  map[string]string // extra request headers
	Bridge   string            // path to sbridge (else $HJS_SPLUGIN, else PATH)
	UA       string            // override User-Agent header
}

// FetchOpts per-request options.
type FetchOpts struct {
	Method  string
	Body    string
	Headers map[string]string
	Profile string // override the session profile for this request
	FP      bool   // also query tls.peet.ws and fill JA3/JA4/Peetprint
	Timeout time.Duration
}

// Result is one fetch. It implements tplugin.Page.
type Result struct {
	URL        string
	Status     int
	ElapsedMS  int64
	HTTPVer    string
	JA3        string
	JA3Hash    string
	JA4        string
	JA4R       string
	Peetprint  string
	ALPN       string
	SeenUA     string
	Captcha    string
	FinalURL   string
	Headers    map[string]string
	cookies    []Cookie
	raw        []byte
	text       string
	title      string
	desc       string
	links      []string
	anchors    []linkRec
}

// Cookie is one jar entry.
type Cookie struct {
	Name   string `json:"name"`
	Value  string `json:"value"`
	Domain string `json:"domain"`
	Path   string `json:"path"`
}

type linkRec struct {
	href string
	text string
}

// Session drives one bridge subprocess with a persistent cookie jar.
type Session struct {
	opt     Options
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	lines   *bufio.Scanner
	mu      sync.Mutex
	rot     int
	enc     *json.Encoder
	dec     *json.Decoder
	lastURL string
}

func findBridge(opts string) (string, error) {
	for _, c := range []string{opts, os.Getenv("HJS_SPLUGIN"),
		filepath.Join(filepath.Dir(os.Args[0]), "sbridge"), "/usr/local/bin/sbridge"} {
		if c != "" {
			if st, err := os.Stat(c); err == nil && !st.IsDir() {
				return c, nil
			}
		}
	}
	if p, err := exec.LookPath("sbridge"); err == nil {
		return p, nil
	}
	return "", fmt.Errorf("splugin: sbridge not found (set HJS_SPLUGIN or Options.Bridge)")
}

// New starts a session (spawns the bridge).
func New(opts Options) (*Session, error) {
	if opts.Profile == "" && len(opts.Profiles) > 0 {
		opts.Profile = opts.Profiles[0]
	}
	if opts.Profile == "" {
		opts.Profile = "chrome_131"
	}
	if opts.Timeout == 0 {
		opts.Timeout = 15 * time.Second
	}
	bin, err := findBridge(opts.Bridge)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(bin)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &Session{
		opt:   opts,
		cmd:   cmd,
		stdin: stdin,
		lines: bufio.NewScanner(stdout),
		enc:   json.NewEncoder(stdin),
		dec:   nil,
	}, nil
}

// Close shuts the bridge down.
func (s *Session) Close() {
	if s == nil || s.cmd == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.enc.Encode(map[string]any{"op": "shutdown"})
	s.stdin.Close()
	_ = s.cmd.Wait()
	s.cmd = nil
}

func (s *Session) call(payload map[string]any) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cmd == nil {
		return nil, fmt.Errorf("splugin: session closed")
	}
	if err := s.enc.Encode(payload); err != nil {
		return nil, fmt.Errorf("splugin: write: %w", err)
	}
	if !s.lines.Scan() {
		if err := s.lines.Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("splugin: bridge closed")
	}
	var resp map[string]any
	if err := json.Unmarshal(s.lines.Bytes(), &resp); err != nil {
		return nil, fmt.Errorf("splugin: bad json: %w", err)
	}
	if e, ok := resp["error"].(string); ok && e != "" {
		return nil, fmt.Errorf("splugin: %s", e)
	}
	return resp, nil
}

func (s *Session) nextProfile(override string) string {
	if override != "" {
		return override
	}
	if len(s.opt.Profiles) > 1 {
		p := s.opt.Profiles[s.rot%len(s.opt.Profiles)]
		s.rot++
		return p
	}
	if len(s.opt.Profiles) == 1 {
		return s.opt.Profiles[0]
	}
	return s.opt.Profile
}

// Fetch performs one request with the session's browser TLS identity.
func (s *Session) Fetch(url string, fo FetchOpts) (*Result, error) {
	if fo.Method == "" {
		fo.Method = "GET"
	}
	to := s.opt.Timeout
	if fo.Timeout > 0 {
		to = fo.Timeout
	}
	headers := map[string]string{}
	for k, v := range s.opt.Headers {
		headers[k] = v
	}
	for k, v := range fo.Headers {
		headers[k] = v
	}
	if s.opt.UA != "" {
		headers["User-Agent"] = s.opt.UA
	}
	http2 := true
	if s.opt.HTTP2 != nil {
		http2 = *s.opt.HTTP2
	}
	resp, err := s.call(map[string]any{
		"op": "fetch", "url": url, "profile": s.nextProfile(fo.Profile),
		"method": fo.Method, "body": fo.Body, "headers": headers,
		"timeout_ms": to.Milliseconds(), "insecure": s.opt.Insecure,
		"http2": http2, "fp": fo.FP,
	})
	if err != nil {
		return nil, err
	}
	r := &Result{}
	r.URL = url
	if fu, ok := resp["final_url"].(string); ok {
		r.FinalURL = fu
	}
	if st, ok := resp["status"].(float64); ok {
		r.Status = int(st)
	}
	if el, ok := resp["elapsed_ms"].(float64); ok {
		r.ElapsedMS = int64(el)
	}
	r.HTTPVer = strOf(resp["http_version"])
	r.JA3 = strOf(resp["ja3"])
	r.JA3Hash = strOf(resp["ja3_hash"])
	r.JA4 = strOf(resp["ja4"])
	r.JA4R = strOf(resp["ja4_r"])
	r.Peetprint = strOf(resp["peetprint"])
	r.ALPN = strOf(resp["alpn"])
	r.SeenUA = strOf(resp["server_seen_ua"])
	if hd, ok := resp["headers"].(map[string]any); ok {
		r.Headers = map[string]string{}
		for k, v := range hd {
			r.Headers[k] = strOf(v)
		}
	}
	if cs, ok := resp["cookies"].([]any); ok {
		for _, c := range cs {
			if m, ok := c.(map[string]any); ok {
				r.cookies = append(r.cookies, Cookie{
					Name: strOf(m["name"]), Value: strOf(m["value"]),
					Domain: strOf(m["domain"]), Path: strOf(m["path"]),
				})
			}
		}
	}
	if b64, ok := resp["body_b64"].(string); ok {
		if data, err := base64.StdEncoding.DecodeString(b64); err == nil {
			r.raw = data
		}
	}
	r.text = cleanText(r.raw)
	r.title = firstMatch(titleRe, r.raw)
	r.desc = firstMatch(descRe, r.raw)
	low := strings.ToLower(string(r.raw))
	for _, m := range captchaMarkers {
		if strings.Contains(low, m.key) {
			r.Captcha = m.name
			break
		}
	}
	for _, a := range anchorRe.FindAllSubmatch(r.raw, -1) {
		r.anchors = append(r.anchors, linkRec{
			href: string(a[1]), text: cleanText(a[2]),
		})
	}
	s.lastURL = url
	return r, nil
}

// Post sends a urlencoded form or JSON body.
func (s *Session) Post(url string, data map[string]string, jsonBody any, hdrs map[string]string) (*Result, error) {
	body := ""
	if hdrs == nil {
		hdrs = map[string]string{}
	}
	if jsonBody != nil {
		b, _ := json.Marshal(jsonBody)
		body = string(b)
		hdrs["Content-Type"] = "application/json"
	} else if len(data) > 0 {
		vals := make([]string, 0, len(data))
		for k, v := range data {
			vals = append(vals, k+"="+urlQueryEscape(v))
		}
		body = strings.Join(vals, "&")
		hdrs["Content-Type"] = "application/x-www-form-urlencoded"
	}
	return s.Fetch(url, FetchOpts{Method: "POST", Body: body, Headers: hdrs})
}

// Profiles returns every bridge profile name.
func (s *Session) Profiles() ([]string, error) {
	resp, err := s.call(map[string]any{"op": "profiles"})
	if err != nil {
		return nil, err
	}
	names := []string{}
	if arr, ok := resp["profiles"].([]any); ok {
		for _, a := range arr {
			names = append(names, strOf(a))
		}
	}
	return names, nil
}

// Cookies returns the live jar snapshot from the bridge.
func (s *Session) Cookies() []Cookie {
	resp, err := s.call(map[string]any{"op": "cookies"})
	if err != nil {
		return nil
	}
	out := []Cookie{}
	if arr, ok := resp["cookies"].([]any); ok {
		for _, a := range arr {
			if m, ok := a.(map[string]any); ok {
				out = append(out, Cookie{strOf(m["name"]), strOf(m["value"]),
					strOf(m["domain"]), strOf(m["path"])})
			}
		}
	}
	return out
}

func (s *Session) LastURL() string { return s.lastURL }

// ---- tplugin.Page interface ----

func (r *Result) GetURL() string        { return r.FinalURL }
func (r *Result) GetTitle() string      { return r.title }
func (r *Result) GetText() string       { return r.text }
func (r *Result) GetLinks() []string {
	if r.links == nil {
		for _, a := range r.anchors {
			if j := urlJoin(r.FinalURL, a.href); !contains(r.links, j) {
				r.links = append(r.links, j)
			}
		}
	}
	return r.links
}

// GetAnchors returns resolved anchors for tplugin viewer labels.
func (r *Result) GetAnchors() []tplugin.Link {
	out := make([]tplugin.Link, 0, len(r.anchors))
	for _, a := range r.anchors {
		out = append(out, tplugin.Link{Href: urlJoin(r.FinalURL, a.href), Text: a.text})
	}
	return out
}

// OK / Blocked mirror hjs.Page semantics.
func (r *Result) OK() bool      { return r.Status >= 200 && r.Status < 300 && r.Captcha == "" }
func (r *Result) Blocked() bool { return r.Captcha != "" }

// Body returns the raw response bytes.
func (r *Result) Body() []byte { return r.raw }

// Compile-time proof that a bridge result works with the tplugin API.
var _ tplugin.Page = (*Result)(nil)

// ---- text helpers (mirror the Python plugin) ----

var (
	titleRe   = regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title>`)
	descRe    = regexp.MustCompile(`(?is)<meta[^>]+name=["']description["'][^>]+content=["'](.*?)["']`)
	tagRe     = regexp.MustCompile(`<[^>]*>`)
	scriptRe  = regexp.MustCompile(`(?is)<script\b.*?</script>`)
	styleRe   = regexp.MustCompile(`(?is)<style\b.*?</style>`)
	wsRe      = regexp.MustCompile(`\s+`)
	anchorRe  = regexp.MustCompile(`(?is)<a\b[^>]*href=["']([^"']*)["'][^>]*>(.*?)</a>`)
)

var captchaMarkers = []struct{ name, key string }{
	{"cloudflare", "cf-challenge"}, {"cloudflare", "checking your browser"},
	{"perimeterx", "captcha-delivery.com"}, {"perimeterx", "px-captcha"},
	{"recaptcha", "recaptcha"}, {"hcaptcha", "hcaptcha.com"},
	{"datadome", "datadome"}, {"incapsula", "incapsula"},
}

func cleanText(raw []byte) string {
	s := string(raw)
	s = scriptRe.ReplaceAllString(s, " ")
	s = styleRe.ReplaceAllString(s, " ")
	s = tagRe.ReplaceAllString(s, " ")
	for _, r := range [][2]string{
		{"&amp;", "&"}, {"&lt;", "<"}, {"&gt;", ">"}, {"&quot;", `"`},
		{"&#39;", "'"}, {"&nbsp;", " "}, {"\u00a0", " "},
	} {
		s = strings.ReplaceAll(s, r[0], r[1])
	}
	return strings.TrimSpace(wsRe.ReplaceAllString(s, " "))
}

func firstMatch(re *regexp.Regexp, raw []byte) string {
	if m := re.FindSubmatch(raw); m != nil {
		return cleanText(bytes.TrimSpace(m[1]))
	}
	return ""
}

func strOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func contains(ss []string, x string) bool {
	for _, s := range ss {
		if s == x {
			return true
		}
	}
	return false
}

// ---- url join / escape without importing net/url at every call ----

func urlJoin(base, ref string) string {
	if strings.Contains(ref, "://") || strings.HasPrefix(ref, "//") {
		return ref
	}
	if strings.HasPrefix(ref, "/") {
		i := strings.Index(base, "://")
		if i < 0 {
			return base + ref
		}
		rest := base[i+3:]
		j := strings.Index(rest, "/")
		if j < 0 {
			return base + ref
		}
		return base[:i+3+j] + ref
	}
	k := strings.LastIndex(base, "/")
	if k < 0 {
		return ref
	}
	return base[:k+1] + ref
}

func urlQueryEscape(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_' || c == '.' || c == '~':
			b.WriteByte(c)
		case c == ' ':
			b.WriteByte('+')
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

var _ = os.Getpid
