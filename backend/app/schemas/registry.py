"""Mirror registry on the group router (disconnected labs, docs/disconnected.md)

The router of a lab group can run Red Hat's mirror-registry (Quay, podman) when
`spec.router.registry.enabled`; oc-mirror v2 (run on the router, which keeps its internet
access) copies an OpenShift release, operator packages and extra images into it.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

IMAGE_REF = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$"


class OperatorPackage(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9._-]*$")
    channel: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # None = the default channel


class OperatorCatalog(BaseModel):
    """Packages of one catalog. catalog None = registry.redhat.io/redhat/redhat-operator-index:v<minor>
    of the request's openshift_version"""
    catalog: Optional[str] = Field(None, pattern=IMAGE_REF)
    packages: List[OperatorPackage] = Field(..., min_length=1)


class MirrorRequest(BaseModel):
    """What to copy into the group's registry. Re-running a request already mirrored is fast."""
    openshift_version: Optional[str] = Field(None, pattern=r"^4\.\d+\.\d+(-[a-z]+\.\d+)?$")  # e.g. "4.19.10"
    operators: List[OperatorCatalog] = []
    additional_images: List[str] = []

    @field_validator("additional_images")
    @classmethod
    def _images(cls, v: List[str]) -> List[str]:
        import re
        out = []
        for image in v:
            image = image.strip()
            if not image:
                continue
            if not re.match(IMAGE_REF, image):
                raise ValueError(f"'{image}' is not an image reference")
            out.append(image)
        return out

    def minor(self) -> Optional[str]:
        """'4.19.10' -> '4.19'"""
        if not self.openshift_version:
            return None
        return ".".join(self.openshift_version.split("-")[0].split(".")[:2])


class MirrorRecord(BaseModel):
    """One mirror run (a request), as listed in the registry status"""
    id: str                                  # hash of the request (its oc-mirror workspace on the router)
    status: str                              # running | done | failed | cancelled
    openshift_version: Optional[str] = None
    operators: List[OperatorCatalog] = []
    additional_images: List[str] = []
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    task_id: Optional[int] = None
    images: Optional[int] = None             # images copied / checked by the last run
    error: Optional[str] = None


class ImageDigestSource(BaseModel):
    """install-config.yaml imageDigestSources entry"""
    source: str
    mirrors: List[str]


class MirrorResult(BaseModel):
    """What an OpenShift install needs to use the mirror. pull_secret_fragment holds the registry's
    credentials: internal use only (merged into install-config's pull secret), never returned by the API."""
    registry_host: str                       # registry.<domain>:<port>
    uplink_registry_host: str                # <uplink_ip>:<port> (how the host reaches it)
    ca_pem: str
    pull_secret_fragment: Dict[str, Any]     # {"auths": {registry_host: {...}, uplink_registry_host: {...}}}
    image_digest_sources: List[ImageDigestSource] = []
    cluster_resources: List[str] = []        # YAML documents from oc-mirror's working-dir/cluster-resources
    catalog_sources: Dict[str, str] = {}     # source catalog image -> CatalogSource name


class RegistryStatus(BaseModel):
    enabled: bool = False
    ready: bool = False                      # registry answering on the router
    state: str = "disabled"                  # disabled | not-installed | installing | restart-required | ready | stopped | error
    message: Optional[str] = None
    url: Optional[str] = None                # registry.<domain>:<port>
    uplink_url: Optional[str] = None         # <uplink_ip>:<port>
    hostname: Optional[str] = None
    port: Optional[int] = None
    ca_pem: Optional[str] = None
    disk_used_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    memory_mb: Optional[int] = None          # router RAM the registry needs (spec)
    vcpus: Optional[int] = None
    router_memory_mb: Optional[int] = None   # router RAM now (live; less than memory_mb = restart needed)
    egress: str = "open"                     # open | blocked
    egress_allow: List[str] = []
    mirrors: List[MirrorRecord] = []
    setup_task_id: Optional[int] = None      # running setup / mirror task, if any
    mirror_task_id: Optional[int] = None


class MirrorStarted(BaseModel):
    task_id: int
