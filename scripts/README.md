# Scripts

This directory contains utility scripts for Music Assistant development and setup.

## Apple Music Token Extractor

The `apple_music_token_playwright.py` script helps you extract the required user token for the Apple Music provider in Music Assistant.

### Prerequisites

1. **Python 3.12+** - The script requires Python 3.12 or higher
2. **Playwright** - Install Playwright and its browser dependencies:
   ```bash
   pip install playwright
   playwright install chromium
   ```

### Usage

1. **Run the script:**
   ```bash
   python scripts/apple_music_token_playwright.py
   ```

2. **Follow the prompts:**
   - The script will open a browser window to Apple Music
   - Log in to your Apple Music account in the browser
   - Once logged in and you can see your library, press Enter in the terminal
   - The script will extract your user token and save it to `apple_music_token.json`

3. **Use the token in Music Assistant:**
   - Copy the token value from the output or from `apple_music_token.json`
   - In Music Assistant, go to Settings → Music Providers → Apple Music
   - Paste the token in the "Music User Token" field

### What the script does

- Opens a browser window to Apple Music
- Waits for you to log in manually (this ensures the token is valid)
- Extracts the `media-user-token` cookie from your browser session
- Saves the token with metadata (extraction time, expiration) to a JSON file
- Provides the token value for use in Music Assistant

### Token expiration

Apple Music user tokens typically expire after 180 days. When your token expires:
1. Re-run this script to get a new token
2. Update the token in your Music Assistant configuration

### Troubleshooting

- **"Could not find media-user-token cookie"**: Make sure you're fully logged in to Apple Music in the browser window
- **Browser doesn't open**: Ensure Playwright is properly installed with `playwright install chromium`
- **Permission errors**: Make sure you have write permissions in the current directory for the JSON output file

### Security notes

- The token is a semi-private key in JWT format
- Store it securely and don't share it publicly
- The script saves the token locally for convenience, but you can delete the JSON file after copying the token to Music Assistant

### Alternative methods

If you prefer to extract the token manually:
1. Open Apple Music in your browser
2. Open Developer Tools (F12)
3. Go to Application/Storage → Cookies → https://music.apple.com
4. Find the `media-user-token` cookie and copy its value
