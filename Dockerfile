FROM python:3.12-bullseye

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates

ADD https://astral.sh/uv/install.sh /uv-installer.sh

RUN sh /uv-installer.sh && rm /uv-installer.sh

ENV PATH="/root/.local/bin/:$PATH"

ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY . /app

RUN uv venv

RUN uv sync --locked

EXPOSE 8765