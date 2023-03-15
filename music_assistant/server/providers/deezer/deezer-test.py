import asyncio
import json

import requests


async def get_sid():
    return requests.get(
        "http://www.deezer.com/ajax/gw-light.php",
        params={"method": "deezer.ping", "api_version": "1.0", "api_token": ""},
    ).json()["results"]["SESSION"]


async def get_user_data(sid):
    return requests.get("https://www.deezer.com/ajax/gw-light.php?method=$method&input=3&api_version=1.0&api_token=$apiToken" --header "Cookie: sid=$session")


# main coroutine
async def main():
    # start executing a shell command in a subprocess
    process = await asyncio.create_subprocess_shell(
        "./dzr-url 1518077822", stdout=asyncio.subprocess.PIPE
    )
    # report the details of the subprocess
    print(f"subprocess: {process}")
    # read a line of output from the program
    data, _ = await process.communicate()
    # report the data
    print(data)


# entry point
asyncio.run(get_sid())
