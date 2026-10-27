# Deploying to Google Cloud

One Docker image runs two ways:

- **Cloud Run service** `mother-lode-jobs` — the website.
- **Cloud Run job** `mother-lode-jobs-worker` — reads job sources, matches and sends alerts.
- **Cloud Scheduler** runs the job every 15 minutes.
- **Cloud SQL (PostgreSQL)** stores everything.
- **Secret Manager** holds passwords and keys.

You can reuse the project, Cloud SQL instance and Artifact Registry from
calaveras_job_agent; just create a new database for this app.

## 1. Setup

```bash
export PROJECT_ID=your-project
export REGION=us-west2
export INSTANCE=your-cloudsql-instance       # existing or new
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/jobs/mother-lode-jobs

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create jobs --repository-format=docker --location=$REGION
```

## 2. Database

```bash
# New instance only if you don't have one:
# gcloud sql instances create $INSTANCE --database-version=POSTGRES_16 --region=$REGION --tier=db-f1-micro

gcloud sql databases create motherlode --instance=$INSTANCE
gcloud sql users create motherlode --instance=$INSTANCE --password='CHOOSE-A-PASSWORD'
```

## 3. Secrets

```bash
printf 'postgresql+psycopg://motherlode:CHOOSE-A-PASSWORD@/motherlode?host=/cloudsql/%s:%s:%s' \
  $PROJECT_ID $REGION $INSTANCE | gcloud secrets create DATABASE_URL --data-file=-
python -c "import secrets;print(secrets.token_urlsafe(48))" | gcloud secrets create SESSION_SECRET --data-file=-
printf 'your-smtp-password' | gcloud secrets create SMTP_PASSWORD --data-file=-
# optional
printf 'sk-...' | gcloud secrets create OPENAI_API_KEY --data-file=-
```

Give the Cloud Run service account (by default `PROJECT_NUMBER-compute@developer.gserviceaccount.com`)
the **Secret Manager Secret Accessor** and **Cloud SQL Client** roles.

## 4. Build and deploy

```bash
gcloud builds submit --tag $IMAGE:first

COMMON="--region=$REGION --image=$IMAGE:first --add-cloudsql-instances=$PROJECT_ID:$REGION:$INSTANCE \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,SESSION_SECRET=SESSION_SECRET:latest,SMTP_PASSWORD=SMTP_PASSWORD:latest \
  --set-env-vars=ENVIRONMENT=production,PUBLIC_BASE_URL=https://jobs.yourdomain.org,SIGNUP_MODE=approval,\
ADMIN_EMAILS=you@example.com,SMTP_HOST=smtp.example.com,SMTP_USERNAME=jobs@example.com,SMTP_FROM=jobs@example.com"

gcloud run deploy mother-lode-jobs $COMMON --allow-unauthenticated --min-instances=0 --max-instances=5

gcloud run jobs create mother-lode-jobs-worker $COMMON \
  --command=python --args=-m,app.cli,run --task-timeout=15m --max-retries=0
```

The web container runs database migrations when it starts. Load the starting
sources once:

```bash
gcloud run jobs execute mother-lode-jobs-worker --region=$REGION --wait \
  --args=-m,app.cli,seed-sources
```

## 5. Schedule the worker

```bash
gcloud scheduler jobs create http mother-lode-jobs-every-15 \
  --location=$REGION --schedule="*/15 * * * *" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/mother-lode-jobs-worker:run" \
  --http-method=POST --oauth-service-account-email=PROJECT_NUMBER-compute@developer.gserviceaccount.com
```

## 6. First admin and domain

1. Open the site, sign up with an address listed in `ADMIN_EMAILS`, confirm the email. You're an admin.
2. Admin → *Check all sources now* (or wait for the schedule).
3. Map your domain: Cloud Run → Manage custom domains. Update `PUBLIC_BASE_URL` to match.
4. Submit `https://your-domain/sitemap.xml` in Google Search Console.

## Turning on optional sources

- **Adzuna:** set `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`, then Admin → Sources → Adzuna → Resume.
- **USAJOBS:** set `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT`, then resume the USAJOBS source.

## Later deploys

`gcloud builds submit --config cloudbuild.yaml` builds a new image and updates
both the service and the job. You can connect it to the GitHub repo as a Cloud
Build trigger on pushes to `main`.

## Notes

- "Check all sources now" in Admin runs inside the web container. On Cloud Run
  the scheduled job is the reliable path; the button is for quick checks.
- Public pages send `Cache-Control: public, max-age=300` to anonymous visitors, so
  you can put Cloud CDN in front of the service if traffic grows.
