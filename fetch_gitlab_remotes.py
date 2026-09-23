import argparse
import csv
import http.cookiejar
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch GitLab branches into existing Bitbucket clones.")
    parser.add_argument("--base-url", default="https://gitlab.example.com")
    parser.add_argument("--destination", default=r"D:\Repos\projects")
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default=os.environ.get("GITLAB_PASSWORD"))
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument("--remote-name", default="gitlab")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


class Logger:
    def __init__(self, log_file, secrets):
        self.log_file = log_file
        self.secrets = [secret for secret in secrets if secret]

    def sanitize(self, text):
        sanitized = text
        for secret in self.secrets:
            sanitized = sanitized.replace(secret, "***")
            sanitized = sanitized.replace(quote(secret, safe=""), "***")
        return sanitized

    def write(self, message):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {self.sanitize(message)}"
        print(line, flush=True)
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def normalize_name(name):
    return re.sub(r"[-_]", "", name.lower())


def credential_url(url, username, password):
    parts = urlsplit(url)
    user = quote(username, safe="")
    secret = quote(password, safe="")
    return urlunsplit((parts.scheme, f"{user}:{secret}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def request_json(opener, base_url, path_and_query):
    url = urljoin(base_url.rstrip("/") + "/", path_and_query.lstrip("/"))
    request = Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=120) as response:
        body = response.read().decode("utf-8")
        headers = response.headers
    return json.loads(body), headers


def gitlab_login(base_url, username, password):
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    sign_in_url = urljoin(base_url.rstrip("/") + "/", "users/sign_in")
    with opener.open(Request(sign_in_url), timeout=120) as response:
        content = response.read().decode("utf-8")

    match = re.search(r'name="authenticity_token" value="([^"]+)"', content)
    if not match:
        raise RuntimeError("Could not find GitLab authenticity_token on sign-in page.")

    form = urlencode(
        {
            "authenticity_token": match.group(1),
            "user[login]": username,
            "user[password]": password,
            "user[remember_me]": "0",
        }
    ).encode("utf-8")
    request = Request(sign_in_url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(request, timeout=120) as response:
        response.read()

    user, _ = request_json(opener, base_url, "/api/v4/user")
    return opener, user


def get_all_projects(opener, base_url, per_page):
    projects = []
    page = 1

    while True:
        query = urlencode(
            {
                "all": "true",
                "simple": "true",
                "per_page": per_page,
                "page": page,
                "order_by": "path",
                "sort": "asc",
            }
        )
        values, headers = request_json(opener, base_url, f"/api/v4/projects?{query}")
        projects.extend(values)

        next_page = headers.get("X-Next-Page", "")
        if not next_page:
            return projects
        page = int(next_page)


def run_git(args, cwd, logger, check=True):
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if completed.stdout:
        for line in completed.stdout.splitlines():
            logger.write(f"git: {line}")

    if check and completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed with exit code {completed.returncode}")

    return completed


def get_git_value(args, cwd):
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return None

    return completed.stdout.strip()


def get_local_repo_index(destination):
    destination.mkdir(parents=True, exist_ok=True)
    by_norm_name = {}

    for child in destination.iterdir():
        if not child.is_dir() or not (child / ".git").exists():
            continue
        by_norm_name.setdefault(normalize_name(child.name), child)

    return by_norm_name


def get_remote_url(repo_path, remote_name):
    return get_git_value(["remote", "get-url", remote_name], repo_path)


def ensure_remote(repo_path, remote_name, url, logger):
    existing = get_remote_url(repo_path, remote_name)
    if existing is None:
        run_git(["remote", "add", remote_name, url], repo_path, logger)
    elif existing.rstrip("/") != url.rstrip("/"):
        run_git(["remote", "set-url", remote_name, url], repo_path, logger)


def main():
    args = parse_args()
    if not args.password:
        raise SystemExit("Password is required. Pass --password or set GITLAB_PASSWORD.")

    destination = Path(args.destination).resolve()
    script_dir = Path(__file__).resolve().parent
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = script_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"gitlab-remotes-{timestamp}.txt"
    report_file = log_dir / f"gitlab-remotes-{timestamp}.csv"
    logger = Logger(log_file, [args.password])

    logger.write(f"Starting GitLab remote fetch. Destination: {destination}")
    opener, user = gitlab_login(args.base_url, args.username, args.password)
    logger.write(f"Authenticated to GitLab as {user.get('username')} (admin={user.get('is_admin')})")

    projects = get_all_projects(opener, args.base_url, args.per_page)
    logger.write(f"Found {len(projects)} GitLab projects.")

    by_norm_name = get_local_repo_index(destination)
    results = []

    for project in projects:
        path = project["path"]
        path_with_namespace = project["path_with_namespace"]
        clean_url = project["http_url_to_repo"].rstrip("/")
        local_path = by_norm_name.get(normalize_name(path))
        action = "skipped"
        status = "ok"
        message = ""

        try:
            if not local_path:
                message = "No matching local repository."
                logger.write(f"Skipping {path_with_namespace}; {message}")
            else:
                origin_url = get_remote_url(local_path, "origin") or ""
                if origin_url.rstrip("/").lower() == clean_url.lower():
                    message = "Repository already uses GitLab as origin."
                    logger.write(f"Skipping {path_with_namespace}; {message}")
                else:
                    action = "fetched"
                    logger.write(f"Fetching GitLab branches for {path_with_namespace} into {local_path}")
                    if not args.dry_run:
                        auth_url = credential_url(clean_url, args.username, args.password)
                        ensure_remote(local_path, args.remote_name, clean_url, logger)
                        run_git(["remote", "set-url", args.remote_name, auth_url], local_path, logger)
                        run_git(
                            [
                                "fetch",
                                args.remote_name,
                                "+refs/heads/*:refs/remotes/{0}/*".format(args.remote_name),
                                "--prune",
                                "--no-tags",
                            ],
                            local_path,
                            logger,
                        )
                        run_git(["remote", "set-url", args.remote_name, clean_url], local_path, logger)
        except Exception as exc:
            status = "failed"
            message = str(exc)
            logger.write(f"FAILED {path_with_namespace}: {message}")
            if local_path and (local_path / ".git").exists():
                try:
                    run_git(["remote", "set-url", args.remote_name, clean_url], local_path, logger)
                except Exception:
                    pass

        results.append(
            {
                "PathWithNamespace": path_with_namespace,
                "Path": path,
                "Action": action,
                "Status": status,
                "LocalPath": str(local_path) if local_path else "",
                "Message": message,
            }
        )

    with report_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["PathWithNamespace", "Path", "Action", "Status", "LocalPath", "Message"]
        )
        writer.writeheader()
        writer.writerows(results)

    fetched = sum(1 for item in results if item["Action"] == "fetched" and item["Status"] == "ok")
    skipped = sum(1 for item in results if item["Action"] == "skipped" and item["Status"] == "ok")
    failed = sum(1 for item in results if item["Status"] == "failed")

    logger.write(f"Done. GitLab projects: {len(projects)}; fetched: {fetched}; skipped: {skipped}; failed: {failed}")
    logger.write(f"Log: {log_file}")
    logger.write(f"Report: {report_file}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
