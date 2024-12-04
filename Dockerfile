# Use a Python base image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy your application files to the container
COPY src/stream-monitor.py /app/

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libmagic1 \
    && apt-get clean

# Copy the requirements file and install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . /app/

# Set the command to run the script
CMD ["python", "stream-monitor.py"]
