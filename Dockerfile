FROM python:3.11-slim

# System libraries required by PyMuPDF
RUN apt-get update && apt-get install -y \
    libmupdf-dev \
    libfreetype6 \
    libharfbuzz0b \
    libjpeg62-turbo \
    libopenjp2-7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify PyMuPDF loads correctly at build time — fails build if broken
RUN python3 -c "import fitz; print('PyMuPDF OK:', fitz.version)"

COPY . .

EXPOSE 8080

CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --timeout 300 --workers 1 --log-level debug --access-logfile - app:app
