FROM python:3.12-slim AS runtime
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY app/ ./app/
EXPOSE 8090
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
