FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPEN_ARCHIVE_STATE_DIR=/data/open-archive
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home archive
COPY backend ./backend
COPY scripts ./scripts
COPY web ./web
COPY data ./data
COPY ["LLM Transcripts", "./LLM Transcripts"]
RUN python scripts/build.py --with-account
EXPOSE 8000
CMD ["python", "-m", "backend.serve"]
