"""Minimal OCI distribution (registry v2) client, used from the host against a group's registry through the
router's uplink address: list repositories / tags, delete a tag, push an image archive.

Archives: `podman save` / `docker save` (docker-archive: manifest.json + layer tars) and OCI archives
(oci-layout + index.json + blobs/, also what docker >= 25 saves). Layers are streamed from the tar file
(never loaded in memory); a docker-archive becomes an OCI image manifest (layers keep their compression:
uncompressed tar or gzip).
"""
import base64
import hashlib
import http.client
import json
import re
import ssl
import tarfile
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
INDEX_TYPES = (OCI_INDEX, DOCKER_LIST)
ACCEPT = ", ".join([OCI_INDEX, DOCKER_LIST, OCI_MANIFEST, DOCKER_MANIFEST])
CHUNK = 1024 * 1024
REPO_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class RegistryError(RuntimeError):
    pass


class RegistryClient:
    def __init__(self, host: str, ca_pem: Optional[str], username: str, password: str, timeout: float = 60):
        self.host = host  # "ip:port"
        self.ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl._create_unverified_context()
        self.basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.timeout = timeout
        self._tokens: Dict[str, str] = {}

    # --------------------------------------------------------------- HTTP

    def _conn(self) -> http.client.HTTPSConnection:
        host, _, port = self.host.rpartition(":")
        return http.client.HTTPSConnection(host, int(port), context=self.ctx, timeout=self.timeout)

    def _token(self, challenge: str, scope: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get("realm")
        if not realm:
            raise RegistryError(f"Unexpected auth challenge: {challenge}")
        query = {"service": params.get("service", self.host)}
        if scope:
            query["scope"] = scope
        url = urllib.parse.urlsplit(realm)
        conn = http.client.HTTPSConnection(url.hostname, url.port or 443, context=self.ctx, timeout=self.timeout)
        try:
            conn.request("GET", f"{url.path}?{urllib.parse.urlencode(query)}", headers={"Authorization": f"Basic {self.basic}"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RegistryError(f"Registry login failed ({resp.status}): {body[:200].decode(errors='replace')}")
            data = json.loads(body)
            return data.get("token") or data.get("access_token")
        finally:
            conn.close()

    def request(self, method: str, path: str, scope: str = "", headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None, stream: Optional[Iterator[bytes]] = None, length: Optional[int] = None,
                ok: Tuple[int, ...] = (200, 201, 202, 204)) -> Tuple[int, Dict[str, str], bytes]:
        """One request, retried once with a bearer token for `scope` after a 401"""
        for attempt in range(2):
            hdrs = dict(headers or {})
            token = self._tokens.get(scope)
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
            conn = self._conn()
            try:
                if stream is not None and attempt == 0 and not token:
                    # don't stream a large body into a 401: get the token first
                    conn.request("GET", "/v2/", headers={})
                    resp = conn.getresponse()
                    resp.read()
                    if resp.status == 401:
                        self._tokens[scope] = self._token(resp.getheader("WWW-Authenticate", ""), scope)
                        continue
                if stream is not None:
                    conn.putrequest(method, path)
                    for k, v in hdrs.items():
                        conn.putheader(k, v)
                    conn.putheader("Content-Length", str(length or 0))
                    conn.endheaders()
                    for chunk in stream:
                        conn.send(chunk)
                else:
                    conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            finally:
                conn.close()
            if resp.status == 401 and attempt == 0:
                self._tokens[scope] = self._token(resp_headers.get("www-authenticate", ""), scope)
                continue
            if resp.status not in ok:
                raise RegistryError(f"{method} {path}: {resp.status} {data[:300].decode(errors='replace')}")
            return resp.status, resp_headers, data
        raise RegistryError(f"{method} {path}: unauthorized")

    # --------------------------------------------------------------- read / delete

    def catalog(self, limit: int = 1000) -> List[str]:
        repos: List[str] = []
        path = f"/v2/_catalog?n={limit}"
        while path and len(repos) < limit:
            _, headers, data = self.request("GET", path, scope="registry:catalog:*")
            repos += json.loads(data).get("repositories") or []
            link = re.search(r"<([^>]+)>", headers.get("link", ""))
            path = link.group(1) if link else ""
        return repos

    def tags(self, repo: str) -> List[str]:
        _, _, data = self.request("GET", f"/v2/{repo}/tags/list", scope=f"repository:{repo}:pull",
                                  ok=(200, 404))
        try:
            return json.loads(data).get("tags") or []
        except ValueError:
            return []

    def digest(self, repo: str, ref: str) -> str:
        _, headers, _ = self.request("HEAD", f"/v2/{repo}/manifests/{ref}", scope=f"repository:{repo}:pull",
                                     headers={"Accept": ACCEPT})
        digest = headers.get("docker-content-digest")
        if not digest:
            raise RegistryError(f"No digest for {repo}:{ref}")
        return digest

    def delete_tag(self, repo: str, tag: str) -> None:
        """Delete the manifest the tag points to (and so the tag)"""
        digest = self.digest(repo, tag)
        self.request("DELETE", f"/v2/{repo}/manifests/{digest}", scope=f"repository:{repo}:pull,push,delete",
                     ok=(200, 202, 204))

    # --------------------------------------------------------------- push

    def _scope(self, repo: str) -> str:
        return f"repository:{repo}:pull,push"

    def blob_exists(self, repo: str, digest: str) -> bool:
        status, _, _ = self.request("HEAD", f"/v2/{repo}/blobs/{digest}", scope=self._scope(repo), ok=(200, 404))
        return status == 200

    def push_blob(self, repo: str, digest: str, size: int, chunks: Callable[[], Iterator[bytes]]) -> None:
        if self.blob_exists(repo, digest):
            return
        _, headers, _ = self.request("POST", f"/v2/{repo}/blobs/uploads/", scope=self._scope(repo), ok=(202,),
                                     headers={"Content-Length": "0"}, body=b"")
        location = urllib.parse.urlsplit(headers["location"])
        query = urllib.parse.parse_qsl(location.query) + [("digest", digest)]
        path = f"{location.path}?{urllib.parse.urlencode(query)}"
        self.request("PUT", path, scope=self._scope(repo), headers={"Content-Type": "application/octet-stream"},
                     stream=chunks(), length=size, ok=(201, 204))

    def put_manifest(self, repo: str, ref: str, data: bytes, media_type: str) -> None:
        self.request("PUT", f"/v2/{repo}/manifests/{ref}", scope=self._scope(repo),
                     headers={"Content-Type": media_type}, body=data, ok=(200, 201, 202))


# ------------------------------------------------------------------- archives

def split_ref(ref: str) -> Tuple[str, str]:
    """'localhost/app:1.0' / 'quay.io/x/app:1' / 'app' -> ('app' / 'x/app', tag); registry host dropped"""
    ref = ref.strip()
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts).lower(), tag


class ArchivePusher:
    """Push every image of a docker-archive / OCI archive tar file"""

    def __init__(self, client: RegistryClient, path: str, progress: Callable[[int, int, str], None],
                 cancelled: Callable[[], bool]):
        self.client, self.path, self.progress, self.cancelled = client, path, progress, cancelled
        self.tar = tarfile.open(path, "r:*")
        self.members = {m.name.lstrip("./"): m for m in self.tar.getmembers() if m.isfile()}
        self.total = sum(m.size for m in self.members.values())
        self.done = 0

    def close(self) -> None:
        self.tar.close()

    def _read(self, name: str) -> bytes:
        f = self.tar.extractfile(self.members[name])
        return f.read() if f else b""

    def _chunks(self, name: str, count: bool = True) -> Iterator[bytes]:
        f = self.tar.extractfile(self.members[name])
        while f is not None:
            if self.cancelled():
                raise RuntimeError("Cancelled")
            chunk = f.read(CHUNK)
            if not chunk:
                break
            if count:
                self.done += len(chunk)
                self.progress(self.done, self.total, name)
            yield chunk

    def _digest(self, name: str) -> Tuple[str, int, bool]:
        """(sha256 digest, size, gzip?) of a member"""
        h = hashlib.sha256()
        first = b""
        for chunk in self._chunks(name, count=False):
            if not first:
                first = chunk[:2]
            h.update(chunk)
        return f"sha256:{h.hexdigest()}", self.members[name].size, first == b"\x1f\x8b"

    def _blob(self, repo: str, name: str, digest: str, size: int) -> None:
        self.client.push_blob(repo, digest, size, lambda: self._chunks(name))

    def push(self, repo: Optional[str], tag: Optional[str]) -> List[str]:
        """Returns the pushed references (repo:tag)"""
        if "index.json" in self.members and "oci-layout" in self.members:
            return self._push_oci(repo, tag)
        if "manifest.json" in self.members:
            return self._push_docker(repo, tag)
        raise ValueError("Not an image archive (podman save / docker save / OCI archive): no manifest.json nor index.json")

    def _target(self, repo: Optional[str], tag: Optional[str], name: Optional[str]) -> Tuple[str, str]:
        d_repo, d_tag = split_ref(name) if name else (None, None)
        repo, tag = repo or d_repo, tag or d_tag or "latest"
        if not repo:
            raise ValueError("The archive has no image name: give the destination repository")
        if not REPO_RE.match(repo) or not TAG_RE.match(tag):
            raise ValueError(f"Invalid destination {repo}:{tag}")
        return repo, tag

    def _push_docker(self, repo: Optional[str], tag: Optional[str]) -> List[str]:
        entries = json.loads(self._read("manifest.json"))
        refs = []
        for entry in entries[:1] if repo else entries:
            names = entry.get("RepoTags") or []
            r, t = self._target(repo, tag, names[0] if names else None)
            config = entry["Config"]
            cfg = self._read(config)
            cfg_digest = f"sha256:{hashlib.sha256(cfg).hexdigest()}"
            self.client.push_blob(r, cfg_digest, len(cfg), lambda: iter([cfg]))
            layers = []
            for layer in entry["Layers"]:
                digest, size, gz = self._digest(layer)
                self._blob(r, layer, digest, size)
                layers.append({"mediaType": "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if gz else ""),
                               "digest": digest, "size": size})
            manifest = json.dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                                   "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                              "digest": cfg_digest, "size": len(cfg)},
                                   "layers": layers}, separators=(",", ":")).encode()
            self.client.put_manifest(r, t, manifest, OCI_MANIFEST)
            refs.append(f"{r}:{t}")
        return refs

    def _blob_name(self, digest: str) -> str:
        alg, _, hexd = digest.partition(":")
        return f"blobs/{alg}/{hexd}"

    def _push_descriptor(self, repo: str, desc: Dict[str, Any]) -> bytes:
        """Blobs of a manifest / index (recursively), then the manifest by digest; returns its bytes"""
        data = self._read(self._blob_name(desc["digest"]))
        media = desc.get("mediaType") or json.loads(data).get("mediaType") or OCI_MANIFEST
        doc = json.loads(data)
        if media in INDEX_TYPES:
            for child in doc.get("manifests") or []:
                if self._blob_name(child["digest"]) in self.members:  # single-platform saves list others
                    self._push_descriptor(repo, child)
        else:
            for blob in [doc["config"]] + (doc.get("layers") or []):
                self._blob(repo, self._blob_name(blob["digest"]), blob["digest"], blob["size"])
        self.client.put_manifest(repo, desc["digest"], data, media)
        return data

    def _push_oci(self, repo: Optional[str], tag: Optional[str]) -> List[str]:
        index = json.loads(self._read("index.json"))
        refs = []
        descs = index.get("manifests") or []
        for desc in descs[:1] if repo else descs:
            ann = desc.get("annotations") or {}
            name = ann.get("io.containerd.image.name") or ann.get("org.opencontainers.image.ref.name")
            if name and "/" not in name and ":" not in name and not repo:
                name = None if not tag else name  # a bare tag ("latest") names no repository
            r, t = self._target(repo, tag, name)
            data = self._push_descriptor(r, desc)
            media = desc.get("mediaType") or json.loads(data).get("mediaType") or OCI_MANIFEST
            self.client.put_manifest(r, t, data, media)
            refs.append(f"{r}:{t}")
        return refs
