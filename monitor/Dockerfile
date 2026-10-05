# syntax=docker/dockerfile:1.7
# =============================================================================
# mcmon: MittelConnect health and metrics sidecar (Go, static binary)
#   Stage 1 builds with CGO disabled (pure-Go SQLite driver).
#   Stage 2 is distroless: no shell, no package manager, runs as uid 10001 so
#   it can read the middleware's 0700 data volume.
# Build:  docker build -t mittelconnect-monitor:1.0.0 monitor/
# =============================================================================

ARG GO_IMAGE=golang:1.26-bookworm

FROM ${GO_IMAGE} AS builder
WORKDIR /src
ENV CGO_ENABLED=0 GOFLAGS=-trimpath
COPY go.mod go.sum ./
RUN go mod download && go mod verify
COPY *.go ./
RUN go vet ./... \
 && go test -count=1 ./... \
 && go build -ldflags "-s -w -X main.version=1.0.0" -o /out/mcmon .

FROM gcr.io/distroless/static-debian12:nonroot
LABEL org.opencontainers.image.title="MittelConnect monitor" \
      org.opencontainers.image.description="Prometheus metrics and health endpoints for MittelConnect" \
      org.opencontainers.image.version="1.0.0"
COPY --from=builder /out/mcmon /usr/local/bin/mcmon
USER 10001:10001
EXPOSE 9464
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/usr/local/bin/mcmon", "-healthcheck"]
ENTRYPOINT ["/usr/local/bin/mcmon"]
