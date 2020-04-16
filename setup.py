from pathlib import Path

from setuptools import find_packages, setup

PROJECT_DIR = Path(__file__).parent.resolve()
README_FILE = PROJECT_DIR / "README.md"
VERSION = "0.0.20"

with open("requirements.txt") as f:
    INSTALL_REQUIRES = f.read().splitlines()
if os.name != "nt":
    INSTALL_REQUIRES.append("uvloop")

setup(
    name="music_assistant",
    version=VERSION,
    url="https://github.com/marcelveldt/musicassistant",
    download_url="https://github.com/marcelveldt/musicassistant",
    author="Marcel van der Veldt",
    author_email="m.vanderveldt@outlook.com",
    description="Music library manager and player based on sox.",
    long_description=README_FILE.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=find_packages(exclude=["test.*", "test"]),
    python_requires=">=3.7",
    include_package_data=True,
    install_requires=INSTALL_REQUIRES,
    zip_safe=False,
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Topic :: Home Automation",
    ],
)
