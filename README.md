# Telegram ИИ-бот на Codex CLI + AgentRouter

Бот передаёт сообщения в официальный Codex CLI (`codex exec`), а тот обращается к AgentRouter
через Responses API (`/v1/responses`). У каждого чата своя папка и своя сессия — бот помнит контекст.

Умеет:
- отвечать на сообщения с учётом истории диалога (модель по умолчанию `gpt-5.6-sol`);
- создавать файлы (код, документы, CSV, HTML, SVG) и присылать их документом;
- смотреть присланные фото и читать файлы.

Не умеет генерировать растровые картинки (у AgentRouter нет такой модели), но может нарисовать SVG.

## Переменные окружения (bothost → «Переменные»)
- `API_KEY` — ключ AgentRouter (`sk-...`)
- `API_BASE_URL` — `https://agentrouter.org/v1`
- `CHAT_MODEL` — `gpt-5.6-sol` (или `gpt-6.1-sol`, `gpt-6-astra`)
- `ALLOWED_USERS` — ваш Telegram id через запятую (рекомендуется: иначе ключ тратят все)
- `REASONING_EFFORT` — необязательно: `low` / `medium` / `high`
- `CODEX_SANDBOX` — по умолчанию `workspace-write`; если команды модели падают с ошибкой песочницы — `danger-full-access`
- токен бота — поле «Токен» в bothost

Команды, которые запускает модель, не видят переменных с KEY/TOKEN/SECRET в имени.

## bothost
Включите «Использовать собственный Dockerfile». Образ ставит Node.js, Codex CLI и Python-зависимости.

## Команды бота
`/start`, `/reset` — новый диалог, `/models` — список моделей, `/model <id>` — выбрать модель.
