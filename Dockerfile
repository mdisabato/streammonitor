FROM python:3.11-alpine

# Install FFmpeg and build dependencies
RUN apk add --no-cache \
    ffmpeg \
    ffmpeg-dev \
    pkgconfig \
    gcc \
    musl-dev \
    python3-dev \
    build-base \
    make

# Create and set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ .

# Create non-root user
RUN adduser -D streammonitor
USER streammonitor

# Command to run the application
CMD ["python", "stream_monitor.py"]#
