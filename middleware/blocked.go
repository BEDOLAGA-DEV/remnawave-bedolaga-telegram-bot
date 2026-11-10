package main

import (
	"encoding/base64"
	"fmt"
	"net"
	"strconv"
	"strings"
)

// blockedTemplates maps the path format suffix to (Content-Type, body) for the
// synthetic "blocked" profile. Unknown suffixes fall back to the base entry.
var blockedTemplates = map[string][2]string{
	"":               {"application/json; charset=utf-8", ""},
	"json":           {"application/json; charset=utf-8", "[]"},
	"v2ray-json":     {"application/json; charset=utf-8", `{"log":{},"outbounds":[{"tag":"blocked","protocol":"freedom","settings":{}}]}`},
	"stash":          {"application/yaml", "proxies: []\nproxy-groups: []\nrules: []\n"},
	"singbox":        {"application/json", `{"log":{"level":"warning"},"outbounds":[{"tag":"blocked","type":"block"}]}`},
	"singbox-legacy": {"application/json", `{"log":{"level":"warning"},"outbounds":[{"tag":"blocked","type":"block"}]}`},
	"mihomo":         {"application/yaml", "proxies: []\nproxy-groups: []\nrules: []\n"},
	"clash":          {"application/yaml", "proxies: []\nproxy-groups: []\nrules: []\n"},
}

var blockedProfileTitle = "base64:" + base64.StdEncoding.EncodeToString([]byte("Subscription"))

// pathSegments splits a request path into its non-empty segments.
func pathSegments(p string) []string {
	var segs []string
	for _, s := range strings.Split(strings.Trim(p, "/"), "/") {
		if s != "" {
			segs = append(segs, s)
		}
	}
	return segs
}

// isFormat reports whether s is a known profile format suffix (the trailing
// segment some clients append to select a config format, e.g. /json, /clash).
func isFormat(s string) bool {
	s = strings.ToLower(s)
	if s == "" {
		return false
	}
	_, ok := blockedTemplates[s]
	return ok
}

// pathKey extracts the subscription short UUID from the request path. The key is
// the LAST path segment, except when the path ends with a known format suffix
// (/{key}/json, /sub/{key}/clash, ...) — then it is the segment before it.
// This works with arbitrary prefix paths such as /sub/{key} or /smth/smth2/{key};
// the only invariant is that the key is the last (or second-to-last) segment.
// This is what scopes a (hwid, short_uuid) ban: a device banned on one
// subscription key can still connect on another.
func pathKey(p string) string {
	segs := pathSegments(p)
	if len(segs) == 0 {
		return ""
	}
	if len(segs) >= 2 && isFormat(segs[len(segs)-1]) {
		return segs[len(segs)-2]
	}
	return segs[len(segs)-1]
}

// pathFormat extracts the profile format suffix from the last path segment
// (e.g. /sub/{key}/json -> "json"); "" when the last segment is not a known
// format (so a bare /{key} or /sub/{key} yields no format).
func pathFormat(p string) string {
	segs := pathSegments(p)
	if len(segs) == 0 {
		return ""
	}
	if f := strings.ToLower(segs[len(segs)-1]); isFormat(f) {
		return f
	}
	return ""
}

// writeBlockedResponse renders the synthetic 200 "blocked profile" response.
func writeBlockedResponse(c net.Conn, req *rawRequest, message string, version int64) {
	hwid := normHwid(req.header("x-hwid"))
	hwid8 := hwid
	if len(hwid8) > 8 {
		hwid8 = hwid8[:8]
	}
	t := blockedTemplates[""]
	contentType, body := t[0], t[1]
	if v, ok := blockedTemplates[pathFormat(req.path())]; ok {
		contentType, body = v[0], v[1]
	}
	if strings.TrimSpace(message) == "" {
		message = "Device disabled by account owner."
	}

	writeRaw(c, 200, "OK", [][2]string{
		{"Content-Type", contentType},
		{"profile-title", blockedProfileTitle},
		{"profile-update-interval", "12"},
		{"subscription-userinfo", "upload=0; download=0; total=0; expire=0"},
		{"announce", "base64:" + base64.StdEncoding.EncodeToString([]byte(message))},
		{"etag", fmt.Sprintf(`W/"blocked-%d-%s"`, version, hwid8)},
		{"cache-control", "no-cache"},
		{"content-length", strconv.Itoa(len(body))},
		{"connection", "close"},
	}, []byte(body))
}
