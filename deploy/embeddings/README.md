# Nemotron embeddings on a dedicated Brev GPU

This service runs `nvidia/Nemotron-3-Embed-1B-BF16` locally, with no NVIDIA hosted
API key. It uses the text-only model for Markdown notes and questions. The model
revision is `c0c9fea93ea424587517f2c59e20db9f1d6bf615`; dependencies are pinned in
the Dockerfile. NVIDIA documents its [prefixes and Sentence Transformers interface](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16).

Use a separate Ampere-or-newer GPU with at least 16 GB VRAM, 16 GB RAM, and 60 GB
disk. CUDA 12.8 wheels require a compatible NVIDIA driver and NVIDIA Container
Toolkit. Check the driver before starting; these files do not upgrade drivers.
The existing chat/speech L40S had 2,976 MiB GPU memory free during the 2026-11-08
inspection, so this configuration does not share that GPU or reduce its caches.

The service binds VM loopback port 8004. It authenticates every request, accepts
only bounded text batches, and processes one request at a time, with inference
batch size one. Busy requests receive 503 for the worker's bounded retry policy.
A 40-second inference watchdog restarts only this service if inference stalls.
It returns actual tokenizer usage and normalized 1,536-dimensional vectors.

## Deployment

These are deployment commands, not verification commands. Copy this directory
to `~/hypha-embeddings` on the chosen VM. In Bash on that VM:

```bash
cd ~/hypha-embeddings
umask 077
test -f .env || python3 -c 'import secrets; print("EMBEDDING_API_KEY=" + secrets.token_urlsafe(32))' > .env
nvidia-smi
docker compose build
docker compose up -d
docker compose ps
```

First startup downloads approximately 2.3 GB of public model weights to `cache/`.
Keep `.env` private and preserve it across restarts. Neither imports nor backend
tests load this model. The service has no access to the application's database.

Connect the backend VM directly to this VM with a persistent authenticated SSH
tunnel or HTTPS endpoint. For SSH, use a dedicated key restricted to forwarding
`127.0.0.1:8004`, verify the destination host key, and bind the forward only to
backend loopback. Do not copy a developer's general-purpose SSH credentials.
Use `tunnel.service` on the backend VM with
`/home/ubuntu/.config/hypha-embedding-tunnel.env` containing `EMBEDDING_SSH_PORT`
and `EMBEDDING_SSH_DESTINATION=user@host`. Its dedicated private key and verified
host key file live at the paths specified in the unit. Restrict the public key
on the embedding VM with `restrict,port-forwarding,permitopen="127.0.0.1:8004",command="/bin/false"`.
The tunnel restarts automatically and does not depend on the laptop.

Set these values in the backend's private `.env`:

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:8004/v1
EMBEDDING_API_KEY=<same secret as the embedding service>
```

Before replacing the backend, verify authenticated `/readyz`, unauthorized
rejection, and synthetic passage/query requests through its configured endpoint.
Then deploy the backend with the existing [single-process procedure](../brev/README.md).
Startup will queue reindexing for old ready vectors. Notes stay pending until
the new index commits; their content is preserved, and old source versions
cannot be dispatched. Do not run overlapping backends during the migration.

## API and checks

`POST /v1/embeddings` accepts the model ID, `input` (1–32 strings of at most
8,000 UTF-8 bytes each), `input_type` (`query` or `passage`), optional dimensions
(128–2048), `encoding_format=float`, `modality=text`, and `truncate=NONE`.
`Authorization: Bearer <EMBEDDING_API_KEY>` is mandatory. `/readyz` reports
readiness and whether inference is busy, without exposing note content.

Offline checks run from the repository root:

```bash
python -m pytest -q tests/test_retrieval.py tests/test_embedding_service.py tests/test_embedding_upgrade.py tests/test_telemetry.py
```

After deployment, the backend's isolated synthetic check is:

```bash
python -m tools.local tools.verify_integrations embeddings --live
```

It calls the private embedding service without invoking chat models, database
writes, or Photon delivery.

## Live deployment: 2026-11-08

`hypha-embeddings` runs on a dedicated Brev RTX A4000 (16 GB) at the selected
offer's $0.18/hour, or $4.32 per 24 hours. This offer cannot be stopped; delete
the VM to end compute charges. The backend's systemd SSH tunnel connects to it
without depending on the laptop. No NVIDIA hosted API key is used.

Authenticated synthetic checks through that tunnel passed both expected note
rankings. Query embeddings took 0.13–0.14 seconds; a batch of 16 approximately
6,600-byte chunks took 2.32 seconds. GPU memory after these requests was 2,623 MiB
of 15,352 MiB. These small synthetic checks do not establish representative
grounding, concurrent capacity, or maximum-notes latency.

The live backend image is `hypha-backend:nemotron-a6a8707`, also tagged as
`hypha-backend:latest` for the existing Compose deployment. The previous image
is retained as `hypha-backend:before-nemotron-embeddings`. Cutover explicitly
stopped the old backend before starting its replacement. The existing saved
note reached `ready` with 1,536-dimensional Nemotron vectors in SpacetimeDB.
Public `/healthz` and `/notes` returned 200; unauthenticated `/api/notes` returned
401. Source and environment backups are under `~/hypha-backups` on the backend VM.

Future backend updates use the normal [Brev update procedure](../brev/README.md).
Keep the embedding VM and its tunnel running while this adapter is configured.
