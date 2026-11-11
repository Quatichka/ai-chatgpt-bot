"""Telegram-бот-ассистент, который работает через Claude Code (claude -p) и AgentRouter.

Каждый чат получает свою папку и свою сессию Claude Code, поэтому бот помнит
контекст. Файлы, которые Claude создал в папке чата, отправляются пользователю.
"""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import FSInputFile, Message

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# bothost может передавать токен под разными именами
TELEGRAM_BOT_TOKEN = next(
    v for v in (os.getenv(k) for k in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TOKEN", "TELEGRAM_TOKEN")) if v
)
API_KEY = os.environ["API_KEY"]
# Claude Code сам добавляет /v1/messages, поэтому /v1 в конце убираем
BASE_URL = os.getenv("API_BASE_URL", "https://agentrouter.org").rstrip("/")
if BASE_URL.endswith("/v1"):
    BASE_URL = BASE_URL[:-3]
CHAT_MODEL = os.getenv("CHAT_MODEL", "")  # пусто = модель по умолчанию Claude Code
CLAUDE_BIN = os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude"
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
def pick_chats_dir() -> Path:
    """Первая папка, куда реально можно писать (bothost монтирует /app/data от root)."""
    candidates = [os.getenv("CHATS_DIR"), Path.home() / "chats", Path(os.getenv("DATA_DIR", "./data")) / "chats"]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / ".w").touch()
            return p
        except OSError:
            continue
    import tempfile
    return Path(tempfile.mkdtemp(prefix="chats_"))


DATA_DIR = pick_chats_dir()
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
MAX_FILE_BYTES = 45 * 1024 * 1024

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты ИИ-ассистент в Telegram, как ChatGPT. Отвечай на языке пользователя, понятно и по делу. "
    "Пиши обычным текстом без Markdown-разметки (без **, #, таблиц). "
    "Если пользователь просит создать файл (код, документ, CSV, HTML, SVG-картинку и т.п.) — "
    "создай его инструментом Write в текущей папке; бот сам отправит файл пользователю. "
    "Растровые картинки (фото, рисунки) ты генерировать не умеешь — предложи нарисовать SVG. "
    "Если пользователь прислал файл или фото — оно лежит в текущей папке, прочитай его инструментом Read.",
)
# Только работа с файлами в папке чата: без Bash и без доступа в интернет
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"

dp = Dispatcher()
sessions: dict[int, str] = {}
locks: dict[int, asyncio.Lock] = {}
user_model: dict[int, str] = {}
state = {"default_model": CHAT_MODEL, "models": []}
NO_CHANNEL_MARKERS = ("无可用渠道", "no available channel", "model_not_found", "not found model")


async def fetch_models() -> list[str]:
    import aiohttp

    headers = {"Authorization": f"Bearer {API_KEY}", "x-api-key": API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE_URL}/v1/models", headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
    ids = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
    state["models"] = ids
    return ids


def rank_models(ids: list[str]) -> list[str]:
    """Сначала Claude Sonnet, потом другие Claude, потом остальные."""
    def key(m: str):
        low = m.lower()
        return (0 if "sonnet" in low else 1 if "claude" in low else 2, m)
    return sorted(ids, key=key)


async def ensure_default_model() -> None:
    if state["default_model"]:
        return
    try:
        ids = rank_models(await fetch_models())
        if ids:
            state["default_model"] = ids[0]
            log.info("Модель по умолчанию: %s (доступно %d)", ids[0], len(ids))
    except Exception as e:
        log.warning("Не удалось получить список моделей: %s", e)


def model_for(chat_id: int) -> str:
    return user_model.get(chat_id) or state["default_model"]


def chat_dir(chat_id: int) -> Path:
    d = DATA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(d: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in d.rglob("*") if p.is_file()}


def allowed(message: Message) -> bool:
    return not ALLOWED_USERS or (message.from_user and message.from_user.id in ALLOWED_USERS)


async def run_claude(chat_id: int, prompt: str) -> dict:
    """Запуск с автоматическим перебором моделей, если у AgentRouter нет канала для текущей."""
    tried = []
    while True:
        model = model_for(chat_id)
        data = await run_claude_once(chat_id, prompt, model)
        text = str(data.get("result", "")).lower()
        if not (data.get("is_error") and any(m.lower() in text for m in NO_CHANNEL_MARKERS)):
            return data
        tried.append(model)
        if not state["models"]:
            try:
                await fetch_models()
            except Exception:
                return data
        rest = [m for m in rank_models(state["models"]) if m not in tried]
        if not rest or len(tried) >= 4:
            return data
        log.warning("Модель %s недоступна, пробую %s", model or "(по умолчанию)", rest[0])
        user_model.pop(chat_id, None)
        state["default_model"] = rest[0]


async def run_claude_once(chat_id: int, prompt: str, model: str) -> dict:
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--allowedTools", ALLOWED_TOOLS,
        "--disallowedTools", "Bash,WebFetch,WebSearch,Task",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if model:
        cmd += ["--model", model]
    if chat_id in sessions:
        cmd += ["--resume", sessions[chat_id]]
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": API_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if model and not model.lower().startswith("claude"):
        # GPT-модели через AgentRouter не принимают reasoning вместе с инструментами:
        # убираем thinking и output_config.effort из запросов Claude Code
        env.setdefault("MAX_THINKING_TOKENS", "0")
        env.setdefault("CLAUDE_CODE_EFFORT_LEVEL", "unset")
    if model:
        # и фоновые запросы Claude Code шлём в ту же доступную модель
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
            env[k] = model
    env.pop("ANTHROPIC_API_KEY", None)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=chat_dir(chat_id), env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Claude не ответил за {TIMEOUT} секунд")
    text = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text.splitlines()[-1]) if text else {}
    except json.JSONDecodeError:
        data = {}
    if not data:
        raise RuntimeError((err.decode("utf-8", "replace") or text or f"код выхода {proc.returncode}")[-1500:])
    if data.get("session_id") and not data.get("is_error"):
        sessions[chat_id] = data["session_id"]
    return data


async def keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def send_long(message: Message, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])


async def ask(message: Message, prompt: str) -> None:
    chat_id = message.chat.id
    lock = locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await message.answer("Подождите, я ещё отвечаю на предыдущее сообщение…")
    async with lock:
        d = chat_dir(chat_id)
        before = snapshot(d)
        stop = asyncio.Event()
        typing = asyncio.create_task(keep_typing(message.bot, chat_id, stop))
        try:
            data = await run_claude(chat_id, prompt)
        except Exception as e:
            log.exception("Ошибка Claude")
            await message.answer(f"Ошибка при обращении к ИИ: {e}")
            return
        finally:
            stop.set()
            await typing

        if data.get("is_error"):
            await message.answer(f"Ошибка ИИ: {data.get('result') or data.get('subtype')}")
            if "conversation" in str(data.get("result", "")).lower():
                sessions.pop(chat_id, None)  # сессия потерялась — начнём новую
            return
        await send_long(message, data.get("result", ""))

        changed = [p for p, m in snapshot(d).items() if before.get(p) != m]
        for p in changed[:10]:
            path = Path(p)
            if path.stat().st_size <= MAX_FILE_BYTES:
                await message.answer_document(FSInputFile(path, filename=path.name))


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я ИИ-ассистент на Claude.\n\n"
        "Просто пиши — отвечу и помню контекст. Попроси «сделай файл …» — пришлю файл "
        "(код, документ, таблицу, SVG-картинку). Можно присылать фото и файлы.\n\n"
        "/reset — начать новый диалог\n"
        "/models — доступные модели, /model <id> — выбрать"
    )


@dp.message(Command("models"))
async def cmd_models(message: Message):
    if not allowed(message):
        return
    try:
        ids = rank_models(await fetch_models())
    except Exception as e:
        await message.answer(f"Не удалось получить список моделей: {e}")
        return
    await send_long(
        message,
        f"Текущая: {model_for(message.chat.id) or '(по умолчанию Claude Code)'}\n\n"
        "Доступные (выбрать: /model <id>):\n" + ("\n".join(ids) or "(пусто)"),
    )


@dp.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject):
    if not allowed(message):
        return
    if not command.args:
        await message.answer(f"Текущая модель: {model_for(message.chat.id) or '(по умолчанию)'}\nВыбрать: /model <id>")
        return
    user_model[message.chat.id] = command.args.strip()
    await message.answer(f"Модель переключена на {command.args.strip()}")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    sessions.pop(message.chat.id, None)
    shutil.rmtree(chat_dir(message.chat.id), ignore_errors=True)
    await message.answer("Начали заново: история и файлы очищены.")


async def save_upload(message: Message, file_obj, name: str) -> str:
    d = chat_dir(message.chat.id)
    target = d / Path(name).name
    await message.bot.download(file_obj, destination=target)
    return target.name


@dp.message(F.photo)
async def on_photo(message: Message):
    if not allowed(message):
        return
    name = await save_upload(message, message.photo[-1], f"photo_{int(time.time())}.jpg")
    await ask(message, f"Пользователь прислал изображение {name}. {message.caption or 'Опиши, что на нём.'}")


@dp.message(F.document)
async def on_document(message: Message):
    if not allowed(message):
        return
    doc = message.document
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие.")
        return
    name = await save_upload(message, doc, doc.file_name or "file")
    await ask(message, f"Пользователь прислал файл {name}. {message.caption or 'Разбери его.'}")


@dp.message(F.text)
async def on_text(message: Message):
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    await ask(message, message.text)


async def main():
    log.info("Папка чатов: %s", DATA_DIR)
    await ensure_default_model()
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    log.info("Бот запущен. Claude Code: %s, API: %s", CLAUDE_BIN, BASE_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
