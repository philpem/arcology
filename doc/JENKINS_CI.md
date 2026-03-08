# Jenkins CI Configuration

This guide covers setting up Jenkins to run the Arcology CI pipeline.

## Prerequisites

On the Jenkins controller or agent:

- **Jenkins 2.400+** with these plugins:
  - [Pipeline](https://plugins.jenkins.io/workflow-aggregator/) (usually pre-installed)
  - [Docker Pipeline](https://plugins.jenkins.io/docker-workflow/) — runs PostgreSQL in a container for migration tests
  - [Git](https://plugins.jenkins.io/git/) (usually pre-installed)
- **Docker** available to the Jenkins user (the user must be in the `docker` group or have equivalent access)
- **Python 3.10+** installed on the agent

## Job Setup

1. In Jenkins, create a new **Multibranch Pipeline** job
2. Under **Branch Sources**, add your GitHub repository URL
3. Under **Build Configuration**, set:
   - Mode: **by Jenkinsfile**
   - Script Path: `Jenkinsfile` (the default)
4. Under **Scan Multibranch Pipeline Triggers**, set the scan interval (e.g. 1 minute) or configure a webhook (see below)
5. Save — Jenkins will scan the repository and create pipeline jobs for each branch containing a `Jenkinsfile`

### Branch Filtering (Optional)

To only build `master` and PR branches, under **Branch Sources > Behaviours**, add:
- **Filter by name (with regular expression)**: `master|PR-.*`

Or use the **Discover pull requests from origin** behaviour to automatically build PRs.

## GitHub Webhook

To trigger builds immediately on push/PR events rather than polling:

1. In your GitHub repository, go to **Settings > Webhooks > Add webhook**
2. Set:
   - Payload URL: `https://<your-jenkins-url>/github-webhook/`
   - Content type: `application/json`
   - Events: Select **Pushes** and **Pull requests**
3. In the Jenkins job configuration, ensure **Scan Multibranch Pipeline Triggers** includes "GitHub" or that the GitHub plugin is configured under **Manage Jenkins > System > GitHub Servers**

For private repositories, you'll also need to configure credentials in Jenkins (SSH key or personal access token) under **Manage Jenkins > Credentials**.

## What the Pipeline Does

The `Jenkinsfile` runs three stages:

| Stage | What it does | Duration |
|-------|-------------|----------|
| **Static Checks** | Python syntax validation, migration sanity checks (autocommit, chain integrity) | ~5s |
| **Migration Tests** | Spins up PostgreSQL 16 in Docker, installs dependencies, runs full upgrade → downgrade → upgrade cycle | ~60s |
| **Docker Build** | Builds both web and worker Docker images (no push) | 5–10 min |

If any stage fails, subsequent stages are skipped.

## Port Configuration

The migration test stage uses port **5433** on the host (not the default 5432) to avoid conflicts with any local PostgreSQL instance. If port 5433 is also in use, edit the `-p 5433:5432` mapping in the `Jenkinsfile`.

## Troubleshooting

### Docker socket permission denied

If you see `permission denied while trying to connect to the Docker daemon socket`:

```bash
sudo usermod -aG docker jenkins
sudo systemctl restart jenkins
```

### Python version mismatch

The pipeline uses `python3`. If your agent has multiple Python versions, you can configure the path in Jenkins under **Manage Jenkins > Tools > Python** or modify the `Jenkinsfile` to use a specific path.

### PostgreSQL fails to start

Check if port 5433 is already in use:

```bash
ss -tlnp | grep 5433
```

If so, either stop the conflicting service or change the port in the `Jenkinsfile`.

### Migration tests fail but pass locally

Ensure the Jenkins agent has the same Python version and dependencies as your local environment. The migration tests use a fresh PostgreSQL instance each run, so the database state is always clean — if migrations pass locally but fail in CI, the issue is likely a dependency version difference.
