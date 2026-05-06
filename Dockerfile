# Use the official lightweight Python image
FROM python:3.11-slim

# Allow statements and log messages to immediately appear in the Knative logs
ENV PYTHONUNBUFFERED True

# Set the working directory in the container
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Run the web service on container startup using uvicorn
# Cloud Run expects the app to listen on the port defined by the PORT environment variable
CMD exec uvicorn spotbot:app --host 0.0.0.0 --port $PORT