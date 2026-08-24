#!/usr/bin/env python3
"""
Create Instana targets in Turbonomic from a CSV file.
API Reference: https://www.ibm.com/docs/en/tarm/8.20.6?topic=endpoint-targets-requests

Authentication flow:
  1. POST /api/v3/login   (form-encoded credentials)  → receives JSESSIONID cookie
  2. POST /api/v3/targets (JSON body + cookie)         → creates each target

CSV columns (header row required):
  hostname_or_ip, api_token

  hostname_or_ip – Hostname or IP address of the Instana server  (required)
  api_token      – Instana API token used by Turbonomic to connect (required)
"""

import csv
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
TURBO_HOST     = "https://turbonomic-turbonomic.apps.itz-0z3jrp.infra01-lb.wdc07.techzone.ibm.com"
ADMIN_USER     = "administrator"
ADMIN_PASSWORD = "administrator"

CSV_FILE       = "targets.csv"                       # path to the input CSV file
# ──────────────────────────────────────────────────────────────────────────────

REQUIRED_COLUMNS = {"hostname_or_ip", "api_token"}


def _make_ssl_context(verify: bool = False) -> ssl.SSLContext:
    """
    Return an SSL context.
    verify=False disables certificate validation (useful for self-signed certs).
    Set verify=True in production when a valid certificate is in place.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _build_opener(ssl_ctx: ssl.SSLContext) -> urllib.request.OpenerDirector:
    """Build a urllib opener that handles cookies automatically."""
    cookie_jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        urllib.request.HTTPCookieProcessor(cookie_jar),
    )


def login(opener: urllib.request.OpenerDirector, host: str, username: str, password: str) -> None:
    """
    POST /api/v3/login — authenticate and store the session cookie in the opener.

    :param opener:   urllib opener with a cookie processor attached
    :param host:     Turbonomic base URL (no trailing slash)
    :param username: Admin username
    :param password: Admin password
    :raises urllib.error.HTTPError: on non-2xx response
    """
    url  = f"{host}/api/v3/login"
    body = urllib.parse.urlencode({"username": username, "password": password}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    with opener.open(req) as response:
        print(f"[login] Authenticated as '{username}' — HTTP {response.status}")


def create_target(opener: urllib.request.OpenerDirector, host: str, target_payload: dict) -> dict:
    """
    POST /api/v3/targets — create a single Instana target using the active session.

    :param opener:         urllib opener carrying the session cookie
    :param host:           Turbonomic base URL (no trailing slash)
    :param target_payload: Dict matching the TargetApiDTO schema
    :returns:              Parsed JSON response from the API
    """
    url  = f"{host}/api/v3/targets"
    body = json.dumps(target_payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )

    with opener.open(req) as response:
        return json.loads(response.read().decode("utf-8"))


def load_targets_from_csv(filepath: str) -> list[dict]:
    """
    Read Instana targets from a CSV file and return a list of TargetApiDTO-compatible dicts.

    Each row must provide:
      - hostname_or_ip  (required) – Instana server hostname or IP address
      - api_token       (required) – Instana API token

    :param filepath: Path to the CSV file
    :returns:        List of target payload dicts
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {filepath}")

    targets = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)

        # Validate that all required columns are present
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row_num, row in enumerate(reader, start=2):   # start=2 accounts for the header row
            # Skip blank rows
            if not any(row.values()):
                continue

            hostname_or_ip = row["hostname_or_ip"].strip()
            api_token      = row["api_token"].strip()

            if not hostname_or_ip:
                print(f"  [row {row_num}] Skipping — 'hostname_or_ip' is empty.")
                continue
            if not api_token:
                print(f"  [row {row_num}] Skipping — 'api_token' is empty.")
                continue

            targets.append({
                "category": "Applications and Databases",
                "type":     "Instana",
                "inputFields": [
                    {"name": "address",                     "value": hostname_or_ip},
                    {"name": "apiToken",                    "value": api_token},
                    {"name": "validateServerCertificates",  "value": "false"},
                ],
            })

    return targets


def bulk_create_targets(host: str, admin_user: str, admin_password: str, csv_file: str) -> None:
    """
    Log in once, then read all Instana targets from csv_file and create them in Turbonomic.
    Continues processing remaining rows even if a single target creation fails.
    """
    ssl_ctx = _make_ssl_context(verify=False)
    opener  = _build_opener(ssl_ctx)

    # Step 1 — authenticate once and keep the session cookie
    login(opener, host, admin_user, admin_password)
    print()

    # Step 2 — load targets from CSV
    targets = load_targets_from_csv(csv_file)
    total   = len(targets)
    print(f"Found {total} target(s) in '{csv_file}'. Starting creation...\n")

    success_count = 0
    failure_count = 0

    for i, target in enumerate(targets, start=1):
        hostname = target["inputFields"][0]["value"]
        print(f"[{i}/{total}] Creating Instana target: {hostname}")
        try:
            result = create_target(opener, host, target)
            print(f"         ✓ Created  — uuid: {result.get('uuid', 'N/A')}, "
                  f"displayName: {result.get('displayName', 'N/A')}")
            success_count += 1
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            print(f"         ✗ Failed   — HTTP {exc.code} {exc.reason}: {error_body}")
            failure_count += 1
        except Exception as exc:                          # noqa: BLE001
            print(f"         ✗ Failed   — {exc}")
            failure_count += 1

    print(f"\nDone. {success_count} created, {failure_count} failed (total: {total}).")


if __name__ == "__main__":
    bulk_create_targets(TURBO_HOST, ADMIN_USER, ADMIN_PASSWORD, CSV_FILE)
