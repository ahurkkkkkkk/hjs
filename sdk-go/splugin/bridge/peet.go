// peet.go - fetch the real TLS fingerprint from tls.peet.ws using the same
// client, so the reported JA3/JA4/Peetprint is exactly what a server would
// see from Splugin traffic.
package main

import (
	"encoding/json"
	"fmt"
	"io"

	fhttp "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
)

func peet(c tls_client.HttpClient, profile string) (map[string]string, error) {
	req, err := fhttp.NewRequest("GET", "https://tls.peet.ws/api/all", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", uaFor(profile, "GET"))
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var v struct {
		TLS struct {
			JA3       string `json:"ja3"`
			JA3Hash   string `json:"ja3_hash"`
			JA4       string `json:"ja4"`
			JA4R      string `json:"ja4_r"`
			Peetprint string `json:"peetprint"`
			Alpn      any    `json:"alpn"`
		} `json:"tls"`
		UserAgent   string `json:"user_agent"`
		HTTPVersion string `json:"http_version"`
	}
	if err := json.Unmarshal(data, &v); err != nil {
		return nil, err
	}
	out := map[string]string{
		"ja3":            v.TLS.JA3,
		"ja3_hash":       v.TLS.JA3Hash,
		"ja4":            v.TLS.JA4,
		"ja4_r":          v.TLS.JA4R,
		"peetprint":      v.TLS.Peetprint,
		"server_seen_ua": v.UserAgent,
		"http_version":   v.HTTPVersion,
	}
	if a := v.TLS.Alpn; a != nil {
		out["alpn"] = fmt.Sprintf("%v", a)
	}
	return out, nil
}
