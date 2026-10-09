# Releases and updates

VM Manager can be installed from a GitHub release and updated in one command (or one button), without touching
running labs: the VMs keep running while the app restarts (libvirt holds them).

## Two ways to run it

| | Git checkout ("checkout mode") | Release install ("release mode") |
|---|---|---|
| For | development (the rig) | hosts that only run it |
| Code | the clone | `/opt/vm-manager/releases/<version>/` (root-owned, own venv); `/opt/vm-manager/current` -> running one |
| Data | `backend/data` | `/var/lib/vm-manager` (owned by the service user) |
| Config | `backend/.env` | `/etc/vm-manager/vm-manager.env` (root:group 0640); updater settings `/etc/vm-manager/update.conf` |
| Update | `git pull && scripts/setup.sh` | `sudo vm-manager-update update`, or Host page > **Update now** |

## Install a host from GitHub

```bash
curl -fLO https://raw.githubusercontent.com/aluneau/labrig/main/scripts/vm-manager-update
sudo python3 vm-manager-update install                           # latest stable release, listens on 127.0.0.1:8000
sudo python3 vm-manager-update install --channel nightly -- --listen 0.0.0.0 --port 8000   # follow main
```

Run it with `sudo` from the account that will run the app (or `--user NAME`). Everything after `--` goes to
`scripts/setup.sh` (`--listen`, `--port`, `--no-boot`, `--wg-ports`, `--no-auth`) and is kept for updates.
The release's `setup.sh --release` does the rest: packages, libvirt, privileged helper, PAM, venv, systemd unit,
and installs the updater as `/usr/sbin/vm-manager-update`.

Moving an existing git-checkout install to a release install (same host):
`sudo python3 vm-manager-update install --import-data ~/Projets/vm-manager` copies its `backend/data` (DB,
OpenShift files, registry credentials) and `backend/.env`. Stop the old service first; the new unit replaces it.

## Update

```bash
sudo vm-manager-update check                 # latest release of the channel vs the running one
sudo vm-manager-update update                # latest stable (or the channel in update.conf)
sudo vm-manager-update update --channel nightly
sudo vm-manager-update update --version 0.2.0
sudo vm-manager-update rollback              # back to the previous release
vm-manager-update status | list
```

An update: resolves the release with the GitHub API -> downloads the tarball and checks its `.sha256` -> unpacks
it next to the others -> runs its `setup.sh --release --no-restart` (new packages, helper, unit, venv) -> refuses
while background tasks run (`--force` to go on: they are marked interrupted, an OpenShift install resumes) ->
switches `current` -> restarts the service -> waits until `/health` reports the new version (2 min), **otherwise
switches back** and restarts the previous one. The 3 newest releases are kept. Log: `/var/log/vm-manager-update.log`.

The **Host page** shows the running version, the latest release of the channel (checked at most every 6 h) with its
release notes, and the command. On a release install, admins get **Update now**: the app asks the privileged
helper (`self-update`), which starts the updater detached (`vm-manager-update.service`, it survives the app's
restart); the page reloads when the new version answers. The helper only installs the **current latest release
of the channel** from the repo in `/etc/vm-manager/update.conf`: the app can't pick another source or an older
version.

During the restart (a few seconds) the UI is unreachable and WireGuard remote access pauses (the relay runs in
the app); VMs, routers and clusters keep running.

## Publishing versions (maintainer)

- Every push to `main` runs CI (`.github/workflows/ci.yml`: Python 3.9 grammar, backend import, templates, tsc +
  UI build, `go vet`) and refreshes the **nightly** pre-release (`release.yml`): `vm-manager-<version>-nightly.
  <date>.<sha>.tar.gz` + `.sha256`.
- A **stable** release: bump `version` in `backend/pyproject.toml`, commit, then `git tag v0.2.0 && git push
  --tags`. The workflow checks the tag matches pyproject, builds the tarball (UI prebuilt) and publishes the
  GitHub release with generated notes (edit them on GitHub: the Host page shows them).
- Locally: `scripts/package.sh [version]` (`NO_UI_BUILD=1` reuses `frontend/build`).

## Settings

| Setting | Default | |
|---|---|---|
| `UPDATE_REPO` | `aluneau/labrig` | GitHub repo checked by the Host page (the updater uses `REPO` in update.conf) |
| `UPDATE_CHANNEL` | `stable` | `stable` = latest release, `nightly` = rolling pre-release from main |
| `UPDATE_CHECK` | `true` | `false`: the Host page doesn't ask GitHub (air-gapped hosts) |

Not yet: signed tarballs (minisign; today the checksum comes from the same GitHub release), an RPM in COPR.

## Verified (2026-10-09)

Nested AlmaLinux 9 (Python 3.9, SELinux enforcing): `install --channel nightly` straight from GitHub (layout, 0640 config,
login), CLI `update` between two nightlies (about 15 s from switch to healthy), the Host page **Update now** button, `rollback`.
