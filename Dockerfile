FROM cgr.dev/chainguard/python:latest-dev as builder

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --user

FROM cgr.dev/chainguard/python:latest

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /home/nonroot/.local /home/nonroot/.local

# Copy application code
COPY . .

# Ensure the script is executable
RUN chmod +x mesh_listen.py

# Expose the default port (can be overridden with API_PORT env var)
EXPOSE 8080

# Use nonroot user for security
USER nonroot

# Set environment variables
ENV PATH="/home/nonroot/.local/bin:$PATH"
ENV PYTHONPATH="/home/nonroot/.local/lib/python3.12/site-packages:$PYTHONPATH"

# Default environment variables (can be overridden at runtime)
ENV MESH_HOST="192.168.0.91"
ENV API_HOST="0.0.0.0"
ENV API_PORT="8080"
ENV LOG_TO_CSV="true"
ENV LOG_PREFIX="meshtastic_log"
ENV REFRESH_EVERY="5.0"
ENV SHOW_UNKNOWN="true"
ENV SHOW_PER_PACKET="true"
ENV HISTORY_MAXLEN="300"
ENV HISTORY_SAMPLE_SECS="2.0"
ENV MAX_MSGS_PER_CONV="2000"

# Health check (uses API_PORT from environment)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://localhost:{os.getenv(\"API_PORT\", \"8080\")}/api/health')" || exit 1

# Run the application
CMD ["python", "mesh_listen.py"]