FROM python:3.13-alpine

# Install borgbackup, openssh-client, and Python dependencies
RUN apk add --no-cache borgbackup openssh-client && \
    pip install --no-cache-dir PyYAML

# Copy Python script
COPY run.py /app/run.py
RUN chmod +x /app/run.py

# Run as PID 1 for proper signal handling
ENTRYPOINT ["python3", "/app/run.py"]
