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

export type NicModel = 'virtio' | 'e1000e' | 'igb' | 'e1000' | 'rtl8139';
export type LinkState = 'up' | 'down';

export interface VMNic {
  network?: string | null;
  mac?: string | null;
  type?: string | null; // network | bridge | direct | hostdev ...
  model?: string | null; // null for SR-IOV VFs (hostdev networks)
  link_state: LinkState;
  device?: string | null; // host tap while running
  vf: boolean; // SR-IOV VF passed through from the host (VF pool network)
  // running VM only: 'attach' = appears at next start, 'detach' = goes away when released, 'change' = saved differs
  pending?: 'attach' | 'detach' | 'change' | null;
}

export interface VMNicCreate {
  network: string;
  model?: NicModel;
  mac?: string;
  link_state?: LinkState;
}

export interface VMNicUpdate {
  link_state?: LinkState;
  network?: string;
}

export interface VMIommu {
  enabled: boolean; // saved config
  active?: boolean | null; // running instance (null = shut off)
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
  nics: VMNic[];
  iommu?: VMIommu | null;
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
  extra_nics?: VMNicCreate[];
  iommu?: boolean;
  guest_kernel_args?: string;
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
  interfaces: NetworkInterface[];
  xml: string;
}

export interface NetworkInterface {
  vm: string;
  mac: string;
  active: boolean; // the VM is running
}

export interface LeaseRelease {
  mac: string;
  ip: string;
  released: boolean;
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
  forward_mode: string; // nat | route | isolated | hostdev (SR-IOV VF pool: forward_dev = PF)
  forward_dev?: string;
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

// Kubernetes clusters (backend/app/schemas/cluster.py)

export type ClusterType = 'k3s' | 'kubeadm' | 'openshift';
export type ClusterStatus = 'provisioning' | 'ready' | 'starting' | 'stopping' | 'stopped' | 'error';

export interface NodeResources {
  memory: number; // MiB
  vcpu: number;
  disk_size: number; // GiB
}

export interface ClusterCreate {
  name: string;
  type?: ClusterType;
  version?: string | null;
  ctlplanes?: number;
  workers?: number;
  ctlplane?: NodeResources;
  worker?: NodeResources;
  cloud_image_id?: number | null;
  domain?: string;
  network?: string | null;
  cidr?: string | null;
  pod_cidr?: string;
  service_cidr?: string;
  extra_args?: string | null;
  username?: string | null;
  password?: string | null;
  ssh_keys?: string[];
  keyboard?: string | null;
}

export interface ClusterNode {
  name: string;
  role: 'ctlplane' | 'worker' | string;
  ip?: string | null;
  mac?: string | null;
  vm_id?: number | null;
  state: string; // libvirt state, or 'missing'
  fqdn?: string | null;
}

export interface Cluster {
  id: number;
  name: string;
  type: ClusterType | string;
  version?: string | null;
  network: string;
  network_owned: boolean;
  domain: string;
  api_hostname: string;
  api_ip?: string | null;
  api_endpoint?: string | null;
  status: ClusterStatus | string;
  status_message?: string | null;
  task_id?: number | null;
  task_running: boolean;
  task_progress?: number | null;
  has_kubeconfig: boolean;
  ctlplanes: number;
  workers: number;
  spec?: Record<string, any> | null;
  nodes: ClusterNode[];
  created_at: string;
  updated_at: string;
}

export interface ClusterCommandOutput {
  command: string;
  node: string;
  exitcode: number;
  stdout: string;
  stderr: string;
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

export type LibvirtState = 'running' | 'stopped' | 'starting' | 'stopping';

export interface LibvirtUnit {
  name: string;
  active_state: string;
  enabled?: string | null;
}

/** GET /hosts/libvirt: read from systemd, never starts libvirt */
export interface LibvirtStatus {
  state: LibvirtState;
  connected: boolean; // the app holds a connection right now
  manageable: boolean; // Start/Stop available (qemu:///system + systemd)
  mode?: 'monolithic' | 'modular' | null;
  daemon_active: boolean; // false when only the sockets listen (socket activation)
  units: LibvirtUnit[];
  idle_timeout_minutes: number;
  helper_installed: boolean;
  dhcp_release_available: boolean;
  uri: string;
}

export type LibvirtStopMode = 'refuse' | 'shutdown' | 'force';

export interface LibvirtAction {
  status: LibvirtStatus;
  task?: Task | null;
  warning?: string | null;
}

// Lab groups (backend/app/schemas/group.py)

export interface DNSRecord {
  name: string; // relative to the group domain ("api.ocp", "*.apps.ocp"), absolute if it ends with '.'
  a?: string | null;
  cname?: string | null;
}

export interface GroupCloudInit {
  username?: string | null;
  password?: string | null;
  ssh_keys?: string[];
  keyboard?: string | null;
}

export interface MemberSpec {
  name: string;
  image?: string;
  memory?: number; // MiB
  vcpu?: number;
  disk_size?: number; // GiB
  role?: string;
  ip?: string | null;
  mac?: string | null;
  cloud_init?: GroupCloudInit | null;
  user_data?: string | null;
}

export interface RouterSpec {
  flavour?: 'el' | 'vyos';
  image?: string | null;
  memory?: number;
  vcpu?: number;
  disk_size?: number;
  dns?: { forwarders?: string[]; records?: DNSRecord[] };
  bgp?: unknown;
  wireguard?: unknown;
  vlans?: unknown[];
  ip?: string | null;
  lan_mac?: string | null;
  uplink_mac?: string | null;
}

export interface GroupSpec {
  name: string;
  cidr: string;
  domain?: string | null;
  uplink?: string | null;
  dhcp?: { start: string; end: string } | null;
  router?: RouterSpec;
  cloud_init?: GroupCloudInit;
  members: MemberSpec[];
}

export interface GroupMemberInfo {
  name: string;
  role: string;
  hostname?: string | null;
  fqdn?: string | null;
  ip?: string | null;
  mac?: string | null;
  vm_id?: number | null;
  vm_name?: string | null;
  vm_uuid?: string | null;
  state: string;
  image?: string | null;
  memory?: number | null;
  vcpu?: number | null;
}

export interface Group {
  id: number;
  name: string;
  cidr: string;
  domain: string;
  uplink?: string | null;
  status: 'creating' | 'ready' | 'updating' | 'error' | 'deleting' | 'missing' | string;
  state: 'running' | 'stopped' | 'partial' | string;
  error_message?: string | null;
  network_name: string;
  network_id?: number | null;
  router: GroupMemberInfo;
  members: GroupMemberInfo[];
  member_count: number;
  config_applied: boolean;
  config_applied_at?: string | null;
  config_error?: string | null;
  spec: GroupSpec;
  created_at: string;
  updated_at: string;
}

export interface GroupLease {
  ip: string;
  mac: string;
  hostname?: string | null;
  expiry?: number | null;
}

export interface GroupDetail extends Group {
  router_uplink_ips: string[];
  leases: GroupLease[];
}

export interface RouterConfig {
  flavour: string;
  user_data: string;
  network_config: string;
  files: Record<string, string>;
  apply_command: string;
  config_applied: boolean;
  config_applied_at?: string | null;
  config_error?: string | null;
}

export interface SriovVF {
  index: number;
  pci?: string | null;
  driver?: string | null; // igbvf, iavf, vfio-pci (passed through), ...
  netdev?: string | null;
}

export interface SriovPF {
  name: string;
  pci?: string | null;
  driver?: string | null;
  vendor_id?: string | null;
  device_id?: string | null;
  vf_device_id?: string | null;
  total_vfs: number;
  num_vfs: number;
  operstate?: string | null;
  vfs: SriovVF[];
}

export interface SriovStatus {
  iommu: { enabled: boolean; groups: number; message?: string | null };
  pfs: SriovPF[];
}
