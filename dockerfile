# Start from an official Python base image
# This is like a fresh Linux machine with Python already installed
FROM python:3.11-slim

# Set the working directory inside the container
# All subsequent commands run from here
WORKDIR /app

# Copy your requirements first (explained below why this order matters)
COPY requirements.txt .

# Install your Python dependencies inside the container
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code into the container
COPY main.py .
COPY static/ ./static/

# Tell Docker this container listens on port 8000
EXPOSE 8000

# The command that runs when the container starts
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]