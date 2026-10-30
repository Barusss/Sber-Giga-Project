FROM python:3.12-slim
WORKDIR /code

# Prefer accuracy over speed for Russian technical documents.
# Tesseract 5 + official LSTM models from tessdata_best (not distro tessdata_fast).
ENV TESSDATA_PREFIX=/usr/local/share/tessdata
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr curl ca-certificates \
    && mkdir -p "$TESSDATA_PREFIX" \
    && curl --fail --location --retry 3 --output "$TESSDATA_PREFIX/rus.traineddata" \
      https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/rus.traineddata \
    && curl --fail --location --retry 3 --output "$TESSDATA_PREFIX/eng.traineddata" \
      https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/eng.traineddata \
    && tesseract --list-langs | grep -qx rus \
    && tesseract --list-langs | grep -qx eng \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir "fastembed>=0.4,<1"
COPY . .
EXPOSE 8000
