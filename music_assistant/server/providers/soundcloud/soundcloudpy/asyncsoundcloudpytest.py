import asyncio

# from asyncsoundcloudpy import Soundcloud
import asyncsoundcloudpy
import yaml


async def main():
    async with asyncsoundcloudpy.SoundcloudAsync(
        o_auth="",
        client_id="",
    ) as account:
        print(account)
        status = await account.login()
        print(status)

    get_account_details = await account.get_account_details()
    print(get_account_details["id"])
    print("get_user_details######################################################################")
    user_id_1 = 3263397
    user_id_2 = 315988522
    get_user_details_1 = await account.get_user_details(user_id_1)
    get_user_details_2 = await account.get_user_details(user_id_2)
    print(get_user_details_1["id"])
    print(get_user_details_2["id"])
    print(get_user_details_2["username"])
    print(get_user_details_2["full_name"])
    print(get_user_details_2["avatar_url"])
    print("get_followers######################################################################")
    get_followers = await account.get_followers()
    print(get_followers["collection"])
    print("get_tracks_liked######################################################################")
    get_tracks_liked = await account.get_tracks_liked()
    print(get_tracks_liked["collection"])
    print("get_following######################################################################")
    user_id_1 = 3263397
    get_following = await account.get_following(user_id_1)
    print(get_following["collection"][0]["id"])
    print(get_following["collection"][0]["full_name"])
    print(get_following["collection"][0]["avatar_url"])
    # print(get_following["collection"][0]["description"])
    print(get_following["collection"][0]["visuals"]["visuals"][0]["visual_url"])
    print(
        "get_tracks_from_user######################################################################"
    )
    user_id_1 = 315988522
    get_tracks_from_user = await account.get_tracks_from_user(user_id_1)
    print(get_tracks_from_user["collection"][0]["title"])
    print(get_tracks_from_user["collection"][0]["id"])
    print(get_tracks_from_user["collection"][0]["duration"])
    print(get_tracks_from_user["collection"][0]["artwork_url"])
    print(get_tracks_from_user["collection"][0]["permalink_url"])
    print(
        "get_popular_tracks_user######################################################################"
    )
    get_popular_tracks_user = await account.get_popular_tracks_user(user_id_1)
    print(get_popular_tracks_user["collection"][0]["title"])
    print(get_popular_tracks_user["collection"][0]["id"])

    # print("get_track_details######################################################################")
    track_id = 1453832722
    get_track_details = await account.get_track_details(track_id)
    print(get_track_details[0]["title"])
    # print("get_popular_tracks_user######################################################################")
    get_popular_tracks_user = await account.get_popular_tracks_user(315988522)
    # print(account.get_popular_tracks_user(315988522)["collection"][0]["duration"])
    # print(account.get_popular_tracks_user(315988522)["collection"][0]["artwork_url"])
    # print(account.get_popular_tracks_user(315988522)["collection"][0]["permalink_url"])
    print(
        "get_album_from_user######################################################################"
    )
    get_album_from_user = await account.get_album_from_user(user_id_1)
    print(get_album_from_user["collection"][0]["title"])
    print(get_album_from_user["collection"][0]["id"])
    print(get_album_from_user["collection"][0]["duration"])
    print(get_album_from_user["collection"][0]["artwork_url"])
    print(get_album_from_user["collection"][0]["permalink_url"])

    print(
        "get_album_from_user  Tracks######################################################################"
    )
    print(get_album_from_user["collection"][0]["tracks"][0]["title"])
    print(get_album_from_user["collection"][0]["tracks"][0]["id"])
    print(get_album_from_user["collection"][0]["tracks"][0]["duration"])
    print(get_album_from_user["collection"][0]["tracks"][0]["artwork_url"])
    print(get_album_from_user["collection"][0]["tracks"][0]["permalink_url"])
    print(
        "get_account_playlists######################################################################"
    )
    get_account_playlists = await account.get_account_playlists()
    print(get_account_playlists["collection"][0]["playlist"]["title"])
    print(get_account_playlists["collection"][0]["playlist"]["id"])
    print(get_account_playlists["collection"][0]["playlist"]["artwork_url"])
    print(
        "get_playlist_details######################################################################"
    )
    playlist_id = 1542606805
    get_playlist_details = await account.get_playlist_details(playlist_id)
    print(get_playlist_details["tracks"][0]["id"])
    # print("######################################################################")
    # print("######################################################################")
    # print("######################################################################")
    # print("######################################################################")
    # print("######################################################################")
    # print(account.get_playlist_details(1542606805))
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])
    # print(account.get_account_details()["id"])


if __name__ == "__main__":
    asyncio.run(main())
