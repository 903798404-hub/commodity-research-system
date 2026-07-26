#!/usr/bin/env bash
# Controlled USDA-only Compose entrypoint. It never builds images or parses
# the root Compose file. Candidate lifecycle remains limited to its explicit
# container/service and must be followed by independent HTTP/inspect evidence.
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage:
  usda_compose.sh production-config <usda-production.env>
  usda_compose.sh candidate-config <usda-candidate.env>
  usda_compose.sh candidate-up <usda-candidate.env>
  usda_compose.sh candidate-remove <usda-candidate.env>
  usda_compose.sh production-up <usda-production.env>
EOF
    exit 2
}

[[ $# -eq 2 ]] || usage
mode="$1"
env_file="$2"
[[ -f "$env_file" ]] || { echo "environment file does not exist: $env_file" >&2; exit 2; }

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
production_compose="${script_dir}/compose.production.yml"
candidate_compose="${script_dir}/compose.candidate.yml"

case "$mode" in
    production-config)
        exec docker compose --project-name market-data --env-file "$env_file" -f "$production_compose" config
        ;;
    candidate-config)
        exec docker compose --project-name market-data-usda-candidate --env-file "$env_file" -f "$candidate_compose" config
        ;;
    candidate-up)
        exec docker compose --project-name market-data-usda-candidate --env-file "$env_file" -f "$candidate_compose" up -d --no-build --pull never --no-deps --force-recreate usda-dashboard
        ;;
    candidate-remove)
        exec docker compose --project-name market-data-usda-candidate --env-file "$env_file" -f "$candidate_compose" rm --stop --force usda-dashboard
        ;;
    production-up)
        exec docker compose --project-name market-data --env-file "$env_file" -f "$production_compose" up -d --no-build --pull never --no-deps --force-recreate usda-dashboard
        ;;
    *)
        usage
        ;;
esac
