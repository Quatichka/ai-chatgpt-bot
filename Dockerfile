FROM node:22-bookworm-slim

# Python для бота + Claude Code (официальный CLI, через него идут запросы к AgentRouter)
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY bot.py .

ENV PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/data && chown -R node:node /app

# Claude Code не стоит запускать от root
USER node
CMD ["python", "bot.py"]
