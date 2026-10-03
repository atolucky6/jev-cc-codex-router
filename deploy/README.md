# CI and deployment

GitHub Actions (`.github/workflows/ci.yml`) checks pull requests and pushes to
`main` on Python 3.9 and 3.13. It checks Python and inline JavaScript syntax,
starts an isolated router, exercises dashboard/API endpoints and proxy
passthrough against a local fake upstream, and tests the deployment gate and
rollback. Tests never copy production `.env`, settings, or databases. They do
not validate live model providers or paid routing calls.

The production host polls `origin/main` every two minutes (plus up to 15 seconds
of jitter). A commit is eligible only when the latest **push** run of `ci.yml`
for that exact SHA on `main` completed successfully. PR runs and manual CI runs
do not authorize deployment. GitHub's workflow-runs endpoint is documented at
https://docs.github.com/en/rest/actions/workflow-runs.

This installation uses the host's existing Git SSH access and the public GitHub
API; no inbound webhook, GitHub runner on production, or new GitHub secrets are
needed. Private repositories need authenticated API access added before using
this script. API/network failures leave the installed version unchanged.

## Install on the existing systemd host

Defaults: `/root/jev-cc-codex-router`, branch `main`, `jev-router.service`, health
URL `http://127.0.0.1:8787`. Override with `JEV_DEPLOY_DIR`, `JEV_DEPLOY_REPO`,
`JEV_DEPLOY_SERVICE`, `JEV_DEPLOY_URL`, or `JEV_DEPLOY_STATE` in a systemd drop-in.
The checkout must be clean, except runtime edits to `settings.json`.

```sh
install -d -m 700 /usr/local/lib/jev-deploy /var/lib/jev-deploy
install -m 700 scripts/deploy.py /usr/local/lib/jev-deploy/deploy.py
install -m 644 deploy/jev-deploy.service deploy/jev-deploy.timer /etc/systemd/system/
systemctl daemon-reload
python3 /usr/local/lib/jev-deploy/deploy.py --dry-run
systemctl enable --now jev-deploy.timer
```

Publish the workflow and tests to `main` before enabling the timer. The initial
installation itself is not a CI-verified release; subsequent updates are gated.
The installed deployment script is deliberately separate from the checkout;
updates to it or the systemd units require manual reinstallation and review.

## Operations

```sh
systemctl list-timers jev-deploy.timer
journalctl -u jev-deploy.service -n 50 --no-pager
systemctl start jev-deploy.service  # Check and deploy now if CI has passed
systemctl disable --now jev-deploy.timer  # Stop automatic deployments
```

Deployments require a fast-forward history and refuse local code changes.
Upstream edits to `.env` or `settings.json` require manual deployment, so saved
runtime configuration cannot be overwritten. Ignored logs and databases are
retained. Python application changes restart the router; dashboard/docs-only
updates do not. Restarting interrupts active requests and resets in-memory
session routing. Health checks validate systemd status, database stats, and the
served dashboard bytes; they do not send requests to live model providers.

If activation or health checks fail, the script restores the previous Git
revision, restarts when needed, and checks health again. The failed SHA is saved
in `/var/lib/jev-deploy/failed-commit` to prevent repeated bad deployments. After
fixing the cause, remove that file to retry, or push a new commit. The prior
revision is recorded in `previous-commit`. Rollback restores code only; database
schema changes must be backward compatible and require a separate backup and
migration plan. A failed rollback is reported as a failed systemd unit and
requires operator intervention. Monitor the journal for deployment failures.
