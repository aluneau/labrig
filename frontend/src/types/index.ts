export interface VM {
  id: number;
  name: string;
  description?: string | null;
  memory: number; // MiB
  vcpu: number;
  os_type?: string | null;
  arch: string;
  uuid?: string | null;
  status: string;
  template_id?: number | null;
  created_at: string;
  updated_at: string;
}

export interface VMDisk {
  device?: string | null; // disk | cdrom
  path?: string | null;
  target?: string | null; // vda, sda, ...
  bus?: string | null;
  format?: string | null;
  capacity?: number | null; // bytes (disks only)
  boot: boolean; // the boot disk (can't be detached)
  // running VM only: 'attach' = appears at next start, 'detach' = goes away when the guest releases it / at shutdown
  pending?: 'attach' | 'detach' | null;
}

export interface VMCdrom {
  target?: string | null;
  path?: string | null; // inserted ISO, null = empty
  pending: boolean; // drive added while running: exists from the next start
}

export type BootDevice = 'hd' | 'cdrom' | 'network';

export interface VMBoot {
  order: BootDevice[]; // persistent order (next cold start)
  once?: BootDevice[] | null; // one-shot order for the next start through this app
}

export interface VMBootUpdate {
  order?: BootDevice[];
  once?: boolean;
}

export interface VMDiskCreate {
  size_gb: number;
  pool?: string;
  format?: 'qcow2' | 'raw';
  bus?: 'virtio' | 'sata';
}

export interface DeviceChange {
  message: string;
  pending: boolean; // applies at the next start / shutdown, not now
  target?: string | null;
  path?: string | null;
}

export interface VMInterface {
  name: string;
  mac?: string | null;
  addresses: string[];
}

export interface ConsoleInfo {
  type: string;
  host: string;
  port?: number | null;
}

export interface VMDetail extends VM {
  autostart: boolean;
  disks: VMDisk[];
  interfaces: VMInterface[];
  console?: ConsoleInfo | null;
  cdrom?: VMCdrom | null;
  boot?: VMBoot | null;
  xml_config?: string | null;
}

export interface VMCreate {
  name: string;
  description?: string;
  memory: number;
  vcpu: number;
  arch?: string;
  os_type?: string;
  disk_size?: number; // GiB
  iso_path?: string;
  cloud_image_id?: number;
  cloudinit_username?: string;
  cloudinit_password?: string;
  cloudinit_ssh_keys?: string[];
  cloudinit_userdata?: string;
  cloudinit_keyboard?: string;
  network_name?: string;
  autostart?: boolean;
  start?: boolean;
}

export interface VMUpdate {
  description?: string;
  os_type?: string;
  autostart?: boolean;
}

export type VMPowerAction = 'start' | 'stop' | 'force_stop' | 'reboot' | 'suspend' | 'resume';

export interface StoragePool {
  id: number;
  uuid?: string | null;
  name: string;
  type: string;
  path?: string | null;
  capacity: number;
  allocation: number;
  available: number;
  state: string;
  autostart: boolean;
  created_at: string;
  updated_at: string;
}

export interface StoragePoolCreate {
  name: string;
  path: string;
  autostart?: boolean;
}

export interface Volume {
  id: number;
  name: string;
  pool_id: number;
  pool_name?: string | null;
  vm_id?: number | null;
  type: string;
  format?: string | null;
  capacity: number;
  allocation: number;
  path?: string | null;
  created_at: string;
  updated_at: string;
}

export interface VolumeCreate {
  name: string;
  format: string;
  capacity: number; // bytes
}

export interface ISOImage {
  name: string;
  pool_name: string;
  path: string;
  size: number;
}

export interface CloudImage {
  id: number;
  name: string;
  distribution: string;
  version: string;
  arch: string;
  url: string;
  path?: string | null;
  size: number;
  status: 'downloading' | 'ready' | 'error' | string;
  download_progress: number;
  description?: string | null;
  created_at: string;
  updated_at: string;
}

export interface CloudImageDistribution {
  name: string;
  label: string;
  versions: { version: string; url: string }[];
}

export interface CloudImageDownload {
  distribution: string;
  version: string;
  url?: string;
}

export interface Network {
  id: number;
  name: string;
  uuid?: string | null;
  type: string;
  bridge_name?: string | null;
  forward_mode: string;
  forward_dev?: string | null;
  domain?: string | null;
  ip_address?: string | null;
  prefix?: number | null;
  netmask?: string | null;
  dhcp_enabled: boolean;
  dhcp_start?: string | null;
  dhcp_end?: string | null;
  active: boolean;
  autostart: boolean;
  persistent: boolean;
  created_at: string;
  updated_at: string;
}

export interface DHCPLease {
  ip_address: string;
  mac_address: string;
  hostname?: string | null;
  expiry?: string | null;
}

export interface NetworkDetail extends Network {
  leases: DHCPLease[];
}

export interface DHCPHost {
  mac: string;
  ip: string;
  name?: string | null;
}

export interface NetworkConfig {
  network: NetworkDetail;
  hosts: DHCPHost[];
  interfaces: { vm: string; mac: string }[];
  xml: string;
}

export interface NetworkUpdate {
  forward_mode: string;
  forward_dev?: string | null;
  domain?: string | null;
  ip_address?: string | null;
  prefix?: number | null;
  dhcp_enabled: boolean;
  dhcp_start?: string | null;
  dhcp_end?: string | null;
  restart: boolean;
}

export interface NetworkCreate {
  name: string;
  forward_mode: string;
  ip_address?: string;
  prefix?: number;
  dhcp_enabled: boolean;
  autostart: boolean;
}

export interface Task {
  id: number;
  uuid: string;
  name: string;
  description?: string | null;
  type: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | string;
  progress: number;
  result?: Record<string, unknown> | null;
  error_message?: string | null;
  target_type?: string | null;
  target_name?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface HostResources {
  cpu_count: number;
  cpu_usage_percent: number;
  memory_total: number;
  memory_used: number;
  memory_free: number;
  memory_usage_percent: number;
  disk_total: number;
  disk_used: number;
  disk_free: number;
  disk_usage_percent: number;
}

export interface HostInfo {
  hostname: string;
  arch: string;
  cpu_model?: string | null;
  cpus: number;
  sockets: number;
  cores: number;
  threads: number;
  mhz: number;
  memory: number; // bytes
  os_type: string;
  os_version: string;
  kernel_version: string;
  libvirt_uri: string;
  libvirt_version: string;
  qemu_version?: string | null;
  kvm_available: boolean;
  emulator_available: boolean;
  issues: string[];
  resources: HostResources;
  total_vms: number;
  running_vms: number;
  stopped_vms: number;
  paused_vms: number;
  total_pools: number;
  total_volumes: number;
  total_networks: number;
  active_networks: number;
}
