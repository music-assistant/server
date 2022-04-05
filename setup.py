"""Music Assistant setup."""
import os
from pathlib import Path

from setuptools import find_packages, setup

PROJECT_NAME = "Music Assistant"
PROJECT_PACKAGE_NAME = "music_assistant"
PROJECT_VERSION = "1.0.7"
PROJECT_REQ_PYTHON_VERSION = "3.9"
PROJECT_LICENSE = "Apache License 2.0"
PROJECT_AUTHOR = "Marcel van der Veldt"
PROJECT_URL = "https://music-assistant.github.io/"
PROJECT_EMAIL = "marcelveldt@users.noreply.github.com"

PROJECT_GITHUB_USERNAME = "music-assistant"
PROJECT_GITHUB_REPOSITORY = "server"

PYPI_URL = f"https://pypi.python.org/pypi/{PROJECT_PACKAGE_NAME}"
GITHUB_PATH = f"{PROJECT_GITHUB_USERNAME}/{PROJECT_GITHUB_REPOSITORY}"
GITHUB_URL = f"https://github.com/{GITHUB_PATH}"

DOWNLOAD_URL = f"{GITHUB_URL}/archive/{PROJECT_VERSION}.zip"
PROJECT_URLS = {
    "Bug Reports": f"{GITHUB_URL}/issues",
    "Website": "https://music-assistant.github.io/",
    "Discord": "https://discord.gg/9xHYFY",
}
PROJECT_DIR = Path(__file__).parent.resolve()
README_FILE = PROJECT_DIR / "README.rst"
REQUIREMENTS_FILE = PROJECT_DIR / "requirements.txt"
PACKAGES = find_packages(exclude=["tests", "tests.*"])
PACKAGE_FILES = []
for (path, directories, filenames) in os.walk("music_assistant/"):
    for filename in filenames:
        PACKAGE_FILES.append(os.path.join("..", path, filename))

setup(
    name=PROJECT_PACKAGE_NAME,
    version=PROJECT_VERSION,
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
    install_requires=REQUIREMENTS_FILE.read_text(encoding="utf-8"),
    python_requires=f">={PROJECT_REQ_PYTHON_VERSION}",
    test_suite="tests",
    entry_points={
        "console_scripts": [
            "mass = music_assistant.__main__:main",
            "musicassistant = music_assistant.__main__:main",
        ]
    },
    package_data={"music_assistant": PACKAGE_FILES},
)
