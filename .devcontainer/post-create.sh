#! /usr/bin/env bash


if [ ! -d .venv ]
then
  python -m venv .venv
fi

source .venv/bin/activate

pip install -e .[test]
pip install -r requirements_all.txt
pre-commit install
