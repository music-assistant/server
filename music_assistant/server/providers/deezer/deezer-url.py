import asyncio
import json

import requests


async def get_sid():
    return requests.get(
        url="http://www.deezer.com/ajax/gw-light.php",
        params={"method": "deezer.ping", "api_version": "1.0", "api_token": ""},
    ).json()["results"]["SESSION"]


async def get_user_data(tok, sid):
    return requests.get(
        url="https://www.deezer.com/ajax/gw-light.php",
        params={
            "method": "deezer.getUserData",
            "input": "3",
            "api_version": "1.0",
            "api_token": tok,
        },
        headers={"Cookie": f"sid={sid}"},
    ).json()["results"]


async def get_song_info(tok, sid, sng_id):
    return requests.post(
        url="https://www.deezer.com/ajax/gw-light.php",
        params={
            "method": "song.getListData",
            "input": "3",
            "api_version": "1.0",
            "api_token": tok,
        },
        headers={"Cookie": f"sid={sid}"},
        data=json.dumps({"sng_ids": sng_id}),
    ).json()["results"]["data"][0]


async def get_url(usr_lic, sng_tok):
    url = "https://media.deezer.com/v1/get_url"
    payload = {
        "license_token": usr_lic,
        "media": [{"type": "FULL", "formats": [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]}],
        "track_tokens": sng_tok,
    }
    response = requests.post(url, data=json.dumps(payload))
    return response


async def main(sng_id):
    print("Trying to find url for song with id: " + str(sng_id))
    sid = await get_sid()
    print("Found sid: " + sid)
    user_data = await get_user_data("", sid)
    user_token = user_data["USER_TOKEN"]
    licence_token = user_data["USER"]["OPTIONS"]["license_token"]
    check_form = user_data["checkForm"]
    print("User token: " + user_token)
    print("license_token: " + licence_token)
    print("checkForm: " + check_form)
    song_info = await get_song_info(sng_id=[sng_id], sid=sid, tok=check_form)
    track_token = song_info["TRACK_TOKEN"]
    sng_id = song_info["SNG_ID"]
    track_name = song_info["SNG_TITLE"]
    print("New sng_id: " + sng_id)
    print("Found song name: " + track_name)
    print("Track token: " + track_token)
    url_resp = await get_url(licence_token, [track_token])
    url_info = url_resp.json()["data"][0]
    url = url_info["media"][0]["sources"][0]["url"]
    print("FOUND URL: " + url)


asyncio.run(main(1518077822))
