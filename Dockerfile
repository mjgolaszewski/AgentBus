FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY agentbus_service.py ./
COPY src/ ./src/
RUN useradd --uid 10001 --create-home agentbus \
    && mkdir /data && chown agentbus:agentbus /data
USER agentbus
ENV AGENTBUS_DB_PATH=/data/messages.sqlite3
EXPOSE 8766
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "agentbus_service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8766", "--no-access-log"]
