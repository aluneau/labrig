import {
  VM, VMCreate, VMUpdate, VMDetail, VMPowerAction, ConsoleInfo, DeviceChange, VMBootUpdate, VMDiskCreate,
  StoragePool, StoragePoolCreate, Volume, VolumeCreate, ISOImage,
  CloudImage, CloudImageDistribution, CloudImageDownload,
  Network, NetworkCreate, NetworkDetail, NetworkConfig, NetworkUpdate, DHCPHost, Task, HostInfo, HostResources,
  LeaseRelease, LibvirtStatus, LibvirtAction, LibvirtStopMode,
  Cluster, ClusterCreate, ClusterCommandOutput,
  PullSecretIn, PullSecretStatus, OpenShiftChannel, OpenShiftVersion, CatalogOperator, ClusterCredentials,
  InstallStatus, InstalledOperator, PackageManifest, AddonRequest, MetalLBScenario,
  Group, GroupDetail, GroupSpec, MemberSpec, DNSRecord, RouterConfig, GroupDHCPHost, GroupLease,
  WireGuardStatus, WireGuardPeerCreated, BGPStatus, BGPSettings, GroupTopology, RegistryStatus, MirrorRequest,
  ImageCopyRequest, RegistryImages, RegistryCredentials,
  VMNicCreate, VMNicUpdate, SriovStatus, SriovPF,
  TemplateList, TemplateDetail, TemplateRender, TemplateCreated, TemplateSave,
} from '../types';

export const API_BASE = process.env.REACT_APP_API_URL || '';

/** WebSocket URL of a VM's VNC console (proxied by the backend) */
export function vncUrl(vmId: number): string {
  const base = API_BASE || window.location.origin;
  return `${base.replace(/^http/, 'ws')}/api/v1/vms/${vmId}/vnc`;
}

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
    headers: isForm ? options.headers : { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
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
  addWorkers: (id: number, count = 1) => post<Task>(`/clusters/${id}/workers`, { count }),
  removeNode: (id: number, node: string) => del<Task>(`/clusters/${id}/nodes/${encodeURIComponent(node)}`),
  kubectl: (id: number, view: 'nodes' | 'pods') => request<ClusterCommandOutput>(`/clusters/${id}/kubectl/${view}`),
  /** Plain link: the backend sends it as an attachment named <cluster>-kubeconfig.yaml */
  kubeconfigUrl: (id: number) => `${API_BASE}/api/v1/clusters/${id}/kubeconfig`,
  // OpenShift
  installStatus: (id: number) => request<InstallStatus>(`/clusters/${id}/openshift/install-status`),
  credentials: (id: number) => request<ClusterCredentials>(`/clusters/${id}/openshift/credentials`),
  /** Plain link: private key of the nodes' core user (text/plain attachment) */
  sshKeyUrl: (id: number) => `${API_BASE}/api/v1/clusters/${id}/openshift/ssh-key`,
  operators: (id: number) => request<InstalledOperator[]>(`/clusters/${id}/openshift/operators`),
  packageManifests: (id: number) => request<PackageManifest[]>(`/clusters/${id}/openshift/packagemanifests`),
  addAddon: (id: number, data: AddonRequest) => post<Task>(`/clusters/${id}/openshift/addons`, data),
  metallb: (id: number) => request<MetalLBScenario>(`/clusters/${id}/openshift/metallb`),
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
  sriov: () => request<SriovStatus>('/hosts/sriov'),
  setNumVfs: (pf: string, numVfs: number) =>
    request<SriovPF>(`/hosts/sriov/${encodeURIComponent(pf)}`, { method: 'PUT', body: JSON.stringify({ num_vfs: numVfs }) }),
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
