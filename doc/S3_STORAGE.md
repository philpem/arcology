# S3-Compatible Storage for Distributed Workers

Arcology supports S3-compatible object storage as an alternative to local
filesystem storage.  This enables analysis workers to run on separate machines
from the web frontend — they access files via S3 instead of requiring shared
Docker volume mounts.

Local filesystem storage remains the default.  S3 is entirely opt-in and
requires no changes for existing deployments.

## When to use S3 storage

- **Workers on separate machines** from the web frontend
- **Cloud deployments** where shared volumes are impractical
- **Scaling workers** across multiple hosts

For single-machine Docker deployments with shared volumes, local storage is
simpler and has no additional dependencies.

## Supported S3 backends

Any S3-compatible service works:

| Backend | License | Notes |
|---------|---------|-------|
| [Garage](https://garagehq.deuxfleurs.fr/) | AGPLv3 | Lightweight, single binary, self-hosted |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | Apache 2.0 | Scalable, good for larger deployments |
| AWS S3 | Commercial | Managed cloud service |
| Other S3-compatible services | Varies | Backblaze B2, Cloudflare R2, etc. |

Garage is recommended for self-hosting due to its simplicity and zero cost.

## Setup with Garage (self-hosted)

### 1. Create data directory and generate secrets

```bash
mkdir -p data/garage
openssl rand -hex 32 > data/garage/rpc_secret
openssl rand -hex 32 > data/garage/admin_token
```

### 2. Start Garage

```bash
docker compose -f docker-compose.yml -f docker-compose.s3.yml up -d garage
```

### 3. Configure Garage (one-time)

```bash
# Set up a shell alias for convenience
alias garage="docker compose exec -ti garage /garage"

# Check node status and note the node ID
garage status

# Assign storage layout (replace <NODE_ID> with the ID from above)
garage layout assign -z dc1 -c 50G <NODE_ID>
garage layout apply --version 1

# Create a bucket for Arcology
garage bucket create arcology

# Create an access key
garage key create arcology-key
# Note the Key ID and Secret Key from the output

# Grant the key access to the bucket
garage bucket allow --read --write --owner arcology --key arcology-key
```

### 4. Configure environment

Add the S3 credentials to your `.env` file:

```bash
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://garage:3900
S3_BUCKET=arcology
S3_ACCESS_KEY=GK...          # Key ID from step 3
S3_SECRET_KEY=...            # Secret Key from step 3
S3_REGION=garage
```

### 5. Start the full stack

```bash
docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build -d
```

## Setup with AWS S3 or other cloud providers

Skip the Garage setup and configure your `.env` directly:

```bash
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=https://s3.us-east-1.amazonaws.com   # or your provider's endpoint
S3_BUCKET=your-bucket-name
S3_ACCESS_KEY=AKIA...
S3_SECRET_KEY=...
S3_REGION=us-east-1
```

Then start normally (without the `docker-compose.s3.yml` override):

```bash
docker compose up --build -d
```

## Configuration reference

All settings can be placed in `.env`, `myapp.cfg`, or set as environment
variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_BACKEND` | `local` | Storage backend: `local` or `s3` |
| `S3_ENDPOINT_URL` | — | S3 API endpoint URL (required for S3) |
| `S3_BUCKET` | `arcology` | S3 bucket name |
| `S3_ACCESS_KEY` | — | S3 access key ID (required for S3) |
| `S3_SECRET_KEY` | — | S3 secret access key (required for S3) |
| `S3_REGION` | `us-east-1` | S3 region (use `garage` for Garage) |

## How it works

### Architecture

```
                          +------------------+
                          |   S3 Storage     |
                          |  (Garage / AWS)  |
                          +--------+---------+
                                   |
                    +--------------+--------------+
                    |                             |
              +-----+-----+              +-------+-------+
              |  Web App  |              |    Worker(s)   |
              | (Flask)   |              | (any machine)  |
              +-----------+              +---------------+
```

- The **web app** uploads artefacts to S3 and redirects downloads to pre-signed
  URLs (no proxying large files through Flask).
- **Workers** download input files from S3 to a local temp directory, run
  analysis tools, then upload results back to S3.
- Workers need only network access to S3 and the web API — no shared volumes.

### Storage keys

Files are stored in S3 with keys mirroring the local directory structure:

- `uploads/<uuid>.<ext>` — uploaded artefacts
- `outputs/<path>` — analysis outputs, extraction trees, visualisations
- `outputs/.cache/<artefact-uuid>/` — cached partition images

### Backward compatibility

- Existing deployments using local storage require no changes.
- `output_path` values stored in the database are relative paths when using S3.
  Legacy absolute paths (from local mode) are handled transparently.
