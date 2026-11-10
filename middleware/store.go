package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ErrStale is returned when a pushed state has a lower version than the stored one.
var ErrStale = errors.New("stale_version")

type StateDevice struct {
	Hwid      string `json:"hwid"`
	UserID    int64  `json:"user_id"`
	ShortUUID string `json:"short_uuid"` // Remnawave subscription key; empty = global ban
	Message   string `json:"message"`
	BannedAt  string `json:"banned_at"`
}

type State struct {
	Version   int64         `json:"version"`
	UpdatedAt string        `json:"updated_at"`
	Devices   []StateDevice `json:"devices"`
}

// blockKey scopes a ban to (normalized hwid, short UUID). An empty shortUUID
// means the ban is global (matches any subscription) — legacy rows and admin
// raw-hwid bans use this.
type blockKey struct {
	hwid      string
	shortUUID string
}

type blockInfo struct {
	message string
	userID  int64
}

// Store keeps the ban-list in memory (O(1) lookup) and persists it to a JSON file.
// The file is written by the middleware itself (atomic tmp+fsync+rename).
type Store struct {
	mu       sync.RWMutex
	file     string
	botURL   string
	botToken string
	version  int64
	blocked  map[blockKey]blockInfo
}

func NewStore(cfg Config) (*Store, error) {
	if cfg.StateFile == "" {
		return nil, errors.New("STATE_FILE is empty")
	}
	return &Store{
		file:     cfg.StateFile,
		botURL:   cfg.BotAPIURL,
		botToken: cfg.BotAPIToken,
		blocked:  map[blockKey]blockInfo{},
	}, nil
}

func normHwid(h string) string {
	return strings.ToLower(strings.TrimSpace(h))
}

// Load reads and applies the state file. A missing file yields an empty state.
func (s *Store) Load() (*State, error) {
	data, err := os.ReadFile(s.file)
	if err != nil {
		if os.IsNotExist(err) {
			return &State{Devices: []StateDevice{}}, nil
		}
		return nil, err
	}
	var st State
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, fmt.Errorf("corrupt state file: %w", err)
	}
	if st.Devices == nil {
		st.Devices = []StateDevice{}
	}
	s.mu.Lock()
	s.applyLocked(&st)
	s.mu.Unlock()
	return &st, nil
}

func (s *Store) applyLocked(st *State) {
	s.version = st.Version
	m := make(map[blockKey]blockInfo, len(st.Devices))
	for _, d := range st.Devices {
		n := normHwid(d.Hwid)
		if n == "" {
			continue
		}
		msg := d.Message
		if strings.TrimSpace(msg) == "" {
			msg = "Device disabled by account owner."
		}
		m[blockKey{hwid: n, shortUUID: strings.TrimSpace(d.ShortUUID)}] = blockInfo{message: msg, userID: d.UserID}
	}
	s.blocked = m
}

// ApplyState applies a new state if its version is >= the stored one (idempotent re-push
// of the same version is accepted). Returns (applied, error).
func (s *Store) ApplyState(st *State) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if st.Version < s.version {
		return false, ErrStale
	}
	s.applyLocked(st)
	return true, nil
}

// Save persists the state atomically: tmp file in the same dir -> fsync -> rename.
func (s *Store) Save(st *State) error {
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	dir := filepath.Dir(s.file)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".state-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return err
	}
	return os.Rename(tmpName, s.file)
}

// BootstrapPull fetches the current state from the bot once (startup recovery).
// Any failure is returned and logged by the caller; the store keeps its (possibly
// empty) state — fail-open.
func (s *Store) BootstrapPull() error {
	if s.botURL == "" || s.botToken == "" {
		return errors.New("bot api not configured")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.botURL+"/device-blocks", nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", s.botToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("bot api: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 10<<20))
	if err != nil {
		return err
	}
	var st State
	if err := json.Unmarshal(body, &st); err != nil {
		return fmt.Errorf("bot api: bad payload: %w", err)
	}
	if st.Devices == nil {
		st.Devices = []StateDevice{}
	}
	applied, err := s.ApplyState(&st)
	if err != nil {
		return err
	}
	if applied {
		if err := s.Save(&st); err != nil {
			log.Printf("WARN: bootstrap pull applied but persist failed: %v", err)
		} else {
			log.Printf("bootstrap pull: version=%d devices=%d", st.Version, len(st.Devices))
		}
	}
	return nil
}

func (s *Store) Version() int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.version
}

func (s *Store) Count() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.blocked)
}

// Lookup returns the announce message and user id for (hwid, shortUUID) if it is
// blocked. A block stored with an empty shortUUID is global and matches any
// shortUUID (legacy rows + admin raw-hwid bans).
func (s *Store) Lookup(hwid string, shortUUID string) (message string, userID int64, ok bool) {
	n := normHwid(hwid)
	s.mu.RLock()
	defer s.mu.RUnlock()
	if info, found := s.blocked[blockKey{hwid: n, shortUUID: strings.TrimSpace(shortUUID)}]; found {
		return info.message, info.userID, true
	}
	if info, found := s.blocked[blockKey{hwid: n, shortUUID: ""}]; found {
		return info.message, info.userID, true
	}
	return "", 0, false
}
