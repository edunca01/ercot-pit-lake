FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn apt source whose signing key has expired, which makes every
# `apt-get update` (and so every feature install) fail. Nothing here uses Yarn.
RUN rm -f /etc/apt/sources.list.d/yarn.list
