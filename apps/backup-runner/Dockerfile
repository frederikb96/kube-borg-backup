FROM python:3.13-alpine

# Install system dependencies for borg, ssh, rsync, and FUSE
# Note: pyfuse3 and borgbackup require many dev libraries for compilation
# Install borgbackup via pip (Alpine package lacks FUSE support)
RUN apk add --no-cache openssh-client rsync fuse fuse3 fuse3-dev gcc musl-dev linux-headers \
        lz4-dev openssl-dev acl-dev zstd-dev xxhash-dev && \
    pip install --no-cache-dir PyYAML pyfuse3 borgbackup

# Copy all Python modules
COPY common.py /app/common.py
COPY backup.py /app/backup.py
COPY list.py /app/list.py
COPY restore.py /app/restore.py
RUN chmod +x /app/backup.py /app/list.py /app/restore.py

WORKDIR /app

# Default to backup operation (backward compatible)
# Using CMD allows easy override in pod spec: command: ["python3", "/app/list.py"]
CMD ["python3", "/app/backup.py"]
