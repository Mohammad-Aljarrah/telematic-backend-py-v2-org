# Use lightweight Python base image
FROM python:3.11-slim

# Do not write .pyc files; send logs straight to Cloud Run's log viewer without buffering.
# PORT is the default for local runs; Cloud Run overrides it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Set working directory inside container
WORKDIR /app

# Copy dependency definition first so this layer is cached between code changes
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the app as a normal user instead of root
RUN useradd --create-home --uid 10001 appuser

# Copy all project code into the container (see .dockerignore for what is left out)
COPY --chown=appuser:appuser . .
USER appuser

# Run Uvicorn on $PORT (Cloud Run sets it to 8080); trust Cloud Run's proxy headers
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"