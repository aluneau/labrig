// Log viewer + diagnostics (backend app/api/v1/endpoints/logs.py, services/log_service.py)

export type LogLevel = 'error' | 'warning' | 'info';

export interface LogEntry {
  n: number;
  text: string;
  level: LogLevel;
  rules: string[];
  before?: string[];
  after?: string[];
}

export interface LogRuleCount {
  id: string;
  count: number;
  hint: string;
  level: LogLevel;
  first_line: number;
}

export interface LogCategorySummary {
  category: string;
  label: string;
  count: number;
  hint: string;
  rules: LogRuleCount[];
}

export interface LogPodBrief {
  namespace: string;
  name: string;
  phase?: string;
  ready?: string;
  restarts?: number;
  node?: string | null;
  problems: string[];
}

export interface LogResult {
  vm?: string;
  cluster?: string;
  source: string;
  command: string;
  total_lines: number;
  matched: number;
  returned: number;
  truncated: boolean;
  levels: Record<LogLevel, number>;
  summary: LogCategorySummary[];
  entries: LogEntry[];
  pods?: LogPodBrief[];
  notes?: string[];
}

export interface LogRule {
  id: string;
  category: string;
  level: LogLevel;
  hint: string;
  pattern: string;
}

export interface LogRules {
  categories: Record<string, string>;
  rules: LogRule[];
}

export interface PodContainer {
  name: string;
  init: boolean;
  ready: boolean;
  restarts: number;
  image?: string;
  problems: string[];
}

export interface PodInfo extends LogPodBrief {
  containers: PodContainer[];
  created?: string;
}

export interface PodList {
  command: string;
  count: number;
  pods: PodInfo[];
  truncated: boolean;
}

export type LogParams = Record<string, string | number | boolean | string[] | null | undefined>;

export interface DiagFinding {
  level: LogLevel;
  text: string;
}

export interface DiagNic {
  name: string;
  mac?: string;
  state?: string;
  mtu?: number;
  addresses: string[];
  driver?: string | null;
  driver_version?: string | null;
  firmware_version?: string | null;
  bus_info?: string | null;
  kind?: string | null;
  vfs?: { vf: number; mac?: string; vlan?: number; trust?: boolean; spoofchk?: boolean; link_state?: string }[];
}

export interface DiagPci {
  slot: string;
  class?: string;
  device: string;
  driver?: string;
  modules?: string;
  subsystem?: string;
}

export interface DiagBlock {
  name: string;
  size?: string;
  type?: string;
  mountpoint?: string | null;
  fstype?: string | null;
  model?: string | null;
  serial?: string | null;
  children?: DiagBlock[];
}

export interface Diagnostics {
  vm: string;
  exec: boolean;
  exec_error?: string;
  agent: Record<string, any>;
  missing_tools: string[];
  findings: DiagFinding[];
  os?: { pretty_name?: string; id?: string; version?: string; kernel?: string; kernel_version?: string;
    cmdline?: string; selinux?: string | null; uptime_s?: number; machine?: string };
  hardware?: { cpu_model?: string | null; cpus?: number | null; virtualization?: string | null;
    memory_mib?: number | null; memory_available_mib?: number | null; swap_mib?: number | null };
  bios?: Record<string, string>;
  system?: Record<string, string>;
  pci?: DiagPci[];
  nics?: DiagNic[];
  disks?: DiagBlock[];
  filesystems?: string[];
  sriov?: { pfs: { pf: string; total_vfs?: number; num_vfs?: number; pci: string; driver?: string }[];
    iommu_groups: number; vfio_devices: string[] };
  failed_units?: string[];
  recent_errors?: string[];
}
