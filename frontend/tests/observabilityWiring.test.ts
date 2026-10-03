import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

/**
 * The observability wiring that Prometheus and Loki actually load.
 *
 * Two failure modes this pins have both happened: a remote-write receiver that
 * any container could write to (forging the RED/service-graph series the alerts
 * read), and a Loki ruler pointed at an Alertmanager nothing runs (dead
 * configuration implying coverage). So:
 *
 * * the RED pipeline stays intact -- Tempo's generator still remote-writes, and
 *   Prometheus still receives -- but the receiver requires basic auth;
 * * ConfessItBackendDown is declared with the exact expression the backend's
 *   liveness endpoint feeds;
 * * Loki carries no ruler block at all.
 *
 * These live here (not in the backend suite) because they read files outside
 * `backend/`, which the backend container never mounts: this suite runs on the
 * host checkout where the whole repo is visible. Plain string assertions rather
 * than YAML parsing -- no YAML dependency for four exact lines, and a changed
 * line fails loudly either way.
 */

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel: string) => readFileSync(join(ROOT, rel), 'utf8');

describe('observability wiring', () => {
  it('watches backend liveness with the scrape the /metrics endpoint feeds', () => {
    const rules = read('observability/alerting-rules.yml');
    expect(rules).toContain('alert: ConfessItBackendDown');
    // The backend's application metrics arrive by push (OTLP), so a dead backend
    // looks identical to an idle one on every pushed series. This rule is the one
    // observation that tells them apart -- it reads Prometheus's own `up{}` for
    // the confessit-backend scrape job -- which is why its exact expression is
    // pinned rather than just its name.
    expect(rules).toContain('expr: up{job="confessit-backend"} == 0');
    expect(rules).toContain('severity: critical');
  });

  it('requires basic auth on the remote-write receiver', () => {
    const prometheus = read('observability/prometheus.yml');
    // `--web.enable-remote-write-receiver` cannot be scoped to one client, so an
    // open receiver lets any container on the network forge the series the RED
    // rules read. The auth itself lives in the web config file (prometheus.yml
    // has no such field); assert the user entry there, not the secret: the bcrypt
    // hash is safe to track, the password itself lives in gitignored
    // observability/.rw-password.
    expect(read('observability/prometheus-web.yml')).toMatch(/^ {2}tempo: \$2[aby]\$/m);
    expect(prometheus).not.toContain('basic_auth_users:');

    // The healthcheck must authenticate too (/-ready is behind the same auth),
    // but never by placing the password on argv: busybox wget has no
    // --user/--password, so a mounted script reads the password file into an
    // Authorization header instead.
    const compose = read('docker-compose.yml');
    expect(compose).toContain('/etc/prometheus/healthcheck.sh');
    const probe = read('observability/prometheus-healthcheck.sh');
    expect(probe).toContain('Authorization: Basic');
    expect(probe).toContain('/etc/prometheus/rw-password');
    // No password on any argv, even the script's own: only comment lines may
    // name the rejected flag.
    const codeLines = probe.split('\n').filter((line) => !line.trimStart().startsWith('#'));
    expect(codeLines.some((line) => line.includes('--password'))).toBe(false);
  });

  it('keeps the RED delivery wired end to end, authenticated', () => {
    // Auth must not have been "fixed" by deleting the delivery both RED rules
    // need: Tempo's generator publishes by remote write only.
    const compose = read('docker-compose.yml');
    expect(compose).toContain('--web.enable-remote-write-receiver');

    const tempo = read('observability/tempo.yml');
    expect(tempo).toContain('url: http://prometheus:9090/api/v1/write');
    expect(tempo).toContain('username: tempo');
    expect(tempo).toContain('password_file: /etc/tempo/rw-password');
  });

  it('carries no dead Loki ruler', () => {
    // The stack deliberately runs no Alertmanager (Prometheus evaluates the rules
    // and exposes their state itself), so a ruler block could only fail closed on
    // rule evaluation while notifying nobody. Assert absence, not a corrected URL:
    // the correct fix was removal. (`^ruler:` so prose mentioning rulers cannot
    // satisfy this by accident.)
    const loki = read('observability/loki-config.yml');
    expect(loki).not.toMatch(/^ruler:/m);
    expect(loki).not.toContain('9093');
  });
});
