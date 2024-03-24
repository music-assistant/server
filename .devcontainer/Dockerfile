#syntax=docker/dockerfile:1.6

FROM mcr.microsoft.com/vscode/devcontainers/python:3.12

RUN <<EOF
set -x
set -e
apt-get update
apt-get install -yq --no-install-recommends \
  ffmpeg \
  ;
apt-get clean
rm -rf /var/lib/apt/lists/*
EOF
