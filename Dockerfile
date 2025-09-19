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

# Expose the default port
EXPOSE 8080

# Use nonroot user for security
USER nonroot

# Set environment variables
ENV PATH="/home/nonroot/.local/bin:$PATH"
ENV PYTHONPATH="/home/nonroot/.local/lib/python3.12/site-packages:$PYTHONPATH"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

# Run the application
CMD ["python", "mesh_listen.py"]