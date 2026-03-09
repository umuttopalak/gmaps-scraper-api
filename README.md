# gmaps-scraper-api

Async job-based REST API wrapper for Google Maps scraping. Built on top of [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper), it provides a non-blocking job queue with polling, automatic CSV-to-JSON conversion, query deduplication, disk persistence, and daily cleanup.

## How It Works

```
Client ──POST /api/jobs──> Wrapper ──POST──> Gosom   (returns 201 immediately)
                                      │
                          Background task (asyncio):
                              poll gosom status
                              download CSV
                              convert to JSON
                              save to disk
                                      │
Client ──GET /api/jobs/{id}──> Wrapper  →  "pending" | "ok" | "failed"
Client ──GET /api/jobs/{id}/result──> Wrapper  →  JSON file
```

Instead of blocking the HTTP connection for minutes while scraping completes, the wrapper returns immediately with a `job_id`. The client polls for status and downloads results when ready.

## Quick Start

```bash
git clone https://github.com/<your-username>/gmaps-scraper-api.git
cd gmaps-scraper-api
docker-compose up -d --build
```

This starts two services:
- **Gosom scraper** on port `8085`
- **Wrapper API** on port `8003`

## API Reference

### Create a Job

```bash
curl -X POST http://localhost:8003/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"query": "restaurants istanbul", "depth": 1, "max_reviews": 10}'
```

**Response (201):**
```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "status": "pending"
}
```

**Parameters:**

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `query` | string | *required* | - | Google Maps search query |
| `depth` | int | 1 | 1-10 | Search depth (pages to scrape) |
| `max_reviews` | int | 10 | 0-500 | Maximum reviews per place |

**Deduplication:** If the same query (case-insensitive) with the same parameters was already submitted today:
- Completed job → returns `200` with `download_url` (no new scraping)
- Pending job → returns `200` with `status: "pending"` (no duplicate job)
- Failed job → creates a new job (retry)

### Check Job Status

```bash
curl http://localhost:8003/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48
```

**Response (200):**
```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "query": "restaurants istanbul",
  "status": "ok",
  "created_at": "2026-03-09T14:28:36.061990",
  "result_count": 20,
  "download_url": "/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result"
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `pending` | Job created, scraping in progress |
| `ok` | Completed, results ready to download |
| `failed` | Error occurred (see `error` field) |

### Download Results

```bash
curl http://localhost:8003/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result
```

Returns a JSON array of place objects. Each object includes:

- `title`, `category`, `address`, `phone`, `website`
- `review_count`, `review_rating`, `reviews_per_rating`
- `latitude`, `longitude`
- `open_hours`, `popular_times`
- `images`, `about`, `user_reviews`
- `owner`, `complete_address`
- Full UTF-8 support (Turkish characters, emojis, etc.)

**Status codes:**

| Code | Meaning |
|------|---------|
| 200 | JSON result file |
| 202 | Job still pending |
| 404 | Job not found |
| 410 | Result file was cleaned up |
| 422 | Job failed |

### List All Jobs

```bash
curl http://localhost:8003/api/jobs
```

Returns all jobs created today.

## Architecture

### File Structure

```
gmaps-scraper-api/
  wrapper.py           # FastAPI application (single file)
  Dockerfile           # Python 3.12-slim + uvicorn
  docker-compose.yml   # Gosom + Wrapper orchestration
  requirements.txt     # fastapi, uvicorn, httpx
  gmapsdata/           # Shared volume between services
    json/
      2026-03-09/
        index.json     # Today's job registry
        <job-id>.json  # Parsed JSON results
```

### Key Features

- **Non-blocking**: POST returns immediately, background asyncio task handles polling
- **Disk persistence**: Results saved to `gmapsdata/json/YYYY-MM-DD/` with atomic writes
- **Restart recovery**: On startup, `index.json` is reloaded and pending jobs resume polling
- **Query deduplication**: Same query today returns cached result or existing pending job
- **Daily cleanup**: Directories older than yesterday are automatically deleted
- **Thread-safe I/O**: All disk writes run in `asyncio.to_thread()` to avoid blocking the event loop
- **Path traversal protection**: Job IDs validated against strict UUID regex, paths checked with `is_relative_to()`

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOSOM_API_URL` | `http://localhost:8080/api/v1` | Gosom scraper API URL |
| `DATA_ROOT` | `gmapsdata/json` | Directory for JSON result storage |

### Polling Behavior

The wrapper polls gosom with an adaptive interval:
- Starts at **1 second**
- Increases by **0.5s** per poll up to **3 seconds**
- Maximum **720 polls** (~36 minute timeout)

### Gosom Status Mapping

| Gosom Status | Wrapper Action |
|---|---|
| `pending` | Continue polling |
| `working` | Continue polling |
| `ok` | Download CSV, convert to JSON, save to disk |
| `failed` | Mark job as failed with error message |

## Local Development (without Docker)

```bash
# Terminal 1: Start gosom (requires Docker)
docker run -p 8080:8080 -v ./gmapsdata:/gmapsdata gosom/google-maps-scraper:latest -data-folder /gmapsdata

# Terminal 2: Start wrapper
pip install fastapi uvicorn[standard] httpx
uvicorn wrapper:app --host 0.0.0.0 --port 8000 --reload
```

## Example: Full Workflow

```bash
# 1. Create a job
JOB_ID=$(curl -s -X POST http://localhost:8003/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"query":"Villamore Trabzon","depth":1,"max_reviews":10}' | python -m json.tool --no-ensure-ascii | grep job_id | cut -d'"' -f4)

echo "Job ID: $JOB_ID"

# 2. Poll until complete
while true; do
  STATUS=$(curl -s http://localhost:8003/api/jobs/$JOB_ID | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Status: $STATUS"
  [ "$STATUS" != "pending" ] && break
  sleep 3
done

# 3. Download results
curl -s http://localhost:8003/api/jobs/$JOB_ID/result | python -m json.tool --no-ensure-ascii

# 4. Same query again — instant cache hit
curl -s -X POST http://localhost:8003/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"query":"Villamore Trabzon","depth":1,"max_reviews":10}' | python -m json.tool
```

## License

MIT
