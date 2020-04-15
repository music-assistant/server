# Upload to PyPI Live
# sudo python3 setup.py sdist bdist_wheel
# sudo python3 -m twine upload dist/*

import setuptools
import os

VERSION = "0.0.20"
NAME = "music_assistant"

with open("README.md", "r") as fh:
    LONG_DESC = fh.read()

with open('requirements.txt') as f:
    INSTALL_REQUIRES = f.read().splitlines()
if os.name != "nt":
    INSTALL_REQUIRES.append("uvloop")

setuptools.setup(
    name=NAME,
    version=VERSION,
    author='Marcel van der Veldt',
    author_email='marcelveldt@users.noreply.github.com',
    description='Music library manager and player based on sox.',
    long_description=LONG_DESC,
    long_description_content_type="text/markdown",
    url = 'https://github.com/marcelveldt/musicassistant.git',
    packages=['music_assistant'],
    classifiers=(
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ),
    install_requires=INSTALL_REQUIRES,
    )