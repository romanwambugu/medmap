# Deploying MedMap

MedMap is now one deployable application: FastAPI serves `/api/v1/*` and, after
a frontend build, serves the Vite app and its client-side routes as well.

## Required data

The API reads the processed Stage 5, Stage 7, and Stage 8 outputs under `data/`.
Set `OHP_PROJECT_ROOT` to the repository root when the working directory is
different. In a container or hosted service, mount or copy `data/` at that root.

## Local development

Run the API and Vite development server in separate terminals:

```powershell
python -m uvicorn optimal_hospital_placer.api.main:app --app-dir src --reload --port 8000
cd web
npm install
npm run dev
```

Vite proxies `/api` to port 8000. Open `http://localhost:5173`.

## Production-style local run

```powershell
cd web
npm ci
npm run build
cd ..
python -m uvicorn optimal_hospital_placer.api.main:app --app-dir src --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. The health check is `/api/v1/health`.

## Docker

From the repository root:

```powershell
python docker/prepare_runtime_data.py
docker compose -f docker/docker-compose.yml up --build
```

The multi-stage image builds the Vite bundle, installs the API dependencies,
embeds copies of the active Stage 5, 7, and 8 snapshots, and runs one
Uvicorn process. The preparation script never changes or deletes `data/`.
The build trims its own copy of the snapshot to what the API reads
(`docker/slim_runtime_data.py`): 5 of the tract table's ~1,300 columns, and no
county table. That takes the data from about 830 MB to 90 MB.
This is the simplest hackathon deployment target for Render, Railway, Fly.io,
Azure Container Apps, or any service accepting a Dockerfile.

For development, optionally mount the repository's live data instead:

```powershell
docker compose -f docker/docker-compose.yml -f docker/docker-compose.local-data.yml up --build
```

The mount is read-only. It hides the image's embedded snapshot for that
container but does not alter either copy.

## Google Cloud Run (the hosted demo)

The public demo runs on Cloud Run: <https://medmap-985509284497.us-central1.run.app>
(project `medmap-515204`, region `us-central1`). It scales to zero when nobody
is visiting and is capped at one instance (1 vCPU, 1 GiB), so a quiet demo
stays inside Cloud Run's monthly free tier. The first visit after a quiet
spell waits a few seconds while an instance starts.

One-time setup, already done (needs the Google Cloud CLI and `gcloud auth login`):

```powershell
gcloud config set project medmap-515204
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
gcloud artifacts repositories create medmap --repository-format=docker --location=us-central1
gcloud auth configure-docker us-central1-docker.pkg.dev
```

The image repository has a cleanup rule that keeps the two newest images.

To redeploy after a change, from the repository root, with Docker running.
`--build-context` points the build at a folder holding the prepared snapshot
(the contents of a `docker/runtime-data/` produced by
`docker/prepare_runtime_data.py`); leave it out when this repository's own
`docker/runtime-data/` is filled.

```powershell
$tag = Get-Date -Format yyyy-MM-dd
$image = "us-central1-docker.pkg.dev/medmap-515204/medmap/medmap:$tag"
docker build -f docker/Dockerfile --build-context "runtime-data=<snapshot folder>" -t $image .
docker push $image
gcloud run deploy medmap --image $image --region us-central1
```

`gcloud run deploy` keeps the service's other settings. They were set at the
first deploy with:

```powershell
gcloud run deploy medmap --image $image --region us-central1 --allow-unauthenticated `
  --port 8000 --cpu 1 --memory 1Gi --min-instances 0 --max-instances 1 --concurrency 80 --timeout 60 `
  --set-env-vars "^;^MEDMAP_CORS_ORIGINS=https://medmap-985509284497.us-central1.run.app,https://medmap-eym4h2a2wa-uc.a.run.app"
```

`MEDMAP_CORS_ORIGINS` lists the origins allowed to call the API from a browser.
The site and the API share one origin here, so it only shuts out other sites.

## Split hosting

The frontend can also be hosted as static files. Build it with the public API
origin configured:

```powershell
$env:VITE_API_BASE = "https://api.example.org"
cd web
npm ci
npm run build
```

Publish `web/dist/` and route all non-file paths to `index.html`. Deploy FastAPI
separately and point `OHP_PROJECT_ROOT` at its data-bearing application root.
By default the API permits cross-origin browser calls from any site; set
`MEDMAP_CORS_ORIGINS` to the frontend's origin (comma-separated if several)
before a long-lived production deployment.

## Deployment smoke test

Verify these URLs after deployment:

- `/api/v1/health`
- `/api/v1/states`
- `/api/v1/services`
- `/map` (refresh this route directly to confirm SPA fallback)

Then choose a state, change a scoring weight, select a candidate, and confirm
that its access bands, recommended services, and alternate configurations load.
