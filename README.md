# Backend guide

The backend is a FastAPI service that provides the camp-management API, applies access rules, and stores camp records in MongoDB. The browser app is in `../frontend`.

## What it does

- **Accounts and access:** issues signed sessions for Camp Directors, Platoon Officers, and corps members. Each API operation checks the caller's role; member records are looked up from the signed-in identity.
- **Streams and camp settings:** creates and activates batch streams, suspends operations, and closes a stream into a closure-review process. Closed streams are archived and read-only. Stores the camp attendance centre, allowed distance, and GPS accuracy limit.
- **Registration and verification:** accepts QR-linked member registrations, validates call-up numbers and consent, securely stores enrolment images, and gives the Director a verification queue.
- **Beds and hostels:** creates upper/lower beds, automatically assigns available beds to verified members, and keeps assignments aligned with member gender and hostel gender. Supports safe reassignments, releases, assignment history, and member-to-member swaps.
- **Platoons:** assigns members automatically using the final digit of their NYSC state code and the Director's 0–9 mapping. Members may add their state code after registration. Assignments do not wait for Director confirmation. Missing codes or mappings remain visible as exceptions.
- **Attendance:** opens weekly schedules, checks face matching before location, records each automated result separately from the final decision, and permits reasoned Director overrides without replacing evidence.
- **Exit management:** supports category-specific temporary and rest-of-camp requests, secure evidence uploads and review, optional Platoon Officer confirmation, Director decisions, departure and return recording, bed reservation/release, and audit history.
- **Camp communication:** complaints and responses, announcements, inbox notifications, and optional Web Push.
- **Reports and audit:** CSV/PDF reports, analytics, audit-log export, and member assignment slips.
- **Scheduled maintenance:** a background loop checks overdue exits, assigns waiting verified members when beds become available, sends attendance reminders, and purges expired attendance selfies.

## Roles and API access

| Role | API scope |
| --- | --- |
| Camp Director | Camp-wide administration, member verification, streams, hostels, platoons, attendance review/overrides, exit decisions, announcements, analytics, reports, and audit log. |
| Platoon Officer | Their assigned platoon roster and exit requests routed to that platoon. Can confirm details or send a request to the Director for review. |
| Corps member | Their own profile, bed, platoon, attendance, exits, swaps, complaints, announcements, and inbox. Member identity comes from the session, not a call-up number supplied to each request. |

The API is grouped under `/api`. FastAPI's interactive API reference is available at `/docs` while the service is running. `/api/health` checks MongoDB connectivity.

## Local setup

Run these commands from the repository root:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your local database and service settings.
uvicorn app.main:app --reload --reload-dir app --port 8000
```

The backend reads `.env` from its current working directory, so start Uvicorn from `backend/`. MongoDB must be reachable before startup; startup creates indexes and seeds the Director account if it does not already exist. Configure `DIRECTOR_USERNAME` and `DIRECTOR_PASSWORD` before first startup, then change the initial password and use a strong `JWT_SECRET`.

The frontend's local API address is `http://localhost:8000`. Set `CORS_ORIGINS` to the exact frontend origin, such as `http://localhost:5173`.

## Configuration

`backend/.env.example` lists the available settings. Main groups:

- **Database and sessions:** `MONGO_URI`, `MONGO_DB`, `JWT_SECRET`, `JWT_MINUTES`, `CORS_ORIGINS`.
- **Camp operations:** `CALLUP_REGEX`, `CAMP_LAT`, `CAMP_LON`, `CAMP_RADIUS_M`, `MAX_GPS_ACCURACY_M`, and `SELFIE_RETENTION_DAYS`. The Director can update the location area in Reports and settings.
- **Private image/document storage:** Cloudinary cloud name, API key, and secret. Registration photos and exit documents use authenticated/private storage and are served through authorized API routes.
- **Face checks:** `FACE_PROVIDER` supports `opencv`, `rekognition`, or `none`. OpenCV requires the YuNet and SFace model files under `backend/app/models`. Rekognition requires AWS credentials. If a provider or its models are unavailable, submissions go to manual review; the service does not mark an unavailable check as a match. Configure and validate thresholds with consented representative data before live use.
- **Optional services:** AWS region and face thresholds, liveness options, SMTP, and VAPID keys for Web Push.

GPS and camera access require HTTPS in production. `localhost` is treated as a secure context by modern browsers for local development.

## Important behavior

- Only active streams accept operational writes. Upcoming and suspended streams are not open for member operations; closed streams are archived and cannot be reopened through the API.
- Bed assignment selects only active hostels with a matching gender. If a member has no valid gender or there is no matching free bed, assignment is not made.
- Platoon assignment is automatic once both the member's numeric state code and its final-digit mapping are available.
- Attendance is face-first: location is evaluated only after the face check passes. Inconclusive or unavailable checks require Director review.
- A temporary exit reserves the member's bed. A rest-of-camp exit follows the Director's bed-release rule. When a return is recorded, the request lock is cleared; a bed is restored or automatically assigned if one is available.
- Audit events record sensitive administrative decisions. API error responses avoid returning internal database connection details.

## Tests

From `backend/` with dependencies installed:

```bash
pytest -q
```

Pure rule tests do not require MongoDB. API integration tests need a reachable MongoDB test database; set a separate test database in the test environment so test data does not mix with camp records.

## Deployment notes

Use a managed MongoDB deployment with tested backups, HTTPS, a strong secret, restricted CORS, private storage credentials, and a completed privacy/retention policy. The background loop and rate limiter are single-process; use a dedicated scheduler and shared rate limiting if deploying multiple backend instances.
