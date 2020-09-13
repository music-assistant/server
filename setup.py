"""Music Assistant setup."""
import os
from pathlib import Path

from setuptools import find_packages, setup

import music_assistant.constants as mass_const

PROJECT_NAME = "Music Assistant"
PROJECT_PACKAGE_NAME = "music_assistant"
PROJECT_LICENSE = "Apache License 2.0"
PROJECT_AUTHOR = "Marcel van der Veldt"
PROJECT_URL = "https://music-assistant.github.io/"
PROJECT_EMAIL = "marcelveldt@users.noreply.github.com"

PROJECT_GITHUB_USERNAME = "music-assistant"
PROJECT_GITHUB_REPOSITORY = "server"

PYPI_URL = f"https://pypi.python.org/pypi/{PROJECT_PACKAGE_NAME}"
GITHUB_PATH = f"{PROJECT_GITHUB_USERNAME}/{PROJECT_GITHUB_REPOSITORY}"
GITHUB_URL = f"https://github.com/{GITHUB_PATH}"

DOWNLOAD_URL = f"{GITHUB_URL}/archive/{mass_const.__version__}.zip"
PROJECT_URLS = {
    "Bug Reports": f"{GITHUB_URL}/issues",
    "Website": "https://music-assistant.github.io/",
    "Discord": "https://discord.gg/9xHYFY",
}
PROJECT_DIR = Path(__file__).parent.resolve()
README_FILE = PROJECT_DIR / "README.md"
PACKAGES = find_packages(exclude=["tests", "tests.*"])
PACKAGE_FILES = []
for (path, directories, filenames) in os.walk("music_assistant/"):
    for filename in filenames:
        PACKAGE_FILES.append(os.path.join("..", path, filename))

with open("requirements.txt") as f:
    REQUIRES = f.read().splitlines()
if os.name != "nt":
    REQUIRES.append("uvloop")

setup(
    name=PROJECT_PACKAGE_NAME,
    version=mass_const.__version__,
    url=PROJECT_URL,
    download_url=DOWNLOAD_URL,
    project_urls=PROJECT_URLS,
    author=PROJECT_AUTHOR,
    author_email=PROJECT_EMAIL,
    long_description=README_FILE.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=PACKAGES,
    include_package_data=True,
    zip_safe=False,
    install_requires=REQUIRES,
    python_requires=f">={mass_const.REQUIRED_PYTHON_VER}",
    test_suite="tests",
    entry_points={
        "console_scripts": [
            "mass = music_assistant.__main__:main",
            "musicassistant = music_assistant.__main__:main",
        ]
    },
    package_data={"music_assistant": PACKAGE_FILES},
)
