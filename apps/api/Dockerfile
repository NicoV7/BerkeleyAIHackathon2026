# Pin 3.12 for broad wheel availability (host may be 3.14).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv for fast dependency resolution.
RUN pip install --no-cache-dir uv

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN uv pip install --system -e .

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
