#!/usr/bin/env python3
"""
Apple Music Token Extractor for Music Assistant.

Uses Playwright to automate token extraction from Apple Music.
"""

import asyncio
import json
from datetime import datetime

from playwright.async_api import async_playwright


async def extract_apple_music_token():
    """Extract the Apple Music user token from browser cookies."""
    async with async_playwright() as p:
        # Launch browser (use chromium for best compatibility)
        browser = await p.chromium.launch(
            headless=False,  # Set to True for headless operation
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
        )

        page = await context.new_page()

        print("🎵 Navigating to Apple Music...")  # noqa: T201
        await page.goto("https://music.apple.com/")

        print("⏳ Please log in to Apple Music in the browser window that opened.")  # noqa: T201
        print("   Once you're logged in and can see your library, press Enter here...")  # noqa: T201
        input()

        # Wait a moment for any redirects/page loads
        await page.wait_for_timeout(2000)

        # Get cookies from the page
        cookies = await context.cookies("https://music.apple.com")

        # Find the media-user-token cookie
        token_cookie = None
        for cookie in cookies:
            if cookie["name"] == "media-user-token":
                token_cookie = cookie
                break

        if token_cookie:
            token_value = token_cookie["value"]
            expires = (
                datetime.fromtimestamp(token_cookie["expires"])
                if "expires" in token_cookie
                else None
            )

            print("✅ Token extracted successfully!")  # noqa: T201
            print(f"🔑 Token: {token_value}")  # noqa: T201
            if expires:
                print(f"⏰ Expires: {expires.strftime('%Y-%m-%d %H:%M:%S')}")  # noqa: T201

            # Save to file
            token_data = {
                "token": token_value,
                "extracted_at": datetime.now().isoformat(),
                "expires_at": expires.isoformat() if expires else None,
            }

            with open("apple_music_token.json", "w") as f:  # noqa: ASYNC230
                json.dump(token_data, f, indent=2)

            print("💾 Token saved to apple_music_token.json")  # noqa: T201
            print("\n🎯 Use this token in Music Assistant:")  # noqa: T201
            print(f"   Music User Token: {token_value}")  # noqa: T201

        else:
            print("❌ Could not find media-user-token cookie.")  # noqa: T201
            print("   Make sure you're logged in to Apple Music.")  # noqa: T201

            # Print all available cookies for debugging
            print("\n🔍 Available cookies:")  # noqa: T201
            for cookie in cookies:
                print(f"   - {cookie['name']}")  # noqa: T201

        await browser.close()
        return token_cookie["value"] if token_cookie else None


async def main():
    """Run the Apple Music token extractor."""
    print("🍎 Apple Music Token Extractor for Music Assistant")  # noqa: T201
    print("=" * 50)  # noqa: T201

    try:
        token = await extract_apple_music_token()
        if token:
            print("\n✨ Success! Your Apple Music token is ready for Music Assistant.")  # noqa: T201
        else:
            print("\n❌ Failed to extract token. Please try again.")  # noqa: T201
    except Exception as e:
        print(f"❌ Error: {e}")  # noqa: T201


if __name__ == "__main__":
    # Install required packages:
    # pip install playwright
    # playwright install chromium
    asyncio.run(main())
