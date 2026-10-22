# Tempo 2.10.x is distroless and does not include an HTTP client. Add only a
# small static BusyBox binary so Compose can probe Tempo's actual /ready route.
FROM busybox:1.37.0-musl AS healthcheck

FROM grafana/tempo:2.10.8
COPY --from=healthcheck /bin/busybox /busybox
