FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
WORKDIR /service
RUN pip install --no-cache-dir torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir transformers==5.5.0 sentence-transformers==5.4.1 fastapi==0.162.2 uvicorn==0.54.0
COPY server.py ./server.py
CMD ["uvicorn", "server:production_app", "--factory", "--host", "0.0.0.0", "--port", "8004", "--workers", "1", "--no-access-log"]
