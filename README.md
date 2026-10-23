# TT Generator

TT Generator is a Class 12 study planner. It combines a syllabus catalogue, a
database-backed timetable engine, daily and weekly task views, recovery for
missed work, revision tracking, mock-test planning, and student/admin workload
review.

The application is built with Flask, SQLAlchemy, Flask-Login, Flask-WTF and
Flask-Migrate. SQLite is used for local development; PostgreSQL is recommended
for production.

## Features

- Student registration and login, onboarding for board, subjects, routine and
  study preferences.
- Conflict-aware scheduling around school, tuition, travel, sleep and existing
  tasks.
- Learning, practice and revision tasks with daily caps, breaks and lighter days.
- Plan regeneration that preserves completed work and reports unplaced work
  instead of silently dropping it.
- Recovery, revision tracker, mock tests, chapter progress and workload pages.
- Chapter-level confidence and revision-only overrides for students.
- Admin syllabus import, workload comparison, estimate review, student
  management, audit log and planner defaults.
- Production safeguards: mandatory strong secrets, CSRF, rate limiting, secure
  session cookies, security headers and centralized error handling.

## Requirements

- Python 3.11 or newer
- SQLite for local use, or PostgreSQL for production
- A Linux host with Nginx and systemd for the included deployment templates

## Local setup

From the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set a long random `SECRET_KEY` and a strong `ADMIN_PASSWORD`.

Initialize and seed the database:

```powershell
flask --app wsgi db upgrade
flask --app wsgi seed-syllabus
flask --app wsgi check-health
```

Run the development server on port 5055:

```powershell
python app.py
```

Open `http://127.0.0.1:5055/`. The administrator panel is at `/admin/`.

## Syllabus dataset

The bundled file is `data/syllabus/cbse_class12_2026_27.json`:

- CBSE Class 12, academic year 2026-27
- 11 subjects and 130 active chapters
- Chapter catalogue checked against official CBSE curriculum links
- Chapter-wise learning, practice and revision hours inferred from scope and
  comparable course material

CBSE publishes chapter scope and marks, not study-time allocations. The workload
hours are planning estimates and are labelled that way in the UI. They are not
board-prescribed durations.

The dataset is reproducible:

```powershell
python scripts/build_cbse_class12_dataset.py
flask --app wsgi validate-syllabus --file data/syllabus/cbse_class12_2026_27.json
flask --app wsgi seed-syllabus --file data/syllabus/cbse_class12_2026_27.json
```

Re-importing the current version label updates the existing version in place.
This preserves student subject links and completed task records while syncing
the catalogue. Chapters no longer in the dataset are marked removed rather than
deleted.

### Personalization

Subject confidence and optional chapter overrides affect learning, practice and
revision minutes. Overrides are bounded:

- Lower confidence increases learning time modestly.
- Practice and revision cannot be erased by confidence changes.
- `revision_only` reduces first-time learning to 15%, keeps 50% of practice and
  retains the full revision allocation.

Students must regenerate their plan after changing a chapter preference. The
workload pages show where overrides differ from the default estimate.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The test suite covers authentication, onboarding, plan generation, scheduling
constraints, task transitions, the bundled dataset and workload routes.

## Production deployment

The files in `deployment/` are templates for:

- `tt-generator.service`: Gunicorn under systemd
- `gunicorn.conf.py`: Gunicorn binding and worker settings
- `nginx-tt-generator.conf`: HTTPS reverse proxy for `tt.techorigin.online`
- `.env.production.example`: production environment template

Typical Ubuntu layout:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin ttgen
sudo mkdir -p /opt/tt-generator
sudo chown ttgen:ttgen /opt/tt-generator
# Extract the release into /opt/tt-generator as ttgen
```

Create `/opt/tt-generator/.env` from
`deployment/.env.production.example`. At minimum set:

- `SECRET_KEY`
- `ADMIN_PASSWORD`
- `DATABASE_URL`
- `SESSION_COOKIE_SECURE=true`
- `FLASK_ENV=production`

Generate secrets with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Install the application:

```bash
cd /opt/tt-generator
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app wsgi db upgrade
.venv/bin/flask --app wsgi seed-syllabus
.venv/bin/flask --app wsgi check-health
```

Install the service and proxy:

```bash
sudo cp deployment/tt-generator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tt-generator
sudo cp deployment/nginx-tt-generator.conf /etc/nginx/sites-available/tt-generator
sudo ln -s /etc/nginx/sites-available/tt-generator /etc/nginx/sites-enabled/tt-generator
sudo nginx -t
sudo systemctl reload nginx
```

Obtain TLS certificates with Certbot or your normal certificate process. Update
the certificate paths in the Nginx configuration if they differ.

Verify:

```bash
curl -fsS https://tt.techorigin.online/healthz
sudo journalctl -u tt-generator -n 100 --no-pager
```

Before deploying a new release, run migrations and repeat the health check.
Back up PostgreSQL and `/opt/tt-generator/.env` together.

## Operational notes

- Production refuses to start with missing, short or placeholder secrets.
- Static files are served by Nginx from `/opt/tt-generator/app/static/`.
- The app writes rotating logs to `logs/` by default.
- SQLite production use is allowed but logs a warning; use PostgreSQL for
  multiple workers and durable operations.
- The in-memory rate-limit store is per process. Use a shared backend if strict
  cross-worker limits are required.
- The planner extrapolates capacity beyond 28 days from the student's routine;
  the UI marks this as approximate.
- Dataset chapters are chapter-level: many have no topic breakdown and are
  planned at chapter level.

## Project layout

```text
app/
  admin/        admin routes and templates
  auth/         registration, login and password flows
  models/       SQLAlchemy models
  planner/      scheduler, plan engine, progress, recovery, workload
  services/     account, task and recovery services
  static/       CSS, JS and images
  student/      student routes
  syllabus/     dataset loader and validator
  templates/    Jinja templates
data/syllabus/  bundled CBSE Class 12 dataset
deployment/     systemd, Gunicorn, Nginx and environment templates
migrations/     Alembic migrations
scripts/        reproducible dataset generator
tests/          pytest suite
```
