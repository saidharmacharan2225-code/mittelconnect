package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// schema mirrors core/pipeline.py (LocalCache.SCHEMA).
const schema = `
PRAGMA journal_mode=WAL;
CREATE TABLE watermarks (job TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, service_path TEXT NOT NULL,
    entity_set TEXT NOT NULL, record_count INTEGER NOT NULL, payload BLOB NOT NULL,
    encrypted INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    next_attempt_at REAL NOT NULL, last_error TEXT);
CREATE TABLE dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, reason TEXT NOT NULL,
    status INTEGER, payload BLOB NOT NULL, encrypted INTEGER NOT NULL, created_at TEXT NOT NULL);
`

var fixedNow = time.Date(2026, 10, 5, 10, 0, 0, 0, time.UTC)

func newFixture(t *testing.T) (dir string, c *Collector) {
	t.Helper()
	dir = t.TempDir()
	dbPath := filepath.Join(dir, "cache.db")
	db, err := sql.Open("sqlite", "file:"+dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(schema); err != nil {
		t.Fatal(err)
	}
	nowEpoch := float64(fixedNow.Unix())
	stmts := []struct {
		q    string
		args []any
	}{
		{`INSERT INTO outbox (job, service_path, entity_set, record_count, payload, encrypted, attempts, created_at, next_attempt_at)
		  VALUES ('material_stock_sync', '/s', 'E', 100, x'00', 1, 3, '2026-10-05T09:00:00+00:00', ?)`, []any{nowEpoch - 10}},
		{`INSERT INTO outbox (job, service_path, entity_set, record_count, payload, encrypted, attempts, created_at, next_attempt_at)
		  VALUES ('material_stock_sync', '/s', 'E', 40, x'00', 1, 7, '2026-10-05T09:30:00+00:00', ?)`, []any{nowEpoch + 300}},
		{`INSERT INTO dead_letters (job, reason, status, payload, encrypted, created_at)
		  VALUES ('material_stock_sync', 'bad', 400, x'00', 1, '2026-10-05T08:00:00+00:00')`, nil},
		{`INSERT INTO dead_letters (job, reason, status, payload, encrypted, created_at)
		  VALUES ('quality_inspection_sync', 'bad', 400, x'00', 1, '2026-10-05T08:00:00+00:00')`, nil},
		// Naive watermark in Europe/Berlin (CEST, UTC+2) = 09:45 UTC.
		{`INSERT INTO watermarks VALUES ('material_stock_sync', '2026-10-05T11:45:00', '2026-10-05T09:59:00+00:00')`, nil},
		{`INSERT INTO watermarks VALUES ('quality_inspection_sync', '2026-10-05T11:00:00.123456', '2026-10-05T09:00:00+00:00')`, nil},
	}
	for _, s := range stmts {
		if _, err := db.Exec(s.q, s.args...); err != nil {
			t.Fatal(err)
		}
	}

	hb := filepath.Join(dir, "heartbeat")
	if err := os.WriteFile(hb, []byte("1"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(hb, fixedNow.Add(-30*time.Second), fixedNow.Add(-30*time.Second)); err != nil {
		t.Fatal(err)
	}
	zone, err := time.LoadLocation("Europe/Berlin")
	if err != nil {
		t.Fatal(err)
	}
	return dir, &Collector{
		CachePath:     dbPath,
		HeartbeatFile: hb,
		SourceZone:    zone,
		Now:           func() time.Time { return fixedNow },
	}
}

func TestCollectAggregatesPerJob(t *testing.T) {
	_, c := newFixture(t)
	snap := c.Collect(context.Background())
	if !snap.CacheUp {
		t.Fatalf("cache not up: %s", snap.CacheError)
	}
	if !snap.HeartbeatPresent || snap.HeartbeatAge != 30*time.Second {
		t.Fatalf("heartbeat = %v %v", snap.HeartbeatPresent, snap.HeartbeatAge)
	}
	m := snap.Jobs["material_stock_sync"]
	if m == nil {
		t.Fatal("missing material_stock_sync")
	}
	if m.OutboxRecords != 140 || m.OutboxBatches != 2 || m.OutboxDueBatches != 1 || m.OutboxMaxAttempts != 7 {
		t.Fatalf("outbox = %+v", m)
	}
	if m.OutboxOldestAge != time.Hour {
		t.Fatalf("oldest age = %v", m.OutboxOldestAge)
	}
	if m.DeadLetters != 1 {
		t.Fatalf("dead letters = %d", m.DeadLetters)
	}
	wantWM := time.Date(2026, 10, 5, 9, 45, 0, 0, time.UTC)
	if !m.HasWatermarkTime || !m.WatermarkTime.Equal(wantWM) {
		t.Fatalf("watermark = %v, want %v", m.WatermarkTime, wantWM)
	}
	if m.WatermarkUpdateAge != time.Minute {
		t.Fatalf("watermark update age = %v", m.WatermarkUpdateAge)
	}
	q := snap.Jobs["quality_inspection_sync"]
	if q == nil || q.HasOutbox || q.DeadLetters != 1 || !q.HasWatermarkTime {
		t.Fatalf("quality job = %+v", q)
	}
}

func TestCollectReportsMissingCache(t *testing.T) {
	c := &Collector{CachePath: filepath.Join(t.TempDir(), "absent.db"), HeartbeatFile: "/nonexistent", Now: func() time.Time { return fixedNow }}
	snap := c.Collect(context.Background())
	if snap.CacheUp || snap.CacheError == "" {
		t.Fatalf("expected cache down, got %+v", snap)
	}
	if snap.HeartbeatPresent {
		t.Fatal("heartbeat should be absent")
	}
	// Opening read-only must not create the file.
	if _, err := os.Stat(c.CachePath); !os.IsNotExist(err) {
		t.Fatalf("collector created the cache file: %v", err)
	}
}

func TestWriteMetricsFormat(t *testing.T) {
	_, c := newFixture(t)
	var buf bytes.Buffer
	if err := WriteMetrics(&buf, c.Collect(context.Background()), `werk "01"`, "1.0.0", "go1.24"); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	for _, want := range []string{
		"# TYPE mittelconnect_cache_up gauge\n",
		`mittelconnect_cache_up{site="werk \"01\""} 1`,
		`mittelconnect_heartbeat_age_seconds{site="werk \"01\""} 30`,
		`mittelconnect_outbox_records{job_name="material_stock_sync",site="werk \"01\""} 140`,
		`mittelconnect_outbox_due_batches{job_name="material_stock_sync",site="werk \"01\""} 1`,
		`mittelconnect_outbox_oldest_age_seconds{job_name="material_stock_sync",site="werk \"01\""} 3600`,
		`mittelconnect_dead_letters{job_name="quality_inspection_sync",site="werk \"01\""} 1`,
		`mittelconnect_watermark_timestamp_seconds{job_name="material_stock_sync",site="werk \"01\""} 1.7911935e+09`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("metrics missing %q\n%s", want, out)
		}
	}
	// A job without outbox entries must not get an oldest-age sample.
	if strings.Contains(out, `mittelconnect_outbox_oldest_age_seconds{job_name="quality_inspection_sync"`) {
		t.Error("oldest age reported for a job with an empty outbox")
	}
	// Each family header appears exactly once.
	if n := strings.Count(out, "# TYPE mittelconnect_outbox_records "); n != 1 {
		t.Errorf("outbox_records TYPE lines = %d", n)
	}
}

func TestHandlers(t *testing.T) {
	_, c := newFixture(t)
	srv := &server{collector: c, maxAge: time.Minute, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	h := srv.routes()

	get := func(path string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec
	}

	if rec := get("/healthz"); rec.Code != http.StatusOK {
		t.Fatalf("/healthz = %d %s", rec.Code, rec.Body)
	}
	if rec := get("/readyz"); rec.Code != http.StatusOK {
		t.Fatalf("/readyz = %d %s", rec.Code, rec.Body)
	}
	rec := get("/metrics")
	if rec.Code != http.StatusOK || !strings.HasPrefix(rec.Header().Get("Content-Type"), "text/plain; version=0.0.4") {
		t.Fatalf("/metrics = %d %q", rec.Code, rec.Header().Get("Content-Type"))
	}

	srv.maxAge = 10 * time.Second
	rec = get("/healthz")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("stale heartbeat /healthz = %d", rec.Code)
	}
	var body healthBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body.Status != "unhealthy" {
		t.Fatalf("body = %s (%v)", rec.Body, err)
	}

	post := httptest.NewRecorder()
	h.ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/metrics", nil))
	if post.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /metrics = %d", post.Code)
	}
}

func TestParseTimestamp(t *testing.T) {
	berlin, _ := time.LoadLocation("Europe/Berlin")
	cases := map[string]time.Time{
		"2026-10-05T09:00:00+00:00": time.Date(2026, 10, 5, 9, 0, 0, 0, time.UTC),
		"2026-10-05T11:00:00":       time.Date(2026, 10, 5, 9, 0, 0, 0, time.UTC),
		"2026-01-05T11:00:00.5":     time.Date(2026, 1, 5, 10, 0, 0, 500000000, time.UTC),
		"2026-10-05 11:00:00":       time.Date(2026, 10, 5, 9, 0, 0, 0, time.UTC),
		"2026-10-05":                time.Date(2026, 10, 4, 22, 0, 0, 0, time.UTC),
		"2026-10-05T09:00:00Z":      time.Date(2026, 10, 5, 9, 0, 0, 0, time.UTC),
	}
	for in, want := range cases {
		got, ok := parseTimestamp(in, berlin)
		if !ok || !got.Equal(want) {
			t.Errorf("parseTimestamp(%q) = %v %v, want %v", in, got, ok, want)
		}
	}
	if _, ok := parseTimestamp("ARTIKEL-42", berlin); ok {
		t.Error("non-timestamp watermark parsed")
	}
}

func TestParseConfig(t *testing.T) {
	t.Setenv("MCMON_HEALTH_MAX_AGE_SECONDS", "120")
	t.Setenv("MCMON_SITE", "werk-02")
	cfg, err := parseConfig([]string{"-listen", "127.0.0.1:9999"})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MaxAge != 2*time.Minute || cfg.Site != "werk-02" || cfg.Listen != "127.0.0.1:9999" {
		t.Fatalf("cfg = %+v", cfg)
	}
	t.Setenv("MCMON_HEALTH_MAX_AGE_SECONDS", "zero")
	if _, err := parseConfig(nil); err == nil {
		t.Fatal("expected error for invalid max age")
	}
}
