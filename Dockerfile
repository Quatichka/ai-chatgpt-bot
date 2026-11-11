FROM node:22-bookworm-slim

# Python для бота + Codex CLI (официальный CLI OpenAI, через него идут запросы к AgentRouter)
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
        librsvg2-bin fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
# Chromium для команд OPEN/SCREENSHOT (ставим от root, вместе с системными библиотеками)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /opt/venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* && chmod -R a+rX /ms-playwright
COPY bot.py web.py ./

ENV PATH="/opt/venv/bin:$PATH" \
    CHATS_DIR=/home/node/chats \
    CODEX_HOME=/home/node/.codex \
    PYTHONUNBUFFERED=1
RUN mkdir -p /home/node/chats /home/node/.codex && chown -R node:node /app /home/node

# Агента не запускаем от root
USER node
CMD ["python", "bot.py"]
