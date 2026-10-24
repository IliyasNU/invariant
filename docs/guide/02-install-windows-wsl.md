# 2. Install on Windows 11 with WSL Ubuntu

Invariant is developed and tested on Linux. On Windows 11, run it inside
WSL 2 (Windows Subsystem for Linux) with Ubuntu: every command in this guide
then works unchanged.

> **Verification:** the Ubuntu commands below (from step 4 on) were run on
> Ubuntu 24.04. The Windows steps (1 to 3) follow Microsoft's and Docker's
> documentation and were not executed for this guide. If a step differs on
> your machine, please correct this page.

## 1. Install WSL and Ubuntu

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart when asked. On first start, Ubuntu asks for a Linux user name and
password; they are separate from your Windows account. Check that it runs
under WSL 2:

```powershell
wsl --list --verbose
```

The `VERSION` column must say `2`. If it says `1`:
`wsl --set-version Ubuntu-24.04 2`.

From now on, run every command in the **Ubuntu terminal** (Start menu →
Ubuntu, or Windows Terminal → Ubuntu), not in PowerShell.

## 2. Install Docker

Pick one of the two.

**A. Docker Desktop for Windows** (simplest). Install it, then in
*Settings → General* keep *Use the WSL 2 based engine* on, and in
*Settings → Resources → WSL integration* turn on your Ubuntu distribution.
Docker Desktop must be running whenever you use `docker` in Ubuntu. Check
its licence terms for your organisation.

**B. Docker Engine inside Ubuntu** (no Docker Desktop). Follow Docker's
"Install Docker Engine on Ubuntu" page, using the `apt` repository, then let
your user run Docker without `sudo`:

```bash
sudo usermod -aG docker "$USER"
# close and reopen the Ubuntu terminal
```

If `docker info` says it cannot connect to the daemon, start it with
`sudo service docker start`, or enable systemd in `/etc/wsl.conf`
(`[boot]` / `systemd=true`) and run `wsl --shutdown` in PowerShell once.

Either way, check:

```bash
docker info --format '{{.ServerVersion}}'
docker compose version
```

## 3. Configure Git

```bash
sudo apt update && sudo apt install -y git curl
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# Keep LF line endings in the repository, whatever Windows tools do.
git config --global core.autocrlf input
```

## 4. Clone into the Linux file system

Clone under your Linux home directory, **not** under `/mnt/c/...`. Files on
the Windows drive are much slower from WSL, and file permissions and file
watching (`fastapi dev` reloads) behave differently there.

```bash
mkdir -p ~/code && cd ~/code
git clone https://github.com/IliyasNU/invariant.git
cd invariant
```

To edit the code from Windows, run `code .` in this directory with the
VS Code *WSL* extension installed, or open `\\wsl$\Ubuntu-24.04\home\<you>\code\invariant`
in Explorer.

## 5. Install uv and Python 3.14

Invariant uses [uv](https://docs.astral.sh/uv/) for Python, dependencies and
commands. Ubuntu's own Python is older than the 3.14 the project needs; uv
downloads 3.14 by itself, from `.python-version`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# open a new terminal, or: source ~/.local/bin/env
uv --version
```

Install the dependencies, the server's and the development tools included:

```bash
uv sync --locked --dev
```

`--locked` fails if `uv.lock` does not match `pyproject.toml`, exactly as in
CI. The environment is created in `.venv/`; you never activate it by hand,
because `uv run <command>` runs a command inside it.

Check:

```bash
uv run python --version          # Python 3.14.x
uv run invariant --version       # invariant 0.3.0.dev0
uv run pytest                    # the suite, on SQLite; no database needed
```

> **Pending v1.0 (stage 5e):** the version will become `1.0.0`.

## 6. Ports between Windows and WSL

With WSL 2, a server that listens on `127.0.0.1` inside Ubuntu is reachable
from Windows at `http://localhost:<port>`. So after chapter 3, open
<http://localhost:8000/docs> in your Windows browser. If it does not answer,
check that no Windows program already uses port 8000 or 5433, and see
[Common errors](07-troubleshooting.md).

Next: [Docker Compose and PostgreSQL](03-docker-postgres.md).
