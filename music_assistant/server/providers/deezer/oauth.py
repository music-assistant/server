"""Bruh."""
import webbrowser

import requests

app_id = "587964"
redirect_uri = "http://localhost:8080/oauth/return"

authorization_url = f"https://connect.deezer.com/oauth/auth.php?\
    app_id={app_id}&redirect_uri={redirect_uri}&perms=basic_access,email"
webbrowser.open(authorization_url)

authorization_code = input("Enter the authorization code: ")

app_secret = "3725582e5aeec225901e4eb03684dbfb"
token_response = requests.post(
    "https://connect.deezer.com/oauth/access_token.php",
    data={
        "code": authorization_code,
        "app_id": app_id,
        "secret": app_secret,
    },
)

# Parse the response to get the access token
access_token = token_response.text.split("=")
print(f"Access token: {access_token}")
