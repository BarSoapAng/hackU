# Hosted dashboard

The user requested public access without authentication on 2026-11-08.
`https://<PUBLIC_URL hostname>/dashboard/` exposes phone numbers, conversation
history, activity, and costs to anyone with the URL. Database credentials and
editing capabilities remain server-side; the dashboard API remains read-only.

The separate dashboard image builds Vite with `/dashboard/` as its base path
and reuses the existing `hypha-backend` image for Python dependencies. The
dashboard factory starts no messaging worker. A gateway on `127.0.0.1:8011`
routes dashboard pages/API calls to `127.0.0.1:8010` and all other requests to
the existing messaging backend at `127.0.0.1:8001`.

The live deployment uses a separate `~/hypha-dashboard` checkout with a private
copy of the existing server environment. Run there after the backend image has
been built:

```bash
docker compose -f deploy/brev/dashboard.compose.yaml build dashboard
docker compose -f deploy/brev/dashboard.compose.yaml up -d --no-build
curl --fail http://127.0.0.1:8011/api/internal/overview
curl --fail http://127.0.0.1:8011/healthz
```

Set the existing ngrok `hypha` tunnel's upstream address to `127.0.0.1:8011`
in its private configuration and restart the ngrok system service. Preserve
the public domain and authtoken. Messaging backend restarts are not needed.
For rollback, restore the ngrok upstream to `127.0.0.1:8001` and restart ngrok
before stopping the dashboard deployment.

Updates to this deployment can use `up -d` because its processes do not run
messaging workers. The messaging backend still requires stop-before-start
updates. Do not rebuild it from unrelated unfinished changes just to update
the dashboard. Frontend environment variables must never contain provider or
database credentials.
