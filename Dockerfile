# Use lightweight Python base image
FROM python:3.10-slim

# Set working directory inside container
WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project code into the container
COPY . .

# Run Uvicorn server on port 8080 (Google Cloud default)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]