FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full project
COPY . .

# Ensure data directory exists (EFS will overlay this at runtime in prod)
RUN mkdir -p /app/data || true

EXPOSE 8080

# Disable Python output buffering so logs appear instantly in App Runner
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
