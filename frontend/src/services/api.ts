import {
  VM, VMCreate, VMUpdate, VMDetail, VMPowerAction, ConsoleInfo, DeviceChange, VMBootUpdate, VMDiskCreate,
  StoragePool, StoragePoolCreate, Volume, VolumeCreate, ISOImage,
  CloudImage, CloudImageDistribution, CloudImageDownload,
  Network, NetworkCreate, NetworkDetail, NetworkConfig, NetworkUpdate, DHCPHost, Task, HostInfo, HostResources, HostDnsStatus,
  HostDnsRecord, HostDnsRecordCreate,
  LeaseRelease, LibvirtStatus, LibvirtAction, LibvirtStopMode,
  Cluster, ClusterCreate, ClusterCommandOutput,
  PullSecretIn, PullSecretStatus, OpenShiftChannel, OpenShiftVersion, CatalogOperator, ClusterCredentials,
  InstallStatus, InstalledOperator, ClusterUpgrades, NodeResources, PackageManifest, AddonRequest, MetalLBScenario, MetalLBOptions, AcmStatus, AcmSpokeNet, AcmSpokeNode, AcmGitops, AcmGitCredentials, BmcStatus,
  Group, GroupDetail, GroupSpec, MemberSpec, DNSRecord, RouterConfig, GroupDHCPHost, GroupLease,
  WireGuardStatus, WireGuardPeerCreated, BGPStatus, BGPSettings, GroupTopology, RegistryStatus, MirrorRequest,
  ImageCopyRequest, RegistryImages, RegistryCredentials,
  VMNicCreate, VMNicUpdate, SriovStatus, SriovPF, SriovPFUpdate,
  AuthStatus, ApiToken, ApiTokenCreated,
  UpdateStatus,
  TemplateList, TemplateDetail, TemplateRender, TemplateCreated, TemplateSave,
  BareMetalHost, BareMetalHostCreate, BareMetalHostUpdate, BareMetalHostNicInfo,
} from '../types';
import { Diagnostics, LogParams, LogResult, LogRules, PodList } from '../types/logs';

export const API_BASE = process.env.REACT_APP_API_URL || '';

/** WebSocket URL of a VM's VNC console (proxied by the backend) */
export function vncUrl(vmId: number): string {
  const base = API_BASE || window.location.origin;
  return `${base.replace(/^http/, 'ws')}/api/v1/vms/${vmId}/vnc`;
}

/** window event fired when an API call fails with 401 (no / expired session): the app shows the login page */
export const UNAUTHORIZED_EVENT = 'vmm-unauthorized';

/** Sent on every request: the backend refuses cookie-authenticated writes without it (CSRF) */
export const CSRF_HEADERS = { 'X-VMM-Request': '1' };

/** window event fired when an API call fails with 503 "libvirt is stopped" */
export const LIBVIRT_STOPPED_EVENT = 'vmm-libvirt-stopped';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

function errorMessage(body: any, status: number): string {
  const detail = body?.detail;
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors: [{loc: [...], msg: '...'}]
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const field = (d.loc || []).slice(1).join('.');
      const msg = String(d.msg).replace(/^Value error, /, '');
      return field ? `${field}: ${msg}` : msg;
    }).join('; ');
  }
  return `Request failed (HTTP ${status})`;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const isForm = options.body instanceof FormData;
  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: isForm ? { ...CSRF_HEADERS, ...options.headers }
      : { 'Content-Type': 'application/json', ...CSRF_HEADERS, ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (response.status === 401 && body?.auth === 'required') window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    // The daemon is down: tell the libvirt state provider (header pill, "libvirt is stopped" pages)
    if (response.status === 503 && body?.libvirt === 'stopped') window.dispatchEvent(new Event(LIBVIRT_STOPPED_EVENT));
    throw new ApiError(response.status, errorMessage(body, response.status));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

const post = <T>(path: string, data?: unknown) =>
  request<T>(path, { method: 'POST', body: data === undefined ? undefined : JSON.stringify(data) });
const del = <T = void>(path: string) => request<T>(path, { method: 'DELETE' });

export const vmApi = {
  list: () => request<VM[]>('/vms'),
  get: (id: number) => request<VMDetail>(`/vms/${id}`),
  create: (data: VMCreate) => post<VM>('/vms', data),
  update: (id: number, data: VMUpdate) =>
    request<VM>(`/vms/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number, deleteDisks = false) => del(`/vms/${id}?delete_disks=${deleteDisks}`),
  power: (id: number, action: VMPowerAction) => post<VM>(`/vms/${id}/${action}`),
  console: (id: number) => request<ConsoleInfo>(`/vms/${id}/console`),
  setCdrom: (id: number, isoPath: string | null) =>
    request<DeviceChange>(`/vms/${id}/cdrom`, { method: 'PUT', body: JSON.stringify({ iso_path: isoPath }) }),
  setBoot: (id: number, data: VMBootUpdate) =>
    request<DeviceChange>(`/vms/${id}/boot`, { method: 'PUT', body: JSON.stringify(data) }),
  addDisk: (id: number, data: VMDiskCreate) => post<DeviceChange>(`/vms/${id}/disks`, data),
  resizeDisk: (id: number, target: string, sizeGb: number) =>
    request<DeviceChange>(`/vms/${id}/disks/${target}`, { method: 'PUT', body: JSON.stringify({ size_gb: sizeGb }) }),
  detachDisk: (id: number, target: string, deleteVolume: boolean) =>
    del<DeviceChange>(`/vms/${id}/disks/${target}?delete_volume=${deleteVolume}`),
  addNic: (id: number, data: VMNicCreate) => post<DeviceChange>(`/vms/${id}/nics`, data),
  updateNic: (id: number, mac: string, data: VMNicUpdate) =>
    request<DeviceChange>(`/vms/${id}/nics/${encodeURIComponent(mac)}`, { method: 'PUT', body: JSON.stringify(data) }),
  removeNic: (id: number, mac: string) => del<DeviceChange>(`/vms/${id}/nics/${encodeURIComponent(mac)}`),
  setIommu: (id: number, enabled: boolean) =>
    request<DeviceChange>(`/vms/${id}/iommu`, { method: 'PUT', body: JSON.stringify({ enabled }) }),
};

export const storageApi = {
  listPools: () => request<StoragePool[]>('/storage/pools'),
  createPool: (data: StoragePoolCreate) => post<StoragePool>('/storage/pools', data),
  startPool: (id: number) => post<StoragePool>(`/storage/pools/${id}/start`),
  stopPool: (id: number) => post<StoragePool>(`/storage/pools/${id}/stop`),
  deletePool: (id: number) => del(`/storage/pools/${id}`),

  listVolumes: () => request<Volume[]>('/storage/volumes'),
  createVolume: (poolId: number, data: VolumeCreate) => post<Volume>(`/storage/pools/${poolId}/volumes`, data),
  deleteVolume: (id: number) => del(`/storage/volumes/${id}`),

  listIsos: () => request<ISOImage[]>('/storage/isos'),
  uploadIso: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<{ path: string }>('/storage/isos/upload', { method: 'POST', body: form });
  },
  downloadIso: (url: string, name?: string) => post<Task>('/storage/isos/download', { url, name }),

  listCloudImages: () => request<CloudImage[]>('/storage/cloud-images'),
  cloudImageDistributions: () => request<CloudImageDistribution[]>('/storage/cloud-images/distributions'),
  downloadCloudImage: (data: CloudImageDownload) => post<Task>('/storage/cloud-images', data),
  deleteCloudImage: (id: number) => del(`/storage/cloud-images/${id}`),
};

export const networkApi = {
  list: () => request<Network[]>('/networks'),
  get: (id: number) => request<NetworkDetail>(`/networks/${id}`),
  create: (data: NetworkCreate) => post<Network>('/networks', data),
  start: (id: number) => post<Network>(`/networks/${id}/start`),
  stop: (id: number) => post<Network>(`/networks/${id}/stop`),
  setAutostart: (id: number, autostart: boolean) =>
    request<Network>(`/networks/${id}/autostart`, { method: 'PUT', body: JSON.stringify({ autostart }) }),
  delete: (id: number) => del(`/networks/${id}`),
  config: (id: number) => request<NetworkConfig>(`/networks/${id}/config`),
  update: (id: number, data: NetworkUpdate) =>
    request<Network>(`/networks/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  replaceXml: (id: number, xml: string, restart: boolean) =>
    request<Network>(`/networks/${id}/xml`, { method: 'PUT', body: JSON.stringify({ xml, restart }) }),
  addHost: (id: number, host: DHCPHost) => post(`/networks/${id}/hosts`, host),
  updateHost: (id: number, mac: string, host: DHCPHost) =>
    request(`/networks/${id}/hosts/${encodeURIComponent(mac)}`, { method: 'PUT', body: JSON.stringify(host) }),
  deleteHost: (id: number, mac: string, releaseLease = false) =>
    del(`/networks/${id}/hosts/${encodeURIComponent(mac)}?release_lease=${releaseLease}`),
  releaseLease: (id: number, mac: string, force = false) =>
    del<LeaseRelease>(`/networks/${id}/leases/${encodeURIComponent(mac)}?force=${force}`),
};

export const groupApi = {
  list: () => request<Group[]>('/groups'),
  bmc: (id: number) => request<BmcStatus>(`/groups/${id}/bmc`),
  bmcCredentials: (id: number) => request<{ username: string; password: string }>(`/groups/${id}/bmc/credentials`),
  addSpokes: (id: number, data: { count: number; memory: number; vcpu: number; disk_size: number; prefix?: string }) =>
    post<GroupDetail>(`/groups/${id}/bmc/spokes`, data),
  get: (id: number) => request<GroupDetail>(`/groups/${id}`),
  create: (spec: GroupSpec) => post<{ group: Group; task_id: number }>('/groups', spec),
  update: (id: number, spec: GroupSpec) =>
    request<GroupDetail>(`/groups/${id}`, { method: 'PUT', body: JSON.stringify(spec) }),
  delete: (id: number, deleteDisks = true) => del(`/groups/${id}?delete_disks=${deleteDisks}`),
  start: (id: number) => post<Task>(`/groups/${id}/start`),
  stop: (id: number, force = false) => post<Task>(`/groups/${id}/stop?force=${force}`),
  addMember: (id: number, member: MemberSpec) => post<GroupDetail>(`/groups/${id}/members`, member),
  removeMember: (id: number, name: string, deleteDisks = true) =>
    del<GroupDetail>(`/groups/${id}/members/${encodeURIComponent(name)}?delete_disks=${deleteDisks}`),
  setRecord: (id: number, record: DNSRecord) => post<GroupDetail>(`/groups/${id}/dns-records`, record),
  removeRecord: (id: number, name: string) => del<GroupDetail>(`/groups/${id}/dns-records/${encodeURIComponent(name)}`),
  routerConfig: (id: number) => request<RouterConfig>(`/groups/${id}/router/config`),
  applyRouterConfig: (id: number) => post<GroupDetail>(`/groups/${id}/router/apply`),
  exportSpec: (id: number) => request<{ yaml: string; spec: GroupSpec }>(`/groups/${id}/export`),
  addDhcpHost: (id: number, host: GroupDHCPHost) => post<GroupDetail>(`/groups/${id}/dhcp-hosts`, host),
  updateDhcpHost: (id: number, mac: string, host: GroupDHCPHost) =>
    request<GroupDetail>(`/groups/${id}/dhcp-hosts/${encodeURIComponent(mac)}`, { method: 'PUT', body: JSON.stringify(host) }),
  deleteDhcpHost: (id: number, mac: string, releaseLease = false) =>
    del<GroupDetail>(`/groups/${id}/dhcp-hosts/${encodeURIComponent(mac)}?release_lease=${releaseLease}`),
  leases: (id: number) => request<GroupLease[]>(`/groups/${id}/leases`),
  wireguard: (id: number) => request<WireGuardStatus>(`/groups/${id}/wireguard`),
  setWireguard: (id: number, body: { enabled: boolean; listen_port?: number | null; host_port?: number | null }) =>
    request<WireGuardStatus>(`/groups/${id}/wireguard`, { method: 'PUT', body: JSON.stringify(body) }),
  addWgPeer: (id: number, body: { name: string; public_key?: string | null; endpoint_host?: string | null }) =>
    post<WireGuardPeerCreated>(`/groups/${id}/wireguard/peers`, body),
  wgPeerConfig: (id: number, name: string, endpointHost?: string) =>
    request<WireGuardPeerCreated>(`/groups/${id}/wireguard/peers/${encodeURIComponent(name)}/config`
      + (endpointHost ? `?endpoint_host=${encodeURIComponent(endpointHost)}` : '')),
  removeWgPeer: (id: number, name: string) => del<WireGuardStatus>(`/groups/${id}/wireguard/peers/${encodeURIComponent(name)}`),
  releaseLease: (id: number, mac: string, force = false) =>
    del<LeaseRelease>(`/groups/${id}/leases/${encodeURIComponent(mac)}?force=${force}`),
  bgp: (id: number) => request<BGPStatus>(`/groups/${id}/bgp`),
  setBgp: (id: number, body: BGPSettings) =>
    request<BGPStatus>(`/groups/${id}/bgp`, { method: 'PUT', body: JSON.stringify(body) }),
  topology: (id: number) => request<GroupTopology>(`/groups/${id}/topology`),
  registry: (id: number) => request<RegistryStatus>(`/groups/${id}/registry`),
  registrySetup: (id: number) => post<{ task_id: number }>(`/groups/${id}/registry/setup`),
  mirror: (id: number, body: MirrorRequest) => post<{ task_id: number }>(`/groups/${id}/registry/mirror`, body),
  registryImages: (id: number) => request<RegistryImages>(`/groups/${id}/registry/images`),
  copyImage: (id: number, body: ImageCopyRequest) => post<{ task_id: number }>(`/groups/${id}/registry/images`, body),
  deleteImage: (id: number, ref: string) => del(`/groups/${id}/registry/images?ref=${encodeURIComponent(ref)}`),
  registryCredentials: (id: number) => request<RegistryCredentials>(`/groups/${id}/registry/credentials`),
  /** Stream an image archive to the registry (XHR for upload progress); resolves with the push task id */
  uploadImage: (id: number, file: File, dest: { repo?: string; tag?: string }, onProgress: (fraction: number) => void) =>
    new Promise<{ task_id: number }>((resolve, reject) => {
      const q = new URLSearchParams({ filename: file.name });
      if (dest.repo) q.set('repo', dest.repo);
      if (dest.tag) q.set('tag', dest.tag);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API_BASE}/api/v1/groups/${id}/registry/upload?${q}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-VMM-Request', '1');
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let body: { task_id?: number; detail?: unknown } = {};
        try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && body.task_id) resolve({ task_id: body.task_id });
        else reject(new Error(typeof body.detail === 'string' ? body.detail : `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error('Upload failed (network error)'));
      xhr.send(file);
    }),
};

export const taskApi = {
  list: () => request<Task[]>('/tasks'),
  cancel: (id: number) => post(`/tasks/${id}/cancel`),
  delete: (id: number) => del(`/tasks/${id}`),
};

export const clusterApi = {
  list: () => request<Cluster[]>('/clusters'),
  get: (id: number) => request<Cluster>(`/clusters/${id}`),
  create: (data: ClusterCreate) => post<Cluster>('/clusters', data),
  delete: (id: number) => del(`/clusters/${id}`),
  start: (id: number) => post<Task>(`/clusters/${id}/start`),
  stop: (id: number) => post<Task>(`/clusters/${id}/stop`),
  addWorkers: (id: number, count = 1, worker?: NodeResources) =>
    post<Task>(`/clusters/${id}/workers`, worker ? { count, worker } : { count }),
  /** OpenShift: ClusterVersion view (channel, updates, conditional risks, progress) */
  upgrades: (id: number) => request<ClusterUpgrades>(`/clusters/${id}/upgrades`),
  setUpgrade: (id: number, data: { channel?: string; version?: string; accept_risks?: boolean }) =>
    post<ClusterUpgrades>(`/clusters/${id}/upgrades`, data),
  removeNode: (id: number, node: string) => del<Task>(`/clusters/${id}/nodes/${encodeURIComponent(node)}`),
  kubectl: (id: number, view: 'nodes' | 'pods') => request<ClusterCommandOutput>(`/clusters/${id}/kubectl/${view}`),
  /** Plain link: the backend sends it as an attachment named <cluster>-kubeconfig.yaml */
  /** Disconnected kubeadm: copy more images into the group's mirror registry (task) */
  mirrorImages: (id: number, images: string[]) => post<Task>(`/clusters/${id}/mirror`, { images }),
  kubeconfigUrl: (id: number) => `${API_BASE}/api/v1/clusters/${id}/kubeconfig`,
  // OpenShift
  installStatus: (id: number) => request<InstallStatus>(`/clusters/${id}/openshift/install-status`),
  credentials: (id: number) => request<ClusterCredentials>(`/clusters/${id}/openshift/credentials`),
  /** Plain link: private key of the nodes' core user (text/plain attachment) */
  sshKeyUrl: (id: number) => `${API_BASE}/api/v1/clusters/${id}/openshift/ssh-key`,
  operators: (id: number) => request<InstalledOperator[]>(`/clusters/${id}/openshift/operators`),
  packageManifests: (id: number) => request<PackageManifest[]>(`/clusters/${id}/openshift/packagemanifests`),
  addAddon: (id: number, data: AddonRequest) => post<Task>(`/clusters/${id}/openshift/addons`, data),
  metallb: (id: number) => request<MetalLBScenario>(`/clusters/${id}/metallb`),
  setMetallb: (id: number, data: MetalLBOptions) =>
    request<Task>(`/clusters/${id}/metallb`, { method: 'PUT', body: JSON.stringify(data) }),
  // ACM hub (OpenShift add-on acm)
  acm: (id: number) => request<AcmStatus>(`/clusters/${id}/acm`),
  acmImport: (id: number, clusterId: number, method: 'auto' | 'secret' | 'manifests' = 'auto') =>
    post<Task>(`/clusters/${id}/acm/managed`, { cluster_id: clusterId, method }),
  acmDetach: (id: number, name: string) => del<Task>(`/clusters/${id}/acm/managed/${encodeURIComponent(name)}`),
  /** The hub provisions a spoke on an emulated-BMC member: returns the new cluster (its own page, kubeconfig, delete) */
  acmSpoke: (id: number, name: string, machine: string, gitops = false, version?: string | null) =>
    post<Cluster>(`/clusters/${id}/acm/spokes`, { name, machine, gitops, version: version || null }),
  /** The hub provisions a single-node spoke on a REAL BareMetalHost with its static network. gitops = Argo CD. */
  acmSpokeHost: (id: number, name: string, hostId: number, net: AcmSpokeNet, gitops = false, version?: string | null) =>
    post<Cluster>(`/clusters/${id}/acm/spokes`, { name, host_id: hostId, net, gitops, version: version || null }),
  /** The hub provisions a MULTI-node spoke across several REAL BareMetalHosts (compact / two-node-with-arbiter).
   *  `hosts` = per-node {host_id, ip, role, root_device?}; `net` carries the shared network + the api/ingress VIPs. */
  acmSpokeMulti: (id: number, name: string, topology: string, hosts: AcmSpokeNode[], net: AcmSpokeNet,
    gitops = false, version?: string | null) =>
    post<Cluster>(`/clusters/${id}/acm/spokes`, { name, topology, hosts, net, gitops, version: version || null }),
  /** The GitOps artifact for a real bare-metal spoke: its SiteConfig ClusterInstance manifest (for git / Argo CD) */
  acmSiteconfig: (spokeId: number) => request<{ manifest: string }>(`/clusters/${spokeId}/acm/siteconfig`),
  /** Reference view for the spoke-create form: the SiteConfig ClusterInstance that WOULD be generated (nothing created) */
  acmSiteconfigPreview: (hubId: number, p: { host_id: number; ip?: string; prefix?: number; gateway?: string; dns?: string; name?: string }) => {
    const q = new URLSearchParams();
    q.set('host_id', String(p.host_id));
    if (p.ip) q.set('ip', p.ip);
    if (p.prefix != null) q.set('prefix', String(p.prefix));
    if (p.gateway) q.set('gateway', p.gateway);
    if (p.dns) q.set('dns', p.dns);
    if (p.name) q.set('name', p.name);
    return request<{ manifest: string }>(`/clusters/${hubId}/acm/siteconfig/preview?${q.toString()}`);
  },
  /** The hub's GitOps repo of spoke manifests (source of truth for deploying clusters from git) */
  acmGitops: (hubId: number) => request<AcmGitops>(`/clusters/${hubId}/acm/gitops`),
  /** Close the loop: push the manifests to the in-lab git server and apply the Argo CD Application */
  acmGitopsDeploy: (hubId: number) => post<AcmGitops>(`/clusters/${hubId}/acm/gitops/deploy`),
  /** The in-lab Gitea admin login (secret read): username + password + web URLs, to open the git server */
  acmGitCredentials: (hubId: number) => request<AcmGitCredentials>(`/clusters/${hubId}/acm/gitops/git-credentials`),
  /** Rotate the in-lab Gitea admin password; returns the new credentials */
  acmGitResetCredentials: (hubId: number) => post<AcmGitCredentials>(`/clusters/${hubId}/acm/gitops/git-credentials`, {}),
};

export const openshiftApi = {
  pullSecret: () => request<PullSecretStatus>('/openshift/pull-secret'),
  setPullSecret: (data: PullSecretIn) =>
    request<PullSecretStatus>('/openshift/pull-secret', { method: 'PUT', body: JSON.stringify(data) }),
  deletePullSecret: () => del<PullSecretStatus>('/openshift/pull-secret'),
  channels: () => request<OpenShiftChannel[]>('/openshift/channels'),
  versions: (channel: string) => request<OpenShiftVersion[]>(`/openshift/versions?channel=${encodeURIComponent(channel)}`),
  catalog: () => request<CatalogOperator[]>('/openshift/catalog'),
};

export const hostApi = {
  info: () => request<HostInfo>('/hosts/info'),
  resources: () => request<HostResources>('/hosts/resources'),
  dns: () => request<HostDnsStatus>('/hosts/dns'),
  dnsRecords: () => request<HostDnsRecord[]>('/hosts/dns/records'),
  addDnsRecord: (record: HostDnsRecordCreate) => post<HostDnsRecord>('/hosts/dns/records', record),
  deleteDnsRecord: (name: string) => del(`/hosts/dns/records/${encodeURIComponent(name)}`),
  sriov: () => request<SriovStatus>('/hosts/sriov'),
  setNumVfs: (pf: string, numVfs: number) =>
    request<SriovPF>(`/hosts/sriov/${encodeURIComponent(pf)}`, { method: 'PUT', body: JSON.stringify({ num_vfs: numVfs }) }),
  updatePf: (pf: string, data: SriovPFUpdate) =>
    request<SriovPF>(`/hosts/sriov/${encodeURIComponent(pf)}`, { method: 'PUT', body: JSON.stringify(data) }),
  update: (refresh = false, channel?: string) =>
    request<UpdateStatus>(`/hosts/update?refresh=${refresh}${channel ? `&channel=${channel}` : ''}`),
  startUpdate: (version: string, channel: string) =>
    post<{ started: boolean; message: string }>('/hosts/update', { version, channel }),
};

export const authApi = {
  status: () => request<AuthStatus>('/auth/status'),
  login: (username: string, password: string) => post<AuthStatus>('/auth/login', { username, password }),
  logout: () => post<void>('/auth/logout'),
  tokens: (all = false) => request<ApiToken[]>(`/auth/tokens${all ? '?all=true' : ''}`),
  createToken: (name: string, expiresDays?: number) =>
    post<ApiTokenCreated>('/auth/tokens', { name, expires_days: expiresDays || null }),
  revokeToken: (id: number) => del(`/auth/tokens/${id}`),
  /** My default token (name "cli", made on first use) */
  cliToken: () => request<ApiTokenCreated>('/auth/tokens/cli'),
  /** Rotate my default token: the previous value stops working */
  rotateCliToken: () => post<ApiTokenCreated>('/auth/tokens/cli', {}),
};

export const libvirtApi = {
  status: () => request<LibvirtStatus>('/hosts/libvirt'),
  start: () => post<LibvirtAction>('/hosts/libvirt/start'),
  stop: (mode: LibvirtStopMode, timeout?: number) => post<LibvirtAction>('/hosts/libvirt/stop', { mode, timeout }),
};

export const templateApi = {
  list: () => request<TemplateList>('/templates'),
  get: (id: string) => request<TemplateDetail>(`/templates/${encodeURIComponent(id)}`),
  render: (id: string, params: Record<string, unknown>, yaml?: string | null) =>
    post<TemplateRender>(`/templates/${encodeURIComponent(id)}/render`, { params, yaml: yaml || null }),
  create: (id: string, params: Record<string, unknown>, yaml?: string | null) =>
    post<TemplateCreated>(`/templates/${encodeURIComponent(id)}/create`, { params, yaml: yaml || null }),
  saveGroup: (body: TemplateSave) => post<TemplateDetail>('/templates', body),
  delete: (id: string) => del(`/templates/${encodeURIComponent(id)}`),
};

// Log viewer + diagnostics (docs/logs.md)
function logQuery(params: LogParams): string {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === null || v === undefined || v === '' || v === false) return;
    if (Array.isArray(v)) v.forEach((x) => x && q.append(k, x));
    else q.append(k, String(v));
  });
  const s = q.toString();
  return s ? `?${s}` : '';
}

export const logsApi = {
  rules: () => request<LogRules>('/logs/rules'),
  /** path = /vms/{id}/logs or /clusters/{id}/logs */
  get: (path: string, params: LogParams) => request<LogResult>(`${path}${logQuery(params)}`),
  /** the selected lines as plain text (format=text), for the Download button */
  text: async (path: string, params: LogParams): Promise<string> => {
    const r = await fetch(`${API_BASE}/api/v1${path}${logQuery({ ...params, format: 'text' })}`,
      { credentials: 'same-origin', headers: { ...CSRF_HEADERS } });
    if (!r.ok) throw new ApiError(r.status, errorMessage(await r.json().catch(() => null), r.status));
    return r.text();
  },
  pods: (clusterId: number, params: LogParams) => request<PodList>(`/clusters/${clusterId}/pods${logQuery(params)}`),
  diagnostics: (vmId: number) => request<Diagnostics>(`/vms/${vmId}/diagnostics`),
};

export const bareMetalApi = {
  list: () => request<BareMetalHost[]>('/bare-metal-hosts'),
  get: (id: number) => request<BareMetalHost>(`/bare-metal-hosts/${id}`),
  create: (data: BareMetalHostCreate) => post<BareMetalHost>('/bare-metal-hosts', data),
  update: (id: number, data: BareMetalHostUpdate) =>
    request<BareMetalHost>(`/bare-metal-hosts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: number) => del(`/bare-metal-hosts/${id}`),
  refresh: (id: number) => post<BareMetalHost>(`/bare-metal-hosts/${id}/refresh`),
  nics: (id: number) => request<BareMetalHostNicInfo[]>(`/bare-metal-hosts/${id}/nics`),
  power: (id: number, action: 'on' | 'off' | 'cycle', graceful = false) =>
    post<{ power_state: string }>(`/bare-metal-hosts/${id}/power`, { action, graceful }),
  reset: (id: number, wipe = true) => post<BareMetalHost>(`/bare-metal-hosts/${id}/reset?wipe=${wipe}`),
  consoleScreenshotUrl: (id: number) => `${API_BASE}/api/v1/bare-metal-hosts/${id}/console-screenshot`,
};
