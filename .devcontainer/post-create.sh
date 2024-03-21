#! /usr/bin/env bash

pre-commit install

if [ ! -d .venv ]
then
  python -m venv .venv
fi

source .venv/bin/activate

pip install -e .[test]
pip install -r requirements_all.txt
