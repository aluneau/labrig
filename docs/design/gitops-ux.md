# GitOps / ZTP card: UX design

The hub's ACM tab card that deploys bare-metal spokes from git (Gitea on the host + Argo CD on the hub). Code:
`frontend/src/components/clusters/AcmGitops.tsx`, wired in `AcmHub.tsx`. Check: `e2e/ui-gitops.js` (every state
mocked, screenshots `e2e/screenshots/after-*.png`, the old card in `before-*.png`).

## What was wrong

- **No answer to "does it work?"** The state was spread over three sections of label/value rows. A failed sync
  showed two orange labels (`OutOfSync` `Degraded`) and nothing about *why*: Argo CD's error never reached the page.
- **Gitea down looked like "not started yet"** (grey label, the error in small grey text under it).
- **Installing was invisible**: while the OpenShift GitOps add-on installed, the card still said "not installed"
  next to a greyed-out button.
- **Steps in the wrong order**: "1. git server" came first, though it starts by itself; the only thing to do
  (install Argo CD, then connect it) was in step 2, behind jargon ("Deploy (apply Application)", "Re-sync").
- **No explanation of direct vs git**, of where the app's commit goes, or of how to edit the repository yourself.
  Worse, the app force-pushes its own copy (`git push --force` on every GitOps provision / delete / re-sync), so
  commits pushed from Gitea are silently lost: nothing said so.
- **"What's in git" was a row of name labels** and a commit list without dates, links, or which commit runs on
  the hub. The spokes table didn't say which spoke came from git.
- At 400 px the label/value layout wrapped awkwardly; the password row broke into several lines.

## Information architecture

1. **Title + one status label** (`Off`, `Installing`, `Not connected`, `Ready`, `Syncing`, `In sync`, `Behind git`,
   `Sync failed`, `Git server down`, `Install failed`) and a *Sync now* button once Argo CD is connected.
2. **One summary sentence** (inline alert) saying what is true now and the single next action, if any.
   It carries Argo CD's own error message when a sync fails.
3. **Three tiles, left to right = the data path**: *Git server on this host* → *Argo CD on the hub* → *Last sync*.
   Each has an icon state, one plain line, and its link (Open in Gitea / Open Argo CD / View in Argo CD). A link that
   can't work yet is shown disabled with a tooltip saying when it will.
4. **What's in git**: branch / folder, the reminder that secrets are never committed, then one row per spoke folder:
   Argo CD sync + health of *that spoke's* resources, the cluster state, links to its three files in Gitea. Then the
   recent commits: short SHA (link to Gitea), message, age, and an `on the hub` label on the commit Argo CD runs.
   Empty state: "No spoke in git yet" + how one gets there.
5. **Work in the repository yourself** (collapsed): `git clone` command to copy, user + *Show password* (secret
   read, *New password* behind a confirm), "applied within 3 minutes", and the warning that the app's copy wins.
6. **What changes when a spoke is deployed from git?** (collapsed, open on first visit when GitOps is off): direct vs
   git side by side, plus the Argo CD Application YAML.

The spoke form's checkbox is now **Deploy from git (GitOps)** with a one-line description; before Argo CD is
installed it is replaced by a hint pointing at the card. Spokes deployed from git carry a `from git` label.

## States (summary line)

| Phase | When | Says / offers |
|---|---|---|
| Off | Argo CD not installed | GitOps is off, spokes are applied directly. **Install OpenShift GitOps** (5–10 min) |
| Installing | add-on task running | spinner + the add-on's message, "updates by itself" (polls every 10 s) |
| Install failed | add-on task error | its message + Try again |
| Not connected | Argo CD, no Application | what connecting does. **Connect Argo CD to git** |
| Git server down | Gitea error, or set up but not running | the Gitea error, "nothing changes on the hub". **Sync now** restarts it |
| Sync failed | operation Failed / Error, or Degraded | Argo CD's message, **Sync now**, View in Argo CD |
| Syncing | operation Running, Progressing, OutOfSync | commit being applied + current step (polls every 10 s) |
| Behind git | hub revision ≠ latest commit | Argo CD polls every 3 min; Sync now applies at once |
| In sync | all above fine, spokes in git | "the hub runs commit X, the latest in git", N spokes, last applied |
| Ready | all fine, no spoke yet | how to deploy the first spoke from git |

## Wording rules

Plain words first, the Kubernetes term second (`Argo CD Application`, `OutOfSync` only as labels). Say where things
run ("on this host", "on the hub"). Every disabled control says why. Times are relative ("18 min ago").

## Backend read fields added (small)

- `application.operation` {phase, message, started_at, finished_at}: Argo CD's last sync operation (the error text).
- `application.reconciled_at`.
- `application.spokes` {name: {sync, health, resources, out_of_sync}}: the Application's resources rolled up per
  spoke folder (namespaced by namespace, cluster-scoped such as the ManagedCluster by name).
- `acm.spokes[].gitops`: the spoke was deployed from git.

## Open points (backend)

- The force push replaces commits made in Gitea. Either fetch + rebase before pushing, or keep the warning.
- When Gitea fails at startup (`reconcile` only logs), `git_server.error` stays empty: the card falls back on
  "set up but not running". Returning the last start error in `git_server.error` would show the reason.
