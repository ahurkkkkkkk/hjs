// Package tplugin is the rendering plugin for hjs: screenshots (PNG),
// PDF export, reader text, print output, and a touch/scroll viewport model
// with link hit-testing. Pure Go stdlib, no Chromium and no external
// renderers, mirroring the Python sibling hjs_tplugin.py.
package tplugin

import (
	"bytes"
	"compress/zlib"
	"fmt"
	"hash/crc32"
	"regexp"
	"sort"
	"strings"
)

// Page is the slice of hjs.Page the plugin needs. *hjs.Page satisfies it via
// the adapter NewPage / NewPageFromHJS.
type Page interface {
	GetURL() string
	GetTitle() string
	GetText() string
	GetLinks() []string
	GetAnchors() []Link
}

// Link mirrors an anchor with href and text.
type Link struct {
	Href string `json:"href"`
	Text string `json:"text"`
}

// ---------------------------------------------------------------------------
// 5x7 bitmap font (same table as Python; bit0 = top row, 5 columns)
// ---------------------------------------------------------------------------

type glyph [5]uint8

// glyphs is filled by glyphs_gen.go (generated from the Python table);
// rune keys are code points, bit0 = top row.
var glyphs = map[rune]glyph{}

func glyphFor(ch rune) glyph {
	if g, ok := glyphs[ch]; ok {
		return g
	}
	return glyphs[' ']
}

// ---------------------------------------------------------------------------
// layout model
// ---------------------------------------------------------------------------

var wordSplit = regexp.MustCompile(`\s+`)

// Line is one laid-out row. LinkIndex points into Page.Links when set.
type Line struct {
	Text      string
	LinkIndex *int
	IsTitle   bool
}

const wordsPerLine = 80

// SplitWrap lays text into lines of at most cols characters.
func SplitWrap(text string, cols int) []string {
	if cols <= 0 {
		cols = 70
	}
	var out []string
	for _, para := range strings.Split(text, "\n") {
		words := wordSplit.Split(strings.TrimSpace(para), -1)
		var kept []string
		for _, w := range words {
			if w != "" {
				kept = append(kept, w)
			}
		}
		if len(kept) == 0 {
			out = append(out, "")
			continue
		}
		cur := ""
		for _, w := range kept {
			if cur != "" && len(cur)+1+len(w) > cols {
				out = append(out, cur)
				cur = w
			} else if cur != "" {
				cur += " " + w
			} else {
				cur = w
			}
		}
		if cur != "" {
			out = append(out, cur)
		}
	}
	return out
}

// PageLines lays a Page out into renderable lines: title, text, then a
// numbered-style link list keyed by index into GetLinks.
func PageLines(p Page) []Line {
	var lines []Line
	if t := strings.TrimSpace(p.GetTitle()); t != "" {
		lines = append(lines, Line{Text: t, IsTitle: true}, Line{})
	}
	hrefToText := map[string]string{}
	for _, a := range p.GetAnchors() {
		if a.Text == "" || a.Href == "" {
			continue
		}
		resolved := a.Href
		if !strings.Contains(resolved, "://") && !strings.HasPrefix(resolved, "//") {
			resolved = resolveURL(p.GetURL(), a.Href)
		}
		if _, dup := hrefToText[resolved]; !dup {
			hrefToText[resolved] = a.Text
		}
	}
	for _, c := range SplitWrap(p.GetText(), wordsPerLine) {
		lines = append(lines, Line{Text: c})
	}
	links := p.GetLinks()
	if len(links) > 0 {
		lines = append(lines, Line{}, Line{Text: "LINKS", IsTitle: true})
		for i, href := range links {
			txt, ok := hrefToText[href]
			if !ok {
				txt = href
			}
			for j, c := range SplitWrap(txt, 70) {
				ln := Line{Text: c}
				if j == 0 {
					k := i
					ln.LinkIndex = &k
				}
				lines = append(lines, ln)
			}
		}
	}
	return lines
}

// resolveURL joins a possibly-relative href against a page URL (simplified).
func resolveURL(base, ref string) string {
	if base == "" {
		return ref
	}
	i := strings.Index(base, "://")
	if i < 0 {
		return ref
	}
	if strings.HasPrefix(ref, "/") {
		rest := base[i+3:]
		j := strings.Index(rest, "/")
		if j < 0 {
			return base + ref
		}
		return base[:i+3+j] + ref
	}
	k := strings.LastIndex(base, "/")
	if k < i+3 {
		k = len(base) - 1
	}
	return base[:k+1] + ref
}

// ---------------------------------------------------------------------------
// PNG writer (RGB8, filter-0 scanlines)
// ---------------------------------------------------------------------------

type rgb struct{ r, g, b uint8 }

var (
	white = rgb{255, 255, 255}
	black = rgb{24, 24, 24}
	blue  = rgb{26, 74, 153}
	navy  = rgb{9, 47, 107}
	linkBG = rgb{220, 232, 255}
)

type image struct {
	w, h int
	data []rgb
}

func newImage(w, h int, bg rgb) *image {
	d := make([]rgb, w*h)
	for i := range d {
		d[i] = bg
	}
	return &image{w, h, d}
}

func (im *image) fill(x0, y0, x1, y1 int, c rgb) {
	if x0 < 0 {
		x0 = 0
	}
	if y0 < 0 {
		y0 = 0
	}
	if x1 > im.w {
		x1 = im.w
	}
	if y1 > im.h {
		y1 = im.h
	}
	for y := y0; y < y1; y++ {
		for x := x0; x < x1; x++ {
			im.data[y*im.w+x] = c
		}
	}
}

func (im *image) drawText(x, y int, text string, c rgb, scale int) int {
	cx := x
	for _, ch := range text {
		cols := glyphFor(ch)
		for ci, bits := range cols {
			for ry := 0; ry < 7; ry++ {
				if bits>>uint(ry)&1 != 1 {
					continue
				}
				im.fill(cx+ci*scale, y+ry*scale, cx+ci*scale+scale, y+ry*scale+scale, c)
			}
		}
		cx += 6 * scale
	}
	return cx
}

func pngChunk(buf *bytes.Buffer, tag string, data []byte) {
	var lenbuf [4]byte
	lenbuf[0] = byte(len(data) >> 24)
	lenbuf[1] = byte(len(data) >> 16)
	lenbuf[2] = byte(len(data) >> 8)
	lenbuf[3] = byte(len(data))
	buf.Write(lenbuf[:])
	tb := []byte(tag)
	buf.Write(tb)
	buf.Write(data)
	crc := crc32.NewIEEE()
	crc.Write(tb)
	crc.Write(data)
	sum := crc.Sum32()
	buf.Write([]byte{byte(sum >> 24), byte(sum >> 16), byte(sum >> 8), byte(sum)})
}

// PNG encodes the image as PNG bytes.
func (im *image) PNG() []byte {
	var idat bytes.Buffer
	zw := zlib.NewWriter(&idat)
	row := make([]byte, im.w*3+1)
	for y := 0; y < im.h; y++ {
		row[0] = 0
		for x := 0; x < im.w; x++ {
			p := im.data[y*im.w+x]
			row[1+x*3] = p.r
			row[1+x*3+1] = p.g
			row[1+x*3+2] = p.b
		}
		zw.Write(row)
	}
	zw.Close()

	var out bytes.Buffer
	out.Write([]byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'})
	ihdr := []byte{
		byte(im.w >> 24), byte(im.w >> 16), byte(im.w >> 8), byte(im.w),
		byte(im.h >> 24), byte(im.h >> 16), byte(im.h >> 8), byte(im.h),
		8, 2, 0, 0, 0,
	}
	pngChunk(&out, "IHDR", ihdr)
	pngChunk(&out, "IDAT", idat.Bytes())
	pngChunk(&out, "IEND", nil)
	return out.Bytes()
}

// Screenshot renders the page text to PNG bytes. Content snapshot, not a
// layout render (hjs has no layout engine by design). scale ~1..3.
func Screenshot(p Page, scale int) []byte {
	if scale < 1 {
		scale = 2
	}
	lines := PageLines(p)
	lineH := 7*scale + scale
	pad := scale * 4
	width := (wordsPerLine*6 + 8) * scale
	if width > 2400 {
		width = 2400
	}
	height := pad*2 + lineH*len(lines)
	if height < 8 {
		height = 8
	}
	im := newImage(width, height, white)
	y := pad
	for _, ln := range lines {
		c := black
		if ln.LinkIndex != nil {
			c = blue
		} else if ln.IsTitle {
			c = navy
		}
		if ln.LinkIndex != nil {
			im.fill(0, y-scale, width, y+7*scale+scale, linkBG)
		}
		im.drawText(pad, y, ln.Text, c, scale)
		if ln.IsTitle {
			y += lineH / 2
		}
		y += lineH
	}
	return im.PNG()
}

// ---------------------------------------------------------------------------
// PDF writer
// ---------------------------------------------------------------------------

func pdfEscape(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		ch := s[i]
		if ch == '(' || ch == ')' || ch == '\\' {
			b.WriteByte('\\')
		}
		b.WriteByte(ch)
	}
	return b.String()
}

// PDF paginates the page text into a valid uncompressed PDF 1.4 (Helvetica).
func PDF(p Page, rowsPerPage int) []byte {
	if rowsPerPage <= 0 {
		rowsPerPage = 56
	}
	var texts []string
	for _, ln := range PageLines(p) {
		texts = append(texts, ln.Text)
	}
	var pages [][]string
	cur := []string{}
	for _, t := range texts {
		cur = append(cur, t)
		if len(cur) >= rowsPerPage {
			pages = append(pages, cur)
			cur = []string{}
		}
	}
	if len(cur) > 0 {
		pages = append(pages, cur)
	}
	if len(pages) == 0 {
		pages = [][]string{{""}}
	}
	n := len(pages)
	pagesID := 2
	fontID := 3 + 2*n + 1
	var objs []string
	objs = append(objs, fmt.Sprintf("<< /Type /Catalog /Pages %d 0 R >>", pagesID))
	kids := make([]string, n)
	for i := 0; i < n; i++ {
		kids[i] = fmt.Sprintf("%d 0 R", 4+2*i)
	}
	objs = append(objs, fmt.Sprintf("<< /Type /Pages /Count %d /Kids [%s] >>", n, strings.Join(kids, " ")))
	for i := 0; i < n; i++ {
		var sb strings.Builder
		sb.WriteString("BT\n/F1 9 Tf\n12 TL\n50 780 Td\n")
		for _, t := range pages[i] {
			sb.WriteString(fmt.Sprintf("(%s) Tj T*\n", pdfEscape(t)))
		}
		sb.WriteString("ET")
		stream := sb.String()
		cid := 3 + 2*i
		objs = append(objs, fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(stream), stream))
		objs = append(objs, fmt.Sprintf(
			"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "+
				"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>",
			pagesID, fontID, cid))
	}
	objs = append(objs, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

	var out bytes.Buffer
	out.WriteString("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
	offsets := []int{0}
	for num, body := range objs {
		offsets = append(offsets, out.Len())
		fmt.Fprintf(&out, "%d 0 obj\n%s\nendobj\n", num+1, body)
	}
	xref := out.Len()
	fmt.Fprintf(&out, "xref\n0 %d\n", len(objs)+1)
	out.WriteString("0000000000 65535 f \n")
	for _, off := range offsets[1:] {
		fmt.Fprintf(&out, "%010d 00000 n \n", off)
	}
	fmt.Fprintf(&out, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n",
		len(objs)+1, xref)
	return out.Bytes()
}

// ---------------------------------------------------------------------------
// print + reader
// ---------------------------------------------------------------------------

var blankRe = regexp.MustCompile(`\n\s*\n`)

// PrintText returns paginated plain text (form-feed separated).
func PrintText(p Page, cols, rows int) string {
	if cols <= 0 {
		cols = 100
	}
	if rows <= 0 {
		rows = 50
	}
	all := SplitWrap(p.GetText(), cols)
	var pages []string
	for i := 0; i < len(all); i += rows {
		end := i + rows
		if end > len(all) {
			end = len(all)
		}
		pages = append(pages, strings.Join(all[i:end], "\n"))
	}
	if len(pages) == 0 {
		pages = []string{""}
	}
	return strings.Join(pages, "\f")
}

// Reader extracts the main prose with a density heuristic (readability-lite).
func Reader(p Page) string {
	var paras []string
	for _, block := range blankRe.Split(p.GetText(), -1) {
		for _, one := range strings.Split(block, "\n") {
			t := strings.TrimSpace(one)
			if t != "" {
				paras = append(paras, t)
			}
		}
	}
	if len(paras) == 0 {
		return p.GetText()
	}
	type scored struct {
		s int
		t string
	}
	var sc []scored
	for _, t := range paras {
		words := len(wordSplit.Split(strings.TrimSpace(t), -1))
		punct := strings.Count(t, ".") + strings.Count(t, ",") + strings.Count(t, ";")
		sc = append(sc, scored{words + 2*punct, t})
	}
	sort.SliceStable(sc, func(i, j int) bool { return sc[i].s > sc[j].s })
	var keep []string
	for _, s := range sc {
		if s.s >= 40 {
			keep = append(keep, s.t)
		}
	}
	if len(keep) == 0 {
		keep = []string{sc[0].t}
	}
	return strings.Join(keep, "\n\n")
}

// ---------------------------------------------------------------------------
// Viewer: scroll + touch
// ---------------------------------------------------------------------------

// Viewer is a virtual scroll position over PageLines with link hit-testing.
type Viewer struct {
	P     Page
	Lines []Line
	Rows  int
	top   int
}

// NewViewer builds a viewer; viewportRows <= 0 defaults to 24.
func NewViewer(p Page, viewportRows int) *Viewer {
	if viewportRows <= 0 {
		viewportRows = 24
	}
	return &Viewer{P: p, Lines: PageLines(p), Rows: viewportRows}
}

// Total rows in the model.
func (v *Viewer) Total() int { return len(v.Lines) }

// Top is the first visible row.
func (v *Viewer) Top() int { return v.top }

func (v *Viewer) clamp(t int) int {
	maxTop := v.Total() - v.Rows
	if maxTop < 0 {
		maxTop = 0
	}
	if t < 0 {
		t = 0
	}
	if t > maxTop {
		t = maxTop
	}
	return t
}

// Scroll moves the viewport by delta rows and returns visible text.
func (v *Viewer) Scroll(delta int) []string {
	v.top = v.clamp(v.top + delta)
	return v.Visible()
}

// ScrollTo jumps to an absolute row.
func (v *Viewer) ScrollTo(row int) []string {
	v.top = v.clamp(row)
	return v.Visible()
}

// ScrollToFraction jumps by a 0..1 position (1.0 = bottom swipe).
func (v *Viewer) ScrollToFraction(frac float64) []string {
	if frac < 0 {
		frac = 0
	}
	if frac > 1 {
		frac = 1
	}
	return v.ScrollTo(int(frac * float64(v.Total()-1)))
}

// Visible returns the text rows currently in the viewport.
func (v *Viewer) Visible() []string {
	end := v.top + v.Rows
	if end > v.Total() {
		end = v.Total()
	}
	var out []string
	for _, ln := range v.Lines[v.top:end] {
		out = append(out, ln.Text)
	}
	return out
}

// Tap hit-tests an absolute row; returns the link URL or "".
func (v *Viewer) Tap(row int) string {
	if row < 0 || row >= v.Total() {
		return ""
	}
	ln := v.Lines[row]
	if ln.LinkIndex != nil {
		links := v.P.GetLinks()
		if *ln.LinkIndex < len(links) {
			return links[*ln.LinkIndex]
		}
	}
	return ""
}

// TapVisible hit-tests a coordinate inside the viewport (0 = first row).
func (v *Viewer) TapVisible(y int) string {
	if y < 0 || y >= v.Rows {
		return ""
	}
	return v.Tap(v.top + y)
}

// TapText scrolls to the first line containing the needle and returns its
// link URL when the line is a link ("" otherwise).
func (v *Viewer) TapText(needle string) string {
	low := strings.ToLower(needle)
	for i, ln := range v.Lines {
		if strings.Contains(strings.ToLower(ln.Text), low) {
			v.ScrollTo(i)
			if ln.LinkIndex != nil {
				return v.Tap(i)
			}
			return ""
		}
	}
	return ""
}
