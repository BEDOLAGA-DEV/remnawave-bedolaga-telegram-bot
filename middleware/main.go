package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Config struct {
	ListenAddr    string
	BotListenAddr string
	UpstreamURL   string
	StateFile     string
	AdminToken    string
	BotAPIURL     string
	BotAPIToken   string
	FailOpen      bool
}

type Server struct {
	cfg          Config
	upstreamHost string
	store        *Store
	started      time.Time
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envBool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		log.Printf("WARN: invalid bool for %s=%q, using %v", key, v, def)
		return def
	}
	return b
}

func loadConfig() (Config, error) {
	upstream := strings.TrimRight(env("UPSTREAM_URL", "http://127.0.0.1:3010"), "/")
	u, err := url.Parse(upstream)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return Config{}, errors.New("invalid UPSTREAM_URL: " + upstream)
	}
	cfg := Config{
		ListenAddr:    env("LISTEN_ADDR", "127.0.0.1:3020"),
		BotListenAddr: env("BOT_LISTEN_ADDR", ""),
		UpstreamURL:   upstream,
		StateFile:     env("STATE_FILE", "/data/state.json"),
		AdminToken:    os.Getenv("ADMIN_TOKEN"),
		BotAPIURL:     strings.TrimRight(env("BOT_API_URL", "http://127.0.0.1:8080"), "/"),
		BotAPIToken:   os.Getenv("BOT_API_TOKEN"),
		FailOpen:      envBool("FAIL_OPEN", true),
	}
	return cfg, nil
}

func logf(format string, args ...any) {
	log.Printf(format, args...)
}

func (s *Server) checkAdmin(req *rawRequest) bool {
	if s.cfg.AdminToken == "" {
		return false
	}
	return subtle.ConstantTimeCompare(
		[]byte(req.header("X-Admin-Token")),
		[]byte(s.cfg.AdminToken),
	) == 1
}

func (s *Server) serveHealth(c net.Conn) {
	payload, _ := json.Marshal(map[string]any{
		"ok":       true,
		"version":  s.store.Version(),
		"count":    s.store.Count(),
		"uptime_s": int64(time.Since(s.started).Seconds()),
	})
	writeRaw(c, 200, "OK", [][2]string{
		{"Content-Type", "application/json; charset=utf-8"},
		{"Content-Length", strconv.Itoa(len(payload))},
		{"Connection", "close"},
	}, payload)
}

func (s *Server) servePutState(c net.Conn, req *rawRequest) {
	if !s.checkAdmin(req) {
		writeRawJSON(c, 401, map[string]string{"error": "unauthorized"})
		return
	}
	body, err := readBody(c, req)
	if err != nil {
		writeRawJSON(c, 400, map[string]string{"error": "read_body"})
		return
	}
	var st State
	if err := json.Unmarshal(body, &st); err != nil {
		writeRawJSON(c, 400, map[string]string{"error": "bad_json"})
		return
	}
	if st.Devices == nil {
		st.Devices = []StateDevice{}
	}
	for i, d := range st.Devices {
		if normHwid(d.Hwid) == "" {
			writeRawJSON(c, 400, map[string]string{"error": "devices[" + strconv.Itoa(i) + "].hwid is empty"})
			return
		}
	}
	if strings.TrimSpace(st.UpdatedAt) == "" {
		st.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	}

	applied, err := s.store.ApplyState(&st)
	if err != nil {
		writeRawJSON(c, 409, map[string]string{"error": "stale_version"})
		return
	}
	if applied {
		if err := s.store.Save(&st); err != nil {
			logf("ERROR: state applied in memory but persist failed: %v", err)
			writeRawJSON(c, 500, map[string]string{"error": "persist_failed"})
			return
		}
	}
	logf("state push: version=%d devices=%d applied=%v", st.Version, len(st.Devices), applied)
	writeRawJSON(c, 200, map[string]any{
		"ok":      true,
		"version": st.Version,
		"count":   s.store.Count(),
	})
}

func writeRawJSON(c net.Conn, code int, v any) {
	payload, _ := json.Marshal(v)
	text := "OK"
	switch code {
	case 400:
		text = "Bad Request"
	case 401:
		text = "Unauthorized"
	case 409:
		text = "Conflict"
	case 500:
		text = "Internal Server Error"
	}
	writeRaw(c, code, text, [][2]string{
		{"Content-Type", "application/json; charset=utf-8"},
		{"Content-Length", strconv.Itoa(len(payload))},
		{"Connection", "close"},
	}, payload)
}

// handleConn serves one client connection: at most one proxied request, then close.
func (s *Server) handleConn(c net.Conn) {
	defer c.Close()

	req, err := readRequest(c)
	if err != nil {
		if errors.Is(err, errHeadersTooLarge) {
			writeRaw(c, 431, "Request Header Fields Too Large", [][2]string{
				{"Content-Length", "0"},
				{"Connection", "close"},
			}, nil)
		} else if err != net.ErrClosed {
			logf("WARN: bad request from %s: %v", c.RemoteAddr(), err)
		}
		return
	}

	if req.method == "GET" && req.path() == "/v1/health" {
		s.serveHealth(c)
		return
	}
	if req.method == "PUT" && req.path() == "/v1/state" {
		s.servePutState(c, req)
		return
	}
	if req.method == "GET" {
		if hwid := req.header("x-hwid"); hwid != "" {
			shortUUID := pathKey(req.path())
			if msg, userID, blocked := s.store.Lookup(hwid, shortUUID); blocked {
				logf("BLOCKED hwid=%s short_uuid=%s user_id=%d path=%s ip=%s",
					normHwid(hwid), shortUUID, userID, req.path(), req.header("x-real-ip"))
				writeBlockedResponse(c, req, msg, s.store.Version())
				return
			}
		}
	}
	s.pipe(c, req)
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	log.Printf("hwid-blocker starting: listen=%s upstream=%s state=%s fail_open=%v",
		cfg.ListenAddr, cfg.UpstreamURL, cfg.StateFile, cfg.FailOpen)

	store, err := NewStore(cfg)
	if err != nil {
		log.Fatalf("state init failed: %v", err)
	}
	if st, err := store.Load(); err != nil {
		if !cfg.FailOpen {
			log.Fatalf("cannot load state file: %v", err)
		}
		log.Printf("WARN: cannot load state file %s: %v (starting empty)", cfg.StateFile, err)
	} else {
		log.Printf("state loaded: version=%d devices=%d", st.Version, len(st.Devices))
	}
	if err := store.BootstrapPull(); err != nil {
		log.Printf("INFO: bootstrap pull skipped: %v", err)
	}

	up, _ := url.Parse(cfg.UpstreamURL)
	srv := &Server{
		cfg:          cfg,
		upstreamHost: up.Host,
		store:        store,
		started:      time.Now(),
	}

	// Primary listener (nginx / proxy path) is fatal on failure.
	// Secondary listener (bot bridge, BOT_LISTEN_ADDR) is best-effort:
	// if its interface is missing the proxy keeps working, the bot's
	// pushes just fail until it is reachable.
	lnAddrs := []struct {
		addr  string
		fatal bool
	}{
		{cfg.ListenAddr, true},
	}
	if cfg.BotListenAddr != "" && cfg.BotListenAddr != cfg.ListenAddr {
		lnAddrs = append(lnAddrs, struct {
			addr  string
			fatal bool
		}{cfg.BotListenAddr, false})
	}

	var lns []net.Listener
	for _, a := range lnAddrs {
		ln, err := net.Listen("tcp", a.addr)
		if err != nil {
			if a.fatal {
				log.Fatalf("listen %s: %v", a.addr, err)
			}
			logf("WARN: listen %s failed: %v (bot pushes unavailable on this interface)", a.addr, err)
			continue
		}
		log.Printf("listening on %s", a.addr)
		lns = append(lns, ln)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var connWG sync.WaitGroup
	acceptErr := make(chan error, len(lns))
	for i := range lns {
		fatal := i == 0
		ln := lns[i]
		go func() {
			for {
				c, err := ln.Accept()
				if err != nil {
					if fatal {
						acceptErr <- err
					}
					return
				}
				connWG.Add(1)
				go func() {
					defer connWG.Done()
					srv.handleConn(c)
				}()
			}
		}()
	}

	select {
	case <-ctx.Done():
		log.Printf("shutting down")
		for _, ln := range lns {
			_ = ln.Close()
		}
	case err := <-acceptErr:
		if !errors.Is(err, net.ErrClosed) {
			log.Fatalf("accept: %v", err)
		}
	}

	done := make(chan struct{})
	go func() {
		connWG.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		log.Printf("WARN: forcing shutdown with in-flight connections")
	}
}
