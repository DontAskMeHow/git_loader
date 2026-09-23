import argparse
import base64
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen


def parse_args():
    parser = argparse.ArgumentParser(description="Update local clones from Bitbucket Server.")
    parser.add_argument("--base-url", default="https://bitbucket.example.com")
    parser.add_argument("--destination", default=r"D:\Repos\projects")
    parser.add_argument("--username", default="user")
    parser.add_argument("--password", default=os.environ.get("BITBUCKET_PASSWORD"))
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


class Logger:
    def __init__(self, log_file):
        self.log_file = log_file

    def write(self, message):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        print(line, flush=True)
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def normalize_git_url(url):
    if not url:
        return ""

    normalized = url.strip()
    if normalized.startswith("http://") or normalized.startswith("https://"):
        scheme, rest = normalized.split("://", 1)
        if "@" in rest:
            normalized = f"{scheme}://{rest.split('@', 1)[1]}"

    return normalized.rstrip("/").lower()


def bitbucket_get(base_url, username, password, path_and_query):
    token = base64.b64encode(f"{username}:{password}".encode("ascii")).decode("ascii")
    url = urljoin(base_url.rstrip("/") + "/", path_and_query.lstrip("/"))
    request = Request(url, headers={"Authorization": f"Basic {token}"})

    with urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def get_paged_values(base_url, username, password, path, page_limit):
    values = []
    start = 0

    while True:
        separator = "&" if "?" in path else "?"
        query = urlencode({"limit": page_limit, "start": start})
        page = bitbucket_get(base_url, username, password, f"{path}{separator}{query}")
        values.extend(page.get("values", []))

        if page.get("isLastPage", True):
            return values

        start = page["nextPageStart"]


def get_http_clone_url(base_url, project_key, repo):
    for clone in repo.get("links", {}).get("clone", []):
        href = clone.get("href", "")
        name = clone.get("name", "")
        if name in {"http", "https"} or href.startswith("http"):
            return href

    return f"{base_url.rstrip('/')}/scm/{project_key.lower()}/{repo['slug']}.git"


def run_git(args, cwd, logger):
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if completed.stdout:
        for line in completed.stdout.splitlines():
            logger.write(f"git: {line}")

    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed with exit code {completed.returncode}")


def get_git_remote(path):
    completed = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return None

    return completed.stdout.strip()


def get_local_repo_index(destination):
    destination.mkdir(parents=True, exist_ok=True)
    by_remote = {}
    by_name = {}

    for child in destination.iterdir():
        if not child.is_dir() or not (child / ".git").exists():
            continue

        remote = get_git_remote(child)
        if remote:
            by_remote.setdefault(normalize_git_url(remote), child)

        by_name.setdefault(child.name.lower(), child)

    return by_remote, by_name


def get_clone_target_path(destination, by_name, project_key, slug):
    if slug.lower() not in by_name:
        return destination / slug

    return destination / f"{project_key.lower()}__{slug}"


def main():
    args = parse_args()
    if not args.password:
        raise SystemExit("Password is required. Pass --password or set BITBUCKET_PASSWORD.")

    destination = Path(args.destination).resolve()
    script_dir = Path(__file__).resolve().parent
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = script_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"update-{timestamp}.txt"
    report_file = log_dir / f"update-{timestamp}.csv"
    logger = Logger(log_file)

    logger.write(f"Starting Bitbucket update. Destination: {destination}")

    projects = get_paged_values(
        args.base_url, args.username, args.password, "/rest/api/1.0/projects", args.page_limit
    )
    logger.write(f"Found {len(projects)} projects.")

    by_remote, by_name = get_local_repo_index(destination)
    results = []
    total_repos = 0

    for project in projects:
        project_key = project["key"]
        logger.write(f"Reading repositories for project {project_key}")
        repos = get_paged_values(
            args.base_url,
            args.username,
            args.password,
            f"/rest/api/1.0/projects/{quote(project_key, safe='')}/repos",
            args.page_limit,
        )

        for repo in repos:
            total_repos += 1
            slug = repo["slug"]
            clone_url = get_http_clone_url(args.base_url, project_key, repo)
            normalized_clone_url = normalize_git_url(clone_url)
            local_path = by_remote.get(normalized_clone_url)
            action = "updated"
            status = "ok"
            message = ""

            try:
                if local_path:
                    logger.write(f"Updating {project_key}/{slug} in {local_path}")
                    if not args.dry_run:
                        run_git(["remote", "set-url", "origin", clone_url], local_path, logger)
                        run_git(["fetch", "origin", "--prune", "--tags"], local_path, logger)
                else:
                    local_path = get_clone_target_path(destination, by_name, project_key, slug)
                    action = "cloned"
                    logger.write(f"Cloning {project_key}/{slug} into {local_path}")
                    if not args.dry_run:
                        run_git(["clone", "--origin", "origin", clone_url, str(local_path)], destination, logger)
                        by_remote[normalized_clone_url] = local_path
                        by_name[local_path.name.lower()] = local_path
            except Exception as exc:
                status = "failed"
                message = str(exc)
                logger.write(f"FAILED {project_key}/{slug}: {message}")

            results.append(
                {
                    "Project": project_key,
                    "Repository": slug,
                    "Action": action,
                    "Status": status,
                    "Path": str(local_path),
                    "Message": message,
                }
            )

    with report_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["Project", "Repository", "Action", "Status", "Path", "Message"]
        )
        writer.writeheader()
        writer.writerows(results)

    updated = sum(1 for item in results if item["Action"] == "updated" and item["Status"] == "ok")
    cloned = sum(1 for item in results if item["Action"] == "cloned" and item["Status"] == "ok")
    failed = sum(1 for item in results if item["Status"] == "failed")

    logger.write(f"Done. Repositories: {total_repos}; updated: {updated}; cloned: {cloned}; failed: {failed}")
    logger.write(f"Log: {log_file}")
    logger.write(f"Report: {report_file}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
