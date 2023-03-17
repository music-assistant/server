"""The default Music Assistant (web) frontend, hosted within the server."""
from __future__ import annotations

import os
from functools import partial

from aiohttp import web
from music_assistant_frontend import where

from music_assistant.server.models.plugin import PluginProvider


class Frontend(PluginProvider):
    """The default Music Assistant (web) frontend, hosted within the server."""

    async def setup(self) -> None:
        """Handle async initialization of the plugin."""
        frontend_dir = where()
        for filename in next(os.walk(frontend_dir))[2]:
            if filename.endswith(".py"):
                continue
            filepath = os.path.join(frontend_dir, filename)
            handler = partial(self.serve_static, filepath)
            self.mass.webapp.router.add_get(f"/{filename}", handler)

        # add assets subdir as static
        self.mass.webapp.router.add_static(
            "/assets", os.path.join(frontend_dir, "assets"), name="assets"
        )

        # add index
        handler = partial(self.serve_static, os.path.join(frontend_dir, "index.html"))
        self.mass.webapp.router.add_get("/", handler)

    async def serve_static(self, file_path: str, _request: web.Request) -> web.FileResponse:
        """Serve file response."""
        headers = {"Cache-Control": "no-cache"}
        return web.FileResponse(file_path, headers=headers)
