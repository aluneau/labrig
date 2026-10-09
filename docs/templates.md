# Customer-case templates

A template is a ready-made lab: a lab group spec (and optionally a cluster) with a few parameters and a
**guide** (the customer symptom, what is in the lab, steps, checks). On the **Templates** page you pick one,
enter the case number (and the other parameters), review the final spec, press **Create**: the lab group is
created (then the cluster, if any) and its page opens on the **Case guide** tab.

- Built-in templates: `backend/app/templates/<id>.yaml` (read-only, shipped with the app)
- User templates: `DATA_DIR/templates/<id>.yaml` (default `backend/data/templates/`), made with **Save as
  template** on a group page, or written by hand; shown with a *custom* badge, deletable
- Validate files without the app: `scripts/check-templates.py [file.yaml …]` (exit 1 on an invalid file)

## Built-in templates

| id | What | Resources |
|---|---|---|
| `basic-lab` | router + 2 Debian members | 2.5 GiB |
| `dhcp-dns-records` | static reservations for non-members, A / CNAME / wildcard / absolute records | 1.5 GiB |
| `wireguard-remote` | WireGuard remote access to a "corp" zone with an nginx intranet | 1.5 GiB |
| `bgp-anycast` | 2 FRR members announce an anycast /32, router ECMP (cloud-init sets FRR up) | 2.5 GiB |
| `disconnected-registry` | egress blocked + mirror registry on the router | 9 GiB (heavy) |
| `kubeadm-lb` | kubeadm cluster in the group, router haproxy in front of the API | 6.5 GiB |
| `openshift-sno` | OpenShift SNO in the group (needs the pull secret) | 24.5 GiB (heavy) |

## File format

```yaml
id: split-dns                 # = the file name (lowercase letters, digits, '-')
title: Split DNS
summary: One line shown on the card.
tags: [dns]
requires: []                  # feature names shown on the card; checked: openshift / pull-secret (pull secret set)
resources: {vcpus: 3, memory_mb: 3584, disk_gb: 30}   # estimate for the card (the review computes its own too)
heavy: false                  # extension: "heavy" badge (lots of RAM / downloads)
author: someone               # extension, optional
params:                       # must include `case`
  - {name: case, label: Case number, type: string, required: true, pattern: '^[0-9a-z][0-9a-z-]{0,11}$'}
  - {name: cidr, label: Group subnet, type: cidr, default: auto}
  - {name: image, label: Cloud image, type: cloud_image, default: debian-13}
name: "c{{case}}-dns"         # the group name (<= 32 chars, lowercase)
group: {...}                  # a GroupSpec (backend/app/schemas/group.py) as YAML, minus `name`
cluster: {...}                # optional ClusterCreate (schemas/cluster.py), created inside the group
guide: |                      # markdown
  # Case {{case}}: ...
```

### Parameters

| type | value | form field |
|---|---|---|
| `string` | text, `pattern` = full-match regex | text input |
| `int` | integer, `min` / `max` | number input |
| `bool` | true / false | checkbox |
| `choice` | one of `choices` | select |
| `cloud_image` | a cloud image name such as `debian-13` (must be downloaded) | select of the ready images |
| `cidr` | an IPv4 network, or `auto` | text input |

Other keys: `label`, `help`, `required`, `default`. An empty optional parameter has no value: a field that is
exactly `"{{param}}"` then becomes `null` (the schema default applies). A parameter named `keyboard` is pre-filled
in the UI with the browser's layout.

### Placeholders

Plain substitution in every string (no Jinja): `{{name}}` with optional spaces.

- A string that is exactly one placeholder takes the parameter's type: `memory: "{{memory}}"` gives an integer.
- Built-ins: `{{group}}` (the group name), `{{domain}}` (the group domain, default `<group>.lab`),
  `{{ip:N}}` (the N-th address of the group subnet, e.g. `{{ip:1}}` = the router), `{{<cidr param>:N}}`
  (the N-th address of a `cidr` parameter, e.g. `{{anycast_range:1}}`). Parameters can't be called `group` or
  `domain`.
- `group.cidr` missing or `auto` (e.g. `cidr: "{{cidr}}"` with the default `auto`): the first free /24 of
  `TEMPLATE_SUBNET_POOL` (default `10.42.0.0/16`) that overlaps no libvirt network nor lab group. Always use
  `{{ip:N}}` for addresses in the group subnet so the template works on any subnet.
- Unknown placeholders are a load error. Quote values inside YAML flow mappings (`a: "{{ip:50}}"`). Substituted
  text is not escaped: a password with quotes breaks a `user_data` that quotes it.

### Conditions (extension)

A mapping with `_if:` is dropped when the condition is false, wherever it is (a list item, a value):

```yaml
router:
  wireguard: {_if: remote_access, enabled: true}     # no wireguard block unless remote_access
  egress:
    allow:
      - {_if: allow, value: "{{allow}}"}             # {_if, value}: a conditional scalar / list item
```

Conditions: `param` (truthy), `!param`, `param == value`, `param != value`.

### Cluster block

A `ClusterCreate` without `group_id` / `network` / `cidr` (the engine puts it in the template's group); `kubeadm` or
`openshift` (k3s uses its own network, refused). Extension: `image: debian-13` (a cloud image name) instead of
`cloud_image_id`. The cluster is created by a second task (`template_cluster`) that waits for the group to be
ready (an app restart in between fails that task: create the cluster from the Clusters page in the group).

## Rendering and creating (API)

- `GET /api/v1/templates` → `{templates, errors}` (`errors` = files that failed to load, with the reason)
- `GET /api/v1/templates/{id}` → metadata + raw `group` / `cluster` / `guide` / the file `yaml`
- `POST /api/v1/templates/{id}/render` `{params, yaml?}` → `{ok, params, group, cluster, yaml, guide, hcl, errors,
  warnings, estimate}`. `yaml` = the document `group:` (+ `cluster:`) edited in the review step: it replaces the
  rendered one (the guide follows its name / subnet). `errors` include invalid values, schema errors and what
  create would refuse on this host (group / network / VM / cluster names taken, subnet overlaps, images not
  downloaded, WireGuard / BGP pools, pull secret). `estimate` = router + members + kubeadm nodes (max with the
  template's `resources`) against the host's available RAM (`fits`).
- `POST /api/v1/templates/{id}/create` `{params, yaml?}` → `{group_id, group_name, task_id, cluster_task_id}`.
  The group spec gets `template: {id, title, case, params, guide, created_at}` (kept across spec updates, stored in
  the router's libvirt metadata like the rest of the spec): the group page shows the guide even if the template
  file goes away.
- `POST /api/v1/templates` `{group_id, id, title, summary, tags, guide?}`: save a group as a user template.
  Assigned fields are dropped (MACs, router IP / image, WireGuard devices and keys, BGP announce ranges, cluster
  entries, defaults), addresses of the group subnet become `{{ip:N}}`, the name becomes `c{{case}}-<old name>`, the
  subnet a `cidr` parameter (`auto`). A kubeadm / OpenShift cluster in the group is saved as the `cluster` block.
- `DELETE /api/v1/templates/{id}` (user templates only), `POST /api/v1/templates/check` `{yaml}` (validate a document).

## OpenTofu

The review step's **OpenTofu** tab gives the same lab as `vmmanager_group` (+ `vmmanager_cluster` with
`group_id`). What the provider can't express (DHCP range, address pools, BGP details, router vCPU / disk, pod / service
CIDRs…) is listed as comments at the end.

## Adding a built-in template

1. Write `backend/app/templates/<id>.yaml` (copy `basic-lab.yaml`). Keep the case number in the group name and use
   `{{ip:N}}` / `{{domain}}` instead of literal addresses / names.
2. Guide sections that work well: **Customer symptom**, **What is in the lab**, **Steps**, **Checks**,
   **Things to try**.
3. `scripts/check-templates.py` (parses, renders with sample values with every bool both ways, prints the estimate).
4. Try it for real: Templates → your card → review → Create.
