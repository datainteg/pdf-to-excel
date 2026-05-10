# PDF to Excel Web App (FastAPI)

FastAPI-based web app for Marathi/English voter-list PDF OCR.

You upload one or more PDF files from the browser, the app parses them, merges all records into one result set, and gives:

- One combined Excel file (`Marathi` + `English` sheets)
- Marathi CSV
- English CSV
- In-browser preview with pagination
- MongoDB-backed file storage for Excel/CSV downloads
- Login authentication for UI and API routes

Default access:

- `http://localhost` (via Dockerized Nginx)
- `http://127.0.0.1:8082` (direct FastAPI, localhost only)
- `https://<your-domain>` when SSL is issued

---

## Project Structure

```text
PDFtoWebsite/
  app/
    main.py                  FastAPI backend
  templates/
    index.html               UI page
  static/
    app.css                  UI styles
    app.js                   UI logic
  run_batch.py               OCR + parsing engine (reused by web API)
  setup.sh                   one-click Docker startup
  setup_ubuntu.sh            local Python environment setup
  docker-compose.yml
  Dockerfile
  certbot/
    conf/                    Let's Encrypt cert storage
    www/                     ACME webroot challenge files
  deploy/
    setup_ssl_certbot.sh     wrapper: domain + SSL via setup.sh
    setup_pdf_datainteg_io.sh wrapper: pdf.datainteg.io + SSL via setup.sh
    nginx/
      templates/
        http-only.conf
        https-enabled.conf
      runtime/
  output/
    jobs/<job_id>/...        generated files per request
```

---

## One-Click Docker Setup (Recommended)

```bash
chmod +x setup.sh
./setup.sh
```

Quick restart on server:

```bash
chmod +x restart.sh
./restart.sh
```

Optional auto-pull + restart:

```bash
AUTO_PULL=1 ./restart.sh
```

After setup:

- Open `http://localhost`
- Optional direct backend: `http://127.0.0.1:8082`
- Login with:
  - Username: `datainteg`
  - Password: `Welcome@911`
- Upload PDFs
- Download merged Excel/CSV outputs
- MongoDB is available at `mongodb://localhost:27017`

Manual Docker command:

```bash
docker compose up --build -d
```

Stop:

```bash
docker compose down
```

---

## Local Python Setup (without Docker)

```bash
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8082
```

Open `http://localhost:8082`

---

## API Endpoints

- `GET /` UI
- `GET /login` login page
- `POST /login` login action
- `POST /logout` logout action
- `GET /health` health check
- `POST /api/process` upload + process PDFs
- `GET /api/jobs/{job_id}` job metadata
- `GET /api/jobs/{job_id}/preview?sheet=marathi|english&page=1&page_size=25`
- `GET /api/jobs/{job_id}/download/excel`
- `GET /api/jobs/{job_id}/download/csv/marathi`
- `GET /api/jobs/{job_id}/download/csv/english`

Note: API endpoints require login session cookie from `/login`.

---

## Domain + Subdomain + SSL (Certbot)

Run this on your Ubuntu server:

```bash
sudo bash deploy/setup_ssl_certbot.sh subdomain.yourdomain.com your-email@domain.com
```

What it does:

1. Uses Dockerized Nginx (`80/443`) and FastAPI backend
2. Stops/disables host non-Docker Nginx to avoid conflict
3. Requests certificate using Certbot (webroot)
4. Switches runtime Nginx config to HTTPS

Important:

- DNS `A` record of your subdomain must point to your server IP before running Certbot.

### Direct setup for `pdf.datainteg.io`

Use the domain-specific one-command script:

```bash
sudo bash deploy/setup_pdf_datainteg_io.sh your-email@domain.com
```

This runs the same Dockerized flow with:

- `APP_DOMAIN=pdf.datainteg.io`
- your provided email for Let's Encrypt

### Direct setup with environment variables

```bash
APP_DOMAIN=pdf.datainteg.io LETSENCRYPT_EMAIL=you@example.com bash setup.sh
```

---

## Notes

- OCR languages expected: `mar` and `eng`
- Uploads are processed per job and saved under `output/jobs/<job_id>/`
- Job metadata and generated files are also stored in MongoDB (`pdf2excel` DB) when `MONGO_URI` is set
- The parser supports `--accuracy-mode` (`fast`, `balanced`, `high`) in both CLI and API workflows
- Auth credentials can be overridden by env vars:
  - `APP_AUTH_USER` (default: `datainteg`)
  - `APP_AUTH_PASS` (default: `Welcome@911`)
  - `SESSION_SECRET` (recommended to set a strong value in production)
