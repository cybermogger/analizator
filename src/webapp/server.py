from __future__ import annotations

from aiohttp import web

from .api import create_web_app


async def start_mini_app(service, settings: dict) -> web.AppRunner:
    runner = web.AppRunner(create_web_app(service, settings), access_log=None, shutdown_timeout=5)
    await runner.setup()
    site = web.TCPSite(runner, str(settings.get("host", "127.0.0.1")), int(settings.get("port", 8080)))
    try:
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
