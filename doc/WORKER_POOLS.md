# Worker Pools and Job Routing

Arcology workers are stateless processes that poll the web API for analysis
jobs.  By default every worker accepts every job type, and all jobs share a
single FIFO queue.  Two configuration knobs let you reshape this for larger
or busier deployments:

| Knob | Where | Effect |
|------|-------|--------|
| `WORKER_ANALYSIS_TYPES` | Worker container env var | Restricts a worker to a subset of `AnalysisType`s |
| `WEB_UI_ANALYSIS_PRIORITY` | Web app config | Makes web-triggered jobs jump ahead of API/CLI jobs |

Both default to *off* (no filtering, all jobs at priority 0), so existing
deployments behave exactly as before.

## When to bother

If you have a handful of workers and analyses generally complete in seconds,
neither feature is worth the operational complexity — leave both unset.

The features become useful when:

- **Heavy analyses block lighter ones.**  Flux decoding can take many minutes;
  while a worker is busy decoding flux, fast jobs like `CHECKSUM_COMPUTE` or
  `METADATA_EXTRACT` queue up behind it.  Dedicating a small pool of workers
  to "light" job types means those finish quickly even under load.
- **You have heterogeneous hosts.**  A GPU-equipped node could run an
  ML-based analysis; commodity nodes handle everything else.  Job-type
  filtering keeps each pool on hardware it suits.
- **Bulk imports stall interactive use.**  A long `arco upload --dir ...`
  enqueues hundreds of jobs.  Setting `WEB_UI_ANALYSIS_PRIORITY=10` lets a
  user run a one-off web upload and see results promptly, without having to
  wait for the bulk queue to drain.
- **You want to scale specific bottlenecks.**  Independent pools mean you can
  scale archive-extraction workers separately from flux-decoding workers,
  rather than scaling the whole worker fleet uniformly.

---

## Feature 1: Job-type filtering with `WORKER_ANALYSIS_TYPES`

Set the `WORKER_ANALYSIS_TYPES` environment variable on a worker container to
a comma-separated list of `AnalysisType` **names** (uppercase).  That worker
will only claim jobs whose `analysis_type` is in the list.  Unset or empty
means "accept any job type" — the original default.

Valid names are defined in [`arcology_shared/enums.py`](../arcology_shared/enums.py); common
examples:

```
CHECKSUM_COMPUTE   FLUX_VISUALISATION    FLUX_DECODE
METADATA_EXTRACT   PARTITION_DETECT      FILE_EXTRACTION
ARCHIVE_DETECT     ARCHIVE_EXTRACT       FORMAT_IDENTIFY
PRODUCT_RECOGNITION  DISC_MASTERING_DETECT  DISC_PROTECTION_DETECT
FORMAT_CONVERT     RISCOS_MODULE_PARSE   ARMLOCK_REMOVE
```

> **Tip — always provide a catch-all pool.**  If every worker has a filter
> set, any job type that isn't in any filter will sit forever in `PENDING`.
> Either keep one unfiltered worker pool, or explicitly list every
> `AnalysisType` across your pools.

The filter is applied server-side (`GET /analysis/pending?types=...`), so
specialised workers don't waste bandwidth fetching jobs they would discard.

### Typical pool layouts

**Two pools — heavy / light split**

The most common useful layout.  One pool of larger workers handles the
expensive analyses; a smaller pool of lightweight workers keeps the
fast-turnaround queue flowing even when the heavy pool is saturated.

```
heavy:  FLUX_VISUALISATION, FLUX_DECODE, FILE_EXTRACTION,
        ARCHIVE_EXTRACT, PARTITION_DETECT, FORMAT_CONVERT
light:  CHECKSUM_COMPUTE, METADATA_EXTRACT, FORMAT_IDENTIFY,
        ARCHIVE_DETECT, PRODUCT_RECOGNITION, DISC_MASTERING_DETECT,
        DISC_PROTECTION_DETECT, ARMLOCK_REMOVE, RISCOS_MODULE_PARSE
```

**One specialised pool + a catch-all**

If only one analysis type is misbehaving (say flux decoding monopolises
workers), pull just that type into its own pool and leave the catch-all
unfiltered:

```
flux:     FLUX_VISUALISATION, FLUX_DECODE
default:  <unfiltered — picks up everything else>
```

Because the flux pool accepts FLUX jobs explicitly and the default pool
accepts everything (including FLUX), the default pool will *also* grab flux
jobs when it's idle.  That's usually what you want — flux jobs prefer the
flux pool but won't starve if it's down.

---

## Feature 2: Priority queue with `WEB_UI_ANALYSIS_PRIORITY`

Every Analysis row has a `priority` column (integer, default 0).  Workers
poll for pending jobs ordered by `priority DESC, created_at ASC`, so a job
with priority 10 jumps ahead of any priority-0 job, but two priority-10
jobs still run FIFO between themselves.

The web app honours one config setting:

```python
WEB_UI_ANALYSIS_PRIORITY = 10
```

When set, jobs queued through the web UI (file uploads, the "Re-analyse"
button) use that priority.  API and CLI submissions always use priority 0.

Leave it unset, or set it to 0, to keep all jobs in a single FIFO queue
(the default behaviour).

---

## Deployment recipes

### Docker Compose — single pool (default)

Out of the box, `docker-compose.yml` defines one `worker` service.  Scale it
with `--scale worker=N`:

```bash
docker compose up -d --scale worker=4
```

### Docker Compose — heavy / light pools

Replace the single `worker:` block with two services that share the same
image and volumes but carry different `WORKER_ANALYSIS_TYPES` values:

```yaml
services:
  worker-light:
    build:
      context: .
      dockerfile: worker/Dockerfile
    depends_on:
      web:
        condition: service_healthy
    volumes:
      - ./data/uploads:/data/uploads
      - ./data/outputs:/data/outputs
    environment:
      - ARCOLOGY_API=http://web:8000/api
      - UPLOAD_DIR=/data/uploads
      - OUTPUT_DIR=/data/outputs
      - WORKER_API_KEY=${WORKER_API_KEY}
      - WORKER_ANALYSIS_TYPES=CHECKSUM_COMPUTE,METADATA_EXTRACT,FORMAT_IDENTIFY,ARCHIVE_DETECT,PRODUCT_RECOGNITION,DISC_MASTERING_DETECT,DISC_PROTECTION_DETECT,ARMLOCK_REMOVE,RISCOS_MODULE_PARSE
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: '1'
          memory: 1G

  worker-heavy:
    build:
      context: .
      dockerfile: worker/Dockerfile
    depends_on:
      web:
        condition: service_healthy
    volumes:
      - ./data/uploads:/data/uploads
      - ./data/outputs:/data/outputs
    environment:
      - ARCOLOGY_API=http://web:8000/api
      - UPLOAD_DIR=/data/uploads
      - OUTPUT_DIR=/data/outputs
      - WORKER_API_KEY=${WORKER_API_KEY}
      - WORKER_ANALYSIS_TYPES=FLUX_VISUALISATION,FLUX_DECODE,FILE_EXTRACTION,ARCHIVE_EXTRACT,PARTITION_DETECT,FORMAT_CONVERT
    deploy:
      replicas: 4
      resources:
        limits:
          cpus: '4'
          memory: 8G
```

Scale them independently:

```bash
docker compose up -d --scale worker-light=2 --scale worker-heavy=6
```

### Kubernetes — separate Deployments per pool

The recommended pattern is one `Deployment` per worker pool, each with its
own `replicas` count and (optionally) its own resource requests.  An HPA can
scale each pool independently against CPU or queue depth.

`worker-light.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: arcology-worker-light
  labels:
    app: arcology-worker
    pool: light
spec:
  replicas: 2
  selector:
    matchLabels:
      app: arcology-worker
      pool: light
  template:
    metadata:
      labels:
        app: arcology-worker
        pool: light
    spec:
      containers:
        - name: worker
          image: ghcr.io/your-org/arcology-worker:latest
          env:
            - name: ARCOLOGY_API
              value: "http://arcology-web:8000/api"
            - name: STORAGE_BACKEND
              value: "s3"
            - name: S3_ENDPOINT_URL
              value: "http://garage:3900"
            - name: S3_BUCKET
              value: "arcology"
            - name: S3_ACCESS_KEY
              valueFrom: { secretKeyRef: { name: arcology-s3, key: access-key } }
            - name: S3_SECRET_KEY
              valueFrom: { secretKeyRef: { name: arcology-s3, key: secret-key } }
            - name: WORKER_API_KEY
              valueFrom: { secretKeyRef: { name: arcology-worker, key: api-key } }
            - name: WORKER_ANALYSIS_TYPES
              value: "CHECKSUM_COMPUTE,METADATA_EXTRACT,FORMAT_IDENTIFY,ARCHIVE_DETECT,PRODUCT_RECOGNITION,DISC_MASTERING_DETECT,DISC_PROTECTION_DETECT,ARMLOCK_REMOVE,RISCOS_MODULE_PARSE"
          resources:
            requests:
              cpu: "200m"
              memory: "256Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
```

`worker-heavy.yaml`: the same shape with `pool: heavy`, a different
`WORKER_ANALYSIS_TYPES` value, and larger CPU/memory limits.  Workers are
stateless, so you can scale either deployment with `kubectl scale` or an
HPA without coordination:

```bash
kubectl scale deployment arcology-worker-heavy --replicas=8
```

> **Storage note.**  Multi-host Kubernetes deployments require a shared
> storage backend — workers and the web app must agree on where artefact
> files live.  Use the S3 backend (see [`S3_STORAGE.md`](S3_STORAGE.md));
> the local-filesystem backend is single-host only.

### Pulling specific types into a GPU pool

If you add a future analysis type that benefits from a GPU (e.g. an OCR or
content-classification pass), give the GPU `Deployment` a node selector and
the appropriate `WORKER_ANALYSIS_TYPES` value:

```yaml
spec:
  template:
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      containers:
        - name: worker
          env:
            - name: WORKER_ANALYSIS_TYPES
              value: "OCR_EXTRACT"
          resources:
            limits:
              nvidia.com/gpu: 1
```

CPU-only pools continue to ignore those job types.

---

## Verifying the filter at runtime

Each worker logs its active filter at startup:

```
2026-05-16 00:01:23 - INFO - Starting Arcology worker
2026-05-16 00:01:23 - INFO - API: http://web:8000/api
2026-05-16 00:01:23 - INFO - Job type filter: FLUX_VISUALISATION, FLUX_DECODE
```

For unfiltered workers:

```
2026-05-16 00:01:23 - INFO - Job type filter: all types
```

To confirm priorities are being applied, hit the API directly and inspect
the `priority` field on pending analyses:

```bash
curl -H "Authorization: Bearer $WORKER_API_KEY" \
     http://localhost:8000/api/analysis/pending | jq '.analyses[] | {uuid, analysis_type, priority, created_at}'
```

The response is ordered as workers see it: priority-descending, then
creation-ascending.

<!-- vim: ts=4 sw=4 et
-->
