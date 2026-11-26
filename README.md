# Foyo — Backend (FastAPI)

> Two separate projects: this one is the **API**; the mobile app is **`../foyo`** (Flutter, its own
> repository), which also holds the spec and the legal texts in its `docs/`.

FastAPI + SQLAlchemy 2 + Alembic. SQLite for development, Postgres in production (`DATABASE_URL`).
Every endpoint the mobile app uses is implemented; the JSON matches the app's `lib/features/*/data` (separate project `../foyo`).

## Run (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- `.env` contains `ENVIRONMENT=development` (git-ignored). Without it the server runs in **production mode**.
- OpenAPI / try-it: http://localhost:8000/docs
- In development, start-up applies the migrations and loads the demo household **Famille Kouassi**
  (dated relative to today, 5 weeks of task history, 6 months of operations).
  Demo logins: `awa.kouassi@email.com` (admin) and `koffi.k@email.com` (adult), password `foyo1234`.
- Reset the demo data: `.venv/bin/python -m scripts.reset_db` (refused outside development)
- Tests: `.venv/bin/python -m pytest -q` — on Postgres: `DATABASE_URL=postgresql+psycopg://… .venv/bin/python -m pytest -q`

## Deploy (production)

```bash
export POSTGRES_PASSWORD=…  JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
docker compose up -d --build
```

- The image runs as a non-root user, applies `alembic upgrade head` once, then starts uvicorn
  (`WEB_CONCURRENCY` workers, default 2) with a `/healthz` health check.
- Put it behind HTTPS (Caddy, Nginx, a PaaS load balancer): release builds of the app only accept `https://`.
- Production refuses to start with a missing/short `JWT_SECRET`, `SEED_DEMO=true` or `CORS_ORIGINS=["*"]`,
  and warns when `DATABASE_URL` is SQLite. Demo data is never loaded.
- Times ("today", due dates, the evening report) use `TIMEZONE` (default `Africa/Abidjan`), not the server clock.
- Sign-in locks an account for 15 min after 5 wrong passwords (`LOGIN_MAX_FAILURES`, `LOGIN_LOCK_MINUTES`);
  a password reset unlocks it.

## Accounts (F1)

- **E-mail verification**: sign-up e-mails a link (`GET /auth/verify-email?token=…`, valid 7 days, built
  from `PUBLIC_API_URL`); `POST /auth/verify-email/resend`.
- **Google / Apple**: `POST /auth/social {provider, id_token}`. The server checks the ID token with the
  provider's keys (`GOOGLE_CLIENT_IDS`, `APPLE_AUDIENCES`; empty = answers 503 "non configurée"). An
  existing account with the same *verified* address is linked; an unverified address never takes over one.
- **Invite code** (WhatsApp / SMS): `POST|DELETE /households/current/invite-link` (new code for a role,
  valid 7 days / turn off), `GET /invitations/code/{code}` (preview), sign-up with `invite_code`, or
  `POST /invitations/join {code}` for an existing account. Households created before this change start
  with the link off.
- Assigning a task e-mails the assignee as well as the push.

## Premium (RevenueCat)

- Every household starts free. Free limits: 3 members (pending invitations count), 10 recurring tasks,
  2 accounts, 50 operations per month, 3 budgets. Premium only: rotation, task statistics, past finance
  reports, savings goals, tontines, debts, and the cross-module links of the Complet plan. A blocked
  action answers **402** with a French message ending in "Essai gratuit de 7 jours dans Réglages."
- `POST /subscriptions/trial` (admin, once per household) gives 7 days. `GET /subscriptions/current`
  returns `is_premium`, `premium_until`, `trial_used`, `billing_id`, `limits` and `usage`.
- Store purchases go through RevenueCat with the household id as app user id (`billing_id`).
  Point the RevenueCat webhook at `POST /api/v1/webhooks/revenuecat` and set the same value as its
  Authorization header and as `REVENUECAT_WEBHOOK_SECRET` (unset = 503). Product ids must contain the plan
  (`complete` / `tasks` / `finances`) and `year` or `month`, e.g. `foyo_complete_year`.

## Timer (spec §5.5)

- `POST /tasks/{id}/start` records who started it; a second running task for the same person answers
  **409** "… une tâche à la fois". `POST /tasks/{id}/stop` (or `/occurrences/{id}/stop`) drops the timer.
- The worker asks "Toujours en cours ?" at 3× the estimate, and at 4 h stops the timer itself: the task is
  done with the estimated duration (marked "estimée"), and the person is told.
- Proof at validation: `note` in the complete body or `PATCH /occurrences/{id}`; photo with
  `POST /occurrences/{id}/photo` (multipart `file`, JPEG/PNG/WebP, 5 MB max), read back with `GET`.
  Files live in `MEDIA_DIR` (a `media` volume in docker-compose).

## Personal data, health consent, telemetry

- `GET /auth/me/export`: everything stored about the caller, as a JSON file. `POST /auth/me/delete`
  `{confirm: "SUPPRIMER", password}` (no password for Google/Apple-only accounts): alone in the household,
  the whole household is erased; otherwise the person leaves it (admin role passes on), their health data
  and private accounts are erased, and the shared history shows "Ancien membre".
- Health data needs explicit consent: `/health` answers **428** until `POST /health/consent`.
  `DELETE /health/consent?erase=true` withdraws it (and erases the caller's own medications and
  appointments). Without consent, no medication reminders reach that person.
- `POST /telemetry/events` (opt-in usage statistics) and `POST /telemetry/crashes` take no account: a
  random install id only. Kept 90 days (worker purge). Summary:
  `.venv/bin/python -m scripts.telemetry_report 7`.
- Rate limits (answer **429** with `Retry-After`): per client IP 120 event requests and 30 crash reports a
  minute, per install 20 event requests a minute, 2 000 events and 30 crashes an hour (`TELEMETRY_*`
  settings). The per-IP check runs before the body is validated. Counts are in memory, per API process
  (`WEB_CONCURRENCY` workers each count separately).
- Client IP: uvicorn trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`). Behind a
  reverse proxy, set it to the proxy's address or network (e.g. `172.16.0.0/12` for a proxy container);
  otherwise every client looks like the proxy and shares one limit. Never `*` when port 8000 is reachable
  from outside: anyone could fake their IP.

## Menus (F14)

- 30 starter dishes (`app/application/starter_dishes.py`) are loaded on every start, demo or not; ids never
  change. Households add their own (`POST/PATCH/DELETE /dishes`), copy a starter to adjust it
  (`POST /dishes/{id}/copy`) and keep favourites (`PUT /dishes/{id}/favorite`, per household).
- Week editing: `POST/PATCH/DELETE /meal-plans` (default time per meal, cost = ingredients to buy),
  `POST /meal-plans/copy {from_week, to_week, replace}` (409 when the target week already has meals).
  `GET /meal-plans` returns the 7 days, empty ones included.
- `POST /shopping-lists/generate?week=` rebuilds the list from the menu: the same ingredient is summed
  across meals ("5 boules" ×3 → "15 boules", "500 g" + "1 kg" → "1,5 kg"); items added by hand
  (`POST /shopping-lists/items`) and ticks are kept. Ticks are saved (`PATCH /shopping-lists/items/{id}`).

## Finances (F15–F19)

- `fee` on `POST /operations`: written as a separate "frais" expense from the same account, linked by
  `parent_id` and deleted with it. `repeat_every_months` (1, 3, 12) also creates a recurring operation.
- `GET/POST/PATCH/DELETE /recurring-operations`: the worker writes them on their day (day 31 = last day),
  catches up after downtime, skips while the free monthly limit is reached, and tells the author.
- Receipt photo: `POST/GET /operations/{id}/receipt` (JPEG/PNG/WebP, 5 MB, `MEDIA_DIR`).
- Budget alerts: an expense that brings a category to 80 % then over 100 % of its monthly budget pushes the
  admins and adults, once per threshold and month.
- `POST /savings/debts/{id}/repay {amount, account_id?}` (partial repayments; with an account, written as
  income or expense), `POST /savings/tontines`, `POST /savings/tontines/{id}/pay {account_id}`; tontine
  reminders at 18:30 the day before and on the contribution day ("C’est votre tour" on the payout month).
- `GET /reports/finances?period=month|quarter|year` and `GET /operations/export.csv?period=…`
  (`;`-separated, UTF-8 with BOM for Excel; past periods, quarter and year are Premium).

## Deadlines and links between modules

- Bills: `POST /deadlines/bills {label, amount, due_date, every_months?}`, `DELETE /deadlines/bills/{id}`;
  paying a recurring bill creates the next one. Reminders at 18:30 on J-7, J-3, J-1 and the due day.
- Vehicle: `PUT /deadlines/vehicles/{id}/mileage` (can't go back); `POST /deadlines/maintenances/{id}/done
  {cost?, mileage?}` reschedules it (km and/or date) and, in Complet, writes the cost as a Transport expense.
- Medication stock falling to its alert level goes on the week's shopping list ("Stock santé bas", Complet);
  `POST /health/stocks/{id}/shopping` does it by hand. `stock_alerts` carry `id` and `in_shopping_list`.
- Appointments: push the evening before (18:30) and 2 h before, to the person or the admin who manages
  their profile, with health consent.
- Daily report (Complet, admins and adults): overspent budgets of the month in the 21:00 push and in
  `GET /reports/daily` (`budget_alert`).

## Offline replays

Actions the app made offline are replayed later with an `Idempotency-Key` header: the server stores
its answer for 30 days and returns it again for the same key, without applying the action twice
(`app/api/idempotency.py`). `POST /tasks/{id}/complete` and `/start` accept the phone's `completed_at` /
`started_at` and the occurrence `date`; when two members tick the same task, the earliest timestamp
wins and points are counted once. `POST /health/intakes/{id}/toggle?taken=true|false` sets an explicit
state so a late replay can't flip it back.

## Database migrations

Schema changes go through Alembic. After editing `app/infrastructure/db/models.py`:

```bash
.venv/bin/alembic revision --autogenerate -m "what changed"   # review the generated file
.venv/bin/alembic upgrade head
```

`tests/unit/test_migrations.py` fails if the models and the migrations drift apart, and checks that
upgrading a SQLite database that already holds data keeps every row (SQLite rebuilds tables for most
changes; foreign keys are switched off during migrations so the rebuild can't cascade). Every test builds
its database through the migrations (SQLite and Postgres both pass).

## E-mail (Gmail SMTP)

Password-reset codes and household invitations are e-mailed. By default (`EMAIL_BACKEND=log`) they are only printed in the server log.
To send them through Gmail:

1. On the Google account that will send the mails, turn on 2-Step Verification.
2. Create an App Password at https://myaccount.google.com/apppasswords (Gmail refuses the normal password).
3. In `.env`:
   ```
   EMAIL_BACKEND=smtp
   SMTP_USERNAME=your.address@gmail.com
   SMTP_PASSWORD=the 16-character app password
   ```
   `SMTP_HOST=smtp.gmail.com` and `SMTP_PORT=587` (STARTTLS) are the defaults; port 465 uses implicit TLS.
4. Restart the API. A missing setting is reported at start-up; a refused login is logged on the first send.

Mails go out in a background task after the response, so a slow or failing SMTP server never blocks the app.
Consumer Gmail is limited to about 500 recipients a day and the sender is always that Gmail address
(or a verified alias via `SMTP_FROM`): use a transactional provider before going to production.

## Push notifications (Firebase Cloud Messaging)

| When | Who | Sent by |
|---|---|---|
| A task is given to you (create / reassign) | the assignee | API, right away |
| You're invited to another household (existing account) | the invitee | API |
| You become the household admin | the new admin | API |
| Task reminder: task time (8:00 when it has none) − its reminder offset | the assignee | worker |
| Task still to do 60 min after its time ("En retard"); 3 h after: the admin(s) too | assignee, then admins | worker |
| Medication intake time (unless already confirmed) — **not muted by quiet hours** | the member | worker |
| 21:00 daily report | every member with an account | worker |
| Bill due in 3 days, tomorrow, today (18:30) | admins and adults | worker |

- A child or other profile without an account is represented by the admin who manages it.
- Preferences per account: push on/off, quiet hours (22:00 → 6:00), daily report
  (`GET/PATCH /notifications/settings`). Phones: `POST /devices`, `DELETE /devices/{token}` (sign-out).
  `POST /notifications/test` sends a test push to the caller's phones.
- Every scheduled push has a key (`task:<occurrence>`, `med:<id>:<date>`…) stored in `notification_logs`,
  so a reminder goes out once even if the worker restarts. Reminders more than 30 min late are dropped.
- Tokens that Firebase reports as uninstalled are deleted automatically.

**Offline reminders** — `GET /reminders/upcoming?days=7` returns the caller's task and medication
reminders for the coming days (UTC instants, ready-made texts, quiet hours and the push switch already
applied, at most 60 for iOS). The app schedules them on the phone so they fire without a connection.
A phone that does this registers with `local_reminders: true`, and the worker then skips task and
medication pushes for it, so nothing arrives twice (other pushes and phones on older app versions
are unaffected).

**Worker** — the scheduled pushes run in a separate process (one instance), checking every minute:

```bash
.venv/bin/python -m app.workers.scheduler     # docker compose runs it as the `worker` service
```

**Turning on real delivery** (default `PUSH_BACKEND=log` only writes pushes to the server log):

1. Create a Firebase project and add the Android app `ci.foyo.foyo` (and the iOS app with the same bundle id).
2. Project settings → Service accounts → *Generate new private key*. Keep that JSON secret.
3. Set `PUSH_BACKEND=fcm` and `FCM_CREDENTIALS_FILE=/path/to/key.json`
   (with Docker: put it in `secrets/fcm.json` and use `FCM_CREDENTIALS_FILE=/run/secrets/fcm.json`).
4. Restart the API and the worker. A wrong path or file is reported at start-up.
5. Build the app with the Firebase values (see `../foyo/README.md`), sign in, and use
   Réglages → *Tester les notifications*.

## Layout

```
app/
├── main.py                       app factory, CORS, table creation + demo seed on start-up
├── core/                         settings, DB engine, JWT/bcrypt, errors, French labels (fr.py)
├── api/deps.py                   current user → member → household (Ctx), role & plan guards
├── api/v1/endpoints/             auth, households, tasks, reports, finances, deadlines, health, menus, subscriptions
├── schemas/                      Pydantic request bodies (validation rules from spec §13)
├── application/tasks.py          occurrence generation, streaks, member load
├── domain/services/              recurrence, rotation, balance rules
└── infrastructure/db/            models.py (ORM), seed.py (demo household)
```

## Endpoints (`/api/v1`, bearer token except sign-up / sign-in)

| Area | Endpoints |
|---|---|
| Auth (F1) | `POST /auth/sign-up` (creates the user's household as admin, or joins via a pending invitation), `POST /auth/sign-in`, `GET /auth/me`, `POST /auth/password/forgot` (6-digit code, 30 min, 5 attempts, 1 e-mail/min), `POST /auth/password/reset` (new password + new session; signs out every other device) |
| Household (F1) | `GET/PATCH /households/current`, `POST /households`, `POST /households/current/transfer-admin` (to a member with an account; the old admin becomes adult), `POST /households/{id}/invitations[/resend]` (e-mails the invitee; re-inviting refreshes the pending invitation; resend at most once a minute; invitations expire after 7 days), `GET/POST /members` |
| Invitations received | `GET /invitations/received`, `POST /invitations/{id}/accept` (existing account moves household; old membership kept inactive for history; a leaving admin hands over to the next member with an account), `POST /invitations/{id}/decline` |
| Tasks (F2–F6) | `GET /tasks/today`, `GET/POST /tasks`, `GET/PATCH/DELETE /tasks/{id}`, `POST /tasks/{id}/start|complete`, `POST|PATCH /occurrences/{id}…`, `GET /categories[/{id}]` |
| Reports | `GET /reports/daily`, `/reports/tasks?period=day|week|month|year`, `/reports/load`, `/reports/finances` |
| Finances (F15–F19) | `GET /finances/summary`, `GET/POST /accounts`, `GET/POST/DELETE /operations`, `GET/PUT /budgets`, `GET /savings`, `POST /savings/goals[/{id}/deposit]`, `POST /savings/debts` |
| Deadlines (F11) | `GET /deadlines`, `POST /deadlines/bills/{id}/pay` (records the expense in the Complet plan) |
| Health (F10) | `GET /health`, `POST /health/intakes/{id}/toggle` |
| Menus (F14) | `GET /dishes[/{id}]`, `GET /meal-plans?week=`, `GET /shopping-lists?week=`, `PATCH /shopping-lists/items/{id}`, `POST /shopping-lists/expense` |
| Documents (F7) | `GET /documents/data?week=&month=` — what the phone needs to draw the PDFs (week's tasks with each day's assignee, week's menu, month's bills and appointments, tasks per day); bills/appointments hidden from teen/child/helper |
| Plans (F12/F20) | `GET /subscriptions/plans`, `GET/PUT /subscriptions/current` |

Rules enforced server-side: private accounts only visible to their owner; an operation updates
account balance, budget and monthly totals in one transaction; transfers are neutral; helpers only
see their own tasks; child/helper can't edit; health & bills hidden from teen/child/helper;
plan gating (tasks / finances / complete) and cross-module links only in Complet.

## Not done yet


Receipt storage.
