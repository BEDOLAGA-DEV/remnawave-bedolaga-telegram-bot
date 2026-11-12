package main

import (
	"encoding/base64"
	"encoding/json"
	"net/url"
	"strconv"
	"strings"
)

// vlessServer holds a parsed vless:// link so the synthetic blocked profile can
// carry a single, connectable server instead of an empty list (some clients
// refuse to load a profile that contains no servers).
type vlessServer struct {
	raw           string
	name          string
	uuid          string
	server        string
	port          int
	network       string // ?type= tcp|ws|grpc
	security      string // ?security= none|tls|reality
	sni           string // ?sni=
	fp            string // ?fp= (utls fingerprint)
	pbk           string // ?pbk= (reality public key)
	sid           string // ?sid= (reality short id)
	spx           string // ?spx= (reality spider path)
	path          string // ?path= (ws/grpc path or grpc service name)
	host          string // ?host= (ws Host header)
	allowInsecure string // ?allowInsecure=
}

// parseVlessServer parses a vless://<uuid>@<server>:<port>?<query>#<name> link.
// It is defensive: any unparseable input still yields a struct with the raw
// link and safe defaults so a blocked profile can always be rendered.
func parseVlessServer(link string) *vlessServer {
	s := &vlessServer{raw: link, name: "blocked"}
	u, err := url.Parse(link)
	if err != nil || u.Host == "" {
		return s
	}
	if u.User != nil {
		s.uuid = u.User.Username()
	}
	s.server = u.Hostname()
	if p := u.Port(); p != "" {
		if n, err := strconv.Atoi(p); err == nil {
			s.port = n
		}
	}
	q := u.Query()
	s.network = strings.ToLower(q.Get("type"))
	s.security = strings.ToLower(q.Get("security"))
	s.sni = q.Get("sni")
	s.fp = strings.ToLower(q.Get("fp"))
	s.pbk = q.Get("pbk")
	s.sid = q.Get("sid")
	s.spx = q.Get("spx")
	s.path = q.Get("path")
	s.host = q.Get("host")
	s.allowInsecure = q.Get("allowInsecure")
	if n := strings.Trim(u.Fragment, "#"); n != "" {
		s.name = n
	}
	if s.port == 0 {
		s.port = 443
	}
	if s.server == "" {
		s.server = "127.0.0.1"
	}
	return s
}

// tlsEnabled reports whether the link uses TLS or REALITY transport security.
func (s *vlessServer) tlsEnabled() bool {
	return s.security == "tls" || s.security == "reality"
}

// allowInsecureTrue reports whether the link disables certificate validation.
func (s *vlessServer) allowInsecureTrue() bool {
	return s.allowInsecure == "1" || strings.EqualFold(s.allowInsecure, "true")
}

// toLinkArray renders the standard subscription list format: a JSON array of
// base64-encoded profile links (used by the base /{key} and /{key}/json paths).
func (s *vlessServer) toLinkArray() string {
	b64 := base64.StdEncoding.EncodeToString([]byte(s.raw))
	return `["` + b64 + `"]`
}

// toMihomoYAML renders a Mihomo / Clash Meta profile with a single vless proxy.
func (s *vlessServer) toMihomoYAML() string {
	var b strings.Builder
	net := s.network
	if net == "" {
		net = "tcp"
	}
	b.WriteString("proxies:\n")
	b.WriteString("  - name: " + yamlScalar(s.name) + "\n")
	b.WriteString("    type: vless\n")
	b.WriteString("    server: " + s.server + "\n")
	b.WriteString("    port: " + strconv.Itoa(s.port) + "\n")
	b.WriteString("    uuid: " + s.uuid + "\n")
	b.WriteString("    network: " + net + "\n")
	switch net {
	case "ws":
		b.WriteString("    ws-opts:\n")
		if s.path != "" {
			b.WriteString("      path: " + yamlScalar(s.path) + "\n")
		}
		if s.host != "" {
			b.WriteString("      headers:\n        Host: " + s.host + "\n")
		}
	case "grpc":
		b.WriteString("    grpc-opts:\n")
		b.WriteString("      grpc-service-name: " + yamlScalar(s.path) + "\n")
	}
	if s.tlsEnabled() {
		b.WriteString("    tls:\n")
		b.WriteString("      enabled: true\n")
		if s.sni != "" {
			b.WriteString("      server-name: " + s.sni + "\n")
		}
		if s.allowInsecureTrue() {
			b.WriteString("      allow-insecure: true\n")
		}
		if s.security == "reality" {
			if s.fp != "" {
				b.WriteString("      utls:\n        enabled: true\n        fingerprint: " + s.fp + "\n")
			}
			b.WriteString("      reality-opts:\n")
			if s.pbk != "" {
				b.WriteString("        public-key: " + s.pbk + "\n")
			}
			if s.sid != "" {
				b.WriteString("        short-id: " + s.sid + "\n")
			}
			b.WriteString("    flow: xtls-rprx-vision\n")
		}
	}
	b.WriteString("proxy-groups:\n")
	b.WriteString("  - name: PROXY\n")
	b.WriteString("    type: select\n")
	b.WriteString("    proxies:\n")
	b.WriteString("      - " + yamlScalar(s.name) + "\n")
	b.WriteString("rules:\n")
	b.WriteString("  - MATCH,PROXY\n")
	return b.String()
}

// toV2RayJSON renders a v2ray-core profile with a single vless outbound.
func (s *vlessServer) toV2RayJSON() string {
	user := map[string]any{"id": s.uuid, "encryption": "none"}
	if s.security == "reality" {
		user["flow"] = "xtls-rprx-vision"
	}
	vnext := map[string]any{
		"address": s.server,
		"port":    s.port,
		"users":   []any{user},
	}
	net := s.network
	if net == "" {
		net = "tcp"
	}
	stream := map[string]any{"network": net}
	switch s.security {
	case "tls":
		stream["security"] = "tls"
		tls := map[string]any{}
		if s.sni != "" {
			tls["serverName"] = s.sni
		}
		if s.allowInsecureTrue() {
			tls["allowInsecure"] = true
		}
		stream["tlsSettings"] = tls
	case "reality":
		stream["security"] = "reality"
		real := map[string]any{"show": false}
		if s.sni != "" {
			real["serverName"] = s.sni
		}
		if s.fp != "" {
			real["fingerprint"] = s.fp
		}
		if s.pbk != "" {
			real["publicKey"] = s.pbk
		}
		if s.spx != "" {
			real["spiderX"] = s.spx
		}
		stream["realitySettings"] = real
	}
	switch net {
	case "ws":
		ws := map[string]any{}
		if s.path != "" {
			ws["path"] = s.path
		}
		if s.host != "" {
			ws["headers"] = map[string]any{"Host": s.host}
		}
		stream["wsSettings"] = ws
	case "grpc":
		stream["grpcSettings"] = map[string]any{"serviceName": s.path}
	}
	out := map[string]any{
		"tag":            "blocked",
		"protocol":       "vless",
		"settings":       map[string]any{"vnext": []any{vnext}},
		"streamSettings": stream,
	}
	payload, err := json.Marshal(map[string]any{
		"log":       map[string]any{"loglevel": "warning"},
		"outbounds": []any{out},
	})
	if err != nil {
		return `{"log":{},"outbounds":[]}`
	}
	return string(payload)
}

// toSingboxJSON renders a sing-box profile with a single vless outbound.
func (s *vlessServer) toSingboxJSON() string {
	out := map[string]any{
		"tag":         "blocked",
		"type":        "vless",
		"server":      s.server,
		"server_port": s.port,
		"uuid":        s.uuid,
	}
	if s.tlsEnabled() {
		tls := map[string]any{"enabled": true}
		if s.sni != "" {
			tls["server_name"] = s.sni
		}
		if s.allowInsecureTrue() {
			tls["insecure"] = true
		}
		if s.security == "reality" {
			if s.fp != "" {
				tls["utls"] = map[string]any{"enabled": true, "fingerprint": s.fp}
			}
			reality := map[string]any{"enabled": true}
			if s.pbk != "" {
				reality["public_key"] = s.pbk
			}
			if s.sid != "" {
				reality["short_id"] = s.sid
			}
			tls["reality"] = reality
		}
		out["tls"] = tls
	}
	if s.network == "ws" {
		ws := map[string]any{}
		if s.path != "" {
			ws["path"] = s.path
		}
		if s.host != "" {
			ws["headers"] = map[string]any{"Host": s.host}
		}
		out["ws"] = ws
	}
	payload, err := json.Marshal(map[string]any{
		"log":       map[string]any{"level": "warning"},
		"outbounds": []any{out},
	})
	if err != nil {
		return `{"log":{},"outbounds":[]}`
	}
	return string(payload)
}

// renderBlockedBody returns (contentType, body) for the synthetic blocked
// profile. When fallbackLink is a valid vless:// link the body carries exactly
// one server (that link) so clients that reject an empty profile still load a
// valid, connectable config. Otherwise the legacy empty-list templates are kept.
func renderBlockedBody(format, fallbackLink string) (string, string) {
	fallbackLink = strings.TrimSpace(fallbackLink)
	if !strings.HasPrefix(fallbackLink, "vless://") {
		t := blockedTemplates[""]
		contentType, body := t[0], t[1]
		if v, ok := blockedTemplates[format]; ok {
			contentType, body = v[0], v[1]
		}
		return contentType, body
	}
	s := parseVlessServer(fallbackLink)
	switch format {
	case "clash", "stash", "mihomo":
		return "application/yaml", s.toMihomoYAML()
	case "v2ray-json":
		return "application/json", s.toV2RayJSON()
	case "singbox", "singbox-legacy":
		return "application/json", s.toSingboxJSON()
	default: // "" and "json"
		return "application/json; charset=utf-8", s.toLinkArray()
	}
}

// yamlScalar renders a string as a bare YAML scalar when safe, otherwise as a
// double-quoted scalar (a JSON-quoted string is a valid YAML double-quoted one).
func yamlScalar(v string) string {
	if v == "" {
		return `""`
	}
	if !strings.ContainsAny(v, ":#{}[]&*!|>'\"%@`, \t\n-") {
		return v
	}
	b, _ := json.Marshal(v)
	return string(b)
}
