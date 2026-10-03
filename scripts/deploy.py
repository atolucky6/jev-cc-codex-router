#!/usr/bin/env python3
"""Pull-based deployment: only deploy the latest main commit after successful CI."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

REPO = Path(os.environ.get('JEV_DEPLOY_DIR', '/root/jev-cc-codex-router'))
STATE = Path(os.environ.get('JEV_DEPLOY_STATE', '/var/lib/jev-deploy'))
SERVICE = os.environ.get('JEV_DEPLOY_SERVICE', 'jev-router.service')
BASE = os.environ.get('JEV_DEPLOY_URL', 'http://127.0.0.1:8787').rstrip('/')
SLUG = os.environ.get('JEV_DEPLOY_REPO', 'atolucky6/jev-cc-codex-router')


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args],
                                   text=True, timeout=90).strip()


def ci_passed(sha):
    query = urllib.parse.urlencode({'branch': 'main', 'event': 'push',
                                    'head_sha': sha, 'per_page': 100})
    url = 'https://api.github.com/repos/%s/actions/workflows/ci.yml/runs?%s' % (SLUG, query)
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'jev-deploy'})
    with urllib.request.urlopen(request, timeout=20) as response:
        runs = json.load(response)['workflow_runs']
    matching = [run for run in runs if run['head_sha'] == sha
                and run['head_branch'] == 'main' and run['event'] == 'push']
    if not matching:
        return False
    latest = max(matching, key=lambda run: run['id'])
    return latest['status'] == 'completed' and latest['conclusion'] == 'success'


def health():
    subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE], check=True, timeout=10)
    with urllib.request.urlopen(BASE + '/api/stats', timeout=5) as response:
        stats = json.load(response)
    if 'error' in stats or 'total_records' not in stats:
        raise RuntimeError('Router stats/database health check failed')
    with urllib.request.urlopen(BASE + '/dashboard', timeout=5) as response:
        if response.read() != (REPO / 'dashboard.html').read_bytes():
            raise RuntimeError('Dashboard does not match deployed revision')


def wait_healthy():
    for attempt in range(15):
        try:
            health()
            return
        except Exception:
            if attempt == 14:
                raise
            time.sleep(1)


def restart():
    subprocess.run(['systemctl', 'restart', SERVICE], check=True, timeout=45)


def deploy(dry_run=False):
    if git('branch', '--show-current') != 'main':
        raise RuntimeError('Deployment requires a main checkout')
    # settings.json is runtime state. Other local edits must never be overwritten.
    if git('status', '--porcelain', '--untracked-files=normal', '--', '.',
           ':(exclude)settings.json'):
        raise RuntimeError('Local changes found; deployment stopped')
    git('fetch', 'origin', 'main')
    old = git('rev-parse', 'HEAD')
    target = git('rev-parse', 'refs/remotes/origin/main')
    if old == target:
        print('Already current:', old, flush=True)
        return
    git('merge-base', '--is-ancestor', old, target)
    failed = STATE / 'failed-commit'
    if failed.exists() and failed.read_text().strip() == target:
        raise RuntimeError('This commit failed deployment before; inspect logs before retrying')
    changed = git('diff', '--name-only', old, target).splitlines()
    if any(name in ('settings.json', '.env', 'decisions.db', 'req-headers.jsonl',
                    'decisions.jsonl', 'last-body.json') or name.startswith('errors-400/')
           for name in changed):
        raise RuntimeError('Upstream changes runtime config; manual deployment required')
    if not ci_passed(target):
        print('Waiting for successful push CI:', target, flush=True)
        return
    print('CI passed; deploy %s -> %s' % (old, target), flush=True)
    if dry_run:
        return
    health()
    (STATE / 'previous-commit').write_text(old + '\n')
    needs_restart = any(name.endswith('.py') and not name.startswith(('tests/', 'scripts/'))
                        for name in changed)
    updated = False
    try:
        git('merge', '--ff-only', target)
        updated = True
        # Do not import application code here: that loads runtime configuration.
        compile((REPO / 'jev_router.py').read_bytes(), 'jev_router.py', 'exec')
        if needs_restart:
            restart()
        wait_healthy()
    except Exception:
        if updated:
            failed.write_text(target + '\n')
            git('reset', '--keep', old)
            if needs_restart:
                restart()
            wait_healthy()
            print('Rolled back to:', old, flush=True)
        raise
    (STATE / 'deployed-commit').write_text(target + '\n')
    failed.unlink(missing_ok=True)
    print('Deployed successfully:', target, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another deployment is running')
        deploy(args.dry_run)
