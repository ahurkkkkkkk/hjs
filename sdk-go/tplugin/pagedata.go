package tplugin

// PageData is a plain-struct Page implementation built from extracted values.
// Feed it straight from an hjs page struct or from your own data.
type PageData struct {
	URL_     string
	Title_   string
	Text_    string
	Links_   []string
	Anchors_ []Link
}

// GetURL returns the page URL.
func (p *PageData) GetURL() string { return p.URL_ }

// GetTitle returns the page title.
func (p *PageData) GetTitle() string { return p.Title_ }

// GetText returns the extracted readable text.
func (p *PageData) GetText() string { return p.Text_ }

// GetLinks returns resolved link URLs in document order.
func (p *PageData) GetLinks() []string { return p.Links_ }

// GetAnchors returns anchors (href + text) for tap hit-testing labels.
func (p *PageData) GetAnchors() []Link { return p.Anchors_ }

var _ Page = (*PageData)(nil)
