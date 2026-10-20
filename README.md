# testbed-management

Tools for managing GPU testbeds on Lambda Cloud.

## `lambda_grab.py`

Polls Lambda Cloud for GPU instance availability and auto-launches instances the moment capacity appears. Notifies you via Slack and/or email when instances are grabbed. For multi-instance jobs, prints private network setup instructions so nodes can communicate over Lambda's internal 10.x.x.x network.

### Requirements

- Python 3.10+
- No external dependencies (stdlib only)

### Setup

```bash
export LAMBDA_API_KEY="your_api_key"          # required
export SLACK_WEBHOOK_URL="https://hooks.slack.com/..."  # optional
```

Add these to `~/.bashrc` to persist across sessions.

### Usage

#### List available instance types

```bash
python lambda_grab.py list
```

Shows all instance types, GPU/vCPU/RAM specs, and which regions currently have capacity.

#### Grab instances

```bash
python lambda_grab.py grab \
    --instance-types gpu_8x_h100_sxm4 gpu_8x_a100 \
    --count 2 \
    --ssh-key my-key \
    --name my-experiment \
    [--extra-ssh-keys key2 key3] \
    [--ssh-key-path ~/.ssh/id_rsa] \
    [--region us-west-1] \
    [--region-only] \
    [--name-start 3] \
    [--poll-interval 30] \
    [--filesystems fs-abc123] \
    [--each] \
    [--dry-run]
```

- `--instance-types` — ordered preference list; the first type with available capacity wins
- `--count` — number of instances to launch
- `--ssh-key` — SSH key name registered in your Lambda Cloud account used for the launch request (Lambda only supports one key at launch)
- `--extra-ssh-keys` — additional key names to inject into each instance after launch (fetched from Lambda by name and appended to `authorized_keys`)
- `--ssh-key-path` — path to the private key file for `--ssh-key`, used when injecting extra keys (defaults to your SSH agent)
- `--region` — preferred region; falls back to any available region if not found
- `--region-only` — launch only in `--region`; capacity elsewhere is ignored (without it, the first instance can land in another region and the rest follow it)
- `--poll-interval` — seconds between API polls, fractional allowed (default: 30)
- `--name` — base name for launched instances (default: `exp-<timestamp>`)
- `--name-start N` — give each instance its own name `<name>-<NNN>`, numbered from N (e.g. `--name b200-us-east-1 --name-start 3` gives `b200-us-east-1-003`, `-004`); without it every instance gets the base name
- `--filesystems` — Lambda filesystem names to attach (optional)
- `--each` — treat every `--instance-types` entry as its own target and launch `--count` of **each** (instead of the first available). One `/instance-types` request per interval serves all targets, so polling stays within Lambda's 1 req/s limit (despite the nominal 1 req/s, use `--poll-interval 2`: 1 s and 1.5 s both hit Cloudflare 429 bans within about a minute; 429s trigger exponential backoff); launches run in background threads so polling doesn't stall
- `--dry-run` — print what would happen without actually launching

Example: poll 1x, 2x and 8x B200 every 2 s and grab one of each:

```bash
python lambda_grab.py grab --each --count 1 --ssh-key my-key --poll-interval 2 \
    --instance-types gpu_1x_b200_sxm6 gpu_2x_b200_sxm6 gpu_8x_b200_sxm6
```

Example: grab two 8x B200 in us-east-1 only, named after the existing fleet (b200-us-east-1-001/-002 exist):

```bash
python lambda_grab.py grab --instance-types gpu_8x_b200_sxm6 --count 2 --ssh-key my-key \
    --region us-east-1 --region-only --name b200-us-east-1 --name-start 3 --poll-interval 2
```

Once a launch request is accepted it is never repeated: waiting for the instance to boot, status errors and
extra-key injection are retried or reported on their own, and an instance that fails to come up is reported, not
replaced. With `--name-start`, a launch request that errored on our side is checked by name before trying again,
so an accepted-but-unacknowledged launch is adopted rather than duplicated.

Tests (offline, no API calls): `python3 test_lambda_grab.py`.

### Notifications

| Method | How to enable |
|--------|--------------|
| Terminal bell + stdout | Always on |
| Slack | Set `SLACK_WEBHOOK_URL` env var |
| Email | Set `NOTIFY_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` |

### Private networking

## `lambda_terminate.py`

Terminates Lambda Cloud instances by ID, or tears down everything currently running.

### Usage

```bash
# List currently running instances (with IDs)
python lambda_terminate.py list

# Terminate specific instances
python lambda_terminate.py terminate --ids i-abc123 i-def456

# Terminate ALL running instances (prompts to confirm)
python lambda_terminate.py terminate --all

# Skip the confirmation prompt
python lambda_terminate.py terminate --all --yes

# Preview without actually terminating
python lambda_terminate.py terminate --all --dry-run
```

- `--ids` — one or more instance IDs to terminate
- `--all` — terminate every running instance on the account
- `--yes` / `-y` — skip the interactive confirmation prompt
- `--dry-run` — print what would be terminated without making the API call

Requires `LAMBDA_API_KEY` (same as `lambda_grab.py`).

---

When multiple instances are launched, the script prints `/etc/hosts` entries and a one-liner to run on each node. Lambda instances in the same region share a `10.x.x.x` private network automatically — no VPN or overlay needed.

```
Private network setup
────────────────────────────────────────
All instances share Lambda's private 10.x.x.x network.
Add the following to /etc/hosts on EACH instance:

  10.10.1.1  my-experiment-0
  10.10.1.2  my-experiment-1

Run this one-liner on each instance to populate /etc/hosts:

  sudo tee -a /etc/hosts << 'EOF'
  10.10.1.1  my-experiment-0
  10.10.1.2  my-experiment-1
  EOF
```
