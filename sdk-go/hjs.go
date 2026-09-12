// Package hjs is a Playwright-flavoured Go client for the hjs stealth
// headless browser (Mojo + QuickJS). A Browser is one session: it owns a
// cookie jar, chains the Referer header like a real browser, rotates
// fingerprints, resolves relative links, detects captcha/bot walls,
// extracts structured data (OG tags, JSON-LD), reads robots.txt and
// sitemaps, and can scrape many URLs in parallel with per-host rate
// limiting.
//
// Quick start:
//
//	b, _ := hjs.New(hjs.Options{Profile: "chrome131"})
//	defer b.Close()
//	page, err := b.Goto("https://example.com", nil)
//	if err != nil { log.Fatal(err) }
//	fmt.Println(page.Status, page.Title)
//	fmt.Println(page.Text)
//	for _, l := range page.Links { fmt.Println(l) }
//
// Fingerprint rotation cycles a list on every request:
//
//	b, _ := hjs.New(hjs.Options{Profiles: []string{"chrome131", "firefox133"}})
//
// Parallel scraping:
//
//	pages, errs := b.ScrapeAll(ctx, urls, 4)
//
// Structured extraction (Open Graph, meta, JSON-LD, links):
//
//	data := page.Structured()
//	fmt.Println(data.OG["og:title"], len(data.JSONLD))
//
// robots.txt + sitemap:
//
//	robots, _ := b.Robots("https://example.com")
//	if robots.Allowed(u) { ... }
//	locs, _ := b.Sitemap("https://example.com/sitemap.xml")
//
// Codegen (Playwright inspector style), records a session into a script:
//
//	rec := b.Record("go")
//	b.Goto("https://example.com", nil)
//	fmt.Println(rec.Code())
//
// The hjs binary must be installed separately (HJS_BIN env var or PATH).
// See the project README.
package hjs

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const defaultLD = "/root/hjs:/root/hbrowser/mojo-home/lib"

// Page mirrors hjs's JSON output for one fetched page.
type Page struct {
	URL       string   `json:"url"`
	Status    int      `json:"status"`
	Bytes     int      `json:"bytes"`
	ElapsedMS int64    `json:"elapsed_ms"`
	Attempts  int      `json:"attempts"`
	Title     string   `json:"title"`
	Desc      string   `json:"description"`
	Links     []string `json:"links"`
	Text      string   `json:"text"`
	Captcha   string   `json:"captcha,omitempty"`

	browser *Browser
	raw     string
}

// OK reports a successful, non-blocked fetch.
func (p *Page) OK() bool { return p.Status >= 200 && p.Status < 300 && p.Captcha == "" }

// Blocked reports a captcha / bot-wall interstitial.
func (p *Page) Blocked() bool { return p.Captcha != "" }

// HTML returns the raw source, fetching it on demand.
func (p *Page) HTML() (string, error) {
	if p.raw == "" {
		raw, err := p.browser.rawGet(p.URL, nil, nil)
		if err != nil {
			return "", err
		}
		p.raw = raw
	}
	return p.raw, nil
}

// Structured extracts title, description, canonical, OG/Twitter, meta,
// JSON-LD and links-with-anchor-text from the page HTML.
func (p *Page) Structured() *Structured {
	raw, err := p.HTML()
	if err != nil || raw == "" {
		return &Structured{URL: p.URL, OG: map[string]string{}, Meta: map[string]string{}}
	}
	s := ParseHTML(raw)
	s.URL = p.URL
	if s.Description == "" {
		s.Description = p.Desc
	}
	return s
}

// FindLinks filters resolved links by substring and/or regex.
func (p *Page) FindLinks(contains, pattern string) []string {
	var rx *regexp.Regexp
	if pattern != "" {
		rx = regexp.MustCompile(pattern)
	}
	var out []string
	for _, l := range p.Links {
		if contains != "" && !strings.Contains(l, contains) {
			continue
		}
		if rx != nil && !rx.MatchString(l) {
			continue
		}
		out = append(out, l)
	}
	return out
}

// Structured is the ParseHTML output.
type Structured struct {
	URL         string            `json:"url"`
	Title       string            `json:"title"`
	Description string            `json:"description"`
	Canonical   string            `json:"canonical"`
	OG          map[string]string `json:"og"`
	Meta        map[string]string `json:"meta"`
	JSONLD      []any             `json:"json_ld"`
	Links       []Link            `json:"links"`
}

// Link is an anchor with href and text.
type Link struct {
	Href string `json:"href"`
	Text string `json:"text"`
}

var (
	attrRe  = regexp.MustCompile(`([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"|([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*'([^']*)'|([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([^\s"'>]+)`)
	titleRe = regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title>`)
	metaRe  = regexp.MustCompile(`(?is)<meta\b([^>]*)>`)
	linkRe  = regexp.MustCompile(`(?is)<link\b([^>]*)>`)
	ldRe    = regexp.MustCompile(`(?is)<script\b[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>`)
	aRe     = regexp.MustCompile(`(?is)<a\b([^>]*)>(.*?)</a>`)
	tagRe   = regexp.MustCompile(`<[^>]+>`)
	spaceRe = regexp.MustCompile(`\s+`)
)

func attrs(raw string) map[string]string {
	out := map[string]string{}
	for _, m := range attrRe.FindAllStringSubmatch(raw, -1) {
		switch {
		case m[1] != "":
			out[strings.ToLower(m[1])] = m[2]
		case m[3] != "":
			out[strings.ToLower(m[3])] = m[4]
		case m[5] != "":
			out[strings.ToLower(m[5])] = m[6]
		}
	}
	return out
}

// ParseHTML extracts structured data from raw HTML (stdlib only).
func ParseHTML(html string) *Structured {
	s := &Structured{OG: map[string]string{}, Meta: map[string]string{}}
	if m := titleRe.FindStringSubmatch(html); m != nil {
		s.Title = cleanText(m[1])
	}
	for _, m := range metaRe.FindAllStringSubmatch(html, -1) {
		a := attrs(m[1])
		name := a["name"]
		if name == "" {
			name = a["property"]
		}
		content := a["content"]
		low := strings.ToLower(name)
		if low == "description" {
			s.Description = content
		}
		if strings.HasPrefix(low, "og:") || strings.HasPrefix(low, "twitter:") {
			s.OG[low] = content
		}
		if name != "" {
			switch low {
			case "description", "keywords", "viewport", "robots":
			default:
				s.Meta[name] = content
			}
		}
	}
	for _, m := range linkRe.FindAllStringSubmatch(html, -1) {
		a := attrs(m[1])
		if strings.EqualFold(a["rel"], "canonical") {
			s.Canonical = a["href"]
		}
	}
	for _, m := range ldRe.FindAllStringSubmatch(html, -1) {
		var v any
		if json.Unmarshal([]byte(m[1]), &v) == nil {
			s.JSONLD = append(s.JSONLD, v)
		}
	}
	for _, m := range aRe.FindAllStringSubmatch(html, -1) {
		a := attrs(m[1])
		href := a["href"]
		if href != "" {
			s.Links = append(s.Links, Link{Href: href, Text: cleanText(tagRe.ReplaceAllString(m[2], ""))})
		}
	}
	return s
}

func cleanText(s string) string {
	for _, r := range [][2]string{{"&amp;", "&"}, {"&lt;", "<"}, {"&gt;", ">"}, {"&quot;", `"`}, {"&#39;", "'"}, {"&nbsp;", " "}} {
		s = strings.ReplaceAll(s, r[0], r[1])
	}
	return strings.TrimSpace(spaceRe.ReplaceAllString(s, " "))
}

// Options configures a Browser session. Zero values use hjs defaults.
type Options struct {
	Profile        string
	Profiles       []string // rotate through these per request when >1
	CookieJar      string
	Session        bool // true keeps a temp cookie jar for the session
	Proxy          string
	Referer        string
	UserAgent      string
	Headers        map[string]string
	TimeoutSec     int
	MaxBytes       int
	JS             bool
	JSBudgetMS     int
	HTTP1          bool
	MinTLS         string
	Ciphers        string
	DelayMS        int
	Retries        int
	BackoffMS      int
	PerHostDelayMS int
	RespectRobots  bool
	MaxPages       int
	Binary         string
	LDPath         string
}

func (o *Options) defaults() {
	if o.TimeoutSec == 0 {
		o.TimeoutSec = 15
	}
	if o.MaxBytes == 0 {
		o.MaxBytes = 2 << 20
	}
	if o.JSBudgetMS == 0 {
		o.JSBudgetMS = 3000
	}
	if o.Retries == 0 {
		o.Retries = 2
	}
	if o.BackoffMS == 0 {
		o.BackoffMS = 500
	}
	if o.MinTLS == "" {
		o.MinTLS = "1.2"
	}
}

// Browser is one browsing session.
type Browser struct {
	opts        Options
	jarOwned    bool
	rotI        int
	lastURL     string
	count       int
	mu          sync.Mutex
	hostMu      map[string]*hostGate
	binPath     string
	ldPath      string
	robotsCache map[string]*Robots
	rec         *Recorder
}

type hostGate struct {
	mu   sync.Mutex
	last time.Time
}

// New starts a session.
func New(opts Options) (*Browser, error) { return NewBrowser(opts) }

// NewBrowser starts a session.
func NewBrowser(opts Options) (*Browser, error) {
	opts.defaults()
	b := &Browser{opts: opts, hostMu: map[string]*hostGate{}, robotsCache: map[string]*Robots{}}

	bin := opts.Binary
	if bin == "" {
		bin = os.Getenv("HJS_BIN")
	}
	if bin == "" {
		if p, err := exec.LookPath("hjs"); err == nil {
			bin = p
		} else {
			for _, c := range []string{"/usr/local/bin/hjs", "/root/hjs/hjs"} {
				if _, err := os.Stat(c); err == nil {
					bin = c
					break
				}
			}
		}
	}
	if bin == "" {
		return nil, fmt.Errorf("hjs: binary not found (set HJS_BIN)")
	}
	if _, err := os.Stat(bin); err != nil {
		return nil, fmt.Errorf("hjs: binary %q not found: %w", bin, err)
	}
	b.binPath = bin

	ld := opts.LDPath
	if ld == "" {
		ld = os.Getenv("HJS_LD")
	}
	if ld == "" && os.Getenv("LD_LIBRARY_PATH") == "" {
		ld = defaultLD
	}
	b.ldPath = ld

	if opts.CookieJar == "" && opts.Session {
		f, err := os.CreateTemp("", "hjs-jar-*.txt")
		if err != nil {
			return nil, err
		}
		f.Close()
		b.opts.CookieJar = f.Name()
		b.jarOwned = true
	}
	return b, nil
}

// Close ends the session.
func (b *Browser) Close() {}

// DeleteJar removes a Browser-owned temp cookie jar.
func (b *Browser) DeleteJar() {
	if b.jarOwned && b.opts.CookieJar != "" {
		_ = os.Remove(b.opts.CookieJar)
	}
}

// JarPath returns the cookie jar path in use.
func (b *Browser) JarPath() string { return b.opts.CookieJar }

// nextProfile cycles the rotation list.
func (b *Browser) nextProfile() string {
	if len(b.opts.Profiles) > 1 {
		b.mu.Lock()
		p := b.opts.Profiles[b.rotI%len(b.opts.Profiles)]
		b.rotI++
		b.mu.Unlock()
		return p
	}
	if len(b.opts.Profiles) == 1 {
		return b.opts.Profiles[0]
	}
	return b.opts.Profile
}

func (b *Browser) rateLimit(target string) {
	if b.opts.PerHostDelayMS <= 0 {
		return
	}
	u, err := url.Parse(target)
	if err != nil {
		return
	}
	b.mu.Lock()
	g := b.hostMu[u.Host]
	if g == nil {
		g = &hostGate{}
		b.hostMu[u.Host] = g
	}
	b.mu.Unlock()
	g.mu.Lock()
	defer g.mu.Unlock()
	wait := g.last.Add(time.Duration(b.opts.PerHostDelayMS) * time.Millisecond).Sub(time.Now())
	if wait > 0 {
		time.Sleep(wait)
	}
	g.last = time.Now()
}

func (b *Browser) checkRobots(target string) error {
	if !b.opts.RespectRobots {
		return nil
	}
	u, err := url.Parse(target)
	if err != nil {
		return nil
	}
	base := u.Scheme + "://" + u.Host
	b.mu.Lock()
	rob, ok := b.robotsCache[base]
	b.mu.Unlock()
	if !ok {
		txt, err := b.rawGet(base+"/robots.txt", nil, nil)
		if err != nil {
			return nil
		}
		rob = ParseRobots(txt, b.opts.UserAgent)
		b.mu.Lock()
		b.robotsCache[base] = rob
		b.mu.Unlock()
	}
	if rob != nil && !rob.Allowed(target) {
		return fmt.Errorf("hjs: disallowed by robots.txt: %s", target)
	}
	return nil
}

func (b *Browser) guardCount() error {
	if b.opts.MaxPages <= 0 {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.count >= b.opts.MaxPages {
		return fmt.Errorf("hjs: max_pages reached")
	}
	return nil
}

func (b *Browser) argv(target, mode string, referer *string, hdrs map[string]string) []string {
	o := b.opts
	prof := b.nextProfile()
	args := []string{b.binPath, target,
		fmt.Sprintf("--timeout=%d", o.TimeoutSec),
		fmt.Sprintf("--max=%d", o.MaxBytes),
		"--mode=" + mode,
		"--links",
	}
	if prof != "" {
		args = append(args, "--profile="+prof)
	}
	if o.CookieJar != "" {
		args = append(args, "--cookies="+o.CookieJar, "--cookie-jar="+o.CookieJar)
	}
	effRef := o.Referer
	if referer != nil {
		effRef = *referer
	} else if effRef == "" && b.lastURL != "" && mode != "html" {
		effRef = b.lastURL
	}
	if effRef != "" {
		args = append(args, "--referer="+effRef)
	}
	if o.Proxy != "" {
		args = append(args, "--proxy="+o.Proxy)
	}
	if o.UserAgent != "" {
		args = append(args, "--ua="+o.UserAgent)
	}
	if o.HTTP1 {
		args = append(args, "--http1")
	}
	if o.MinTLS != "" {
		args = append(args, "--min-tls="+o.MinTLS)
	}
	if o.Ciphers != "" {
		args = append(args, "--ciphers="+o.Ciphers)
	}
	if o.DelayMS > 0 {
		args = append(args, fmt.Sprintf("--delay-ms=%d", o.DelayMS))
	}
	if o.Retries != 2 {
		args = append(args, fmt.Sprintf("--retries=%d", o.Retries))
	}
	if o.BackoffMS != 500 {
		args = append(args, fmt.Sprintf("--backoff-ms=%d", o.BackoffMS))
	}
	if mode == "json" {
		if o.JS {
			args = append(args, "--js", fmt.Sprintf("--js-budget-ms=%d", o.JSBudgetMS))
		} else {
			args = append(args, "--no-js")
		}
	}
	for k, v := range o.Headers {
		args = append(args, fmt.Sprintf("--header=%s: %s", k, v))
	}
	for k, v := range hdrs {
		args = append(args, fmt.Sprintf("--header=%s: %s", k, v))
	}
	return args
}

func (b *Browser) run(args []string) ([]byte, error) {
	cmd := exec.Command(args[0], args[1:]...)
	if b.ldPath != "" && os.Getenv("LD_LIBRARY_PATH") == "" {
		cmd.Env = append(os.Environ(), "LD_LIBRARY_PATH="+b.ldPath)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		if msg := strings.TrimSpace(stderr.String()); msg != "" {
			return nil, fmt.Errorf("hjs: %s", msg)
		}
		return nil, fmt.Errorf("hjs: %w", err)
	}
	return out, nil
}

func (b *Browser) rawGet(target string, referer *string, hdrs map[string]string) (string, error) {
	if err := b.guardCount(); err != nil {
		return "", err
	}
	b.rateLimit(target)
	if b.rec != nil {
		b.rec.record(target, "html")
	}
	out, err := b.run(b.argv(target, "html", referer, hdrs))
	if err != nil {
		return "", err
	}
	b.mu.Lock()
	b.count++
	b.lastURL = target
	b.mu.Unlock()
	return string(out), nil
}

// Goto fetches a URL with JSON extraction.
func (b *Browser) Goto(target string, hdrs map[string]string) (*Page, error) {
	if err := b.guardCount(); err != nil {
		return nil, err
	}
	if err := b.checkRobots(target); err != nil {
		return nil, err
	}
	b.rateLimit(target)
	if b.rec != nil {
		b.rec.record(target, "json")
	}
	out, err := b.run(b.argv(target, "json", nil, hdrs))
	if err != nil {
		return nil, err
	}
	var p Page
	if err := json.Unmarshal(out, &p); err != nil {
		return nil, fmt.Errorf("hjs: bad json: %w", err)
	}
	p.browser = b
	p.resolveLinks()
	b.mu.Lock()
	b.count++
	b.lastURL = target
	b.mu.Unlock()
	return &p, nil
}

func (p *Page) resolveLinks() {
	base, err := url.Parse(p.URL)
	if err != nil {
		return
	}
	seen := map[string]bool{}
	var resolved []string
	for _, l := range p.Links {
		if l == "" {
			continue
		}
		if ref, err := url.Parse(l); err == nil {
			l = base.ResolveReference(ref).String()
		}
		if !seen[l] {
			seen[l] = true
			resolved = append(resolved, l)
		}
	}
	sort.Strings(resolved)
	p.Links = resolved
}

// GotoReferer fetches with an explicit Referer override.
func (b *Browser) GotoReferer(target, referer string, hdrs map[string]string) (*Page, error) {
	if err := b.guardCount(); err != nil {
		return nil, err
	}
	b.rateLimit(target)
	r := referer
	out, err := b.run(b.argv(target, "json", &r, hdrs))
	if err != nil {
		return nil, err
	}
	var p Page
	if err := json.Unmarshal(out, &p); err != nil {
		return nil, fmt.Errorf("hjs: bad json: %w", err)
	}
	p.browser = b
	p.resolveLinks()
	b.mu.Lock()
	b.count++
	b.lastURL = target
	b.mu.Unlock()
	return &p, nil
}

// GotoHTML fetches and returns the raw HTML source.
func (b *Browser) GotoHTML(target string, hdrs map[string]string) (string, error) {
	return b.rawGet(target, nil, hdrs)
}

// WaitFor polls a URL until its text contains substr (or status matches).
func (b *Browser) WaitFor(ctx context.Context, target, substr string, status int, timeout time.Duration) (*Page, error) {
	deadline := time.Now().Add(timeout)
	for {
		p, err := b.Goto(target, nil)
		if err != nil {
			return nil, err
		}
		if status != 0 && p.Status == status {
			return p, nil
		}
		if substr == "" || strings.Contains(strings.ToLower(p.Text), strings.ToLower(substr)) {
			return p, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("hjs: WaitFor timed out on %s", target)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// ScrapeResult pairs an input index with its page or error.
type ScrapeResult struct {
	Index int
	URL   string
	Page  *Page
	Err   error
}

// Scrape fetches many URLs in parallel (results by completion).
func (b *Browser) Scrape(ctx context.Context, urls []string, workers int) <-chan ScrapeResult {
	if workers < 1 {
		workers = 1
	}
	out := make(chan ScrapeResult, len(urls))
	go func() {
		defer close(out)
		sem := make(chan struct{}, workers)
		var wg sync.WaitGroup
		for i, u := range urls {
			wg.Add(1)
			go func(i int, u string) {
				defer wg.Done()
				select {
				case sem <- struct{}{}:
				case <-ctx.Done():
					out <- ScrapeResult{Index: i, URL: u, Err: ctx.Err()}
					return
				}
				defer func() { <-sem }()
				p, err := b.Goto(u, nil)
				out <- ScrapeResult{Index: i, URL: u, Page: p, Err: err}
			}(i, u)
		}
		wg.Wait()
	}()
	return out
}

// ScrapeAll collects input-ordered results.
func (b *Browser) ScrapeAll(ctx context.Context, urls []string, workers int) ([]*Page, []error) {
	results := make([]*Page, len(urls))
	errs := make([]error, len(urls))
	for r := range b.Scrape(ctx, urls, workers) {
		results[r.Index] = r.Page
		errs[r.Index] = r.Err
	}
	return results, errs
}

// Sitemap lists every <loc> in a sitemap.xml (index files expand one level).
// Pass "" to use the last visited host, or a direct sitemap URL.
func (b *Browser) Sitemap(target string) ([]string, error) {
	u, err := url.Parse(target)
	if err != nil || u.Scheme == "" {
		if target == "" {
			target = b.lastURL
		}
		u, err = url.Parse(target)
		if err != nil || u.Scheme == "" {
			return nil, fmt.Errorf("hjs: Sitemap needs a URL or a prior Goto")
		}
	}
	xmlURL := fmt.Sprintf("%s://%s/sitemap.xml", u.Scheme, u.Host)
	if strings.HasSuffix(target, ".xml") {
		xmlURL = target
	}
	txt, err := b.rawGet(xmlURL, nil, nil)
	if err != nil {
		return nil, err
	}
	locs := parseSitemap(txt)
	if strings.Contains(txt, "<sitemapindex") {
		var expanded []string
		for i, child := range locs {
			if i >= 200 {
				break
			}
			sub, err := b.rawGet(child, nil, nil)
			if err != nil {
				continue
			}
			expanded = append(expanded, parseSitemap(sub)...)
		}
		return expanded, nil
	}
	return locs, nil
}

var locRe = regexp.MustCompile(`(?is)<loc>(.*?)</loc>`)

func parseSitemap(x string) []string {
	var out []string
	for _, m := range locRe.FindAllStringSubmatch(x, -1) {
		out = append(out, strings.TrimSpace(m[1]))
	}
	return out
}

// Robots is a parsed robots.txt for one user agent.
type Robots struct {
	Disallow   []string
	Allow      []string
	Sitemaps   []string
	CrawlDelay float64
}

// Robots fetches and parses robots.txt for a host.
func (b *Browser) Robots(target string) (*Robots, error) {
	u, err := url.Parse(target)
	if err != nil {
		return nil, err
	}
	txt, err := b.rawGet(u.Scheme+"://"+u.Host+"/robots.txt", nil, nil)
	if err != nil {
		return nil, err
	}
	return ParseRobots(txt, b.opts.UserAgent), nil
}

// ParseRobots reads a robots.txt body for a user agent.
func ParseRobots(text, ua string) *Robots {
	r := &Robots{}
	blocks := map[string][]string{}
	var cur string
	for _, line := range strings.Split(text, "\n") {
		if i := strings.Index(line, "#"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			cur = ""
			continue
		}
		low := strings.ToLower(line)
		switch {
		case strings.HasPrefix(low, "user-agent:"):
			cur = strings.ToLower(strings.TrimSpace(line[len("user-agent:"):]))
		case strings.HasPrefix(low, "sitemap:"):
			// Sitemap lines may appear anywhere in the file.
			r.Sitemaps = append(r.Sitemaps, strings.TrimSpace(line[len("sitemap:"):]))
		case cur != "":
			blocks[cur] = append(blocks[cur], low)
		}
	}
	if ua == "" {
		ua = "*"
	}
	chosen, ok := blocks[strings.ToLower(ua)]
	if !ok {
		chosen = blocks["*"]
	}
	for _, line := range chosen {
		switch {
		case strings.HasPrefix(line, "disallow:"):
			if v := strings.TrimSpace(line[len("disallow:"):]); v != "" {
				r.Disallow = append(r.Disallow, v)
			}
		case strings.HasPrefix(line, "allow:"):
			if v := strings.TrimSpace(line[len("allow:"):]); v != "" {
				r.Allow = append(r.Allow, v)
			}
		case strings.HasPrefix(line, "crawl-delay:"):
			fmt.Sscanf(line[len("crawl-delay:"):], "%f", &r.CrawlDelay)
		}
	}
	return r
}

// Allowed checks whether a URL is permitted by the robots rules.
func (r *Robots) Allowed(target string) bool {
	u, err := url.Parse(target)
	if err != nil {
		return true
	}
	path := u.Path
	if path == "" {
		path = "/"
	}
	type rule struct {
		prefix string
		allow  bool
	}
	var rules []rule
	for _, p := range r.Allow {
		rules = append(rules, rule{p, true})
	}
	for _, p := range r.Disallow {
		rules = append(rules, rule{p, false})
	}
	sort.SliceStable(rules, func(i, j int) bool {
		return len(rules[i].prefix) > len(rules[j].prefix)
	})
	for _, rl := range rules {
		if globMatch(path, rl.prefix) {
			return rl.allow
		}
	}
	return true
}

func globMatch(path, pattern string) bool {
	if pattern == "" {
		return false
	}
	if !strings.ContainsAny(pattern, "*?") {
		return strings.HasPrefix(path, pattern)
	}
	re := regexp.QuoteMeta(pattern)
	re = strings.ReplaceAll(re, `\*`, ".*")
	re = strings.ReplaceAll(re, `\?`, ".")
	matched, _ := regexp.MatchString("^"+re, path)
	return matched
}

// Cookies returns the current cookie jar contents.
func (b *Browser) Cookies() (string, error) {
	if b.opts.CookieJar == "" {
		return "", nil
	}
	f, err := os.Open(b.opts.CookieJar)
	if err != nil {
		return "", nil
	}
	defer f.Close()
	var sb strings.Builder
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		sb.WriteString(sc.Text())
		sb.WriteString("\n")
	}
	return sb.String(), nil
}

// Record attaches a Recorder (codegen). lang is "go" or "python".
func (b *Browser) Record(lang string) *Recorder {
	rec := &Recorder{browser: b, lang: lang}
	b.rec = rec
	return rec
}

// BinaryDir reports the directory containing the hjs binary.
func (b *Browser) BinaryDir() string { return filepath.Dir(b.binPath) }

// ---------------------------------------------------------------------------
// Recorder
// ---------------------------------------------------------------------------

// Recorder captures Goto calls and emits a runnable script (codegen).
type Recorder struct {
	browser *Browser
	lang    string
	mu      sync.Mutex
	calls   [][2]string
}

func (r *Recorder) record(url, mode string) {
	r.mu.Lock()
	r.calls = append(r.calls, [2]string{url, mode})
	r.mu.Unlock()
}

// Calls returns the recorded (url, mode) pairs.
func (r *Recorder) Calls() [][2]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([][2]string, len(r.calls))
	copy(out, r.calls)
	return out
}

// Code returns the generated script for the recorded session.
func (r *Recorder) Code() string {
	if r.lang == "python" {
		return r.python()
	}
	return r.golang()
}

func (r *Recorder) python() string {
	b := r.browser
	s := "from hjs import Browser\n\n"
	s += fmt.Sprintf("# captured by hjs codegen (%d requests)\n", len(r.calls))
	s += "b = Browser(\n"
	if b.opts.Profile != "" {
		s += fmt.Sprintf("    profile=%q,\n", b.opts.Profile)
	}
	s += "    js=True,\n)\ntry:\n"
	if len(r.calls) == 0 {
		s += "    pass  # no requests recorded\n"
	}
	for _, c := range r.calls {
		if c[1] == "json" {
			s += fmt.Sprintf("    page = b.goto(%q)\n    print(page.status, page.title)\n", c[0])
		} else {
			s += fmt.Sprintf("    html = b.goto(%q, mode='html')\n", c[0])
		}
	}
	s += "finally:\n    b.close()\n"
	return s
}

func (r *Recorder) golang() string {
	b := r.browser
	s := "package main\n\nimport (\n\t\"fmt\"\n\t\"log\"\n\n\t\"github.com/ahurkkkkkkk/hjs/sdk-go/hjs\"\n)\n\n"
	s += "func main() {\n"
	s += fmt.Sprintf("\t// captured by hjs codegen (%d requests)\n", len(r.calls))
	s += "\topts := hjs.Options{JS: true}\n"
	if b.opts.Profile != "" {
		s += fmt.Sprintf("\topts.Profile = %q\n", b.opts.Profile)
	}
	s += "\tb, err := hjs.New(opts)\n\tif err != nil {\n\t\tlog.Fatal(err)\n\t}\n\tdefer b.Close()\n"
	for _, c := range r.calls {
		if c[1] == "json" {
			s += fmt.Sprintf("\tp, err := b.Goto(%q, nil)\n\tif err != nil {\n\t\tlog.Fatal(err)\n\t}\n\tfmt.Println(p.Status, p.Title)\n", c[0])
		} else {
			s += fmt.Sprintf("\traw, err := b.GotoHTML(%q, nil)\n\tif err != nil {\n\t\tlog.Fatal(err)\n\t}\n\t_ = raw\n", c[0])
		}
	}
	s += "}\n"
	return s
}
