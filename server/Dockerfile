FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt \
    && useradd --create-home --uid 10001 interpreter

COPY src /app/src
COPY server /app/server

RUN mkdir -p /tmp/ai-voice-interpreter \
    && chown -R interpreter:interpreter /app /tmp/ai-voice-interpreter

USER interpreter

EXPOSE 8000

CMD ["uvicorn", "server.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
