import {
  VM, VMCreate, VMUpdate, VMDetail, VMPowerAction, ConsoleInfo,
  StoragePool, StoragePoolCreate, Volume, VolumeCreate, ISOImage,
  CloudImage, CloudImageDistribution, CloudImageDownload,
  Network, NetworkCreate, NetworkDetail, NetworkConfig, NetworkUpdate, DHCPHost, Task, HostInfo, HostResources,
  Cluster, ClusterCreate, ClusterCommandOutput,
} from '../types';

export const API_BASE = process.env.REACT_APP_API_URL || '';

/** WebSocket URL of a VM's VNC console (proxied by the backend) */
export function vncUrl(vmId: number): string {
  const base = API_BASE || window.location.origin;
  return `${base.replace(/^http/, 'ws')}/api/v1/vms/${vmId}/vnc`;
}

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
    return detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
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
    throw new ApiError(response.status, errorMessage(body, response.status));
  }
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
  deleteHost: (id: number, mac: string) => del(`/networks/${id}/hosts/${encodeURIComponent(mac)}`),
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
};

export const hostApi = {
  info: () => request<HostInfo>('/hosts/info'),
  resources: () => request<HostResources>('/hosts/resources'),
};
