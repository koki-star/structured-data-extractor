FROM python:3.11-slim

# Get uv from its official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so Docker can cache this step
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# Copy the app code and the sample files
COPY src ./src
COPY samples ./samples

EXPOSE 8501
CMD ["uv", "run", "--no-sync", "streamlit", "run", "src/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
