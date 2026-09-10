FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir uv==0.12.9
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY agentbus_service.py ./
RUN useradd --uid 10001 --create-home agentbus \
    && mkdir /data && chown agentbus:agentbus /data
USER agentbus
ENV AGENTBUS_DB_PATH=/data/messages.sqlite3
EXPOSE 8766
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "agentbus_service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8766", "--no-access-log"]
