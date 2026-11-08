#!/usr/bin/env bash
set -euo pipefail
[[ -n ${BASH_SOURCE[0]:-} ]] || { printf 'Run the saved start.sh file with bash.\n' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
driver_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)
if [[ ${driver_version%%.*} -lt 580 ]]; then
    printf 'This configuration requires NVIDIA driver R580 or newer; found %s.\n' "$driver_version" >&2
    exit 1
fi
docker compose config --quiet
docker compose up -d
