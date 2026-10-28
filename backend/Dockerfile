FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY backend/pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY backend/app ./app
COPY agent ./agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
