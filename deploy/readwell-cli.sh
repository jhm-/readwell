#!/bin/sh
# /usr/local/bin/readwell: run the Readwell CLI as the service user, with the service environment.
#   readwell check | readwell run-once [ID ...] | readwell status | readwell discover URL
[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"
set -a
# shellcheck disable=SC1091
. /etc/readwell/env
set +a
cd /opt/readwell || exit 1
exec runuser -u readwell -- /opt/readwell/.venv/bin/readwell "$@"
