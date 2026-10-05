package provider

import (
	"context"
	"fmt"
	"strconv"
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
)

type groupResource struct{ client *Client }

type groupMemberModel struct {
	Name     types.String `tfsdk:"name"`
	Image    types.String `tfsdk:"image"`
	Memory   types.Int64  `tfsdk:"memory"`
	VCPU     types.Int64  `tfsdk:"vcpu"`
	DiskSize types.Int64  `tfsdk:"disk_size"`
	Role     types.String `tfsdk:"role"`
	IP       types.String `tfsdk:"ip"`
}

type dnsRecordModel struct {
	Name  types.String `tfsdk:"name"`
	A     types.String `tfsdk:"a"`
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
}

// API payloads

type apiGroupMemberSpec struct {
	Name     string  `json:"name"`
	Image    string  `json:"image"`
	Memory   int64   `json:"memory"`
	VCPU     int64   `json:"vcpu"`
	DiskSize int64   `json:"disk_size"`
	Role     string  `json:"role"`
	IP       *string `json:"ip,omitempty"`
}

type apiDNSRecord struct {
	Name  string  `json:"name"`
	A     *string `json:"a,omitempty"`
	CNAME *string `json:"cname,omitempty"`
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
	} `json:"router"`
	CloudInit map[string]any       `json:"cloud_init,omitempty"`
	Members   []apiGroupMemberSpec `json:"members"`
	DHCPHosts []apiGroupDHCPHost   `json:"dhcp_hosts"`
}

type apiGroupMember struct {
	Name  string  `json:"name"`
	IP    *string `json:"ip"`
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
		} `json:"router"`
		Members   []apiGroupMemberSpec `json:"members"`
		DHCPHosts []apiGroupDHCPHost   `json:"dhcp_hosts"`
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
			"router_memory": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1024),
				PlanModifiers: []planmodifier.Int64{int64planmodifier.RequiresReplace()}, Description: "MiB."},
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
					"user_data": schema.StringAttribute{Optional: true, Description: "Not supported for groups (use per-member settings later)."},
				},
			},
			"running":       schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Start (router first) or stop (router last) the whole group."},
			"network_name":  schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"router_ip":     schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"router_vm_id":  schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"member_ips":    schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> IP."},
			"member_macs":   schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> MAC."},
			"member_vm_ids": schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Member name -> vmmanager VM id."},
		},
		Blocks: map[string]schema.Block{
			"member": schema.ListNestedBlock{
				Description: "A member VM (cloud image + cloud-init), reachable as <name>.<domain>.",
				NestedObject: schema.NestedBlockObject{Attributes: map[string]schema.Attribute{
					"name":      schema.StringAttribute{Required: true},
					"image":     schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("debian-13")},
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
		s.Router.DNS.Records = append(s.Router.DNS.Records, apiDNSRecord{Name: rec.Name.ValueString(), A: strPtr(rec.A), CNAME: strPtr(rec.CNAME)})
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
	s.DHCPHosts = []apiGroupDHCPHost{}
	for _, h := range m.DHCPHosts {
		s.DHCPHosts = append(s.DHCPHosts, apiGroupDHCPHost{MAC: h.MAC.ValueString(), IP: h.IP.ValueString(), Hostname: strPtr(h.Hostname)})
	}
	s.Members = []apiGroupMemberSpec{}
	for _, mem := range m.Members {
		s.Members = append(s.Members, apiGroupMemberSpec{
			Name: mem.Name.ValueString(), Image: mem.Image.ValueString(), Memory: mem.Memory.ValueInt64(),
			VCPU: mem.VCPU.ValueInt64(), DiskSize: mem.DiskSize.ValueInt64(), Role: mem.Role.ValueString(), IP: strPtr(mem.IP),
		})
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
	for _, mem := range m.Members {
		userIP[mem.Name.ValueString()] = !mem.IP.IsNull()
	}
	byName := map[string]apiGroupMember{}
	for _, mem := range g.Members {
		byName[mem.Name] = mem
	}
	var members []groupMemberModel
	ips, macs, vmIDs := map[string]string{}, map[string]string{}, map[string]string{}
	for _, s := range g.Spec.Members {
		mm := groupMemberModel{
			Name: types.StringValue(s.Name), Image: types.StringValue(s.Image), Memory: types.Int64Value(s.Memory),
			VCPU: types.Int64Value(s.VCPU), DiskSize: types.Int64Value(s.DiskSize), Role: types.StringValue(s.Role),
			IP: types.StringNull(),
		}
		if userIP[s.Name] && s.IP != nil {
			mm.IP = types.StringValue(*s.IP)
		}
		members = append(members, mm)
		live := byName[s.Name]
		if live.IP != nil {
			ips[s.Name] = *live.IP
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
		records = append(records, dnsRecordModel{Name: types.StringValue(rec.Name), A: strOrNull(rec.A), CNAME: strOrNull(rec.CNAME)})
	}
	m.DNSRecords = records
	var hosts []groupDHCPHostModel
	for _, h := range g.Spec.DHCPHosts {
		hosts = append(hosts, groupDHCPHostModel{MAC: types.StringValue(h.MAC), IP: types.StringValue(h.IP), Hostname: strOrNull(h.Hostname)})
	}
	m.DHCPHosts = hosts
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
