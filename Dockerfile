# PHI Handling Console -- production image.
#
# Two stages: build the React SPA, then run the FastAPI backend which
# serves both the API and the built SPA from one process (4.15/4.3: same
# origin means the operator cookie never needs a cross-origin credential).

# --- Stage 1: frontend build -------------------------------------------------
FROM node:20-alpine AS frontend-build

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# --ignore-scripts: no package in the dependency tree needs a lifecycle
# script to build correctly (verified 2026-08, see 4.22). If a future
# dependency bump breaks this, add back only the specific package whose
# script is required, and record why here.
RUN npm ci --ignore-scripts

COPY frontend/ ./
# Empty REACT_APP_BACKEND_URL: the built bundle calls same-origin /api,
# because this image serves the API and the SPA from one process.
# INLINE_RUNTIME_CHUNK=false: this app currently builds to a single JS
# bundle so there is no separate runtime chunk to inline anyway, but if a
# future change (e.g. React.lazy route splitting) makes webpack emit one,
# an inlined <script> would violate the script-src 'self' CSP set in
# 4.20. Keep the runtime as an external file unconditionally.
ENV REACT_APP_BACKEND_URL=""
ENV INLINE_RUNTIME_CHUNK=false
RUN npm run build

# --- Stage 2: backend runtime -----------------------------------------------
FROM python:3.11-slim AS backend

# tesseract-ocr + poppler-utils (pdftoppm/pdftocairo): required by
# pytesseract/pdf2image for scanned-PDF OCR (phi_core/file_readers.py).
# Declared nowhere else in the repo before this Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /src/frontend/build /app/frontend/build

EXPOSE 8001
# --proxy-headers with the safe default --forwarded-allow-ips=127.0.0.1:
# trusts X-Forwarded-For only from a reverse proxy on the same loopback
# (or network-namespace-shared sidecar). Rate limiting (4.20) keys off
# the resolved client address, so an operator fronting this with a proxy
# on a different address MUST set FORWARDED_ALLOW_IPS to that proxy's
# address/CIDR, or every real client would share one rate-limit bucket.
# Never set it to "*" unless the network path guarantees no client can
# reach this port directly (that would let any client spoof the header
# and dodge rate limiting entirely).
CMD uvicorn server:app --host 0.0.0.0 --port 8001 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}
