# Nemotron on Brev

Target instance: `secure-amethyst-ferret`, one NVIDIA L40S, reached using Brev from WSL. The builder uses Nemotron 3.5 Lightning 30B with four-bit weights through Marlin on Ada; OpenAI project answers and embeddings remain separate.

The configuration adapts NVIDIA's [Ada serving recipe](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent/blob/main/docker/docker-compose.nemotron35-lightning.yaml). It pins the checkpoint revision and vLLM version, limits total context to 16,384 tokens, and permits two sequences. Actual memory usage, latency, and tool-call correctness still require a requested live test.

## Preparation

Copy this directory to the instance and prepare its secret and container image from WSL:

```bash
brev exec secure-amethyst-ferret 'mkdir -p ~/hackutd-nemotron'
scp deploy/nemotron/{compose.yaml,prepare.sh,start.sh,README.md} secure-amethyst-ferret:~/hackutd-nemotron/
brev exec secure-amethyst-ferret 'bash ~/hackutd-nemotron/prepare.sh'
```

`prepare.sh` creates a private `.env` only if absent, validates Compose, and downloads the image. It does not start inference. The model weights download on first start. Never commit or print the `.env` file; use its `NEMOTRON_API_KEY` value as the backend's builder credential.

## Driver prerequisite

Inspection found NVIDIA driver `565.57.01`, Ubuntu 22.04, Docker 27.3.1, Compose 2.29.7, and 107 GB free disk. The CUDA 13 serving image requires R580 or newer for this configuration. R565 is not a supported target for its forward-compatibility option. See [vLLM driver requirements](https://docs.vllm.ai/en/v0.31.0/getting_started/installation/gpu/#running-on-systems-with-older-cuda-drivers).

Upgrade to a supported driver and reboot before starting. Review the package transaction first because this image includes NVIDIA's R565 driver and Fabric Manager metapackages. Preparation does not modify drivers or reboot the VM.

The image holds its old NVIDIA packages. A simulation replacing the installed R565 packages explicitly with `nvidia-driver-580-server` succeeds: 20 old driver-related packages removed, 17 packages installed, and no unrelated upgrades. The inspected candidate is `580.178.04`. Recheck the transaction before applying it; do not broadly unhold packages or upgrade the entire OS.

Preparation completed: the CUDA 13.0.2 image was downloaded and pinned by digest, shell syntax and Compose configuration validated, and the generated `.env` confirmed private (mode `600`). About 86 GB disk remains. No driver change, reboot, inference startup, model-weight download, or live model test has been performed.

## Start and connect

Run these only when server startup is requested, after the driver upgrade:

```bash
brev exec secure-amethyst-ferret 'bash ~/hackutd-nemotron/start.sh'
brev port-forward secure-amethyst-ferret --port 8000:8000
```

Keep the forwarding terminal open. From WSL, the builder endpoint is `http://127.0.0.1:8000/v1`, the model ID is `nemotron-builder`, and bearer authentication uses the private generated key. The service binds only to the VM's loopback interface; a later deployed backend needs its own private tunnel/network connection or an authenticated HTTPS proxy.

Inspect logs or stop the model process from WSL:

```bash
brev exec secure-amethyst-ferret 'cd ~/hackutd-nemotron && docker compose logs --tail 80'
brev exec secure-amethyst-ferret 'cd ~/hackutd-nemotron && docker compose down'
```

Stopping the container does not stop Brev instance billing. Manage the instance lifecycle separately in Brev.
