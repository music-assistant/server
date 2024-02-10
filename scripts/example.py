"""Example script to test the MusicAssistant server and client."""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from aiorun import run

from music_assistant.client.client import MusicAssistantClient
from music_assistant.server.server import MusicAssistant

# ruff: noqa: ANN201,PTH102,PTH112,PTH113,PTH118,PTH123,T201

DEFAULT_PORT = 8095
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_STORAGE_PATH = os.path.join(Path.home(), ".musicassistant")

logging.basicConfig(level=logging.DEBUG)

# Get parsed passed in arguments.
parser = argparse.ArgumentParser(description="MusicAssistant Server Example.")
parser.add_argument(
    "--config",
    type=str,
    default=DEFAULT_STORAGE_PATH,
    help="Storage path to keep persistent (configuration) data, "
    "defaults to {DEFAULT_STORAGE_PATH}",
)
parser.add_argument(
    "--log-level",
    type=str,
    default="info",
    help="Provide logging level. Example --log-level debug, default=info, "
    "possible=(critical, error, warning, info, debug)",
)

args = parser.parse_args()


if __name__ == "__main__":
    # configure logging
    logging.basicConfig(level=args.log_level.upper())

    # make sure storage path exists
    if not os.path.isdir(args.config):
        os.mkdir(args.config)

    # Init server
    server = MusicAssistant(args.config)

    async def run_mass():
        """Run the MusicAssistant server and client."""
        # start MusicAssistant Server
        await server.start()

        # run the client
        async with MusicAssistantClient(DEFAULT_URL, None) as client:
            # start listening
            await client.start_listening()

    async def handle_stop(loop: asyncio.AbstractEventLoop):  # noqa: ARG001
        """Handle server stop."""
        await server.stop()

    # run the server
    run(run_mass(), shutdown_callback=handle_stop)
