#!/usr/bin/env python3
"""
lambda_grab.py — Poll Lambda Cloud for instance availability and auto-launch.

Usage:
    python lambda_grab.py \
        --instance-types gpu_8x_h100_sxm4 gpu_8x_a100 \
        --count 2 \
        --ssh-key my-key-name \
        [--extra-ssh-keys key2 key3] \
        [--ssh-key-path ~/.ssh/id_rsa] \
        [--region us-west-1] \
        [--poll-interval 30] \
        [--name my-experiment] \
        [--filesystems fs-abc123] \
        [--dry-run]

    --ssh-key is used for the launch request (Lambda only supports one key at
    launch time). Once instances are active, --extra-ssh-keys are fetched by
    name from the Lambda API and injected into each node's authorized_keys.

Notifications (optional env vars):
    SLACK_WEBHOOK_URL   — post to a Slack channel when instances are grabbed
    NOTIFY_EMAIL        — recipient address (also requires SMTP_* vars below)
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS — SMTP credentials

Auth:
    LAMBDA_API_KEY      — Lambda Cloud API key (required)
"""

import argparse
import json
import os
import shlex
import smtplib
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.text import MIMEText


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://cloud.lambda.ai/api/v1"


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("LAMBDA_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: LAMBDA_API_KEY environment variable is not set.")
    return key


def _auth_header() -> dict:
    import base64
    token = base64.b64encode(f"{_api_key()}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "User-Agent": "lambda-grab/1.0",
    }


class ApiHTTPError(RuntimeError):
    """The API answered with an HTTP error status (the request was received and refused)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _request(method: str, path: str, body: dict | None = None, dry_run: bool = False):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None

    if dry_run and method != "GET":
        print(f"  [dry-run] {method} {url}")
        if body:
            print(f"           {json.dumps(body, indent=2)}")
        return {}

    req = urllib.request.Request(url, data=data, headers=_auth_header(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise ApiHTTPError(exc.code, f"HTTP {exc.code} {method} {url}: {body_text}") from exc


def list_instance_types() -> dict:
    """Returns {instance_type_name: {regions_with_capacity_available: [...], ...}}"""
    result = _request("GET", "/instance-types")
    return result.get("data", {})


def launch_instances(
    instance_type: str,
    region: str,
    count: int,
    ssh_key: str,
    name: str,
    filesystem_ids: list[str],
    dry_run: bool = False,
) -> list[str]:
    """Launch `count` instances and return their IDs."""
    # Lambda Cloud only supports a single SSH key per launch request.
    body = {
        "instance_type_name": instance_type,
        "region_name": region,
        "ssh_key_names": [ssh_key],
        "quantity": count,
        "name": name,
    }
    if filesystem_ids:
        body["file_system_names"] = filesystem_ids

    result = _request("POST", "/instance-operations/launch", body, dry_run=dry_run)
    return result.get("data", {}).get("instance_ids", [])


def get_instance(instance_id: str) -> dict:
    result = _request("GET", f"/instances/{instance_id}")
    return result.get("data", {})


def list_ssh_keys() -> list[dict]:
    """Return all SSH keys registered on the Lambda account.

    Each entry is a dict with 'id', 'name', and 'public_key'.
    """
    result = _request("GET", "/ssh-keys")
    return result.get("data", [])


def get_public_keys_by_name(key_names: list[str]) -> list[str]:
    """Look up public key material for the given key names from Lambda.

    Returns a list of public key strings (one per matched name).
    Warns and skips any names not found in the account.
    """
    all_keys = list_ssh_keys()
    by_name = {k["name"]: k["public_key"] for k in all_keys if "name" in k and "public_key" in k}

    public_keys = []
    for name in key_names:
        if name in by_name:
            public_keys.append(by_name[name])
        else:
            available = list(by_name.keys())
            print(f"  [warn] SSH key '{name}' not found in Lambda account. Available: {available}")
    return public_keys


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def find_candidate(
    instance_types: dict,
    wanted: list[str],
    preferred_region: str | None,
    region_only: bool = False,
) -> tuple[str, str] | None:
    """
    Return (instance_type, region) for the first entry in `wanted` that has
    capacity. Respects preferred_region if supplied; with region_only, capacity
    elsewhere does not count.
    """
    for type_name in wanted:
        info = instance_types.get(type_name)
        if not info:
            continue
        regions = [r.get("name", "") for r in info.get("regions_with_capacity_available", [])]
        if not regions:
            continue
        if preferred_region and preferred_region in regions:
            return type_name, preferred_region
        if region_only:
            continue
        return type_name, regions[0]
    return None


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def notify(message: str) -> None:
    """Print, ring the terminal bell, and fire optional webhook/email."""
    print(f"\n{'='*60}")
    print(f"  NOTIFICATION [{_ts()}]")
    print(f"  {message}")
    print(f"{'='*60}\n")
    print("\a", end="", flush=True)  # terminal bell

    # Slack
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            payload = json.dumps({"text": f":gpu: {message}"}).encode()
            req = urllib.request.Request(
                webhook, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            print("  [notified via Slack]")
        except Exception as exc:
            print(f"  [Slack notification failed: {exc}]")

    # Email
    to_addr = os.environ.get("NOTIFY_EMAIL", "").strip()
    if to_addr:
        try:
            smtp_host = os.environ.get("SMTP_HOST", "localhost")
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASS", "")
            msg = MIMEText(message)
            msg["Subject"] = "Lambda Cloud: instances grabbed"
            msg["From"] = smtp_user or "lambda_grab@localhost"
            msg["To"] = to_addr
            with smtplib.SMTP(smtp_host, smtp_port) as s:
                s.ehlo()
                if smtp_port != 25:
                    s.starttls()
                if smtp_user:
                    s.login(smtp_user, smtp_pass)
                s.sendmail(msg["From"], [to_addr], msg.as_string())
            print(f"  [notified via email → {to_addr}]")
        except Exception as exc:
            print(f"  [email notification failed: {exc}]")


# ---------------------------------------------------------------------------
# Wait for instances to reach running state
# ---------------------------------------------------------------------------

def wait_for_instances(instance_ids: list[str], timeout: int = 600) -> list[dict]:
    """Poll until all instances are 'active', return their info dicts."""
    print(f"Waiting for {len(instance_ids)} instance(s) to become active…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        infos = [get_instance(iid) for iid in instance_ids]
        statuses = [i.get("status", "unknown") for i in infos]
        print(f"  [{_ts()}] statuses: {statuses}")
        if all(s == "active" for s in statuses):
            return infos
        if any(s in ("error", "terminated") for s in statuses):
            raise RuntimeError(f"One or more instances failed: {statuses}")
        time.sleep(15)
    raise TimeoutError("Instances did not become active within timeout.")


# ---------------------------------------------------------------------------
# Extra SSH key injection (post-launch)
# ---------------------------------------------------------------------------

def inject_extra_ssh_keys(
    instances: list[dict],
    extra_public_keys: list[str],
    ssh_key_path: str | None = None,
) -> None:
    """Append extra public keys to authorized_keys on each active instance.

    Connects via SSH using the launch key (from the agent or --ssh-key-path)
    and pipes the key material into ~/.ssh/authorized_keys.
    """
    if not extra_public_keys:
        return

    keys_block = "\n".join(extra_public_keys) + "\n"
    print(f"\nInjecting {len(extra_public_keys)} extra SSH key(s) into {len(instances)} instance(s)…")

    for inst in instances:
        ip = inst.get("ip")
        if not ip:
            print(f"  [warn] Instance {inst.get('id')} has no public IP, skipping key injection")
            continue

        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=30",
            "-o", "BatchMode=yes",
        ]
        if ssh_key_path:
            ssh_cmd += ["-i", ssh_key_path]
        ssh_cmd += [f"ubuntu@{ip}", "tee -a ~/.ssh/authorized_keys > /dev/null"]

        print(f"  {ip} … ", end="", flush=True)
        try:
            subprocess.run(
                ssh_cmd,
                input=keys_block,
                text=True,
                check=True,
                timeout=60,
            )
            print("ok")
        except subprocess.CalledProcessError as exc:
            print(f"failed (exit {exc.returncode})")
            print(f"    [warn] Could not inject keys into {ip}. You can do it manually:")
            for key in extra_public_keys:
                print(f"    echo {shlex.quote(key)} >> ~/.ssh/authorized_keys")
        except subprocess.TimeoutExpired:
            print("timed out")


# ---------------------------------------------------------------------------
# Private network setup
# ---------------------------------------------------------------------------

def print_network_setup(instances: list[dict], name: str) -> str:
    """
    Print /etc/hosts setup instructions for the private network.
    Lambda instances in the same region share a 10.x.x.x private network.
    Returns a summary string for notifications.
    """
    if len(instances) <= 1:
        return ""

    lines = [
        "",
        "Private network setup",
        "─" * 40,
        "All instances share Lambda's private 10.x.x.x network.",
        "Add the following to /etc/hosts on EACH instance:",
        "",
    ]

    hosts_entries = []
    for i, inst in enumerate(instances):
        private_ip = inst.get("private_ip") or inst.get("ip", "N/A")
        hostname = f"{name}-{i}"
        hosts_entries.append(f"{private_ip}  {hostname}")
        lines.append(f"  {private_ip}  {hostname}")

    lines += [
        "",
        "Run this one-liner on each instance to populate /etc/hosts:",
        "",
        "  sudo tee -a /etc/hosts << 'EOF'",
    ]
    for entry in hosts_entries:
        lines.append(f"  {entry}")
    lines += ["  EOF", ""]

    lines.append("Verify connectivity (from any instance):")
    for i in range(len(instances)):
        lines.append(f"  ping -c1 {name}-{i}")

    print("\n".join(lines))
    return "\n".join(hosts_entries)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def cmd_list_types(args) -> None:
    instance_types = list_instance_types()
    rows = []
    for type_name, info in instance_types.items():
        specs = info.get("instance_type", {})
        gpus = specs.get("gpus", 0)
        vcpus = specs.get("vcpus", "?")
        mem = specs.get("memory_gib", "?")
        regions = [r.get("name", "") for r in info.get("regions_with_capacity_available", [])]
        rows.append((type_name, gpus, vcpus, mem, regions))

    # Sort by GPU count descending, then name
    rows.sort(key=lambda r: (-r[1], r[0]))

    col_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{'INSTANCE TYPE':<{col_w}} {'GPUs':>4}  {'vCPUs':>5}  {'RAM (GiB)':>9}  AVAILABLE REGIONS")
    print("─" * (col_w + 50))
    for type_name, gpus, vcpus, mem, regions in rows:
        avail = ", ".join(regions) if regions else "—"
        print(f"{type_name:<{col_w}} {gpus:>4}  {vcpus:>5}  {mem:>9}  {avail}")
    print()


def instance_name(base: str, num: int, args, suffix: str = "") -> str:
    """Name of the num-th launched instance (1-based). With --name-start N the instances are numbered
    <base>-<NNN> from N (e.g. b200-us-east-1-003, -004), so every instance gets a distinct name."""
    if getattr(args, "name_start", None) is None:
        return f"{base}{suffix}"
    return f"{base}-{args.name_start + num - 1:03d}"


def type_region(inst: dict) -> tuple[str | None, str | None]:
    return (inst.get("instance_type") or {}).get("name"), (inst.get("region") or {}).get("name")


def find_instance_by_name(name: str, types=None, region: str | None = None) -> dict | None:
    """The non-terminated instance named `name`, if any (launch idempotency check). `types` is the allowed instance type
    (or collection of types). An instance with that name but another type or region is not ours to adopt: stop for
    operator review rather than count or ignore it."""
    allowed = {types} if isinstance(types, str) else (set(types) if types else None)
    for inst in _request("GET", "/instances").get("data", []):
        if inst.get("name") != name or inst.get("status") in ("terminating", "terminated"):
            continue
        it, rg = type_region(inst)
        if (allowed and it not in allowed) or (region and rg != region):
            notify(f"{name} exists as {it} in {rg}, not {sorted(allowed) if allowed else 'the requested type'} in "
                   f"{region or 'any region'}: name clash, stopped without launching. Check the Lambda console.")
            sys.exit(4)
        return inst
    return None


RECONCILE_SECONDS = 600   # how long to look for an instance whose launch request had an unknown outcome


def is_definite_rejection(exc: Exception) -> bool:
    """A 4xx answer means the launch was refused (no capacity, bad request, rate limit): safe to try again later.
    Anything else (5xx, timeout, dropped connection, unreadable reply) may have launched an instance."""
    return isinstance(exc, ApiHTTPError) and 400 <= exc.code < 500


def resolve_unknown_launch(target: str, unique_names: bool, why, type_name: str | None = None,
                           region: str | None = None) -> str:
    """A launch request whose outcome is unknown is never sent again. With unique names, look for the instance by name
    for RECONCILE_SECONDS (the instance list can lag) and adopt it; if it never shows up, stop for operator review."""
    print(f"[{_ts()}] Launch of {target}: outcome unknown ({why}); not sending it again.")
    if unique_names:
        deadline = time.time() + RECONCILE_SECONDS
        while True:
            try:
                inst = find_instance_by_name(target, type_name, region)
                if inst:
                    print(f"[{_ts()}] {target} exists ({inst.get('id')}); adopting it.")
                    return inst.get("id")
            except Exception as exc:
                print(f"[{_ts()}] Lookup of {target} failed (will retry): {exc}")
            if time.time() >= deadline:
                break
            time.sleep(15)
    notify(f"Launch of {target} has an unknown outcome ({why}) and the instance did not appear by name. Stopped without "
           f"retrying: check the Lambda console, then rerun with the right --count/--name-start.")
    sys.exit(3)


def await_instance(instance_ids: list[str], poll_interval: float) -> dict:
    """Wait for an already-launched instance. Retries only the status polling, never the launch: once the launch
    request was accepted the instance exists (and bills), whatever happens while it boots."""
    while True:
        try:
            return wait_for_instances(instance_ids)[0]
        except TimeoutError:
            print(f"[{_ts()}] {instance_ids} not active yet; still waiting (no new launch).")
        except RuntimeError as exc:
            if "failed" in str(exc):   # error/terminated: report it, do not replace it
                print(f"[{_ts()}] {instance_ids} failed to come up: {exc}. Not relaunching.")
                return {"id": instance_ids[0], "status": "failed"}
            print(f"[{_ts()}] Status check error (will retry, no new launch): {exc}")
        except Exception as exc:
            print(f"[{_ts()}] Status check error (will retry, no new launch): {exc}")
        time.sleep(max(poll_interval, 15))


def poll_and_launch(args) -> None:
    wanted = args.instance_types
    count = args.count
    name = args.name or f"exp-{int(time.time())}"
    unique_names = getattr(args, "name_start", None) is not None

    print(f"[{_ts()}] Polling Lambda Cloud for {count}× one of: {wanted}")
    if args.region:
        print(f"  Preferred region: {args.region}" + (" (only)" if args.region_only else ""))
    print(f"  Poll interval: {args.poll_interval}s")
    if args.dry_run:
        print("  DRY RUN — no instances will actually be launched.")
    print()

    all_instances: list[dict] = []
    locked_type: str | None = None
    locked_region: str | None = None

    for instance_num in range(1, count + 1):
        target = instance_name(name, instance_num, args)
        print(f"[{_ts()}] Acquiring instance {instance_num}/{count} ({target})…")
        attempt = 0
        instance_ids: list[str] = []
        # Phase 0: an instance with this name may already exist (earlier run, or a launch whose reply was lost).
        if unique_names and not args.dry_run:
            while True:
                try:
                    existing = find_instance_by_name(target, wanted, args.region if args.region_only else None)
                    break
                except Exception as exc:
                    print(f"[{_ts()}] Lookup of {target} failed (will retry): {exc}")
                    time.sleep(max(args.poll_interval, 5))
            if existing:
                ex_type, ex_region = type_region(existing)
                if locked_type is None:
                    locked_type, locked_region = ex_type, ex_region   # the rest of the set follows the first instance
                elif (ex_type, ex_region) != (locked_type, locked_region):
                    notify(f"{target} exists as {ex_type} in {ex_region}, but this set is {locked_type} in {locked_region}: "
                           f"stopped without launching. Check the Lambda console.")
                    sys.exit(4)
                print(f"  {target} already exists ({existing.get('id')}, {ex_type} in {ex_region}); adopting it, no new launch.")
                instance_ids = [existing.get("id")]
        # Phase 1: poll and launch. Retried only while launches are refused outright; an accepted or unknown launch
        # request is never sent again.
        while not instance_ids:
            attempt += 1
            try:
                instance_types = list_instance_types()

                if locked_type and locked_region:
                    # Stay on the same type+region for consistency
                    info = instance_types.get(locked_type)
                    regions = [r.get("name", "") for r in info.get("regions_with_capacity_available", [])] if info else []
                    match = (locked_type, locked_region) if locked_region in regions else None
                    if not match:
                        print(f"[{_ts()}] Attempt {attempt}: {locked_type} in {locked_region} no longer has capacity. Retrying in {args.poll_interval}s…")
                else:
                    match = find_candidate(instance_types, wanted, args.region, args.region_only)
                    if not match:
                        print(f"[{_ts()}] Attempt {attempt}: no capacity for {wanted}. Retrying in {args.poll_interval}s…")

                if match:
                    type_name, region = match
                    if locked_type is None:
                        locked_type = type_name
                        locked_region = region

                    print(f"[{_ts()}] CAPACITY FOUND: {type_name} in {region}")
                    print(f"Launching instance {instance_num}/{count}…")
                    try:
                        instance_ids = launch_instances(
                            instance_type=type_name,
                            region=region,
                            count=1,
                            ssh_key=args.ssh_key,
                            name=target,
                            filesystem_ids=args.filesystems or [],
                            dry_run=args.dry_run,
                        )
                    except Exception as exc:
                        if not is_definite_rejection(exc):
                            instance_ids = [resolve_unknown_launch(target, unique_names, exc, type_name, region)]
                            break
                        print(f"[{_ts()}] Launch refused ({exc}); will try again when capacity shows.")
                        instance_ids = []
                    else:
                        if args.dry_run:
                            print(f"  [dry-run] Launch call complete for instance {instance_num}/{count}.")
                            break
                        if instance_ids:
                            print(f"  Launched instance ID: {instance_ids}")
                            break
                        instance_ids = [resolve_unknown_launch(target, unique_names, "launch reply had no instance ID",
                                                               type_name, region)]
                        break

            except KeyboardInterrupt:
                print("\nInterrupted by user. Exiting.")
                if all_instances:
                    print(f"Grabbed {len(all_instances)}/{count} instance(s) before interruption.")
                return
            except Exception as exc:
                print(f"[{_ts()}] Error (will retry): {exc}")

            time.sleep(args.poll_interval)

        if args.dry_run:
            continue

        # Phase 2: wait, inject keys, notify. Failures here never trigger another launch.
        try:
            inst = await_instance(instance_ids, args.poll_interval)
        except KeyboardInterrupt:
            print(f"\nInterrupted while waiting for {instance_ids} (launched). Exiting.")
            return
        all_instances.append(inst)
        if inst.get("status") == "failed":
            notify(f"Launched instance {instance_num}/{count} ({target}, {instance_ids[0]}) failed to come up; not replaced.")
            continue

        # Inject extra SSH keys fetched by name from Lambda
        if getattr(args, "extra_ssh_keys", None):
            try:
                inject_extra_ssh_keys(
                    [inst],
                    get_public_keys_by_name(args.extra_ssh_keys),
                    ssh_key_path=getattr(args, "ssh_key_path", None),
                )
            except Exception as exc:
                print(f"[{_ts()}] Extra key injection failed (instance kept): {exc}")

        ip = inst.get("ip", "N/A")
        priv = inst.get("private_ip", "N/A")
        iid = inst.get("id", "?")
        notify(
            f"Grabbed instance {instance_num}/{count}: {locked_type} in {locked_region} ({target})\n"
            f"  ID: {iid}  public={ip}  private={priv}\n"
            f"  SSH: ssh ubuntu@{ip}"
        )

    if args.dry_run:
        print(f"  [dry-run] All {count} launch call(s) complete. Exiting.")
        return

    ready = [i for i in all_instances if i.get("status") != "failed"]
    if count > 1 and ready:
        net_info = print_network_setup(ready, name)
        if net_info:
            notify(f"Private network — add to /etc/hosts on each node:\n{net_info}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report_launched(args, type_name: str, region: str, instance_ids: list[str]) -> None:
    """Wait for a launched instance, inject keys, notify (runs in a thread)."""
    try:
        instances = wait_for_instances(instance_ids)
        if args.extra_ssh_keys:
            inject_extra_ssh_keys(
                instances,
                get_public_keys_by_name(args.extra_ssh_keys),
                ssh_key_path=args.ssh_key_path,
            )
        for inst in instances:
            ip = inst.get("ip", "N/A")
            notify(
                f"Grabbed {type_name} in {region}\n"
                f"  ID: {inst.get('id', '?')}  public={ip}  private={inst.get('private_ip', 'N/A')}\n"
                f"  SSH: ssh ubuntu@{ip}"
            )
    except Exception as exc:
        notify(f"{type_name} {instance_ids} in {region} launched but did not come up: {exc}")


def poll_each(args) -> None:
    """
    Treat every --instance-types entry as an independent target and launch
    --count instances of EACH (or N, when the entry is written "TYPE:N"). A single /instance-types request per poll
    serves all targets, so the API rate limit (1 req/s) is shared, not multiplied.
    Launches run in background threads so polling never stalls.
    """
    import threading

    base = args.name or f"exp-{int(time.time())}"
    # "TYPE:N" asks for N of that type; a bare "TYPE" uses --count.
    remaining = {}
    for entry in args.instance_types:
        type_name, _, want = entry.partition(":")
        remaining[type_name] = int(want) if want else args.count
    threads: list[threading.Thread] = []
    launched = 0   # across all types, for --name-start numbering
    unique_names = getattr(args, "name_start", None) is not None
    # Adopt instances that already carry the next names (earlier run, or a launch whose reply was lost) before polling.
    if unique_names and not args.dry_run:
        for type_name in list(remaining):
            while remaining[type_name]:
                existing = find_instance_by_name(instance_name(base, launched + 1, args, suffix=f"-{type_name}"),
                                                 type_name, args.region if args.region_only else None)
                if not existing:
                    break
                print(f"[{_ts()}] {instance_name(base, launched + 1, args)} already exists ({existing.get('id')}); adopting it.", flush=True)
                remaining[type_name] -= 1
                launched += 1

    print(f"[{_ts()}] Polling every {args.poll_interval}s for: "
          + ", ".join(f"{n}× {t}" for t, n in remaining.items()))
    if args.dry_run:
        print("  DRY RUN — no instances will actually be launched.")

    attempt = 0
    backoff = 0.0
    try:
        while any(remaining.values()):
            attempt += 1
            started = time.monotonic()
            try:
                instance_types = list_instance_types()
                for type_name, left in remaining.items():
                    if not left:
                        continue
                    match = find_candidate(instance_types, [type_name], args.region, args.region_only)
                    if not match:
                        continue
                    region = match[1]
                    print(f"[{_ts()}] CAPACITY FOUND: {type_name} in {region}", flush=True)
                    target = instance_name(base, launched + 1, args, suffix=f"-{type_name}")
                    try:
                        instance_ids = launch_instances(
                            instance_type=type_name,
                            region=region,
                            count=1,
                            ssh_key=args.ssh_key,
                            name=target,
                            filesystem_ids=args.filesystems or [],
                            dry_run=args.dry_run,
                        )
                    except Exception as exc:
                        if not is_definite_rejection(exc):
                            # The request may have launched an instance: never send it again (adopt or stop).
                            instance_ids = [resolve_unknown_launch(target, unique_names, exc, type_name, region)]
                        else:
                            # Capacity races and launch rate limits are common; keep polling for it.
                            print(f"[{_ts()}] Launch of {type_name} refused, still polling: {exc}", flush=True)
                            continue
                    if not instance_ids and not args.dry_run:
                        instance_ids = [resolve_unknown_launch(target, unique_names, "launch reply had no instance ID",
                                                               type_name, region)]
                    remaining[type_name] -= 1
                    launched += 1
                    print(f"[{_ts()}] {type_name}: launched {instance_ids} "
                          f"({remaining[type_name]} left)", flush=True)
                    if args.dry_run:
                        continue
                    t = threading.Thread(
                        target=_report_launched,
                        args=(args, type_name, region, instance_ids),
                    )
                    t.start()
                    threads.append(t)
                if attempt % 60 == 0:
                    print(f"[{_ts()}] Attempt {attempt}: still waiting for "
                          f"{ {k: v for k, v in remaining.items() if v} }", flush=True)
                backoff = 0.0
            except Exception as exc:
                if "HTTP 429" in str(exc):
                    # Cloudflare rate-limit ban (1015): hammering keeps it alive, so back off.
                    backoff = min(max(backoff * 2, 5.0), 120.0)
                    print(f"[{_ts()}] Rate limited (429); backing off {backoff:.0f}s", flush=True)
                    time.sleep(backoff)
                else:
                    print(f"[{_ts()}] Error (will retry): {exc}", flush=True)
            time.sleep(max(0.0, args.poll_interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nInterrupted; no further launches. Waiting for in-flight launches…")
    for t in threads:
        t.join()


def parse_args():
    p = argparse.ArgumentParser(
        description="Poll Lambda Cloud and auto-grab instances when capacity appears.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    # --- list ---
    sub.add_parser("list", help="List all instance types and their current availability.")

    # --- grab ---
    grab = sub.add_parser("grab", help="Poll for capacity and launch instances.")
    grab.add_argument(
        "--instance-types", nargs="+", required=True, metavar="TYPE",
        help="Ordered list of acceptable instance types. First with capacity wins.",
    )
    grab.add_argument(
        "--count", type=int, required=True,
        help="Number of instances to launch.",
    )
    grab.add_argument(
        "--ssh-key", required=True, metavar="KEY",
        help="SSH key name registered on Lambda Cloud used for the launch request (Lambda only supports one key at launch).",
    )
    grab.add_argument(
        "--extra-ssh-keys", nargs="+", default=[], metavar="KEY",
        help="Additional SSH key names registered on Lambda Cloud to inject into each instance after launch.",
    )
    grab.add_argument(
        "--ssh-key-path", default=None, metavar="PATH",
        help="Path to the private key file for --ssh-key (used to connect when injecting extra keys). Defaults to your SSH agent.",
    )
    grab.add_argument(
        "--region", default=None,
        help="Preferred region (e.g. us-west-1). Falls back to any region if unavailable, unless --region-only.",
    )
    grab.add_argument(
        "--region-only", action="store_true",
        help="Launch only in --region; capacity in other regions is ignored.",
    )
    grab.add_argument(
        "--poll-interval", type=float, default=30, metavar="SECONDS",
        help="How often to poll the API (default: 30s).",
    )
    grab.add_argument(
        "--name", default=None,
        help="Base name for the instances (default: exp-<timestamp>).",
    )
    grab.add_argument(
        "--name-start", type=int, default=None, metavar="N",
        help="Give each instance its own name <name>-<NNN>, numbered from N (e.g. --name b200-us-east-1 "
             "--name-start 3 -> b200-us-east-1-003, -004). Without it every instance gets the base name.",
    )
    grab.add_argument(
        "--filesystems", nargs="*", default=[],
        help="Lambda filesystem names to attach (optional).",
    )
    grab.add_argument(
        "--each", action="store_true",
        help="Launch --count of EACH listed type instead of the first available one "
             "(one shared poll request per interval).",
    )
    grab.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without actually launching instances.",
    )

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)
    if args.command == "grab" and args.region_only and not args.region:
        p.error("--region-only needs --region")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.command == "list":
        cmd_list_types(args)
    elif args.command == "grab":
        if args.each:
            poll_each(args)
        else:
            poll_and_launch(args)
