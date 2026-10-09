# Authentication

VM Manager can create VMs on the host and reach libvirt as a `libvirt` group member, which amounts to root.
So the API and the UI require a login by default. Users are the **host's Linux accounts**, checked through
**PAM**. VM Manager stores no password. Browsers get a session cookie. Scripts and OpenTofu use **API tokens**.

## Who may log in

| Account | Role |
|---|---|
| the account the app runs as (the `vm-manager` service user, i.e. whoever ran `setup.sh`) | admin |
| members of `AUTH_ADMIN_GROUPS` (default `vm-manager,wheel,sudo`) | admin: everything |
| members of `AUTH_VIEWER_GROUPS` (default empty) | viewer: read-only |
| anyone else, and `root` | refused |

Give someone access with `sudo usermod -aG vm-manager alice`. For read-only access, create a group
(`sudo groupadd labviewers`), put `AUTH_VIEWER_GROUPS=labviewers` in `backend/.env`, and restart the service.
Groups are read from NSS (files, SSSD/LDAP…) on every request, cached for 1 minute. Removing someone from the
group also cuts their sessions and tokens. No restart is needed.

**Viewers** can read every page and get the live events. They can't change anything (any write → 403),
except their own API tokens and logging out. They can't open VM consoles either: a VNC console takes
keyboard and mouse input, so the WebSocket counts as a write. They can't fetch credentials either: kubeconfig, OpenShift credentials /
SSH key, registry credentials and WireGuard device configs. Secrets inside ordinary JSON reads are masked
as `***` for them (`password`, `user_data`, `token`, … keys, `user:password@` in URLs: members' login password,
proxy credentials in group specs). The UI still shows the action buttons, but
they fail with "read-only account (viewer)". A "read-only" label is shown next to the user menu.

## Logging in

- **Browser**: the login page appears whenever there is no valid session. It takes the user name and the
  Linux password. The session cookie `vmm_session` is random, HttpOnly, `SameSite=Strict`, and `Secure`
  when served over https (`AUTH_COOKIE_SECURE=auto|true|false`). The server only stores its SHA-256.
  The session expires after `AUTH_SESSION_HOURS` (12) idle hours and lasts at most `AUTH_SESSION_MAX_DAYS`
  (7) days. Log out from the user menu (top right).
- **API tokens** (scripts, OpenTofu, e2e): open the user menu, then **API tokens**, then **Create token**. The
  token is shown once (`vmm_…`) and only its hash is stored. Use it as `Authorization: Bearer <token>`.
  The list shows the creation date, the expiry (optional, in days) and the last use time + IP.
  **Revoke** deletes the token. Admins can also list and revoke everyone's tokens ("All users' tokens").
  A token acts as its user, with that user's current role.
- **Headless** (no browser, e.g. right after `setup.sh` on a lab machine), from `backend/` as the service user:

  ```bash
  venv/bin/python -m app.cli token create --user $USER --name opentofu   # prints the token once
  venv/bin/python -m app.cli token list [--user U]       |  token revoke <id>
  venv/bin/python -m app.cli status                      # settings, PAM service, admin/viewer groups
  venv/bin/python -m app.cli user show alice             # groups + resulting role
  venv/bin/python -m app.cli user check alice            # test a password through PAM (prompted)
  venv/bin/python -m app.cli session revoke --user alice # log alice out everywhere
  ```

Examples:

```bash
export VMM_TOKEN=vmm_…
curl -H "Authorization: Bearer $VMM_TOKEN" http://127.0.0.1:8000/api/v1/vms
export VMMANAGER_TOKEN=$VMM_TOKEN; tofu apply      # OpenTofu provider (or provider "vmmanager" { token = … })
```

The "Use with kubectl" commands of a cluster add `-H "Authorization: Bearer $VMM_TOKEN"` when
authentication is on.

### Public paths

`/health`, the static UI files (the login page needs them), `GET /api/v1/auth/status`, `POST
/api/v1/auth/login` and `/logout`, and the registry CA certificate (`GET /api/v1/groups/{id}/registry/ca.crt`,
which the registry page's `curl` commands fetch). Everything else under `/api` (including the SSE stream
`/api/v1/events` and the VNC WebSocket `/api/v1/vms/{id}/vnc`) requires a session or a token.

## PAM

The login uses the PAM service `vm-manager`. `setup.sh` installs `/etc/pam.d/vm-manager` as the distro's
normal password stack:

| Distro | `/etc/pam.d/vm-manager` |
|---|---|
| EL / Fedora / Arch | `auth include system-auth` + `account include system-auth` |
| Debian / Ubuntu | `@include common-auth` + `@include common-account` |

So whatever the host uses works: local `/etc/shadow`, SSSD (FreeIPA / AD / LDAP), Kerberos, and so on.
The account phase rejects expired or locked accounts. If you edit the file, remove its "Installed by VM
Manager" line and `setup.sh` will keep your version. If the file is missing, the app falls back to `system-auth`
(or `login` on Debian) and logs a warning. `AUTH_PAM_SERVICE` names another service.

**Limitation of pam_unix (local accounts) and how it's handled.** The app runs as an ordinary user, not root.
From a non-root process, `pam_unix` (through `unix_chkpwd`) can only check **that process's own user's**
password. It refuses every other local user. Network accounts (SSSD `pam_sss`, Kerberos) don't have this
limitation. So:

- the service user's own password is checked in-process (ctypes on `libpam`, no Python dependency);
- for **any other account**, the app asks the root helper (`/usr/libexec/vm-manager/helper pam-auth <user>`,
  password on stdin, through `pkexec` like the helper's other commands). The helper always uses the fixed
  service `vm-manager`, never `su`/`sudo`, whose `pam_rootok` would let root in. It refuses uid 0 and
  returns only "ok" or "denied". `AUTH_PAM_HELPER=false` turns this off (then only the service user and
  SSSD/Kerberos accounts can log in).

**pam_faillock**: on hosts whose `system-auth` has `pam_faillock` (enabled with `authselect … with-faillock`),
failed web logins count like failed SSH logins. Enough of them lock the account for its `unlock_time`
everywhere. VM Manager's own backoff (below) slows attempts down, but it can't stop someone from locking a
known account name on purpose. Expose the app only to networks you trust, or skip faillock in
`/etc/pam.d/vm-manager`.

## Protections

- **Login backoff**, per client IP + user name: 3 free failures, then 2 s, 4 s, 8 s… up to 5 min between attempts
  (`429` with `Retry-After`). A client IP is also limited to 30 failures in 15 min, whatever user names it tries.
  Unknown users take as long as wrong passwords (≈1.5 s minimum), so they can't be told apart. The counters
  live in memory: a restart resets them.
- **CSRF**: the cookie is `SameSite=Strict`. On top of that, every write made with the cookie must carry the
  `X-VMM-Request` header (the UI adds it; a cross-site form can't), and its `Origin`, if any, must be the
  app's own (or one of `CORS_ORIGINS`, for the dev UI on :3000). Requests with a Bearer token are exempt:
  browsers never send a token on their own.
- **WebSocket hijacking**: the VNC WebSocket needs the session cookie **and** a same-origin `Origin` header.
  Otherwise the handshake is refused (HTTP 403).
- **Audit log**: in the app log (`journalctl -u vm-manager`), logger `vmm.audit`. It records logins (ok /
  failed / throttled / refused), logouts, token create/revoke, every write (`user=… role=… via=session|token
  ip=… POST /api/v1/vms/12/start -> 200`) and every refusal (403).
- **Over the network**: the app serves plain http. Behind a TLS reverse proxy (nginx, Caddy…), the cookie
  becomes `Secure` when the proxy forwards `X-Forwarded-Proto: https` and uvicorn runs with
  `--proxy-headers`, or with `AUTH_COOKIE_SECURE=true`. The login backoff then sees the proxy's IP unless
  uvicorn trusts its `X-Forwarded-For` (`--forwarded-allow-ips`).

## Turning authentication off

`AUTH_ENABLED=false` in `backend/.env` (or `scripts/setup.sh --no-auth`, which writes it; `--auth` turns it
back on), then restart the service. Everything then works as before authentication existed: no login and
no tokens, and everyone is admin. The masthead shows an orange **authentication disabled** label. The log
warns at startup, more loudly when the app listens on a non-loopback address. Use this only on a single-user
machine listening on 127.0.0.1. Tokens created earlier stay in the DB and work again once authentication is
back on.

## Settings (`backend/.env`)

| Variable | Default | |
|---|---|---|
| `AUTH_ENABLED` | `true` | `false` = no login |
| `AUTH_ADMIN_GROUPS` | `vm-manager,wheel,sudo` | Linux groups with full access (the service user always has it) |
| `AUTH_VIEWER_GROUPS` | (empty) | Linux groups with read-only access |
| `AUTH_PAM_SERVICE` | `vm-manager` | `/etc/pam.d/<service>` |
| `AUTH_PAM_HELPER` | `true` | check other local users' passwords through the root helper |
| `AUTH_SESSION_HOURS` / `AUTH_SESSION_MAX_DAYS` | `12` / `7` | idle expiry (sliding) / absolute cap |
| `AUTH_COOKIE_SECURE` | `auto` | `Secure` cookie flag: `auto` = when the request is https |
| `AUTH_PAM_CONFDIR` | (empty) | tests only: PAM stack directory (`pam_start_confdir`) |

## e2e tests

Every e2e script loads `e2e/auth.js` (in place of `playwright-core`). When the backend has authentication on,
it logs browser contexts in and adds credentials to `fetch()` calls to `BASE_URL`. It uses `VMM_TOKEN` (a
token), or `E2E_USER` + `E2E_PASSWORD`. With authentication off it does nothing. `e2e/auth-test.js` tests
this feature: login page, wrong password, backoff, a user outside the groups, CSRF / Origin, SSE and the
VNC WebSocket with and without a session (and cross-site), a token created in the UI then used and revoked,
the viewer role, logout. It also covers the disabled mode.
Without real passwords at hand, a sandbox backend can use a test PAM stack (`AUTH_PAM_CONFDIR` pointing at a
directory with a `vm-manager` file that accepts a fixed password through `pam_exec`). The real thing
(pam_unix, other users through the pkexec helper) was verified on a nested AlmaLinux 9 install.

## API

| | |
|---|---|
| `GET /api/v1/auth/status` | `{enabled, user: {name, role, via} \| null, admin_groups, viewer_groups}` (public) |
| `POST /api/v1/auth/login` `{username, password}` | sets the session cookie; with an empty body and a Bearer token, opens a session for the token's user |
| `POST /api/v1/auth/logout` | |
| `GET /api/v1/auth/tokens` (`?all=true` for admins), `POST` `{name, expires_days?}`, `DELETE …/{id}` | API tokens |
