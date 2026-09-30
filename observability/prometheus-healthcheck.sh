#!/bin/sh
# Readiness probe for Prometheus behind HTTP basic auth (see prometheus-web.yml).
#
# busybox wget has no --user/--password flags, so the credential goes in an
# Authorization header built from the mounted password file -- never on a
# command line, where `docker inspect` and `ps` would retain it (a previous
# revision passed --password= on argv and the value ended up in the health log).
set -eu
cred="$(cat /etc/prometheus/rw-password)"
header="$(printf 'tempo:%s' "$cred" | base64 | tr -d '\n\r ')"
exec wget --spider -q --header="Authorization: Basic $header" http://127.0.0.1:9090/-/ready
