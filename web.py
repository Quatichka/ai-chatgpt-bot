"""Браузер для бота: открыть страницу (текст) и сделать скриншот.

Работает через Playwright + headless Chromium. Любой запрос страницы (включая
редиректы, картинки, скрипты) проверяется: пускаем только http/https на публичные
IP-адреса, чтобы через бота нельзя было достучаться до внутренних адресов сервера.
"""
import asyncio
import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

log = logging.getLogger("web")

MAX_TEXT = 12000
PAGE_TIMEOUT_MS = 30000
_browser = None
_pw = None
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(2)
_dns_cache: dict[str, bool] = {}


# Доп. разрешённые сети (CIDR через запятую) — только для локальной отладки за VPN с fake-IP DNS.
EXTRA_NETS = [ipaddress.ip_network(n.strip()) for n in os.getenv("WEB_EXTRA_ALLOWED_NETS", "").split(",") if n.strip()]


class WebError(Exception):
    pass


def _ip_ok(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return ip.is_global or any(ip in n for n in EXTRA_NETS)


async def _host_is_public(host: str) -> bool:
    if host in _dns_cache:
        return _dns_cache[host]
    try:
        ok = _ip_ok(host.strip("[]"))
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            _dns_cache[host] = False
            return False
        ok = bool(infos) and all(_ip_ok(i[4][0]) for i in infos)
    if len(_dns_cache) > 2000:
        _dns_cache.clear()
    _dns_cache[host] = ok
    return ok


async def check_url(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise WebError("Можно открывать только http/https ссылки")
    if not await _host_is_public(u.hostname):
        raise WebError("Этот адрес недоступен (внутренние и локальные адреса запрещены)")
    return url


async def _get_browser():
    global _browser, _pw
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
            )
    return _browser


async def _guard(route):
    u = urlparse(route.request.url)
    if u.scheme in ("http", "https"):
        if not u.hostname or not await _host_is_public(u.hostname):
            return await route.abort()
    elif u.scheme not in ("data", "blob", "about"):
        return await route.abort()
    await route.continue_()


DECLINE_TEXTS = ["Отклонить все", "Отклонить всё", "Reject all", "Decline all", "Alle ablehnen",
                 "Tout refuser", "Rechazar todo", "Отказаться"]


async def _decline_cookies(page) -> None:
    """Закрывает баннер cookies вариантом «отклонить» (без лишнего отслеживания)."""
    for t in DECLINE_TEXTS:
        try:
            btn = page.locator("button, [role=button]", has_text=t).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3000)
                try:
                    await btn.wait_for(state="hidden", timeout=6000)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                return
        except Exception:
            continue


async def _with_page(url: str, fn):
    url = await check_url(url)
    async with _sem:
        browser = await _get_browser()
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
            accept_downloads=False,
            user_agent=None,
        )
        try:
            await ctx.route("**/*", _guard)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                if "Timeout" not in str(e):
                    raise WebError(f"Не удалось открыть {url}: {str(e).splitlines()[0][:200]}")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await _decline_cookies(page)
            return await fn(page)
        finally:
            await ctx.close()


async def fetch_text(url: str) -> str:
    async def get(page):
        title = await page.title()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n…(обрезано)"
        return f"URL: {page.url}\nЗаголовок: {title}\n\n{text}"

    return await _with_page(url, get)


async def screenshot(url: str, full_page: bool = False) -> bytes:
    async def shot(page):
        return await page.screenshot(type="jpeg", quality=80, full_page=full_page, timeout=PAGE_TIMEOUT_MS)

    return await _with_page(url, shot)
