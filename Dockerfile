FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HAYSTACK_TELEMETRY_ENABLED=false TIKTOKEN_CACHE_DIR=/opt/tiktoken
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY app ./app
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8001
CMD ["uvicorn", "app.main:production_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--no-access-log"]
