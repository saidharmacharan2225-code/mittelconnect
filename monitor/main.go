// Command mcmon is the MittelConnect health and metrics sidecar.
//
// It reads the Python daemon's local SQLite cache (read-only) and heartbeat
// file and serves:
//
//	GET /metrics   Prometheus metrics: outbox depth and age, dead letters,
//	               watermark progress per job, heartbeat age
//	GET /healthz   200 while the daemon finished a cycle recently, else 503
//	GET /readyz    200 while the cache database can be read, else 503
//
// Configuration is taken from environment variables (flags override them):
//
//	MCMON_LISTEN                  listen address            (default ":9464")
//	MCMON_CACHE_PATH              SQLite cache              (default /app/data/mittelconnect_cache.db)
//	MCMON_HEARTBEAT_FILE          daemon heartbeat file     (default /app/data/heartbeat)
//	MCMON_HEALTH_MAX_AGE_SECONDS  max heartbeat age         (default 600)
//	MCMON_SOURCE_TZ               zone of naive watermarks  (default Europe/Berlin)
//	MCMON_SITE                    "site" label on metrics   (default empty)
//
// "mcmon -healthcheck" probes /healthz on the local listener and exits 0 or 1,
// for Docker HEALTHCHECK in an image without a shell or curl.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
	_ "time/tzdata" // embedded IANA zones for distroless/scratch images
)

// version is overridden at build time with -ldflags "-X main.version=...".
var version = "1.0.0"

type config struct {
	Listen        string
	CachePath     string
	HeartbeatFile string
	MaxAge        time.Duration
	SourceTZ      string
	Site          string
	Healthcheck   bool
}

func envOr(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && strings.TrimSpace(v) != "" {
		return v
	}
	return fallback
}

func parseConfig(args []string) (config, error) {
	var cfg config
	maxAgeDefault, err := strconv.Atoi(envOr("MCMON_HEALTH_MAX_AGE_SECONDS", "600"))
	if err != nil || maxAgeDefault <= 0 {
		return cfg, fmt.Errorf("MCMON_HEALTH_MAX_AGE_SECONDS must be a positive integer")
	}
	fs := flag.NewFlagSet("mcmon", flag.ContinueOnError)
	fs.StringVar(&cfg.Listen, "listen", envOr("MCMON_LISTEN", ":9464"), "listen address")
	fs.StringVar(&cfg.CachePath, "cache", envOr("MCMON_CACHE_PATH", "/app/data/mittelconnect_cache.db"), "SQLite cache path")
	fs.StringVar(&cfg.HeartbeatFile, "heartbeat", envOr("MCMON_HEARTBEAT_FILE", "/app/data/heartbeat"), "daemon heartbeat file")
	maxAge := fs.Int("max-age", maxAgeDefault, "maximum heartbeat age in seconds for /healthz")
	fs.StringVar(&cfg.SourceTZ, "source-tz", envOr("MCMON_SOURCE_TZ", "Europe/Berlin"), "time zone of naive watermarks")
	fs.StringVar(&cfg.Site, "site", envOr("MCMON_SITE", ""), "site label added to every metric")
	fs.BoolVar(&cfg.Healthcheck, "healthcheck", false, "probe /healthz on the local listener and exit")
	if err := fs.Parse(args); err != nil {
		return cfg, err
	}
	if *maxAge <= 0 {
		return cfg, fmt.Errorf("-max-age must be positive")
	}
	cfg.MaxAge = time.Duration(*maxAge) * time.Second
	return cfg, nil
}

type server struct {
	collector *Collector
	maxAge    time.Duration
	site      string
	logger    *slog.Logger
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /metrics", s.handleMetrics)
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReady)
	return securityHeaders(mux)
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

func (s *server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	snap := s.collector.Collect(r.Context())
	if !snap.CacheUp {
		s.logger.Warn("cache not readable", "error", snap.CacheError)
	}
	var buf bytes.Buffer
	if err := WriteMetrics(&buf, snap, s.site, version, runtime.Version()); err != nil {
		http.Error(w, "render metrics", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = w.Write(buf.Bytes())
}

type healthBody struct {
	Status              string  `json:"status"`
	HeartbeatPresent    bool    `json:"heartbeat_present"`
	HeartbeatAgeSeconds float64 `json:"heartbeat_age_seconds,omitempty"`
	MaxAgeSeconds       float64 `json:"max_age_seconds"`
	CacheUp             bool    `json:"cache_up"`
	CacheError          string  `json:"cache_error,omitempty"`
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	snap := s.collector.Collect(r.Context())
	healthy := snap.HeartbeatPresent && snap.HeartbeatAge < s.maxAge
	body := healthBody{
		Status:              statusWord(healthy),
		HeartbeatPresent:    snap.HeartbeatPresent,
		HeartbeatAgeSeconds: snap.HeartbeatAge.Seconds(),
		MaxAgeSeconds:       s.maxAge.Seconds(),
		CacheUp:             snap.CacheUp,
	}
	writeJSON(w, healthy, body)
}

func (s *server) handleReady(w http.ResponseWriter, r *http.Request) {
	snap := s.collector.Collect(r.Context())
	body := healthBody{
		Status:        statusWord(snap.CacheUp),
		CacheUp:       snap.CacheUp,
		CacheError:    snap.CacheError,
		MaxAgeSeconds: s.maxAge.Seconds(),
	}
	writeJSON(w, snap.CacheUp, body)
}

func statusWord(ok bool) string {
	if ok {
		return "ok"
	}
	return "unhealthy"
}

func writeJSON(w http.ResponseWriter, ok bool, body any) {
	w.Header().Set("Content-Type", "application/json")
	if ok {
		w.WriteHeader(http.StatusOK)
	} else {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	_ = json.NewEncoder(w).Encode(body)
}

// probe calls /healthz on the local listener; used as the container healthcheck.
func probe(listen string) int {
	host, port, err := net.SplitHostPort(listen)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid listen address:", err)
		return 1
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	client := http.Client{Timeout: 4 * time.Second}
	resp, err := client.Get("http://" + net.JoinHostPort(host, port) + "/healthz")
	if err != nil {
		fmt.Fprintln(os.Stderr, "healthcheck:", err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintln(os.Stderr, "healthcheck: status", resp.StatusCode)
		return 1
	}
	return 0
}

func run(args []string) int {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("logger", "mcmon")
	cfg, err := parseConfig(args)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		logger.Error("invalid configuration", "error", err)
		return 2
	}
	if cfg.Healthcheck {
		return probe(cfg.Listen)
	}
	zone, err := time.LoadLocation(cfg.SourceTZ)
	if err != nil {
		logger.Error("unknown time zone", "zone", cfg.SourceTZ, "error", err)
		return 2
	}

	srv := &server{
		collector: &Collector{CachePath: cfg.CachePath, HeartbeatFile: cfg.HeartbeatFile, SourceZone: zone},
		maxAge:    cfg.MaxAge,
		site:      cfg.Site,
		logger:    logger,
	}
	httpServer := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		logger.Info("mcmon started", "version", version, "listen", cfg.Listen, "cache", cfg.CachePath)
		errCh <- httpServer.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server failed", "error", err)
			return 1
		}
		return 0
	case <-ctx.Done():
	}
	logger.Info("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		logger.Error("graceful shutdown failed", "error", err)
		return 1
	}
	return 0
}

func main() {
	os.Exit(run(os.Args[1:]))
}
