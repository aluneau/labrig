# Disconnected labs

## OpenShift

A cluster created with **Disconnected** (`openshift.disconnected: true`, create time only) is installed like
at a customer site without internet access. The group router plays the bastion: it keeps its own internet
access, runs the mirror registry, and the group's machines lose theirs.

```
mirror.openshift.com / quay.io / registry.redhat.io
        │  (router only: oc-mirror v2)
        ▼
router: mirror registry  registry.<domain>:8443  (also <router uplink IP>:8443 from the host)
        ▲  egress blocked: nothing from the group network goes out
        │
nodes: release, operators, demo image pulled from the mirror (IDMS / ITMS, mirrored catalog)
```

What the create task does, in order:

1. **Registry**: enabled on the group router (an owned change of the group spec). An auto-created group's
   router is created with it; an existing group's router gets the registry's RAM (8 GiB, 4 vCPUs) and is
   restarted once, plus a 250 GiB thin disk. The registry is set up (Quay, see above).
2. **Mirror**: one oc-mirror v2 request with
   - the release (`openshift_version`),
   - the operator packages of the chosen add-ons, default channel (as the add-ons subscribe):
     `lvms-operator`, `local-storage-operator` + `odf-operator`, `sriov-network-operator`, `metallb-operator`,
     and the extra operators picked in the dialog. oc-mirror copies only the packages it is given, so the app
     adds their dependencies: it reads `redhat-operator-index:v<minor>` on the host (`oc image extract` of its
     `/configs`, ~200 MB, cached a week in `DATA_DIR/openshift/catalogs/`) and follows `olm.package.required`
     of each package's default-channel head, plus the subscriptions an operator creates itself (ODF 4.18+:
     `odf-operator` -> `odf-dependencies` -> ocs, mcg, rook-ceph, cephcsi, csi-addons, ocs-client,
     prometheus, recipe, external-snapshotter). Without the catalog, a built-in v4.20 list is used.
   - the images the add-ons use outside the catalogs: `registry.access.redhat.com/ubi9/httpd-24` (hello demo).

   First time: ~20 GB or more (a release is ~190 images, ODF adds several GB), 30 to 90 minutes depending on
   the line. A request mirrored before is only read back (seconds), so a second cluster of the same version
   and add-ons in the group starts at once.
3. **Egress blocked** (`router.egress.mode: blocked`), in the same router push as the node reservations,
   DNS records and load balancers: before any node boots.
4. **install-config.yaml**:
   - `pullSecret` = the registry's credentials only (for `registry.<domain>:8443` and `<uplink IP>:8443`). Not
     the Red Hat pull secret: nothing in the cluster could use it, and without `cloud.openshift.com` there is
     no telemetry and Insights stays disabled (the `insights` operator reports it, not an error).
   - `imageDigestSources`: oc-mirror's release mirrors (`quay.io/openshift-release-dev/ocp-release` ->
     `registry.<domain>:8443/openshift/release-images`, `ocp-v4.0-art-dev` -> `.../openshift/release`), each
     with a second mirror on `<router uplink IP>:8443`.
   - `additionalTrustBundle` = the registry CA, `additionalTrustBundlePolicy: Always` (also in the cluster-wide
     trust bundle, so OLM's catalog pods and operators trust the mirror).
5. **Agent ISO on the host**: `openshift-install agent create image` reads the release (release info, base
   ISO, agent tools) through the same mirror configuration, with `oc ... --insecure=true --icsp-file`
   (4.16 to 4.20 installers alike). The host can't resolve `registry.<domain>`, so it falls through to the
   second mirror, `<uplink IP>:8443`, which the host reaches like the API load balancer (no TLS check by the
   installer there; nothing to change on the host). The nodes use the first one, through the router's DNS.
6. **After the install**: oc-mirror's cluster resources are applied (IDMS, ITMS for the tag-pulled demo image,
   the `cs-redhat-operator-index-v4-NN` CatalogSource, release signature ConfigMaps; kinds the release doesn't
   know, e.g. OLM v1 `ClusterCatalog` on older releases, are skipped), `OperatorHub` gets
   `disableAllDefaultSources: true`, and the app waits for the CatalogSource to be `READY`. The add-ons then
   subscribe to that CatalogSource.

The install itself follows the usual path (Assisted Service on master-0 polled from the router, then the API
through the router's haproxy): none of it needs internet. NTP comes from the router.

**Day 2**: the cluster page shows a *disconnected* badge and the registry. The Operators tab lists the
mirrored catalog only; installing an operator or enabling an add-on first mirrors it (a request with every
operator of the cluster: oc-mirror pushes one filtered catalog image per index, so a smaller request would
drop the packages mirrored before), then updates the cluster's catalog. A package not mirrored yet can be
typed by name.

**Delete**: an auto-created group goes with the cluster (registry included). In an existing group, the
group's egress returns to what it was before the cluster; the registry stays enabled with its content
(reusable by the next cluster; disable it on the group page to give the RAM back).

**Restart of the app during the create**: before the node VMs exist (registry setup, mirroring) the cluster
ends in `error` ("Interrupted by a server restart"): delete it and create it again. The oc-mirror run goes on
in the router meanwhile (detached unit) and the new create picks it up or reads its results. Once the nodes
booted, the install resumes as for a connected cluster (`openshift_installer.resume`), mirror resources
included.

Known limits:
- `insights` reports disabled (no `cloud.openshift.com` token); console links to Red Hat sites don't open from
  the lab. The samples operator imports nothing (it detects that `registry.redhat.io` is unreachable).
- Updates: the release graph isn't mirrored (`graph: false`); updates stay disabled (`disable_updates`).
- Operators from certified / community catalogs are mirrored from their index too, without the dependency
  lookup (only `redhat-operators` is read).
- The cluster's IDMS keeps the `<uplink IP>:8443` mirror second: harmless (the nodes resolve the first one).
