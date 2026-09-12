package tplugin

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"hash/crc32"
	"testing"
)

func sample() *PageData {
	return &PageData{
		URL_:   "https://example.com/page",
		Title_: "Test Page",
		Text_: "The quick brown fox jumps over the lazy dog.\n\n" +
			"Second paragraph with a bit more text so wrapping is exercised by the " +
			"layout model and produces several lines of output.",
		Links_: []string{"https://example.com/a", "https://example.com/b"},
		Anchors_: []Link{
			{Href: "https://example.com/a", Text: "Link Alpha"},
			{Href: "/b", Text: "Link Beta"},
		},
	}
}

func TestPNGValid(t *testing.T) {
	png := Screenshot(sample(), 2)
	if !bytes.HasPrefix(png, []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}) {
		t.Fatal("bad signature")
	}
	if !bytes.Contains(png, []byte("IEND")) {
		t.Fatal("no IEND")
	}
	// IHDR dims
	w := binary.BigEndian.Uint32(png[16:20])
	h := binary.BigEndian.Uint32(png[20:24])
	if w < 200 || w > 2600 || h < 100 {
		t.Fatalf("dims %dx%d", w, h)
	}
	// decode IDAT and verify raw size
	var idat []byte
	i := 8
	for i < len(png) {
		ln := binary.BigEndian.Uint32(png[i : i+4])
		tag := png[i+4 : i+8]
		data := png[i+8 : i+8+int(ln)]
		if bytes.Equal(tag, []byte("IDAT")) {
			idat = append(idat, data...)
		}
		// verify CRC
		c := crc32.NewIEEE()
		c.Write(tag)
		c.Write(data)
		want := binary.BigEndian.Uint32(png[i+8+int(ln) : i+12+int(ln)])
		if c.Sum32() != want {
			t.Fatal("chunk CRC mismatch")
		}
		i += 12 + int(ln)
	}
	raw, err := zlib.NewReader(bytes.NewReader(idat))
	if err != nil {
		t.Fatal(err)
	}
	decoded := bytes.Buffer{}
	if _, err := decoded.ReadFrom(raw); err != nil {
		t.Fatal(err)
	}
	want := (int(w)*3 + 1) * int(h)
	if decoded.Len() != want {
		t.Fatalf("raw size %d want %d", decoded.Len(), want)
	}
	// filter bytes all 0 (we emit only filter 0)
	stride := int(w)*3 + 1
	for y := 0; y < int(h); y++ {
		if decoded.Bytes()[y*stride] != 0 {
			t.Fatal("unexpected filter byte")
		}
	}
}

func TestPDFValid(t *testing.T) {
	p := PDF(sample(), 56)
	if !bytes.HasPrefix(p, []byte("%PDF-1.4")) {
		t.Fatal("header")
	}
	if !bytes.HasSuffix(bytes.TrimSpace(p), []byte("%%EOF")) {
		t.Fatal("eof")
	}
	// startxref points at xref
	sx := bytes.LastIndex(p, []byte("startxref"))
	rest := bytes.TrimSpace(p[sx+len("startxref"):])
	rest = rest[:bytes.IndexByte(rest, '\n')]
	off := 0
	for _, b := range rest {
		off = off*10 + int(b-'0')
	}
	if !bytes.HasPrefix(p[off:], []byte("xref")) {
		t.Fatalf("startxref %d invalid", off)
	}
}

func TestViewerTap(t *testing.T) {
	s := sample()
	v := NewViewer(s, 5)
	if v.Total() <= 5 {
		t.Fatalf("model too small %d", v.Total())
	}
	v.ScrollToFraction(1.0)
	if v.Top() != v.Total()-v.Rows {
		t.Fatalf("top %d total %d rows %d", v.Top(), v.Total(), v.Rows)
	}
	v.ScrollTo(0)
	// find first link row
	for i, ln := range v.Lines {
		if ln.LinkIndex != nil {
			got := v.Tap(i)
			want := s.GetLinks()[*ln.LinkIndex]
			if got != want {
				t.Fatalf("tap %d: %q want %q", i, got, want)
			}
			break
		}
	}
	if got := v.TapText("Link Alpha"); got != "https://example.com/a" {
		t.Fatalf("tap_text %q", got)
	}
}

func TestHelpers(t *testing.T) {
	if got := len(SplitWrap("word ", 40)); got == 0 {
		t.Fatal("wrap produced nothing")
	}
	if pt := PrintText(sample(), 100, 3); !bytes.Contains([]byte(pt), []byte("\f")) {
		t.Fatal("print missing pagination")
	}
	if r := Reader(sample()); !bytes.Contains([]byte(r), []byte("paragraph")) {
		t.Fatalf("reader %q", r)
	}
}
