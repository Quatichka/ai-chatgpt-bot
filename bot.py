"""Telegram-бот-ассистент, который работает через Codex CLI (codex exec) и AgentRouter.

Каждый чат получает свою папку и свою сессию Codex, поэтому бот помнит контекст.
Файлы, которые модель создала в папке чата, отправляются пользователю.
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
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
BASE_URL = os.getenv("API_BASE_URL", "https://agentrouter.org/v1").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL += "/v1"
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5.6-sol")
REASONING = os.getenv("REASONING_EFFORT", "")  # low / medium / high, пусто = по умолчанию Codex
CODEX_BIN = os.getenv("CODEX_BIN") or shutil.which("codex") or "codex"
# workspace-write: модель пишет только в папку чата и без сети.
# Если песочница не работает в контейнере — задайте CODEX_SANDBOX=danger-full-access.
SANDBOX = os.getenv("CODEX_SANDBOX", "workspace-write")
TIMEOUT = int(os.getenv("AI_TIMEOUT", "300"))
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
MAX_FILE_BYTES = 45 * 1024 * 1024
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты ИИ-ассистент в Telegram, как ChatGPT. Отвечай на языке пользователя, понятно и по делу. "
    "Пиши обычным текстом без Markdown-разметки (без **, #, таблиц). "
    "Ты работаешь в своей папке. Если пользователь просит создать файл (код, документ, CSV, HTML, "
    "SVG-картинку и т.п.) — создай его в текущей папке; бот сам отправит файл пользователю. "
    "Не создавай служебных и временных файлов. "
    "Растровые картинки генерировать не умеешь — предложи нарисовать SVG. "
    "Присланные пользователем файлы лежат в текущей папке.",
)


def pick_chats_dir() -> Path:
    """Первая папка, куда реально можно писать (bothost монтирует /app/data от root)."""
    for c in (os.getenv("CHATS_DIR"), Path.home() / "chats", Path(os.getenv("DATA_DIR", "./data")) / "chats"):
        if not c:
            continue
        p = Path(c)
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / ".w").touch()
            return p
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="chats_"))


DATA_DIR = pick_chats_dir()

dp = Dispatcher()
sessions: dict[int, str] = {}
locks: dict[int, asyncio.Lock] = {}
user_model: dict[int, str] = {}
state = {"default_model": CHAT_MODEL, "models": []}
NO_CHANNEL_MARKERS = ("无可用渠道", "no available channel", "model_not_found", "not found model")


def chat_dir(chat_id: int) -> Path:
    d = DATA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(d: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in d.rglob("*") if p.is_file() and not p.name.startswith(".")}


def allowed(message: Message) -> bool:
    return not ALLOWED_USERS or (message.from_user and message.from_user.id in ALLOWED_USERS)


def model_for(chat_id: int) -> str:
    return user_model.get(chat_id) or state["default_model"]


async def fetch_models() -> list[str]:
    """Сначала /v1/models с ключом, если пусто — публичный прайс AgentRouter."""
    import aiohttp

    root = BASE_URL[:-3]
    ids: set[str] = set()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        try:
            async with s.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {API_KEY}"}) as r:
                data = await r.json(content_type=None)
            ids |= {m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")}
        except Exception as e:
            log.warning("/v1/models: %s", e)
        if not ids:
            async with s.get(f"{root}/api/pricing") as r:
                data = await r.json(content_type=None)
            ids |= {m.get("model_name") for m in data.get("data", []) if isinstance(m, dict) and m.get("model_name")}
    state["models"] = sorted(ids)
    return state["models"]


def codex_args(model: str) -> list[str]:
    a = [
        "--json", "--skip-git-repo-check", "--ignore-user-config",
        "-c", 'model_provider="agentrouter"',
        "-c", 'model_providers.agentrouter.name="AgentRouter"',
        "-c", f"model_providers.agentrouter.base_url={json.dumps(BASE_URL)}",
        "-c", 'model_providers.agentrouter.env_key="AGENTROUTER_API_KEY"',
        "-c", 'model_providers.agentrouter.wire_api="responses"',
        "-c", f"sandbox_mode={json.dumps(SANDBOX)}",
        "-c", 'approval_policy="never"',
        # команды, которые запускает модель, не видят ключей и токенов
        "-c", 'shell_environment_policy.inherit="core"',
        "-c", 'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*"]',
    ]
    if REASONING:
        a += ["-c", f"model_reasoning_effort={json.dumps(REASONING)}"]
    if model:
        a += ["-m", model]
    return a


async def run_codex_once(chat_id: int, prompt: str, model: str, images: list[str]) -> dict:
    sid = sessions.get(chat_id)
    if sid:
        cmd = [CODEX_BIN, "exec", "resume", *codex_args(model)]
    else:
        cmd = [CODEX_BIN, "exec", *codex_args(model)]
        prompt = f"{SYSTEM_PROMPT}\n\nСообщение пользователя:\n{prompt}"
    for img in images:
        cmd += ["-i", img]
    cmd += [sid, prompt] if sid else [prompt]

    env = {**os.environ, "AGENTROUTER_API_KEY": API_KEY}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=chat_dir(chat_id), env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"ИИ не ответил за {TIMEOUT} секунд")

    texts, error, thread = [], "", ""
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread = ev.get("thread_id", "")
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            texts.append(ev["item"].get("text", ""))
        elif t == "turn.failed":
            error = (ev.get("error") or {}).get("message", "ошибка")
        elif t == "error" and not error and "Reconnecting" not in ev.get("message", ""):
            error = ev.get("message", "")
    if not texts and not error:
        error = (err.decode("utf-8", "replace").strip() or f"код выхода {proc.returncode}")[-1500:]
    if thread and not error:
        sessions[chat_id] = thread
    return {"is_error": bool(error) and not texts, "result": "\n\n".join(texts) if texts else error}


async def run_ai(chat_id: int, prompt: str, images: list[str]) -> dict:
    """Запуск с перебором моделей, если у AgentRouter нет канала для текущей."""
    tried = []
    while True:
        model = model_for(chat_id)
        data = await run_codex_once(chat_id, prompt, model, images)
        text = str(data.get("result", "")).lower()
        if not (data["is_error"] and any(m.lower() in text for m in NO_CHANNEL_MARKERS)):
            return data
        tried.append(model)
        try:
            ids = state["models"] or await fetch_models()
        except Exception:
            return data
        rest = [m for m in ids if m not in tried]
        if not rest or len(tried) >= 4:
            return data
        log.warning("Модель %s недоступна, пробую %s", model, rest[0])
        user_model.pop(chat_id, None)
        state["default_model"] = rest[0]


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
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])


async def ask(message: Message, prompt: str, images: list[str] | None = None, uploaded: set[str] | None = None) -> None:
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
            data = await run_ai(chat_id, prompt, images or [])
        except Exception as e:
            log.exception("Ошибка ИИ")
            await message.answer(f"Ошибка при обращении к ИИ: {e}")
            return
        finally:
            stop.set()
            await typing

        if data["is_error"]:
            await message.answer(f"Ошибка ИИ: {data['result']}")
            return
        await send_long(message, data["result"] or "(пустой ответ)")

        changed = [p for p, m in snapshot(d).items() if before.get(p) != m and p not in (uploaded or set())]
        for p in changed[:10]:
            path = Path(p)
            if path.stat().st_size <= MAX_FILE_BYTES:
                await message.answer_document(FSInputFile(path, filename=path.name))


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я ИИ-ассистент.\n\n"
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
        ids = await fetch_models()
    except Exception as e:
        await message.answer(f"Не удалось получить список моделей: {e}")
        return
    await send_long(
        message,
        f"Текущая: {model_for(message.chat.id)}\n\nДоступные (выбрать: /model <id>):\n" + ("\n".join(ids) or "(пусто)"),
    )


@dp.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject):
    if not allowed(message):
        return
    if not command.args:
        await message.answer(f"Текущая модель: {model_for(message.chat.id)}\nВыбрать: /model <id>")
        return
    user_model[message.chat.id] = command.args.strip()
    await message.answer(f"Модель переключена на {command.args.strip()}")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    sessions.pop(message.chat.id, None)
    shutil.rmtree(chat_dir(message.chat.id), ignore_errors=True)
    await message.answer("Начали заново: история и файлы очищены.")


async def save_upload(message: Message, file_obj, name: str) -> Path:
    target = chat_dir(message.chat.id) / Path(name).name
    await message.bot.download(file_obj, destination=target)
    return target


@dp.message(F.photo)
async def on_photo(message: Message):
    if not allowed(message):
        return
    path = await save_upload(message, message.photo[-1], f"photo_{int(time.time())}.jpg")
    await ask(message, message.caption or "Опиши, что на изображении.", images=[str(path)], uploaded={str(path)})


@dp.message(F.document)
async def on_document(message: Message):
    if not allowed(message):
        return
    doc = message.document
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие.")
        return
    path = await save_upload(message, doc, doc.file_name or "file")
    images = [str(path)] if path.suffix.lower() in IMAGE_EXT else []
    await ask(message, f"Пользователь прислал файл {path.name}. {message.caption or 'Разбери его.'}",
              images=images, uploaded={str(path)})


@dp.message(F.text)
async def on_text(message: Message):
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    await ask(message, message.text)


async def main():
    log.info("Папка чатов: %s", DATA_DIR)
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    log.info("Бот запущен. Codex: %s, API: %s, модель: %s, песочница: %s", CODEX_BIN, BASE_URL, CHAT_MODEL, SANDBOX)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
