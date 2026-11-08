# Nemotron on Brev

Target instance: `secure-amethyst-ferret`, one NVIDIA L40S, reached using Brev from WSL. The builder uses Nemotron 3.5 Lightning 30B with four-bit weights through Marlin on Ada; OpenAI project answers and embeddings remain separate.

The configuration adapts NVIDIA's [Ada serving recipe](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent/blob/main/docker/docker-compose.nemotron35-lightning.yaml). It pins the checkpoint revision and vLLM version, limits total context to 16,384 tokens, and permits two sequences. Live inference verification passed on 2026-11-07 (America/Winnipeg).

## Preparation

Copy this directory to the instance and prepare its secret and container image from WSL:

```bash
brev exec secure-amethyst-ferret 'mkdir -p ~/hackutd-nemotron'
scp deploy/nemotron/{compose.yaml,prepare.sh,start.sh,verify.py,README.md} secure-amethyst-ferret:~/hackutd-nemotron/
brev exec secure-amethyst-ferret 'bash ~/hackutd-nemotron/prepare.sh'
```

`prepare.sh` creates a private `.env` only if absent, validates Compose, and downloads the image. It does not start inference. The model weights download on first start. Never commit or print the `.env` file; use its `NEMOTRON_API_KEY` value as the backend's builder credential.

## Driver prerequisite

The instance runs NVIDIA driver `580.178.04`, Ubuntu 22.04, Docker 27.3.1, and Compose 2.29.7. The CUDA 13 serving image requires R580 or newer for this configuration. R565 is not a supported target for its forward-compatibility option. See [vLLM driver requirements](https://docs.vllm.ai/en/v0.31.0/getting_started/installation/gpu/#running-on-systems-with-older-cuda-drivers).

The approved driver upgrade replaced 20 old R565 driver-related packages with 17 R580 packages, without unrelated upgrades. Reboot completed, and `nvidia-smi` confirmed `580.178.04` before startup. The transaction log is `/home/ubuntu/hackutd-nemotron/driver-upgrade.log`. Preparation scripts do not modify drivers or reboot the VM.

On a replacement VM, inspect and simulate its driver package transaction before upgrading; do not broadly unhold packages or upgrade the entire OS.

## Start and connect

The service is currently running. To start it again and establish a local tunnel from WSL:

```bash
brev exec secure-amethyst-ferret 'bash ~/hackutd-nemotron/start.sh'
brev port-forward secure-amethyst-ferret --port 8000:8000
```

The builder endpoint is `http://127.0.0.1:8000/v1`, the model ID is `nemotron-builder`, and bearer authentication uses `NEMOTRON_API_KEY` in `/home/ubuntu/hackutd-nemotron/.env` (mode `600`). Brev 0.6.337 leaves the SSH forward running after this command returns. Re-establish it after WSL or the tunnel stops. WSL authenticated requests and Windows `/health` access both passed. The service binds only to the VM's loopback interface; a later deployed backend needs its own private tunnel/network connection or an authenticated HTTPS proxy.

For the tested builder behavior, send `chat_template_kwargs={"enable_thinking": false}` with chat-completion requests (via `extra_body` when using a compatible SDK). The smoke tests use temperature zero. Keep the application's 12,000-input-token, 2,048-output-token, and two-concurrent-request limits within the server's configured capacity.

## Live verification

`verify.py` uses only Python's standard library. Run it on the VM:

```bash
brev exec secure-amethyst-ferret 'python3 ~/hackutd-nemotron/verify.py'
```

To check the entire WSL tunnel path without saving another copy of the secret, run from this repository after establishing the forward:

```bash
set -o pipefail
ssh -T secure-amethyst-ferret cat /home/ubuntu/hackutd-nemotron/.env |
  python3 deploy/nemotron/verify.py --env-file /dev/stdin
```

The 2026-11-07 run through WSL passed:

| Check | Result |
|---|---|
| Missing and invalid credentials | Both rejected with HTTP 401 |
| Authenticated model discovery | `nemotron-builder` available |
| Short text response | Correct, 0.30 s |
| Automatic tool call | Correct function and JSON arguments, 0.40 s |
| Tool-result follow-up | Correct use of synthetic result, 0.34 s |
| Two concurrent long-context requests | Both correct; 11,431 input tokens each; 1.27 s and 1.43 s |

These are smoke-test timings with very short outputs, not a throughput benchmark or a verification of application tools. Reasoning was disabled. No application/database mutation was invoked.

The checkpoint downloaded successfully (20.08 GiB). vLLM reported 17.82 GiB for model loading; `nvidia-smi` showed 39,715 MiB used of 46,068 MiB after allocating the serving caches. About 65 GiB disk remained. The container and Brev instance were running at handoff.

Inspect logs or stop the model process from WSL:

```bash
brev exec secure-amethyst-ferret 'cd ~/hackutd-nemotron && docker compose logs --tail 80'
brev exec secure-amethyst-ferret 'cd ~/hackutd-nemotron && docker compose down'
```

Stopping the container does not stop Brev instance billing. Manage the instance lifecycle separately in Brev.
