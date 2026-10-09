package provider

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/objectplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &groupResource{}
	_ resource.ResourceWithImportState = &groupResource{}
)

const (
	groupCreateTimeout = 25 * time.Minute // router first boot installs packages, then members
	groupPowerTimeout  = 10 * time.Minute
	// router restart + mirror-registry download (~1.3 GB) + Quay install
	registrySetupTimeout = 60 * time.Minute
)

type groupResource struct{ client *Client }

type groupMemberModel struct {
	Name      types.String          `tfsdk:"name"`
	Source    types.String          `tfsdk:"source"`
	Image     types.String          `tfsdk:"image"`
	ISO       types.String          `tfsdk:"iso"`
	Memory    types.Int64           `tfsdk:"memory"`
	VCPU      types.Int64           `tfsdk:"vcpu"`
	DiskSize  types.Int64           `tfsdk:"disk_size"`
	Role      types.String          `tfsdk:"role"`
	IP        types.String          `tfsdk:"ip"`
	CloudInit *memberCloudInitModel `tfsdk:"cloud_init"`
	UserData  types.String          `tfsdk:"user_data"`
}

// Per-member login, replacing the group's cloud_init for that member (cloud_image members only)
type memberCloudInitModel struct {
	Username types.String `tfsdk:"username"`
	Password types.String `tfsdk:"password"`
	SSHKeys  types.List   `tfsdk:"ssh_keys"`
	Keyboard types.String `tfsdk:"keyboard"`
}

type dnsRecordModel struct {
	Name  types.String `tfsdk:"name"`
	A     types.String `tfsdk:"a"`
	AAAA  types.String `tfsdk:"aaaa"`
	CNAME types.String `tfsdk:"cname"`
}

type groupDHCPHostModel struct {
	MAC      types.String `tfsdk:"mac"`
	IP       types.String `tfsdk:"ip"`
	Hostname types.String `tfsdk:"hostname"`
}

type groupModel struct {
	ID            types.String         `tfsdk:"id"`
	Name          types.String         `tfsdk:"name"`
	CIDR          types.String         `tfsdk:"cidr"`
	Domain        types.String         `tfsdk:"domain"`
	Uplink        types.String         `tfsdk:"uplink"`
	RouterImage   types.String         `tfsdk:"router_image"`
	RouterMemory  types.Int64          `tfsdk:"router_memory"`
	DNSForwarders types.List           `tfsdk:"dns_forwarders"`
	CloudInit     *cloudInitModel      `tfsdk:"cloud_init"`
	Running       types.Bool           `tfsdk:"running"`
	Members       []groupMemberModel   `tfsdk:"member"`
	DNSRecords    []dnsRecordModel     `tfsdk:"dns_record"`
	DHCPHosts     []groupDHCPHostModel `tfsdk:"dhcp_host"`
	NetworkName   types.String         `tfsdk:"network_name"`
	RouterIP      types.String         `tfsdk:"router_ip"`
	RouterVMID    types.String         `tfsdk:"router_vm_id"`
	MemberIPs     types.Map            `tfsdk:"member_ips"`
	MemberMACs    types.Map            `tfsdk:"member_macs"`
	MemberVMIDs   types.Map            `tfsdk:"member_vm_ids"`
	WireGuard     types.Bool           `tfsdk:"wireguard"`
	WGListenPort  types.Int64          `tfsdk:"wireguard_listen_port"`
	WGHostPort    types.Int64          `tfsdk:"wireguard_host_port"`
	WGSubnet      types.String         `tfsdk:"wireguard_subnet"`
	WGPublicKey   types.String         `tfsdk:"wireguard_public_key"`
	BGP           types.Bool           `tfsdk:"bgp"`
	BGPRange      types.String         `tfsdk:"bgp_announce_range"`
	Egress        *egressModel         `tfsdk:"egress"`
	Registry      *registryModel       `tfsdk:"registry"`
	IPv6          *ipv6Model           `tfsdk:"ipv6"`
	RouterIP6     types.String         `tfsdk:"router_ip6"`
	MemberIP6s    types.Map            `tfsdk:"member_ip6s"`
}

// Dual stack group network (docs/ipv6.md)
type ipv6Model struct {
	Enabled types.Bool   `tfsdk:"enabled"`
	Prefix  types.String `tfsdk:"prefix"`
	Egress  types.String `tfsdk:"egress"`
}

type apiIPv6 struct {
	Enabled bool    `json:"enabled"`
	Prefix  *string `json:"prefix,omitempty"`
	Egress  string  `json:"egress,omitempty"`
}

type apiGroupNetwork struct {
	IPv6 *apiIPv6 `json:"ipv6"`
}

// Egress switch of the router (docs/disconnected.md)
type egressModel struct {
	Mode  types.String `tfsdk:"mode"`
	Allow types.List   `tfsdk:"allow"`
}

// Mirror registry on the router (docs/disconnected.md)
type registryModel struct {
	Enabled  types.Bool   `tfsdk:"enabled"`
	Port     types.Int64  `tfsdk:"port"`
	DiskGB   types.Int64  `tfsdk:"disk_gb"`
	MemoryMB types.Int64  `tfsdk:"memory_mb"`
	VCPUs    types.Int64  `tfsdk:"vcpus"`
	Hostname types.String `tfsdk:"hostname"`
	CAPEM    types.String `tfsdk:"ca_pem"`
}

type apiEgress struct {
	Mode  string   `json:"mode"`
	Allow []string `json:"allow"`
}

type apiRegistry struct {
	Enabled  bool    `json:"enabled"`
	Port     int64   `json:"port,omitempty"`
	DiskGB   int64   `json:"disk_gb,omitempty"`
	MemoryMB int64   `json:"memory_mb,omitempty"`
	VCPUs    int64   `json:"vcpus,omitempty"`
	Hostname *string `json:"hostname,omitempty"`
	CAPEM    *string `json:"ca_pem,omitempty"`
}

// apiBGP is the spec's router.bgp block. Only `enabled` is sent: the server keeps the announce
// ranges / neighbors (assigned, or set in the UI) when the block omits them.
type apiBGP struct {
	Enabled        bool `json:"enabled"`
	AnnounceRanges []struct {
		Prefix string  `json:"prefix"`
		Owner  *string `json:"owner"`
	} `json:"announce_ranges,omitempty"`
}

// apiWireGuard is the spec's router.wireguard block (devices: vmmanager_wireguard_peer)
type apiWireGuard struct {
	Enabled    bool    `json:"enabled"`
	ListenPort int64   `json:"listen_port,omitempty"`
	HostPort   *int64  `json:"host_port,omitempty"`
	Subnet     *string `json:"subnet,omitempty"`
	PublicKey  *string `json:"public_key,omitempty"`
}

// API payloads

type apiGroupMemberSpec struct {
	Name      string         `json:"name"`
	Source    string         `json:"source,omitempty"`
	Image     *string        `json:"image"`
	ISO       *string        `json:"iso,omitempty"`
	Memory    int64          `json:"memory"`
	VCPU      int64          `json:"vcpu"`
	DiskSize  int64          `json:"disk_size"`
	Role      string         `json:"role"`
	IP        *string        `json:"ip,omitempty"`
	CloudInit map[string]any `json:"cloud_init,omitempty"`
	UserData  *string        `json:"user_data,omitempty"`
}

type apiDNSRecord struct {
	Name  string  `json:"name"`
	A     *string `json:"a,omitempty"`
	AAAA  *string `json:"aaaa,omitempty"`
	CNAME *string `json:"cname,omitempty"`
	Owner *string `json:"owner,omitempty"`
}

type apiGroupDHCPHost struct {
	MAC      string  `json:"mac"`
	IP       string  `json:"ip"`
	Hostname *string `json:"hostname,omitempty"`
}

type apiGroupSpec struct {
	Name   string  `json:"name"`
	CIDR   string  `json:"cidr"`
	Domain *string `json:"domain,omitempty"`
	Uplink *string `json:"uplink"`
	Router struct {
		Flavour string  `json:"flavour"`
		Image   *string `json:"image,omitempty"`
		Memory  int64   `json:"memory"`
		DNS     struct {
			Forwarders []string       `json:"forwarders"`
			Records    []apiDNSRecord `json:"records"`
		} `json:"dns"`
		WireGuard *apiWireGuard `json:"wireguard,omitempty"`
		BGP       *apiBGP       `json:"bgp,omitempty"`
		// omitted: the server keeps the stored blocks
		Egress   *apiEgress   `json:"egress,omitempty"`
		Registry *apiRegistry `json:"registry,omitempty"`
	} `json:"router"`
	CloudInit map[string]any       `json:"cloud_init,omitempty"`
	Members   []apiGroupMemberSpec `json:"members"`
	DHCPHosts []apiGroupDHCPHost   `json:"dhcp_hosts"`
	Network   *apiGroupNetwork     `json:"network,omitempty"` // omitted: the server keeps the stored one
}

type apiGroupMember struct {
	Name  string  `json:"name"`
	IP    *string `json:"ip"`
	IP6   *string `json:"ip6"`
	MAC   *string `json:"mac"`
	VMID  *int64  `json:"vm_id"`
	State string  `json:"state"`
}

type apiGroup struct {
	ID           int64            `json:"id"`
	Name         string           `json:"name"`
	CIDR         string           `json:"cidr"`
	Domain       string           `json:"domain"`
	Uplink       *string          `json:"uplink"`
	Status       string           `json:"status"`
	State        string           `json:"state"`
	ErrorMessage *string          `json:"error_message"`
	NetworkName  string           `json:"network_name"`
	Router       apiGroupMember   `json:"router"`
	Members      []apiGroupMember `json:"members"`
	Spec         struct {
		Router struct {
			Image  *string `json:"image"`
			Memory int64   `json:"memory"`
			DNS    struct {
				Forwarders []string       `json:"forwarders"`
				Records    []apiDNSRecord `json:"records"`
			} `json:"dns"`
			WireGuard *apiWireGuard `json:"wireguard"`
			BGP       *apiBGP       `json:"bgp"`
			Egress    *apiEgress    `json:"egress"`
			Registry  *apiRegistry  `json:"registry"`
		} `json:"router"`
		Members   []apiGroupMemberSpec `json:"members"`
		DHCPHosts []apiGroupDHCPHost   `json:"dhcp_hosts"`
		Network   apiGroupNetwork      `json:"network"`
	} `json:"spec"`
}

func NewGroupResource() resource.Resource { return &groupResource{} }

func (r *groupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_group"
}

func (r *groupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	replaceStr := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "A lab group: an isolated network, a router VM (DHCP, DNS, NAT) and member VMs with fixed " +
			"addresses and DNS names. Members, DNS records and DHCP reservations are added/removed in place (applied live on the " +
			"router); a member whose image/size changes is recreated. Network/router settings recreate the group.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"name": schema.StringAttribute{Required: true, PlanModifiers: replaceStr, Description: "Group name; VMs are <name>-rtr and <name>-<member>."},
			"cidr": schema.StringAttribute{Required: true, PlanModifiers: replaceStr, Description: "IPv4 subnet, e.g. 10.42.7.0/24 (router = first address)."},
			"domain": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: keep,
				Description: "DNS zone served by the router. Defaults to <name>.lab."},
			"uplink": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("default"), PlanModifiers: replaceStr,
				Description: "libvirt network the router uses for internet access (NAT)."},
			"router_image": schema.StringAttribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace(), stringplanmodifier.UseStateForUnknown()},
				Description:   "EL cloud image of the router, e.g. almalinux-9 (default: first ready EL image)."},
			"router_memory": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(512),
				PlanModifiers: []planmodifier.Int64{int64planmodifier.RequiresReplace()},
				Description:   "MiB (512 holds on AlmaLinux 9/10: cloud-init adds a 1 GiB swap file for the first-boot dnf run)."},
			"dns_forwarders": schema.ListAttribute{Optional: true, ElementType: types.StringType,
				Description: "Upstream DNS servers of the router (default: the uplink's)."},
			"cloud_init": schema.SingleNestedAttribute{
				Optional:      true,
				Description:   "Login for the router and members.",
				PlanModifiers: []planmodifier.Object{objectplanmodifier.RequiresReplace()},
				Attributes: map[string]schema.Attribute{
					"username":  schema.StringAttribute{Optional: true},
					"password":  schema.StringAttribute{Optional: true, Sensitive: true},
					"ssh_keys":  schema.ListAttribute{Optional: true, ElementType: types.StringType},
					"keyboard":  schema.StringAttribute{Optional: true},
					"user_data": schema.StringAttribute{Optional: true, Description: "Not supported at group level: use user_data in a member block."},
				},
			},
			"running":       schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Start (router first) or stop (router last) the whole group."},
			"network_name":  schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"router_ip":     schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"router_vm_id":  schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"member_ips":    schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> IP."},
			"member_macs":   schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> MAC."},
			"member_vm_ids": schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> vmmanager VM id."},
			"router_ip6":    schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "The router's IPv6 address (<prefix>::1) when ipv6 is enabled."},
			"member_ip6s":   schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> IPv6 address (ipv6 enabled)."},
			"ipv6": schema.SingleNestedAttribute{Optional: true,
				Description: "Dual stack (docs/ipv6.md): the group network also gets an IPv6 /64; the router sends router " +
					"advertisements and serves DHCPv6, every machine gets <prefix>::<host number of its IPv4> and an AAAA record. " +
					"Lab-internal (no IPv6 internet). Applied live. Omitted: left as it is on the server.",
				Attributes: map[string]schema.Attribute{
					"enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
					"prefix": schema.StringAttribute{Optional: true, Computed: true,
						PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
						Description:   "The /64 (default: a free /64 of the server's IPV6_ULA_POOL)."},
					"egress": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("reject"),
						Description: "IPv6 from the lab to outside it: reject (fails at once) or drop (hangs until timeouts)."},
				}},
			"wireguard": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Remote access: WireGuard on the router, relayed from UDP wireguard_host_port of the host. Add devices " +
					"with vmmanager_wireguard_peer. false after true disables it and keeps devices and keys. Applied live."},
			"wireguard_listen_port": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(51820),
				Description: "UDP port of WireGuard on the router (its uplink address)."},
			"wireguard_host_port": schema.Int64Attribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.Int64{int64planmodifier.UseStateForUnknown()},
				Description:   "UDP port on the host that devices connect to (default: first free port of the server's WG_HOST_PORTS)."},
			"wireguard_subnet":     schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "Tunnel subnet (the router has its first address, also the DNS server)."},
			"wireguard_public_key": schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "The router's WireGuard public key."},
			"bgp": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "BGP on the router (FRR, AS 64512): machines of the group network (AS 64513) announce addresses of " +
					"bgp_announce_range, the router routes them (ECMP). See docs/bgp.md. Applied live."},
			"bgp_announce_range": schema.StringAttribute{Computed: true, PlanModifiers: keep,
				Description: "Range the router accepts BGP routes for (a /27 of BGP_ANNOUNCE_POOL, assigned by the server)."},
			"egress": schema.SingleNestedAttribute{Optional: true,
				Description: "Internet access of the lab machines (docs/disconnected.md). mode = \"blocked\": the router refuses " +
					"what the group network sends towards the internet / host networks, except allow; the router itself, DNS, NTP, " +
					"load balancers, the registry and WireGuard keep working. Applied live. Omitted: left as it is on the server.",
				Attributes: map[string]schema.Attribute{
					"mode": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("open"),
						Description: "open or blocked."},
					"allow": schema.ListAttribute{Optional: true, ElementType: types.StringType,
						Description: "CIDRs / addresses still reachable when blocked (e.g. a proxy)."},
				}},
			"registry": schema.SingleNestedAttribute{Optional: true,
				Description: "Mirror registry on the router (mirror-registry / Quay, filled with oc-mirror v2): the router gets " +
					"memory_mb / vcpus and a disk_gb thin disk (restarted once when enabled on an existing group). Apply waits " +
					"until it is installed (~10-30 min). Omitted: left as it is on the server.",
				Attributes: map[string]schema.Attribute{
					"enabled":   schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
					"port":      schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(8443)},
					"disk_gb":   schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(250), Description: "GiB (can grow, not shrink)."},
					"memory_mb": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(8192), Description: "Router RAM while the registry is enabled."},
					"vcpus":     schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(4)},
					"hostname": schema.StringAttribute{Computed: true, PlanModifiers: keep,
						Description: "registry.<domain> (served by the router's DNS); images are <hostname>:<port>/<path>."},
					"ca_pem": schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "CA certificate of the registry (PEM)."},
				}},
		},
		Blocks: map[string]schema.Block{
			"member": schema.ListNestedBlock{
				Description: "A member VM, reachable as <name>.<domain> with a fixed IP from the router. From a cloud image " +
					"(cloud-init), or booted from an ISO / an empty disk (no cloud-init: the installed OS gets its reserved " +
					"address by DHCP). Changing source, image, iso, size, role, cloud_init or user_data recreates that member.",
				NestedObject: schema.NestedBlockObject{Attributes: map[string]schema.Attribute{
					"name": schema.StringAttribute{Required: true},
					"source": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("cloud_image"),
						Description: "cloud_image, iso or empty."},
					"image": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("debian-13"),
						Description: "Cloud image (source = cloud_image), e.g. debian-13, almalinux-9."},
					"iso": schema.StringAttribute{Optional: true,
						Description: "ISO volume name or path (source = iso; see GET /storage/isos)."},
					"cloud_init": schema.SingleNestedAttribute{Optional: true,
						Description: "Login for this member instead of the group's cloud_init (source = cloud_image).",
						Attributes: map[string]schema.Attribute{
							"username": schema.StringAttribute{Optional: true},
							"password": schema.StringAttribute{Optional: true, Sensitive: true},
							"ssh_keys": schema.ListAttribute{Optional: true, ElementType: types.StringType},
							"keyboard": schema.StringAttribute{Optional: true},
						}},
					"user_data": schema.StringAttribute{Optional: true,
						Description: "Raw #cloud-config for this member, replacing the generated one (source = cloud_image)."},
					"memory":    schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1024), Description: "MiB."},
					"vcpu":      schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1)},
					"disk_size": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(10), Description: "GiB."},
					"role":      schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("member")},
					"ip":        schema.StringAttribute{Optional: true, Description: "Fixed IP (assigned by the app if unset: see member_ips)."},
				}},
			},
			"dhcp_host": schema.ListNestedBlock{
				Description: "Static DHCP reservation for a machine that is not a member (e.g. a vmmanager_vm on " +
					"network_name). Applied live on the router; with hostname, <hostname>.<domain> resolves too.",
				NestedObject: schema.NestedBlockObject{Attributes: map[string]schema.Attribute{
					"mac":      schema.StringAttribute{Required: true, Description: "Lowercase MAC address."},
					"ip":       schema.StringAttribute{Required: true, Description: "Address in cidr (not the router's or a member's)."},
					"hostname": schema.StringAttribute{Optional: true},
				}},
			},
			"dns_record": schema.ListNestedBlock{
				Description: "DNS record served by the router; name is relative to domain (\"api.ocp\", \"*.apps.ocp\").",
				NestedObject: schema.NestedBlockObject{Attributes: map[string]schema.Attribute{
					"name":  schema.StringAttribute{Required: true},
					"a":     schema.StringAttribute{Optional: true},
					"aaaa":  schema.StringAttribute{Optional: true, Description: "IPv6 address (with or without a)."},
					"cname": schema.StringAttribute{Optional: true},
				}},
			},
		},
	}
}

func (r *groupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *groupResource) spec(ctx context.Context, m groupModel, d diags) apiGroupSpec {
	var s apiGroupSpec
	s.Name = m.Name.ValueString()
	s.CIDR = m.CIDR.ValueString()
	s.Domain = strPtr(m.Domain)
	uplink := m.Uplink.ValueString()
	s.Uplink = &uplink
	s.Router.Flavour = "el"
	s.Router.Image = strPtr(m.RouterImage)
	s.Router.Memory = m.RouterMemory.ValueInt64()
	s.Router.DNS.Forwarders = []string{}
	if !m.DNSForwarders.IsNull() && !m.DNSForwarders.IsUnknown() {
		m.DNSForwarders.ElementsAs(ctx, &s.Router.DNS.Forwarders, false)
	}
	s.Router.DNS.Records = []apiDNSRecord{}
	for _, rec := range m.DNSRecords {
		s.Router.DNS.Records = append(s.Router.DNS.Records, apiDNSRecord{Name: rec.Name.ValueString(), A: strPtr(rec.A), AAAA: strPtr(rec.AAAA), CNAME: strPtr(rec.CNAME)})
	}
	if ci := m.CloudInit; ci != nil {
		if !ci.UserData.IsNull() {
			d.AddError("cloud_init.user_data is not supported for groups", "Use username/password/ssh_keys/keyboard.")
		}
		c := map[string]any{"username": strPtr(ci.Username), "password": strPtr(ci.Password), "keyboard": strPtr(ci.Keyboard)}
		keys := []string{}
		if !ci.SSHKeys.IsNull() && !ci.SSHKeys.IsUnknown() {
			ci.SSHKeys.ElementsAs(ctx, &keys, false)
		}
		c["ssh_keys"] = keys
		s.CloudInit = c
	}
	switch {
	case m.WireGuard.ValueBool():
		s.Router.WireGuard = &apiWireGuard{Enabled: true, ListenPort: m.WGListenPort.ValueInt64()}
		if !m.WGHostPort.IsNull() && !m.WGHostPort.IsUnknown() {
			port := m.WGHostPort.ValueInt64()
			s.Router.WireGuard.HostPort = &port
		}
	case !m.WGSubnet.IsNull() && !m.WGSubnet.IsUnknown():
		// was enabled: disable, keeping the router's key and the devices
		s.Router.WireGuard = &apiWireGuard{Enabled: false, ListenPort: m.WGListenPort.ValueInt64()}
	}
	switch {
	case m.BGP.ValueBool():
		s.Router.BGP = &apiBGP{Enabled: true}
	case !m.BGPRange.IsNull() && !m.BGPRange.IsUnknown():
		s.Router.BGP = &apiBGP{Enabled: false} // was enabled: disable, keeping its settings
	}
	if e := m.Egress; e != nil {
		s.Router.Egress = &apiEgress{Mode: e.Mode.ValueString(), Allow: []string{}}
		if s.Router.Egress.Mode == "" {
			s.Router.Egress.Mode = "open"
		}
		if !e.Allow.IsNull() && !e.Allow.IsUnknown() {
			e.Allow.ElementsAs(ctx, &s.Router.Egress.Allow, false)
		}
	}
	if v := m.IPv6; v != nil {
		ip := &apiIPv6{Enabled: v.Enabled.IsNull() || v.Enabled.IsUnknown() || v.Enabled.ValueBool(), Egress: v.Egress.ValueString()}
		if !v.Prefix.IsNull() && !v.Prefix.IsUnknown() && v.Prefix.ValueString() != "" {
			prefix := v.Prefix.ValueString()
			ip.Prefix = &prefix
		}
		s.Network = &apiGroupNetwork{IPv6: ip}
	}
	if reg := m.Registry; reg != nil {
		s.Router.Registry = &apiRegistry{Enabled: reg.Enabled.IsNull() || reg.Enabled.IsUnknown() || reg.Enabled.ValueBool(),
			Port: reg.Port.ValueInt64(), DiskGB: reg.DiskGB.ValueInt64(), MemoryMB: reg.MemoryMB.ValueInt64(), VCPUs: reg.VCPUs.ValueInt64()}
	}
	s.DHCPHosts = []apiGroupDHCPHost{}
	for _, h := range m.DHCPHosts {
		s.DHCPHosts = append(s.DHCPHosts, apiGroupDHCPHost{MAC: h.MAC.ValueString(), IP: h.IP.ValueString(), Hostname: strPtr(h.Hostname)})
	}
	s.Members = []apiGroupMemberSpec{}
	for _, mem := range m.Members {
		spec := apiGroupMemberSpec{
			Name: mem.Name.ValueString(), Source: mem.Source.ValueString(), Memory: mem.Memory.ValueInt64(),
			VCPU: mem.VCPU.ValueInt64(), DiskSize: mem.DiskSize.ValueInt64(), Role: mem.Role.ValueString(),
			IP: strPtr(mem.IP), ISO: strPtr(mem.ISO), UserData: strPtr(mem.UserData),
		}
		if spec.Source == "" || spec.Source == "cloud_image" {
			spec.Image = strPtr(mem.Image) // the server clears image for iso / empty members
		}
		if ci := mem.CloudInit; ci != nil {
			c := map[string]any{"username": strPtr(ci.Username), "password": strPtr(ci.Password), "keyboard": strPtr(ci.Keyboard)}
			keys := []string{}
			if !ci.SSHKeys.IsNull() && !ci.SSHKeys.IsUnknown() {
				ci.SSHKeys.ElementsAs(context.Background(), &keys, false)
			}
			c["ssh_keys"] = keys
			spec.CloudInit = c
		}
		s.Members = append(s.Members, spec)
	}
	return s
}

// waitTask polls a task until it ends; returns its error message if it failed.
func (r *groupResource) waitTask(ctx context.Context, taskID int64, timeout time.Duration) error {
	return Poll(ctx, 5*time.Second, timeout, func() (bool, error) {
		var t apiTask
		if err := r.client.Do(ctx, "GET", fmt.Sprintf("/tasks/%d", taskID), nil, &t); err != nil {
			return false, err
		}
		switch t.Status {
		case "completed":
			return true, nil
		case "failed", "cancelled":
			msg := t.Status
			if t.ErrorMessage != nil {
				msg = *t.ErrorMessage
			}
			return false, fmt.Errorf("%s", msg)
		}
		return false, nil
	})
}

// waitRegistry waits until the router's mirror registry answers (setup task: restart, download, Quay install)
func (r *groupResource) waitRegistry(ctx context.Context, id string) error {
	return Poll(ctx, 10*time.Second, registrySetupTimeout, func() (bool, error) {
		var st struct {
			State   string  `json:"state"`
			Message *string `json:"message"`
			Setup   *int64  `json:"setup_task_id"`
		}
		if err := r.client.Do(ctx, "GET", "/groups/"+id+"/registry", nil, &st); err != nil {
			return false, err
		}
		switch {
		case st.State == "ready":
			return true, nil
		case st.State == "error" && st.Setup == nil:
			msg := "registry setup failed"
			if st.Message != nil {
				msg = *st.Message
			}
			return false, fmt.Errorf("%s", msg)
		}
		return false, nil
	})
}

func (r *groupResource) power(ctx context.Context, id string, running bool) error {
	action := "stop"
	if running {
		action = "start"
	}
	var t apiTask
	if err := r.client.Do(ctx, "POST", "/groups/"+id+"/"+action, nil, &t); err != nil {
		return err
	}
	return r.waitTask(ctx, t.ID, groupPowerTimeout)
}

func (r *groupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan groupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := r.spec(ctx, plan, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	var created struct {
		Group  apiGroup `json:"group"`
		TaskID int64    `json:"task_id"`
	}
	if err := r.client.Do(ctx, "POST", "/groups", body, &created); err != nil {
		resp.Diagnostics.AddError("Cannot create group", err.Error())
		return
	}
	plan.ID = types.StringValue(strconv.FormatInt(created.Group.ID, 10))
	// Save the ID right away so a failure below doesn't leak the group (destroy cleans it up).
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), plan.ID)...)
	if err := r.waitTask(ctx, created.TaskID, groupCreateTimeout); err != nil {
		resp.Diagnostics.AddError("Group creation failed", err.Error())
		return
	}
	if plan.Registry != nil && plan.Registry.Enabled.ValueBool() {
		if err := r.waitRegistry(ctx, plan.ID.ValueString()); err != nil {
			resp.Diagnostics.AddError("Registry setup failed", err.Error())
			return
		}
	}
	if !plan.Running.ValueBool() {
		if err := r.power(ctx, plan.ID.ValueString(), false); err != nil {
			resp.Diagnostics.AddError("Cannot stop group", err.Error())
			return
		}
	}
	r.readInto(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

// readInto refreshes m from the API; returns false if the group no longer exists.
func (r *groupResource) readInto(ctx context.Context, m *groupModel, d diags) bool {
	var g apiGroup
	if err := r.client.Do(ctx, "GET", "/groups/"+m.ID.ValueString(), nil, &g); err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read group", err.Error())
		return true
	}
	m.Name = types.StringValue(g.Name)
	m.CIDR = types.StringValue(g.CIDR)
	m.Domain = types.StringValue(g.Domain)
	m.Uplink = strOrNull(g.Uplink)
	m.RouterImage = strOrNull(g.Spec.Router.Image)
	m.RouterMemory = types.Int64Value(g.Spec.Router.Memory)
	if len(g.Spec.Router.DNS.Forwarders) > 0 || !m.DNSForwarders.IsNull() {
		m.DNSForwarders, _ = types.ListValueFrom(ctx, types.StringType, g.Spec.Router.DNS.Forwarders)
	}
	m.Running = types.BoolValue(g.State != "stopped")
	m.NetworkName = types.StringValue(g.NetworkName)
	m.RouterIP = strOrNull(g.Router.IP)
	if g.Router.VMID != nil {
		m.RouterVMID = types.StringValue(strconv.FormatInt(*g.Router.VMID, 10))
	} else {
		m.RouterVMID = types.StringNull()
	}

	// Members: IPs only kept in the block when they were set in the config (else computed in member_ips)
	userIP := map[string]bool{}
	prior := map[string]groupMemberModel{}
	for _, mem := range m.Members {
		userIP[mem.Name.ValueString()] = !mem.IP.IsNull()
		prior[mem.Name.ValueString()] = mem
	}
	byName := map[string]apiGroupMember{}
	for _, mem := range g.Members {
		byName[mem.Name] = mem
	}
	var members []groupMemberModel
	ips, macs, vmIDs := map[string]string{}, map[string]string{}, map[string]string{}
	ip6s := map[string]string{}
	for _, s := range g.Spec.Members {
		mm := groupMemberModel{
			Name: types.StringValue(s.Name), Memory: types.Int64Value(s.Memory),
			VCPU: types.Int64Value(s.VCPU), DiskSize: types.Int64Value(s.DiskSize), Role: types.StringValue(s.Role),
			IP: types.StringNull(), ISO: types.StringNull(), UserData: types.StringNull(),
		}
		mm.Source = types.StringValue("cloud_image")
		if s.Source != "" {
			mm.Source = types.StringValue(s.Source)
		}
		prev, known := prior[s.Name]
		switch {
		case s.Image != nil:
			mm.Image = types.StringValue(*s.Image)
		case known: // iso / empty member: the server clears image, keep the configured (default) value
			mm.Image = prev.Image
		default:
			mm.Image = types.StringValue("debian-13")
		}
		if s.ISO != nil {
			mm.ISO = types.StringValue(*s.ISO)
			// The server stores the resolved volume path: keep the name the config used for it
			if known && !prev.ISO.IsNull() && (prev.ISO.ValueString() == *s.ISO || strings.HasSuffix(*s.ISO, "/"+prev.ISO.ValueString())) {
				mm.ISO = prev.ISO
			}
		}
		if s.UserData != nil {
			mm.UserData = types.StringValue(*s.UserData)
		}
		if s.CloudInit != nil {
			if known && prev.CloudInit != nil {
				mm.CloudInit = prev.CloudInit // the server keeps what was sent (password included)
			} else {
				ci := &memberCloudInitModel{Username: optString(s.CloudInit["username"]), Password: optString(s.CloudInit["password"]),
					Keyboard: optString(s.CloudInit["keyboard"]), SSHKeys: types.ListNull(types.StringType)}
				if keys, ok := s.CloudInit["ssh_keys"].([]any); ok && len(keys) > 0 {
					vals := []string{}
					for _, k := range keys {
						if ks, ok := k.(string); ok {
							vals = append(vals, ks)
						}
					}
					ci.SSHKeys, _ = types.ListValueFrom(context.Background(), types.StringType, vals)
				}
				mm.CloudInit = ci
			}
		}
		if userIP[s.Name] && s.IP != nil {
			mm.IP = types.StringValue(*s.IP)
		}
		members = append(members, mm)
		live := byName[s.Name]
		if live.IP != nil {
			ips[s.Name] = *live.IP
		}
		if live.IP6 != nil {
			ip6s[s.Name] = *live.IP6
		}
		if live.MAC != nil {
			macs[s.Name] = *live.MAC
		}
		if live.VMID != nil {
			vmIDs[s.Name] = strconv.FormatInt(*live.VMID, 10)
		}
	}
	m.Members = members
	var records []dnsRecordModel
	for _, rec := range g.Spec.Router.DNS.Records {
		if rec.Owner != nil && *rec.Owner != "" {
			continue // managed by the server (e.g. a kubeadm cluster's api / node records)
		}
		records = append(records, dnsRecordModel{Name: types.StringValue(rec.Name), A: strOrNull(rec.A), AAAA: strOrNull(rec.AAAA), CNAME: strOrNull(rec.CNAME)})
	}
	m.DNSRecords = records
	var hosts []groupDHCPHostModel
	for _, h := range g.Spec.DHCPHosts {
		hosts = append(hosts, groupDHCPHostModel{MAC: types.StringValue(h.MAC), IP: types.StringValue(h.IP), Hostname: strOrNull(h.Hostname)})
	}
	m.DHCPHosts = hosts
	if wg := g.Spec.Router.WireGuard; wg != nil {
		m.WireGuard = types.BoolValue(wg.Enabled)
		m.WGListenPort = types.Int64Value(wg.ListenPort)
		if wg.HostPort != nil {
			m.WGHostPort = types.Int64Value(*wg.HostPort)
		} else {
			m.WGHostPort = types.Int64Null()
		}
		m.WGSubnet, m.WGPublicKey = strOrNull(wg.Subnet), strOrNull(wg.PublicKey)
	} else {
		m.WireGuard = types.BoolValue(false)
		if m.WGListenPort.IsNull() || m.WGListenPort.IsUnknown() {
			m.WGListenPort = types.Int64Value(51820)
		}
		m.WGHostPort, m.WGSubnet, m.WGPublicKey = types.Int64Null(), types.StringNull(), types.StringNull()
	}
	m.BGP, m.BGPRange = types.BoolValue(false), types.StringNull()
	if b := g.Spec.Router.BGP; b != nil {
		m.BGP = types.BoolValue(b.Enabled)
		for _, r := range b.AnnounceRanges {
			if r.Owner == nil || *r.Owner == "" {
				m.BGPRange = types.StringValue(r.Prefix)
				break
			}
		}
	}
	// egress / registry: only tracked when the config manages them (omitted = left to the UI / API)
	if m.Egress != nil && g.Spec.Router.Egress != nil {
		e := g.Spec.Router.Egress
		allow := m.Egress.Allow
		if len(e.Allow) > 0 || !allow.IsNull() {
			allow, _ = types.ListValueFrom(ctx, types.StringType, e.Allow)
		}
		m.Egress = &egressModel{Mode: types.StringValue(e.Mode), Allow: allow}
	}
	if m.Registry != nil && g.Spec.Router.Registry != nil {
		reg := g.Spec.Router.Registry
		m.Registry = &registryModel{Enabled: types.BoolValue(reg.Enabled), Port: types.Int64Value(reg.Port),
			DiskGB: types.Int64Value(reg.DiskGB), MemoryMB: types.Int64Value(reg.MemoryMB), VCPUs: types.Int64Value(reg.VCPUs),
			Hostname: strOrNull(reg.Hostname), CAPEM: strOrNull(reg.CAPEM)}
	}
	if v := g.Spec.Network.IPv6; m.IPv6 != nil && v != nil {
		egress := v.Egress
		if egress == "" {
			egress = "reject"
		}
		m.IPv6 = &ipv6Model{Enabled: types.BoolValue(v.Enabled), Prefix: strOrNull(v.Prefix), Egress: types.StringValue(egress)}
	}
	m.RouterIP6 = strOrNull(g.Router.IP6)
	m.MemberIP6s, _ = types.MapValueFrom(ctx, types.StringType, ip6s)
	m.MemberIPs, _ = types.MapValueFrom(ctx, types.StringType, ips)
	m.MemberMACs, _ = types.MapValueFrom(ctx, types.StringType, macs)
	m.MemberVMIDs, _ = types.MapValueFrom(ctx, types.StringType, vmIDs)
	return true
}

func (r *groupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state groupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !r.readInto(ctx, &state, &resp.Diagnostics) {
		resp.State.RemoveResource(ctx)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

func (r *groupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state groupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := state.ID.ValueString()
	plan.ID = state.ID

	// Members, DNS records, forwarders, domain: one spec PUT, applied live by the server
	body := r.spec(ctx, plan, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Do(ctx, "PUT", "/groups/"+id+"?replace_members=true&delete_disks=true", body, nil); err != nil {
		resp.Diagnostics.AddError("Cannot update group", err.Error())
		return
	}
	if !plan.Running.Equal(state.Running) {
		if err := r.power(ctx, id, plan.Running.ValueBool()); err != nil {
			resp.Diagnostics.AddError("Cannot change group power state", err.Error())
			return
		}
	}
	if plan.Registry != nil && plan.Registry.Enabled.ValueBool() && plan.Running.ValueBool() &&
		(state.Registry == nil || !state.Registry.Enabled.ValueBool() || state.Registry.CAPEM.IsNull()) {
		if err := r.waitRegistry(ctx, id); err != nil {
			resp.Diagnostics.AddError("Registry setup failed", err.Error())
			return
		}
	}
	r.readInto(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *groupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state groupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.client.Do(ctx, "DELETE", "/groups/"+state.ID.ValueString()+"?delete_disks=true", nil, nil)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete group", err.Error())
	}
}

func (r *groupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// optString maps a JSON value (string or null) to a types.String
func optString(v any) types.String {
	if str, ok := v.(string); ok {
		return types.StringValue(str)
	}
	return types.StringNull()
}
