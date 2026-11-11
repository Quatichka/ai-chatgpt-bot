"""Проверка ключа: какие модели доступны, работает ли чат и генерация картинок.
Запуск: python check_api.py   (берёт API_KEY / API_BASE_URL из .env или окружения)
"""
import os

from openai import OpenAI

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

client = OpenAI(api_key=os.environ["API_KEY"], base_url=os.getenv("API_BASE_URL", "https://agentrouter.org/v1"))

print("== Модели ==")
try:
    ids = sorted(m.id for m in client.models.list().data)
    print("\n".join(ids) or "(пусто)")
except Exception as e:
    ids = []
    print("Ошибка:", e)

model = os.getenv("CHAT_MODEL") or (ids[0] if ids else "")
print(f"\n== Чат ({model}) ==")
try:
    r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Скажи привет одним словом"}])
    print("OK:", r.choices[0].message.content)
except Exception as e:
    print("Ошибка:", e)

img_model = os.getenv("IMAGE_MODEL", "gpt-image-1")
print(f"\n== Картинки ({img_model}) ==")
try:
    client.images.generate(model=img_model, prompt="a red apple", size="1024x1024", n=1)
    print("OK: генерация картинок работает")
except Exception as e:
    print("Ошибка:", e)
