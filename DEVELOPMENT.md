Developer docs
==================================

## 📝 Prerequisites
* ffmpeg (minimum version 4, version 5 recommended), must be available in the path so install at OS level
* Python 3.11 is minimal required (or check the pyproject for current required version)
* [Python venv](https://docs.python.org/3/library/venv.html)

It is recommended to use Visual Studio Code as your IDE, since launch files to start Music Assistant are provided as part of the repository. Furthermore, the current code base is not verified to work on a native Windows machine. If you would like to develop on a Windows machine, install [WSL2](https://code.visualstudio.com/blogs/2019/09/03/wsl2) to increase your swag-level 🤘.

## 🚀 Setting up your development environment
With this repository cloned locally, execute the following commands in a terminal from the root of your repository:
* `python -m venv .venv` (create a new separate virtual environment to nicely separate the project dependencies)
* `source .venv/bin/activate` (activate the virtual environment)
* `pip install -r requirements_all.txt` (install the project's dependencies)
* Hit (Fn +) F5 to start Music Assistant locally
* The pre-compiled UI of Music Assistant will be available at `localhost:8095` 🎉

## 🎵 Building your own Music Provider
Will follow soon™

## ▶️ Building your own Player Provider
Will follow soon™

## 💽 Building your own Metadata Provider
Will follow soon™

## 🔌 Building your own Plugin Provider
Will follow soon™
