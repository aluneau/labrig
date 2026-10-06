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
  group_id?: number | null; // kubeadm: existing lab group; null = a group created for the cluster
  router_memory?: number | null;
  cidr?: string | null;
  pod_cidr?: string;
  service_cidr?: string;
  extra_args?: string | null;
  username?: string | null;
  password?: string | null;
  ssh_keys?: string[];
  keyboard?: string | null;
  openshift?: OpenShiftOptions | null; // type 'openshift' (counts and sizes follow the topology)
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
  group_id?: number | null;
  group_name?: string | null;
  group_owned: boolean;
  load_balancer?: {
    name: string; port: number; backends: string[]; router_ip?: string | null; uplink_ip?: string | null;
  } | null;
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
  console_url?: string | null; // OpenShift web console
  ctlplanes: number;
  workers: number;
  spec?: Record<string, any> | null;
  nodes: ClusterNode[];
  created_at: string;
  updated_at: string;
}

// OpenShift (backend/app/schemas/openshift.py)

export type OpenShiftTopology = 'sno' | 'compact' | 'ha';
export type OpenShiftStorage = 'none' | 'lvms' | 'odf';

export interface OperatorRequest {
  name: string; // package name
  channel?: string | null; // null = the package's defaultChannel
  source?: string; // default redhat-operators
  namespace?: string | null;
}

export interface SriovOptions {
  enabled: boolean;
  nics: number; // igb NICs per node (1-4)
  vfs: number; // VFs per NIC (1-7)
  device_type: 'netdevice' | 'vfio-pci';
  ipam_range: string;
}

export interface MetalLBOptions {
  enabled: boolean;
  mode?: 'l2' | 'bgp'; // l2: pool in the group network, ARP; bgp: a /27 outside it, announced to the router
  addresses: number; // L2 pool size (2-64); BGP pools are a /27
  demo: boolean;
  pool?: string | null; // assigned: "10.43.5.230-10.43.5.245" (l2) or "10.45.0.0/27" (bgp)
}

export type OdfProfile = 'lab' | 'lean';

export interface OpenShiftOptions {
  version?: string | null; // null = latest of the channel
  channel: string;
  topology: OpenShiftTopology;
  storage: OpenShiftStorage;
  storage_disk_size: number; // GiB per storage node
  odf_profile?: OdfProfile; // lab (default): small Ceph, no object storage; lean: Red Hat sizing
  operators: OperatorRequest[];
  sriov: SriovOptions;
  metallb: MetalLBOptions;
  disable_updates: boolean;
}

export interface PullSecretIn {
  content?: string | null;
  path?: string | null;
}

export interface PullSecretStatus {
  configured: boolean;
  registries: string[];
  source?: string | null;
}

export interface OpenShiftChannel {
  name: string; // stable-4.20
  minor: string; // 4.20
  latest?: string | null;
}

export interface OpenShiftVersion {
  version: string;
  payload?: string | null;
  cached: boolean; // openshift-install + oc already downloaded
}

export interface CatalogOperator {
  name: string;
  display_name: string;
  description: string;
  source: string;
  category: string;
  managed_by?: 'storage:lvms' | 'storage:odf' | 'sriov' | 'metallb' | string | null;
  min_nodes: number;
}

export interface InstalledOperator {
  name: string;
  namespace: string;
  channel?: string | null;
  source?: string | null;
  csv?: string | null;
  phase?: string | null; // Succeeded, Installing, Failed...
  version?: string | null;
}

export interface PackageManifest {
  name: string;
  display_name?: string | null;
  provider?: string | null;
  source: string;
  default_channel?: string | null;
  channels: string[];
  description?: string | null;
  suggested_namespace?: string | null;
  all_namespaces_only: boolean;
}

export interface ClusterCredentials {
  username: string;
  password?: string | null;
  console_url?: string | null;
}

export type AddonKind = 'operator' | 'lvms' | 'odf' | 'sriov' | 'metallb' | 'metallb-demo';

export interface AddonRequest {
  kind: AddonKind;
  operator?: OperatorRequest | null;
  sriov?: SriovOptions | null;
  metallb?: MetalLBOptions | null;
}

export interface ScenarioCheck {
  name: string;
  ok?: boolean | null; // null = not run / unknown
  detail: string;
}

export interface MetalLBEndpoint {
  pod: string;
  node: string;
  ip: string;
  ready?: boolean | null;
}

export interface MetalLBScenario {
  enabled: boolean;
  mode?: 'l2' | 'bgp' | string;
  bgp_peers?: { node: string; ip?: string | null; state: string }[]; // bgp: each node's session with the router
  bgp_nexthops?: string[]; // bgp: nodes the router sends the service IP to (ECMP)
  pool?: string | null;
  service_ip?: string | null;
  hostname?: string | null; // hello.<domain>
  announcing_node?: string | null;
  endpoints: MetalLBEndpoint[];
  router_ip?: string | null;
  group_cidr?: string | null;
  wireguard: boolean;
  wireguard_port?: number | null; // host relay UDP port, null when remote access is off
  checks: ScenarioCheck[];
}

export interface AssistedHost {
  name: string;
  role?: string | null;
  status?: string | null;
  stage?: string | null;
  progress?: number | null;
}

export interface ClusterOperatorStatus {
  name: string;
  available?: boolean | null;
  progressing?: boolean | null;
  degraded?: boolean | null;
  message?: string | null;
  version?: string | null;
}

export type InstallPhase = 'preparing' | 'booting' | 'installing' | 'finalizing' | 'addons' | 'ready' | 'error' | 'stopped';

export interface AddonState {
  kind: string;
  name: string;
  state: 'pending' | 'installing' | 'done' | 'error' | string;
  message?: string | null;
}

export interface InstallStatus {
  phase: InstallPhase | string;
  assisted_status?: string | null;
  assisted_info?: string | null;
  progress?: number | null;
  hosts: AssistedHost[];
  version?: string | null;
  cluster_operators: ClusterOperatorStatus[];
  addons: AddonState[];
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
  owner?: string | null; // managed by the app (e.g. "cluster:k1"): read-only
}

/** Host on the group network that is not a member (e.g. a cluster node), owned by the app */
export interface ReservationSpec {
  name: string;
  mac: string;
  ip: string;
  owner?: string | null;
}

/** TCP load balancer on the router (haproxy), reachable on the router's LAN and uplink addresses */
export interface LoadBalancerSpec {
  name: string;
  port: number;
  backends: string[]; // ip:port
  mode?: 'tcp';
  owner?: string | null;
}

export interface GroupCloudInit {
  username?: string | null;
  password?: string | null;
  ssh_keys?: string[];
  keyboard?: string | null;
}

export interface MemberSpec {
  name: string;
  // cloud_image: `image` + cloud-init; iso: boots `iso` (path or name); empty: blank disk.
  // iso/empty members get no cloud-init: the router gives them their reserved IP + name by DHCP.
  source?: 'cloud_image' | 'iso' | 'empty';
  image?: string | null;
  iso?: string | null;
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
  bgp?: BGPSpec | null;
  wireguard?: WireGuardSpec | null;
  vlans?: unknown[];
  egress?: EgressSpec;
  registry?: RegistrySpec;
  ip?: string | null;
  lan_mac?: string | null;
  uplink_mac?: string | null;
  uplink_ip?: string | null; // fixed (reserved) address on the uplink network
}

/** Egress switch of the group router (disconnected labs, docs/disconnected.md) */
export interface EgressSpec {
  mode: 'open' | 'blocked';
  allow?: string[]; // CIDRs / addresses still reachable when blocked
}

/** Mirror registry (mirror-registry / Quay) on the group router */
export interface RegistrySpec {
  enabled: boolean;
  port?: number;
  disk_gb?: number;
  memory_mb?: number;
  vcpus?: number;
  hostname?: string | null; // assigned: registry.<domain>
  ca_pem?: string | null;   // read back from the router
}

export interface OperatorPackage {
  name: string;
  channel?: string | null;
}

export interface OperatorCatalog {
  catalog?: string | null; // default registry.redhat.io/redhat/redhat-operator-index:v<minor>
  packages: OperatorPackage[];
}

export interface MirrorRequest {
  openshift_version?: string | null;
  operators?: OperatorCatalog[];
  additional_images?: string[];
}

export interface MirrorRecord {
  id: string;
  status: 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted' | string;
  openshift_version?: string | null;
  operators: OperatorCatalog[];
  additional_images: string[];
  started_at?: string | null;
  finished_at?: string | null;
  task_id?: number | null;
  images?: number | null;
  error?: string | null;
}

export interface RegistryStatus {
  enabled: boolean;
  ready: boolean;
  state: 'disabled' | 'not-installed' | 'installing' | 'restart-required' | 'ready' | 'stopped' | 'error' | 'starting' | string;
  message?: string | null;
  url?: string | null;
  uplink_url?: string | null;
  hostname?: string | null;
  port?: number | null;
  ca_pem?: string | null;
  disk_used_gb?: number | null;
  disk_total_gb?: number | null;
  memory_mb?: number | null;
  vcpus?: number | null;
  router_memory_mb?: number | null;
  egress: 'open' | 'blocked' | string;
  egress_allow: string[];
  mirrors: MirrorRecord[];
  setup_task_id?: number | null;
  mirror_task_id?: number | null;
}

export interface ImageCopyRequest {
  source: string;
  dest_repo?: string | null;
  dest_tag?: string | null;
  username?: string | null;
  password?: string | null;
}

export interface RegistryImage {
  repository: string;
  tags: string[];
  added: boolean;
  sources: Record<string, string>;
}

export interface RegistryImages {
  registry: string;
  uplink_registry?: string | null;
  images: RegistryImage[];
  truncated: boolean;
}

export interface RegistryCredentials {
  username: string;
  password: string;
  registry: string;
  uplink_registry?: string | null;
}

/** BGP on the router (FRR): sessions from the group network (listen) + explicit neighbors */
export interface BGPNeighbor {
  ip: string;
  asn: number;
  name?: string | null;
  owner?: string | null;
}

/** Prefixes the router accepts (and anything more specific) */
export interface BGPAnnounceRange {
  prefix: string;
  name?: string | null;
  owner?: string | null; // "cluster:<name>" (MetalLB BGP pool)
}

export interface BGPSpec {
  enabled?: boolean;
  asn?: number;
  listen?: boolean;
  peer_asn?: number | null; // null = any other ASN
  neighbors?: BGPNeighbor[];
  announce_ranges?: BGPAnnounceRange[];
  maximum_paths?: number;
}

export interface BGPSettings {
  enabled: boolean;
  asn?: number | null;
  listen?: boolean | null;
  peer_asn?: number | null;
  any_peer_asn?: boolean;
  maximum_paths?: number | null;
  announce_ranges?: BGPAnnounceRange[] | null;
  neighbors?: BGPNeighbor[] | null;
}

export interface BGPSession {
  peer: string;
  name?: string | null;
  remote_as?: number | null;
  state: string;
  established: boolean;
  uptime?: string | null;
  uptime_seconds?: number | null;
  prefixes_received?: number | null;
  dynamic: boolean;
  description?: string | null;
}

export interface BGPRoute {
  prefix: string;
  installed: boolean;
  selected: boolean;
  nexthops: { ip?: string | null; name?: string | null; interface?: string | null; active: boolean }[];
}

export interface BGPStatus {
  configured: boolean;
  enabled: boolean;
  asn?: number | null;
  router_ip?: string | null;
  listen: boolean;
  listen_range?: string | null;
  peer_asn?: number | null;
  maximum_paths?: number | null;
  neighbors: BGPNeighbor[];
  announce_ranges: BGPAnnounceRange[];
  router_running: boolean;
  router_error?: string | null;
  frr_version?: string | null;
  sessions: BGPSession[];
  routes: BGPRoute[];
}

/** GET /groups/{id}/topology */
export interface TopologyMachine {
  kind: 'member' | 'node' | 'reservation' | string;
  name: string;
  vm_name?: string | null;
  vm_id?: number | null;
  role?: string | null;
  cluster?: string | null;
  ip?: string | null;
  mac?: string | null;
  fqdn?: string | null;
  state: string;
  bgp_state?: string | null;
  bgp_prefixes: string[];
  l2_announces: string[];
}

export interface TopologyVip {
  address: string;
  kind: 'metallb-l2' | 'metallb-bgp' | 'bgp-route' | string;
  name?: string | null;
  hostname?: string | null;
  cluster?: string | null;
  via: string[];
}

export interface TopologyCluster {
  id: number;
  name: string;
  type: string;
  status: string;
  nodes: string[];
  api_url?: string | null;
  console_url?: string | null;
  apps_domain?: string | null;
  load_balancer_ports: number[];
  metallb_enabled: boolean;
  metallb_mode?: 'l2' | 'bgp' | null;
  metallb_pool?: string | null;
  service_ip?: string | null;
  service_hostname?: string | null;
}

export interface GroupTopology {
  id: number;
  name: string;
  cidr: string;
  domain: string;
  network_name: string;
  state: string;
  status: string;
  router: {
    name: string;
    vm_id?: number | null;
    state: string;
    lan_ip?: string | null;
    uplink_ip?: string | null;
    uplink_network?: string | null;
    tunnel_ip?: string | null;
    roles: string[];
    dhcp_range?: string | null;
    dns_records: number;
    dns_forwarders: string[];
    load_balancers: { name: string; port: number; backends: string[]; owner?: string | null }[];
    config_applied: boolean;
  };
  wireguard: {
    enabled: boolean;
    subnet?: string | null;
    router_ip?: string | null;
    host_port?: number | null;
    listen_port?: number | null;
    endpoint?: string | null;
    relay_listening: boolean;
    client_allowed_ips: string[];
    peers: { name: string; ip?: string | null; latest_handshake?: number | null; endpoint?: string | null }[];
  };
  bgp: BGPStatus;
  machines: TopologyMachine[];
  clusters: TopologyCluster[];
  vips: TopologyVip[];
  address_pools: { name: string; start: string; end: string; owner?: string | null }[];
  errors: string[];
}

/** A device allowed in through the router's WireGuard (only its public key is stored) */
export interface WireGuardPeer {
  name?: string | null;
  public_key: string;
  ip?: string | null; // tunnel address (assigned)
  endpoint?: string | null; // site-to-site peers only
  allowed_ips?: string[];
}

/** Remote access: wg0 on the router, relayed from the host's UDP host_port */
export interface WireGuardSpec {
  enabled?: boolean;
  listen_port?: number; // on the router
  host_port?: number | null; // assigned: UDP port on the host
  subnet?: string | null; // assigned tunnel subnet
  public_key?: string | null; // the router's, read back from it
  peers?: WireGuardPeer[];
}

export interface WireGuardPeerInfo {
  name: string;
  public_key: string;
  ip?: string | null;
  allowed_ips: string[];
  endpoint?: string | null;
  latest_handshake?: number | null; // epoch s, 0 = never
  rx_bytes?: number | null;
  tx_bytes?: number | null;
}

export interface WireGuardStatus {
  configured: boolean;
  enabled: boolean;
  listen_port?: number | null;
  host_port?: number | null;
  subnet?: string | null;
  router_tunnel_ip?: string | null;
  public_key?: string | null;
  endpoint_host: string;
  endpoint?: string | null;
  client_allowed_ips: string[];
  relay_listening: boolean;
  relay_error?: string | null;
  host_port_range: string; // WG_HOST_PORTS, e.g. 51820-51869
  firewall?: string | null; // ufw | firewalld: must let host_port/udp in
  router_running: boolean;
  router_error?: string | null;
  peers: WireGuardPeerInfo[];
}

export interface WireGuardPeerCreated {
  peer: WireGuardPeerInfo;
  config: string;
  filename: string;
  has_private_key: boolean;
  warning?: string | null;
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
  reservations?: ReservationSpec[];
  load_balancers?: LoadBalancerSpec[];
  owner?: string | null; // "cluster:<name>": created for that cluster, deleted with it
  dhcp_hosts?: GroupDHCPHost[];
}

export interface GroupHostInfo {
  name: string;
  ip: string;
  mac: string;
  owner?: string | null;
  fqdn?: string | null;
  vm_id?: number | null;
  state: string;
}

/** Static DHCP reservation of a non-member machine on the group network */
export interface GroupDHCPHost {
  mac: string;
  ip: string;
  hostname?: string | null; // also served as <hostname>.<domain>
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
  hosts: GroupHostInfo[]; // reserved hosts (cluster nodes)
  clusters: { id: number; name: string; type: string }[];
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
  kind?: 'member' | 'reservation' | 'dynamic';
  member?: string | null;
  vm_name?: string | null; // VM with this MAC on the group network
  vm_running?: boolean;
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
