// hfetch - minimal Go wrapper around the hbrowser Mojo binary.
//
// Usage from Go code:
//   out, err := hfetch.Fetch("https://example.com", nil)
//
// The hbrowser binary must be on PATH or set Env.HBrowser.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Result mirrors hbrowser's JSON output.
type Result struct {
	URL         string   `json:"url"`
	Status      int      `json:"status"`
	Bytes       int      `json:"bytes"`
	ElapsedMS   int64    `json:"elapsed_ms"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Links       []string `json:"links,omitempty"`
	Text        string   `json:"text"`
}

// Opts control the hbrowser subprocess.
type Opts struct {
	TimeoutSec int      // default 15
	MaxBytes   int      // default 2 MiB
	Headers    []string // extra request headers "K: V"
	UserAgent  string
	Links      bool // include links
	Binary     string // override binary path
}

func (o *Opts) args(url string) []string {
	args := []string{url}
	if o.TimeoutSec > 0 {
		args = append(args, fmt.Sprintf("--timeout=%d", o.TimeoutSec))
	}
	if o.MaxBytes > 0 {
		args = append(args, fmt.Sprintf("--max=%d", o.MaxBytes))
	}
	for _, h := range o.Headers {
		args = append(args, "--header="+h)
	}
	if o.UserAgent != "" {
		args = append(args, "--ua="+o.UserAgent)
	}
	if o.Links {
		args = append(args, "--links")
	}
	return args
}

// Fetch runs hbrowser and returns the parsed result.
func Fetch(url string, opts *Opts) (*Result, error) {
	if opts == nil {
		opts = &Opts{}
	}
	bin := opts.Binary
	if bin == "" {
		bin = os.Getenv("HBROWSER")
		if bin == "" {
			bin = "hbrowser"
		}
	}
	cmd := exec.Command(bin, opts.args(url)...)
	// If the Mojo runtime .so libs live outside the default loader path,
	// point LD_LIBRARY_PATH at them via HBROWSER_LD.
	if ld := os.Getenv("HBROWSER_LD"); ld != "" {
		cmd.Env = append(os.Environ(), "LD_LIBRARY_PATH="+ld)
	}
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
			msg := strings.TrimSpace(string(ee.Stderr))
			if strings.Contains(msg, "fetch failed") {
				return nil, fmt.Errorf("hbrowser: network error: %s", msg)
			}
		}
		return nil, fmt.Errorf("hbrowser: %w", err)
	}
	var res Result
	if err := json.Unmarshal(out, &res); err != nil {
		return nil, fmt.Errorf("hbrowser: bad json: %w", err)
	}
	return &res, nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: hfetch <url>")
		os.Exit(1)
	}
	res, err := Fetch(os.Args[1], nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(res)
}
