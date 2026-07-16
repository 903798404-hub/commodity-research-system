#!/bin/sh
set -eu

base_url="http://127.0.0.1/oil-world"

wget -qO- "${base_url}/" >/dev/null

latest_payload="$(wget -qO- "${base_url}/data/oil_world/latest.json")"
case "${latest_payload}" in
  \{*\}) ;;
  *) exit 1 ;;
esac
release="$(printf '%s' "${latest_payload}" | sed -n 's/.*"release"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

test -n "${release}"
wget -qO- "${base_url}/data/oil_world/releases/${release}/index.json" >/dev/null
