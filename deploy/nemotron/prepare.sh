#!/usr/bin/env bash
set -euo pipefail
[[ -n ${BASH_SOURCE[0]:-} ]] || { printf 'Run the saved prepare.sh file with bash.\n' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
python3 - <<'PY'
from pathlib import Path
import secrets

path = Path('.env')
try:
    with path.open('x') as output:
        output.write(f'NEMOTRON_API_KEY={secrets.token_urlsafe(32)}\n')
except FileExistsError:
    pass
path.chmod(0o600)
PY
docker compose config --quiet
docker compose pull --quiet
printf 'Configuration and image prepared. The inference server has not been started.\n'
