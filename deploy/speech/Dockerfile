FROM nvcr.io/nvidia/nemo:25.11@sha256:ccefea2e98d0b1e7b4b5e83b52dbd1567336f6b04b4934c063b1d2251b72381d
WORKDIR /srv/speech
RUN pip install --no-cache-dir fastapi==0.162.2 uvicorn==0.54.0
COPY server.py ./server.py
ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
ENV PYTHONPATH=/opt/NeMo:${PYTHONPATH}
CMD ["uvicorn", "server:production_app", "--factory", "--host", "0.0.0.0", "--port", "8002", "--workers", "1", "--no-access-log"]
