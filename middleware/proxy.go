package main

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	maxHeaderBytes      = 64 << 10
	maxBodyBytes        = 10 << 20
	headerReadTimeout   = 30 * time.Second
	bodyIdleTimeout     = 30 * time.Second
	upstreamDialTimeout = 3 * time.Second
)

var errHeadersTooLarge = errors.New("request headers too large")

// rawRequest is a parsed HTTP request whose original bytes are kept intact
// (head + whatever body bytes already arrived) so pass-through is byte-exact.
type rawRequest struct {
	method  string
	target  string
	proto   string
	headers map[string][]string // lowercased name -> values
	headLen int                 // length of request line + headers
	raw     []byte              // head + buffered body bytes
}

func (r *rawRequest) header(name string) string {
	if vv, ok := r.headers[strings.ToLower(name)]; ok && len(vv) > 0 {
		return vv[0]
	}
	return ""
}

// path returns the request path without query (absolute-form targets supported).
func (r *rawRequest) path() string {
	t := r.target
	if !strings.HasPrefix(t, "/") && !strings.HasPrefix(t, "*") {
		if u, err := url.Parse(t); err == nil && u.Path != "" {
			t = u.Path
		}
	}
	if i := strings.IndexByte(t, '?'); i >= 0 {
		t = t[:i]
	}
	return t
}

func readRequest(c net.Conn) (*rawRequest, error) {
	_ = c.SetReadDeadline(time.Now().Add(headerReadTimeout))
	br := bufio.NewReaderSize(c, 32*1024)
	var buf []byte
	for {
		if len(buf) > maxHeaderBytes {
			return nil, errHeadersTooLarge
		}
		line, err := br.ReadSlice('\n')
		if len(line) > 0 {
			buf = append(buf, line...)
			// end of headers: last line is an empty line
			if len(buf) >= 2 && (bytes.HasSuffix(buf, []byte("\r\n\r\n")) || bytes.HasSuffix(buf, []byte("\n\n"))) {
				break
			}
		}
		if err != nil {
			if err == bufio.ErrBufferFull {
				continue
			}
			if len(buf) == 0 {
				return nil, err
			}
			break // connection closed mid-headers
		}
	}

	// locate end of headers
	headEnd := -1
	if i := bytes.Index(buf, []byte("\r\n\r\n")); i >= 0 {
		headEnd = i + 4
	} else if i := bytes.Index(buf, []byte("\n\n")); i >= 0 {
		headEnd = i + 2
	}
	if headEnd < 0 {
		return nil, errors.New("incomplete request headers")
	}

	// grab any body bytes already buffered
	var rest []byte
	if n := br.Buffered(); n > 0 {
		b, _ := br.Peek(n)
		rest = append(rest, b...)
	}

	req, err := parseRequest(buf[:headEnd], rest)
	if err != nil {
		return nil, err
	}
	return req, nil
}

func parseRequest(head, rest []byte) (*rawRequest, error) {
	lines := strings.Split(strings.ReplaceAll(string(head), "\r\n", "\n"), "\n")
	if len(lines) < 2 {
		return nil, errors.New("incomplete request line")
	}
	parts := strings.SplitN(lines[0], " ", 3)
	if len(parts) != 3 || !strings.HasPrefix(parts[2], "HTTP/") {
		return nil, errors.New("bad request line")
	}
	method := strings.ToUpper(parts[0])
	if !isToken(method) {
		return nil, errors.New("bad method")
	}
	req := &rawRequest{
		method:  method,
		target:  parts[1],
		proto:   parts[2],
		headers: map[string][]string{},
		headLen: len(head),
	}
	for _, l := range lines[1:] {
		if l == "" {
			continue
		}
		i := strings.IndexByte(l, ':')
		if i <= 0 {
			continue
		}
		name := strings.ToLower(l[:i])
		value := strings.TrimSpace(l[i+1:])
		req.headers[name] = append(req.headers[name], value)
	}
	req.raw = make([]byte, 0, len(head)+len(rest))
	req.raw = append(req.raw, head...)
	req.raw = append(req.raw, rest...)
	return req, nil
}

func isToken(s string) bool {
	for _, c := range s {
		if !(c == '*' || c == '_' || c == '-' || c == '.' ||
			(c >= '!' && c <= '~')) {
			return false
		}
	}
	return len(s) > 0
}

// buildResponse renders a raw HTTP/1.1 response.
func buildResponse(status int, statusText string, headers [][2]string, body []byte) []byte {
	var b bytes.Buffer
	fmt.Fprintf(&b, "HTTP/1.1 %d %s\r\n", status, statusText)
	for _, h := range headers {
		fmt.Fprintf(&b, "%s: %s\r\n", h[0], h[1])
	}
	b.WriteString("\r\n")
	b.Write(body)
	return b.Bytes()
}

func writeRaw(c net.Conn, status int, statusText string, headers [][2]string, body []byte) {
	_, _ = c.Write(buildResponse(status, statusText, headers, body))
}

// pipe streams the already-received request bytes to the upstream and then
// shuttles both directions until the upstream closes the connection.
// The upstream request gets an extra `Connection: close` so the response
// always terminates and the pipe can end cleanly.
// buildUpstreamBytes returns the original request bytes with hop-by-hop
// Connection/Keep-Alive headers removed and a single "Connection: close"
// inserted before the terminating blank line, so the upstream always closes
// after the response and the pipe terminates cleanly.
func buildUpstreamBytes(req *rawRequest) []byte {
	head := req.raw[:req.headLen]
	rest := req.raw[req.headLen:]

	var out []byte
	for _, line := range strings.Split(string(head), "\r\n") {
		if line == "" {
			continue
		}
		if i := strings.IndexByte(line, ':'); i > 0 {
			name := strings.ToLower(strings.TrimSpace(line[:i]))
			if name == "connection" || name == "keep-alive" {
				continue
			}
		}
		out = append(out, line...)
		out = append(out, '\r', '\n')
	}
	out = append(out, "Connection: close\r\n\r\n"...)
	out = append(out, rest...)
	return out
}

func (s *Server) pipe(c net.Conn, req *rawRequest) {
	up, err := net.DialTimeout("tcp", s.upstreamHost, upstreamDialTimeout)
	if err != nil {
		logf("ERROR upstream dial: %v", err)
		writeRaw(c, 502, "Bad Gateway", [][2]string{
			{"Content-Type", "text/plain"},
			{"Content-Length", "9"},
			{"Connection", "close"},
		}, []byte("502 error"))
		return
	}
	defer up.Close()

	upBytes := buildUpstreamBytes(req)
	if _, err := up.Write(upBytes); err != nil {
		logf("ERROR upstream write: %v", err)
		writeRaw(c, 502, "Bad Gateway", [][2]string{
			{"Content-Type", "text/plain"},
			{"Content-Length", "9"},
			{"Connection", "close"},
		}, []byte("502 error"))
		return
	}

	_ = c.SetReadDeadline(time.Time{})
	_ = up.SetReadDeadline(time.Time{})

	done := make(chan struct{})
	go func() {
		idleCopy(up, c, bodyIdleTimeout)
		_ = up.Close()
		close(done)
	}()
	idleCopy(c, up, bodyIdleTimeout)
	_ = c.Close()
	<-done
}

// idleCopy copies src->dst, treating >idle of inactivity as an error.
func idleCopy(dst io.Writer, src net.Conn, idle time.Duration) (int64, error) {
	_ = src.SetReadDeadline(time.Now().Add(idle))
	var buf [32 * 1024]byte
	var n int64
	for {
		nn, rerr := src.Read(buf[:])
		if nn > 0 {
			_ = src.SetReadDeadline(time.Now().Add(idle))
			if _, werr := dst.Write(buf[:nn]); werr != nil {
				return n, werr
			}
			n += int64(nn)
		}
		if rerr != nil {
			return n, rerr
		}
	}
}

// readBody reads the full request body for in-process (admin) requests,
// honoring Content-Length and chunked Transfer-Encoding.
func readBody(c net.Conn, req *rawRequest) ([]byte, error) {
	if e := strings.ToLower(req.header("expect")); strings.Contains(e, "100-continue") {
		_, _ = c.Write([]byte("HTTP/1.1 100 Continue\r\n\r\n"))
	}
	body := req.raw[req.headLen:]
	if cl := req.header("content-length"); cl != "" {
		n, err := strconv.ParseInt(cl, 10, 64)
		if err != nil || n < 0 || n > maxBodyBytes {
			return nil, errors.New("bad content-length")
		}
		_ = c.SetReadDeadline(time.Now().Add(bodyIdleTimeout))
		if int64(len(body)) < n {
			rest, err := io.ReadAll(io.LimitReader(c, n-int64(len(body))))
			if err != nil {
				return nil, err
			}
			body = append(body, rest...)
		}
		return body[:n], nil
	}
	if te := strings.ToLower(req.header("transfer-encoding")); strings.Contains(te, "chunked") {
		return readChunked(c, body)
	}
	return body, nil
}

func readChunked(c net.Conn, initial []byte) ([]byte, error) {
	_ = c.SetReadDeadline(time.Now().Add(bodyIdleTimeout))
	br := bufio.NewReader(io.MultiReader(bytes.NewReader(initial), c))
	var out []byte
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return nil, err
		}
		sizeStr := strings.TrimSpace(strings.SplitN(line, ";", 2)[0])
		size, err := strconv.ParseInt(sizeStr, 16, 64)
		if err != nil || size < 0 {
			return nil, errors.New("bad chunk size")
		}
		if int64(len(out))+size > maxBodyBytes {
			return nil, errors.New("body too large")
		}
		if size == 0 {
			for {
				l, err := br.ReadString('\n')
				if err != nil {
					return nil, err
				}
				if l == "\r\n" || l == "\n" {
					return out, nil
				}
			}
		}
		chunk := make([]byte, size+2)
		if _, err := io.ReadFull(br, chunk); err != nil {
			return nil, err
		}
		out = append(out, chunk[:size]...)
	}
}
