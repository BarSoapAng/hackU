# Backend on the existing Brev server

Run from `~/hypha` on `secure-amethyst-ferret`. This deployment uses the existing
GPU instance; it does not make Brev or the external providers free.

The Linux host network lets the backend reach Nemotron on `127.0.0.1:8000`
and speech on `127.0.0.1:8002`. The backend itself binds only `127.0.0.1:8001`.
The ngrok system service provides public HTTPS using the existing domain.
Neither the laptop nor an SSH port forward is needed to serve users.

Keep the private `.env` on the server, with these deployment-specific values:

```dotenv
NEMOTRON_BASE_URL=http://127.0.0.1:8000/v1
TRANSCRIPTION_URL=http://127.0.0.1:8002
FFMPEG_BINARY=ffmpeg
EMBEDDING_BASE_URL=http://127.0.0.1:8004/v1
EMBEDDING_API_KEY=<private-embedding-service-secret>
```

For a separate embedding VM, port 8004 must be an authenticated private tunnel
from this backend host to the embedding service, independent of the laptop. See
[embedding deployment](../embeddings/README.md). Do not expose plain HTTP publicly.

Preserve `PUBLIC_URL`, database/provider credentials, `ENCRYPTION_KEYS`, and
`ACTIVE_KEY`. Never commit `.env` or the ngrok configuration.

## Updating

Build before stopping the existing backend. Stop any laptop backend before the
first cloud start; never run two backends against this database. Subsequent
updates explicitly stop the old container before starting the replacement:

```bash
cd ~/hypha
docker compose -f deploy/brev/compose.yaml build
docker compose -f deploy/brev/compose.yaml stop backend
docker compose -f deploy/brev/compose.yaml up -d --no-build backend
curl --fail http://127.0.0.1:8001/healthz
```

These commands build and start the app; run only for an intended deployment.
Do not use the root Compose file for this deployment or enable automatic
rolling replacements. Keep one backend process and one replica.

## Operations

```bash
docker compose -f deploy/brev/compose.yaml ps
docker compose -f deploy/brev/compose.yaml logs --tail 80 backend
sudo systemctl status ngrok --no-pager
```

Docker and ngrok must be enabled at boot. Both model containers also need a
restart policy. Restart policies cannot restart a stopped or deleted Brev VM.
The ngrok free tier has traffic quotas and a browser warning page.

`/healthz` checks database access and the worker task; it does not prove model
generation or message delivery. Check `/notes`, unauthenticated API rejection,
and model readiness separately. A real text/voice/notes journey with the laptop
off is the final user acceptance check. Broader production limitations remain
documented in `docs/verification.md`.

For rollback to the laptop, stop the cloud backend and ngrok first. Only then
restart the laptop backend, its inference forwards, and its ngrok endpoint.
