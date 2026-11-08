# English voice notes on the existing Brev server

Implemented behind `VOICE_ENABLED=false`. No speech container has been built or
started, and no GPU capacity or real iMessage recording has been verified for
this feature. Run the following rollout only when explicitly requested.

The backend accepts Photon `voice` events and reserves a durable turn before
acknowledging the webhook. Its worker authenticates attachment downloads,
accepts at most 10 MiB of audio, and uses FFmpeg to decode at most two minutes
into 16 kHz mono 16-bit PCM WAV. Transcription, including one transient retry,
has a persisted 45-second budget within the existing 120-second turn deadline.
A completed transcript is saved as the turn's user question before commands,
retrieval, or model generation. Recovery reuses it. Later text waits behind
the recording; explicit agent navigation cancels obsolete work. Replies use
the existing text outbox, citations, read receipts, and one-to-three bubbles.

## Service contract and pins

- Separate service and port: `127.0.0.1:8002`; builder stays on `8000`.
- `POST /transcribe`, `Authorization: Bearer <TRANSCRIPTION_API_KEY>`,
  `Content-Type: audio/wav`, raw normalized WAV body.
- Result: `{"text":"...","language":"en","duration_seconds":1.0}`.
  Silence returns empty text; the backend sends the friendly retry message.
- Authenticated `GET /readyz` returns `{"ready":true,"busy":false}` after
  model loading; 503 before readiness. Busy transcription rejects another
  recording with 503 and `Retry-After: 1`.
- NVIDIA runtime image `nvcr.io/nvidia/nemo:25.11` (NeMo Speech 2.6.0,
  PyTorch 2.9.0a0, CUDA 13.0.1); FastAPI 0.162.2 and Uvicorn 0.54.0.
- Model `nvidia/nemotron-speech-streaming-en-0.6b`, `.nemo` checkpoint at
  revision `ebe59e5a817142986528bbbee5dba8db7b38ed50`. Loaded once on CUDA
  and used through NeMo's recorded-file `transcribe` interface, batch size 1.
  Model extraction uses temporary disk space under `/cache`, separate from
  the 512 MiB recording tmpfs. Allow disk space for both checkpoint and extraction.
- One Uvicorn process, one inference at a time. A 40-second inference hang
  exits only the speech container, allowing Compose to restart it and clean
  temporary audio. The builder's process and allocation are independent.

Sources: [NVIDIA model card](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b),
[pinned checkpoint](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b/tree/ebe59e5a817142986528bbbee5dba8db7b38ed50),
[NVIDIA runtime versions](https://docs.nvidia.com/nemo-framework/user-guide/25.11/softwarecomponentversions.html),
[recorded-file NeMo interface](https://github.com/NVIDIA/NeMo/blob/v2.6.0/nemo/collections/asr/models/rnnt_models.py).

## Rollout gate

1. Leave `VOICE_ENABLED=false`. On `secure-amethyst-ferret`, inspect GPU and
   disk capacity while the existing builder is running. Its previous recorded
   allocation was 39,715 of 46,068 MiB, leaving about 6.2 GiB; that is historical
   evidence, not current headroom. This model's roughly 618M float32 weights
   alone need about 2.3 GiB, plus runtime, decoder, and audio activations.
   Measure peak usage with two-minute recordings before deciding capacity.
   Do not change the builder's 0.85 GPU allocation, context, or concurrency.

   ```bash
   ssh secure-amethyst-ferret nvidia-smi
   ssh secure-amethyst-ferret df -h
   ```

2. Copy this directory to a separate location. Create a distinct speech secret
   in a mode-600 `.env` on Brev, then build/start only the speech service.
   The existing R580 driver supports the selected CUDA runtime; if deployment
   compatibility or capacity fails, stop speech and leave voice disabled.

   ```bash
   ssh secure-amethyst-ferret 'mkdir -p ~/hackutd-speech'
   scp deploy/speech/{Dockerfile,compose.yaml,server.py,README.md} secure-amethyst-ferret:~/hackutd-speech/
   ssh secure-amethyst-ferret
   cd ~/hackutd-speech
   umask 077
   test -f .env || python3 -c 'import secrets; print("TRANSCRIPTION_API_KEY=" + secrets.token_urlsafe(32))' > .env
   docker compose build speech
   docker compose up -d speech
   ```

   Copy the credential into the backend's private environment without putting
   it in chat, logs, or version control. Do not reuse the language-model key.

3. Establish a separate loopback SSH tunnel on the backend host (Bash/WSL).
   The tunnel must be reachable from the backend's own network namespace;
   a container's `127.0.0.1` is not its host. Deploy a private tunnel in that
   namespace or an authenticated HTTPS endpoint if the backend is containerized.
   Never expose the Brev speech port publicly.

   ```bash
   ssh -N -L 127.0.0.1:8002:127.0.0.1:8002 -o ExitOnForwardFailure=yes secure-amethyst-ferret
   ```

4. Check authenticated readiness, unauthorized requests, and a synthetic WAV
   through that tunnel. Run the existing builder's `deploy/nemotron/verify.py`
   concurrently with short and two-minute speech requests and monitor GPU
   peak memory with `nvidia-smi`. Compare against the builder's own baseline
   and check for OOMs, slow responses, restarts, and failures. Do not enable
   voice if the two services cannot coexist reliably; record the measured
   peak memory and additional headroom required. Do not provision another VM.

5. Set `TRANSCRIPTION_URL=http://127.0.0.1:8002` and its credential, then
   temporarily set `VOICE_ENABLED=true` for a controlled real iMessage test
   and restart the single backend process as part of the requested rollout.
   Send an English voice note to Hypha, then a project agent with indexed notes.
   Confirm download → transcription → text response, citations, configured
   multi-text replies, read receipts, a voice note immediately followed by text,
   and navigation during transcription. Keep the flag enabled only after this
   gate passes; disable it immediately on failure.

## Retention, measurements, and rollback

Recordings exist only in temporary local files and request memory. Backend
files live under the OS temp directory's `acorn-voice` directory; speech files
live in the container's `/tmp` tmpfs. Completion/failure removes them, and
single-process startup removes abandoned files. No recording is written to
SpacetimeDB. Transcripts use existing question/conversation retention: turn
content expires after seven days; conversation history after 30 days/200 items.
Attachment IDs are cleared with turn content. Normal cleanup retains compact
deduplication records for the configured window.

Per-turn `audio` metadata records status, bounded attempt count, queue wait in
milliseconds, successful pipeline latency, duration, MIME type, byte count,
and failure category. Use these fields for latency/failure review without
logging audio, transcript, credentials, or provider response bodies.

Rollback: set `VOICE_ENABLED=false` and restart the backend. Text messaging
continues; new or untranscribed voice turns receive one friendly text asking
the sender to type instead. Completed transcripts and uncertain deliveries
remain durable. Stop the separate speech container when no longer needed:

```bash
cd ~/hackutd-speech
docker compose down
```

Offline checks (no service startup):

```bash
python -m pytest tests/test_voice.py tests/test_speech_service.py -q
```
