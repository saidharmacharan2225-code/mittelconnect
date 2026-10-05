package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Snapshot is one read of the middleware's local cache and heartbeat.
type Snapshot struct {
	TakenAt          time.Time
	CacheUp          bool
	CacheError       string
	HeartbeatPresent bool
	HeartbeatAge     time.Duration
	Jobs             map[string]*JobState
	ScrapeDuration   time.Duration
}

// JobState aggregates the cache tables for one pipeline job.
type JobState struct {
	OutboxRecords      int64
	OutboxBatches      int64
	OutboxDueBatches   int64
	OutboxMaxAttempts  int64
	OutboxOldestAge    time.Duration
	HasOutbox          bool
	DeadLetters        int64
	WatermarkTime      time.Time
	HasWatermarkTime   bool
	WatermarkUpdateAge time.Duration
	HasWatermark       bool
}

// Collector reads the SQLite cache written by the Python daemon. It never
// writes: the database is opened with mode=ro, and payload columns (which
// hold encrypted record data) are never selected.
type Collector struct {
	CachePath     string
	HeartbeatFile string
	SourceZone    *time.Location
	QueryTimeout  time.Duration
	Now           func() time.Time
}

func (c *Collector) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

func (c *Collector) dsn() string {
	query := url.Values{}
	query.Set("mode", "ro")
	query.Add("_pragma", "busy_timeout(5000)")
	query.Add("_pragma", "query_only(1)")
	return "file:" + c.CachePath + "?" + query.Encode()
}

// Collect takes a fresh snapshot. Errors reading the cache are reported in
// the snapshot (CacheUp=false) rather than returned, so /metrics still
// exposes the heartbeat when the database is locked or not created yet.
func (c *Collector) Collect(ctx context.Context) Snapshot {
	start := c.now()
	snap := Snapshot{TakenAt: start, Jobs: map[string]*JobState{}}

	if info, err := os.Stat(c.HeartbeatFile); err == nil {
		snap.HeartbeatPresent = true
		snap.HeartbeatAge = nonNegative(start.Sub(info.ModTime()))
	}

	if err := c.readCache(ctx, &snap); err != nil {
		snap.CacheUp = false
		snap.CacheError = err.Error()
	} else {
		snap.CacheUp = true
	}
	snap.ScrapeDuration = nonNegative(c.now().Sub(start))
	return snap
}

func (c *Collector) readCache(ctx context.Context, snap *Snapshot) error {
	if _, err := os.Stat(c.CachePath); err != nil {
		return fmt.Errorf("cache file: %w", err)
	}
	timeout := c.QueryTimeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	db, err := sql.Open("sqlite", c.dsn())
	if err != nil {
		return fmt.Errorf("open cache: %w", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)

	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return fmt.Errorf("begin read: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only transaction

	nowEpoch := float64(snap.TakenAt.UnixNano()) / 1e9
	rows, err := tx.QueryContext(ctx, `
		SELECT job,
		       COALESCE(SUM(record_count), 0),
		       COUNT(*),
		       COALESCE(SUM(CASE WHEN next_attempt_at <= ? THEN 1 ELSE 0 END), 0),
		       COALESCE(MAX(attempts), 0),
		       MIN(created_at)
		FROM outbox GROUP BY job`, nowEpoch)
	if err != nil {
		return fmt.Errorf("query outbox: %w", err)
	}
	for rows.Next() {
		var job string
		var oldest sql.NullString
		state := &JobState{HasOutbox: true}
		if err := rows.Scan(&job, &state.OutboxRecords, &state.OutboxBatches,
			&state.OutboxDueBatches, &state.OutboxMaxAttempts, &oldest); err != nil {
			rows.Close()
			return fmt.Errorf("scan outbox: %w", err)
		}
		if oldest.Valid {
			if created, ok := parseTimestamp(oldest.String, time.UTC); ok {
				state.OutboxOldestAge = nonNegative(snap.TakenAt.Sub(created))
			}
		}
		mergeJob(snap.Jobs, job, func(s *JobState) {
			s.OutboxRecords, s.OutboxBatches = state.OutboxRecords, state.OutboxBatches
			s.OutboxDueBatches, s.OutboxMaxAttempts = state.OutboxDueBatches, state.OutboxMaxAttempts
			s.OutboxOldestAge, s.HasOutbox = state.OutboxOldestAge, true
		})
	}
	if err := closeRows(rows); err != nil {
		return fmt.Errorf("read outbox: %w", err)
	}

	rows, err = tx.QueryContext(ctx, `SELECT job, COUNT(*) FROM dead_letters GROUP BY job`)
	if err != nil {
		return fmt.Errorf("query dead letters: %w", err)
	}
	for rows.Next() {
		var job string
		var count int64
		if err := rows.Scan(&job, &count); err != nil {
			rows.Close()
			return fmt.Errorf("scan dead letters: %w", err)
		}
		mergeJob(snap.Jobs, job, func(s *JobState) { s.DeadLetters = count })
	}
	if err := closeRows(rows); err != nil {
		return fmt.Errorf("read dead letters: %w", err)
	}

	rows, err = tx.QueryContext(ctx, `SELECT job, value, updated_at FROM watermarks`)
	if err != nil {
		return fmt.Errorf("query watermarks: %w", err)
	}
	for rows.Next() {
		var job, value, updated string
		if err := rows.Scan(&job, &value, &updated); err != nil {
			rows.Close()
			return fmt.Errorf("scan watermarks: %w", err)
		}
		mergeJob(snap.Jobs, job, func(s *JobState) {
			s.HasWatermark = true
			// Naive watermarks come straight from the legacy database and are
			// in the source time zone; updated_at is always written in UTC.
			if wm, ok := parseTimestamp(value, c.SourceZone); ok {
				s.WatermarkTime, s.HasWatermarkTime = wm, true
			}
			if at, ok := parseTimestamp(updated, time.UTC); ok {
				s.WatermarkUpdateAge = nonNegative(snap.TakenAt.Sub(at))
			}
		})
	}
	if err := closeRows(rows); err != nil {
		return fmt.Errorf("read watermarks: %w", err)
	}
	return nil
}

func closeRows(rows *sql.Rows) error {
	iterErr := rows.Err()
	closeErr := rows.Close()
	return errors.Join(iterErr, closeErr)
}

func mergeJob(jobs map[string]*JobState, job string, apply func(*JobState)) {
	state, ok := jobs[job]
	if !ok {
		state = &JobState{}
		jobs[job] = state
	}
	apply(state)
}

func nonNegative(d time.Duration) time.Duration {
	if d < 0 {
		return 0
	}
	return d
}

// timestampLayouts covers Python's datetime.isoformat() output (with and
// without microseconds and offsets) and the space-separated SQL style.
var timestampLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02 15:04:05.999999999",
	"2006-01-02",
}

// parseTimestamp parses an ISO-8601 timestamp; values without an offset are
// interpreted in zone.
func parseTimestamp(text string, zone *time.Location) (time.Time, bool) {
	text = strings.TrimSpace(text)
	if zone == nil {
		zone = time.UTC
	}
	for _, layout := range timestampLayouts {
		var (
			parsed time.Time
			err    error
		)
		if strings.Contains(layout, "Z07:00") {
			parsed, err = time.Parse(layout, text)
		} else {
			parsed, err = time.ParseInLocation(layout, text, zone)
		}
		if err == nil {
			return parsed, true
		}
	}
	return time.Time{}, false
}

// WriteMetrics renders a snapshot in the Prometheus text exposition format
// (version 0.0.4).
func WriteMetrics(w io.Writer, snap Snapshot, site, version, goVersion string) error {
	m := &metricWriter{w: w, site: site}

	m.family("mittelconnect_monitor_build_info", "gauge", "Build information of the MittelConnect monitor.")
	m.sample("mittelconnect_monitor_build_info", map[string]string{"version": version, "goversion": goVersion}, 1)

	m.family("mittelconnect_cache_up", "gauge", "1 if the local cache database could be read during this scrape.")
	m.sample("mittelconnect_cache_up", nil, boolFloat(snap.CacheUp))

	m.family("mittelconnect_monitor_scrape_duration_seconds", "gauge", "Time taken to read the cache and heartbeat.")
	m.sample("mittelconnect_monitor_scrape_duration_seconds", nil, snap.ScrapeDuration.Seconds())

	m.family("mittelconnect_heartbeat_present", "gauge", "1 if the daemon heartbeat file exists.")
	m.sample("mittelconnect_heartbeat_present", nil, boolFloat(snap.HeartbeatPresent))
	if snap.HeartbeatPresent {
		m.family("mittelconnect_heartbeat_age_seconds", "gauge", "Seconds since the daemon last finished a pipeline cycle.")
		m.sample("mittelconnect_heartbeat_age_seconds", nil, snap.HeartbeatAge.Seconds())
	}

	jobs := make([]string, 0, len(snap.Jobs))
	for job := range snap.Jobs {
		jobs = append(jobs, job)
	}
	sort.Strings(jobs)

	type jobMetric struct {
		name, help string
		value      func(*JobState) (float64, bool)
	}
	perJob := []jobMetric{
		{"mittelconnect_outbox_records", "Records parked in the encrypted outbox awaiting delivery to SAP.",
			func(s *JobState) (float64, bool) { return float64(s.OutboxRecords), true }},
		{"mittelconnect_outbox_batches", "Outbox entries (OData batches) awaiting delivery.",
			func(s *JobState) (float64, bool) { return float64(s.OutboxBatches), true }},
		{"mittelconnect_outbox_due_batches", "Outbox entries whose next replay attempt is due now.",
			func(s *JobState) (float64, bool) { return float64(s.OutboxDueBatches), true }},
		{"mittelconnect_outbox_max_attempts", "Highest replay attempt count among outbox entries.",
			func(s *JobState) (float64, bool) { return float64(s.OutboxMaxAttempts), true }},
		{"mittelconnect_outbox_oldest_age_seconds", "Age of the oldest outbox entry.",
			func(s *JobState) (float64, bool) { return s.OutboxOldestAge.Seconds(), s.HasOutbox }},
		{"mittelconnect_dead_letters", "Records rejected by SAP or by validation, kept for review.",
			func(s *JobState) (float64, bool) { return float64(s.DeadLetters), true }},
		{"mittelconnect_watermark_timestamp_seconds", "Source timestamp up to which the job has delivered, as Unix time.",
			func(s *JobState) (float64, bool) {
				return float64(s.WatermarkTime.UnixNano()) / 1e9, s.HasWatermarkTime
			}},
		{"mittelconnect_watermark_updated_age_seconds", "Seconds since the job's watermark last advanced.",
			func(s *JobState) (float64, bool) { return s.WatermarkUpdateAge.Seconds(), s.HasWatermark }},
	}
	for _, metric := range perJob {
		headerWritten := false
		for _, job := range jobs {
			value, ok := metric.value(snap.Jobs[job])
			if !ok {
				continue
			}
			if !headerWritten {
				m.family(metric.name, "gauge", metric.help)
				headerWritten = true
			}
			m.sample(metric.name, map[string]string{"job_name": job}, value)
		}
	}
	return m.err
}

type metricWriter struct {
	w    io.Writer
	site string
	err  error
}

func (m *metricWriter) printf(format string, args ...any) {
	if m.err != nil {
		return
	}
	_, m.err = fmt.Fprintf(m.w, format, args...)
}

func (m *metricWriter) family(name, kind, help string) {
	m.printf("# HELP %s %s\n# TYPE %s %s\n", name, escapeHelp(help), name, kind)
}

func (m *metricWriter) sample(name string, labels map[string]string, value float64) {
	all := map[string]string{}
	if m.site != "" {
		all["site"] = m.site
	}
	for k, v := range labels {
		all[k] = v
	}
	keys := make([]string, 0, len(all))
	for k := range all {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString(name)
	if len(keys) > 0 {
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteString(k)
			b.WriteString(`="`)
			b.WriteString(escapeLabel(all[k]))
			b.WriteByte('"')
		}
		b.WriteByte('}')
	}
	m.printf("%s %s\n", b.String(), strconv.FormatFloat(value, 'g', -1, 64))
}

func escapeLabel(v string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(v)
}

func escapeHelp(v string) string {
	return strings.NewReplacer(`\`, `\\`, "\n", `\n`).Replace(v)
}

func boolFloat(b bool) float64 {
	if b {
		return 1
	}
	return 0
}
