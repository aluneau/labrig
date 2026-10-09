package provider

import (
	"context"
	"net/url"
	"strconv"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure      = &networkResource{}
	_ resource.ResourceWithImportState    = &networkResource{}
	_ resource.ResourceWithModifyPlan     = &networkResource{}
	_ resource.ResourceWithValidateConfig = &networkResource{}
)

// hostdev = SR-IOV VF pool: libvirt hands out VFs of the physical function forward_dev as PCI
// passthrough NICs. No bridge, address, DHCP or domain, and the API can't edit it in place.
const modeHostdev = "hostdev"

type networkResource struct{ client *Client }

type dhcpHostModel struct {
	MAC  types.String `tfsdk:"mac"`
	IP   types.String `tfsdk:"ip"`
	Name types.String `tfsdk:"name"`
}

type networkModel struct {
	ID          types.String    `tfsdk:"id"`
	Name        types.String    `tfsdk:"name"`
	Mode        types.String    `tfsdk:"mode"`
	ForwardDev  types.String    `tfsdk:"forward_dev"`
	VLAN        types.Int64     `tfsdk:"vlan"`
	IPAddress   types.String    `tfsdk:"ip_address"`
	Prefix      types.Int64     `tfsdk:"prefix"`
	DHCPEnabled types.Bool      `tfsdk:"dhcp_enabled"`
	DHCPStart   types.String    `tfsdk:"dhcp_start"`
	DHCPEnd     types.String    `tfsdk:"dhcp_end"`
	Domain      types.String    `tfsdk:"domain"`
	Autostart   types.Bool      `tfsdk:"autostart"`
	DHCPHosts   []dhcpHostModel `tfsdk:"dhcp_hosts"`
	Bridge      types.String    `tfsdk:"bridge"`
	Active      types.Bool      `tfsdk:"active"`
}

func NewNetworkResource() resource.Resource { return &networkResource{} }

func (r *networkResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_network"
}

func (r *networkResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A libvirt virtual network. Settings changes restart the network; DHCP reservations are applied live.",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"name":         schema.StringAttribute{Required: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"mode":         schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("nat"), Description: "nat, route, open, isolated, or hostdev (SR-IOV VF pool: forward_dev = the physical function, no address/DHCP; see GET /hosts/sriov)."},
			"forward_dev":  schema.StringAttribute{Optional: true, Description: "Host interface to forward through (any if unset); for mode = hostdev, the SR-IOV physical function (required)."},
			"vlan": schema.Int64Attribute{Optional: true, PlanModifiers: []planmodifier.Int64{int64planmodifier.RequiresReplace()},
				Description: "mode = hostdev only: VLAN tag the PF applies to every VF of the pool (the guest sees untagged traffic). A vmmanager_nic can override it. Changing it recreates the pool."},
			"ip_address":   schema.StringAttribute{Optional: true, Description: "Host address on the network, e.g. 192.168.150.1."},
			"prefix":       schema.Int64Attribute{Optional: true, Description: "Subnet prefix length, e.g. 24."},
			"dhcp_enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"dhcp_start":   schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: keep, Description: "Defaults to the 2nd host address."},
			"dhcp_end":     schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: keep, Description: "Defaults to the last host address."},
			"domain":       schema.StringAttribute{Optional: true, Description: "DNS domain served to VMs."},
			"autostart":    schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"dhcp_hosts": schema.SetNestedAttribute{
				Optional:    true,
				Description: "Static DHCP reservations.",
				NestedObject: schema.NestedAttributeObject{Attributes: map[string]schema.Attribute{
					"mac":  schema.StringAttribute{Required: true},
					"ip":   schema.StringAttribute{Required: true},
					"name": schema.StringAttribute{Optional: true},
				}},
			},
			"bridge": schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"active": schema.BoolAttribute{Computed: true, PlanModifiers: []planmodifier.Bool{boolplanmodifier.UseStateForUnknown()}},
		},
	}
}

// ModifyPlan: when the subnet changes and the DHCP range isn't configured, the
// server picks a new range, so it must show as "known after apply".
func (r *networkResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	if req.Plan.Raw.IsNull() {
		return // destroy
	}
	var config, plan networkModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if plan.Mode.ValueString() == modeHostdev {
		// A VF pool has no DHCP: don't let dhcp_enabled's default (true) or the computed range show as drift
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("dhcp_enabled"), types.BoolValue(false))...)
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("dhcp_start"), types.StringNull())...)
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("dhcp_end"), types.StringNull())...)
	}
	if req.State.Raw.IsNull() {
		return // create
	}
	var state networkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	// The API can't turn a network into a VF pool (or back) or move a pool to another PF in place
	if (plan.Mode.ValueString() == modeHostdev || state.Mode.ValueString() == modeHostdev) &&
		(!plan.Mode.Equal(state.Mode) || !plan.ForwardDev.Equal(state.ForwardDev)) {
		resp.RequiresReplace = append(resp.RequiresReplace, path.Root("mode"), path.Root("forward_dev"))
		return
	}
	if plan.Mode.ValueString() == modeHostdev {
		return
	}
	subnetChanged := !plan.IPAddress.Equal(state.IPAddress) || !plan.Prefix.Equal(state.Prefix) ||
		!plan.DHCPEnabled.Equal(state.DHCPEnabled)
	if !subnetChanged {
		return
	}
	if config.DHCPStart.IsNull() {
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("dhcp_start"), types.StringUnknown())...)
	}
	if config.DHCPEnd.IsNull() {
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("dhcp_end"), types.StringUnknown())...)
	}
}

func (r *networkResource) ValidateConfig(ctx context.Context, req resource.ValidateConfigRequest, resp *resource.ValidateConfigResponse) {
	var c networkModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &c)...)
	if resp.Diagnostics.HasError() || c.Mode.IsUnknown() {
		return
	}
	if c.Mode.ValueString() != modeHostdev {
		if !c.VLAN.IsNull() {
			resp.Diagnostics.AddAttributeError(path.Root("vlan"), "VLAN only on SR-IOV VF pools",
				"vlan needs mode = \"hostdev\": on other networks, tag inside the guest.")
		}
		return
	}
	if c.ForwardDev.IsNull() {
		resp.Diagnostics.AddAttributeError(path.Root("forward_dev"), "Missing physical function",
			"mode = \"hostdev\" (SR-IOV VF pool) needs forward_dev = an SR-IOV capable host interface (see GET /api/v1/hosts/sriov).")
	}
	unset := map[string]bool{
		"ip_address": !c.IPAddress.IsNull(), "prefix": !c.Prefix.IsNull(), "dhcp_start": !c.DHCPStart.IsNull(),
		"dhcp_end": !c.DHCPEnd.IsNull(), "domain": !c.Domain.IsNull(), "dhcp_hosts": c.DHCPHosts != nil,
		"dhcp_enabled": !c.DHCPEnabled.IsNull() && !c.DHCPEnabled.IsUnknown() && c.DHCPEnabled.ValueBool(),
	}
	for attr, set := range unset {
		if set {
			resp.Diagnostics.AddAttributeError(path.Root(attr), "Not available on an SR-IOV VF pool",
				attr+" can't be set with mode = \"hostdev\": the VFs are passed through to the VMs, which get their "+
					"addresses from the network the physical function is plugged into.")
		}
	}
}

func (r *networkResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func settingsBody(m networkModel) map[string]any {
	body := map[string]any{
		"forward_mode": m.Mode.ValueString(),
		"forward_dev":  strPtr(m.ForwardDev),
		"ip_address":   strPtr(m.IPAddress),
		"dhcp_enabled": m.DHCPEnabled.ValueBool(),
		"dhcp_start":   strPtr(m.DHCPStart),
		"dhcp_end":     strPtr(m.DHCPEnd),
		"domain":       strPtr(m.Domain),
	}
	if !m.Prefix.IsNull() && !m.Prefix.IsUnknown() {
		body["prefix"] = m.Prefix.ValueInt64()
	}
	return body
}

func hostBody(h dhcpHostModel) apiDHCPHost {
	return apiDHCPHost{MAC: strings.ToLower(h.MAC.ValueString()), IP: h.IP.ValueString(), Name: strPtr(h.Name)}
}

func (r *networkResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan networkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := settingsBody(plan)
	body["name"] = plan.Name.ValueString()
	if !plan.VLAN.IsNull() && !plan.VLAN.IsUnknown() {
		body["vlan"] = plan.VLAN.ValueInt64()
	}
	body["autostart"] = plan.Autostart.ValueBool()

	var created apiNetwork
	if err := r.client.Do(ctx, "POST", "/networks", body, &created); err != nil {
		resp.Diagnostics.AddError("Cannot create network", err.Error())
		return
	}
	plan.ID = types.StringValue(strconv.FormatInt(created.ID, 10))
	// Save the ID right away so a failure below doesn't leak the network.
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), plan.ID)...)

	for _, h := range plan.DHCPHosts {
		if err := r.client.Do(ctx, "POST", "/networks/"+plan.ID.ValueString()+"/hosts", hostBody(h), nil); err != nil {
			resp.Diagnostics.AddError("Cannot add DHCP reservation "+h.MAC.ValueString(), err.Error())
			return
		}
	}
	r.readInto(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

type diags interface {
	AddError(summary, detail string)
	AddWarning(summary, detail string)
}

// readInto refreshes m from the API; returns false if the network no longer exists.
func (r *networkResource) readInto(ctx context.Context, m *networkModel, d diags) bool {
	var cfg apiNetworkConfig
	if err := r.client.Do(ctx, "GET", "/networks/"+m.ID.ValueString()+"/config", nil, &cfg); err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read network", err.Error())
		return true
	}
	n := cfg.Network
	m.Name = types.StringValue(n.Name)
	m.Mode = types.StringValue(n.ForwardMode)
	m.ForwardDev = strOrNull(n.ForwardDev)
	if n.VLAN != nil {
		m.VLAN = types.Int64Value(*n.VLAN)
	} else {
		m.VLAN = types.Int64Null()
	}
	m.IPAddress = strOrNull(n.IPAddress)
	if n.Prefix != nil && n.IPAddress != nil {
		m.Prefix = types.Int64Value(*n.Prefix)
	} else {
		m.Prefix = types.Int64Null()
	}
	m.DHCPEnabled = types.BoolValue(n.DHCPEnabled)
	m.DHCPStart = strOrNull(n.DHCPStart)
	m.DHCPEnd = strOrNull(n.DHCPEnd)
	m.Domain = strOrNull(n.Domain)
	m.Autostart = types.BoolValue(n.Autostart)
	m.Bridge = strOrNull(n.BridgeName)
	m.Active = types.BoolValue(n.Active)

	if len(cfg.Hosts) == 0 {
		m.DHCPHosts = nil
	} else {
		m.DHCPHosts = make([]dhcpHostModel, 0, len(cfg.Hosts))
		for _, h := range cfg.Hosts {
			m.DHCPHosts = append(m.DHCPHosts, dhcpHostModel{MAC: types.StringValue(h.MAC), IP: types.StringValue(h.IP), Name: strOrNull(h.Name)})
		}
	}
	return true
}

func (r *networkResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state networkModel
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

func (r *networkResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state networkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := state.ID.ValueString()
	plan.ID = state.ID

	newSettings, oldSettings := settingsBody(plan), settingsBody(state)
	// A DHCP range left out of the config is chosen by the server: don't pin the
	// old one (it may not fit a new subnet) and don't count it as a change.
	var config networkModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	for attr, value := range map[string]types.String{"dhcp_start": config.DHCPStart, "dhcp_end": config.DHCPEnd} {
		if value.IsNull() {
			newSettings[attr], oldSettings[attr] = (*string)(nil), (*string)(nil)
		}
	}
	changed := false
	for k, v := range newSettings {
		if !equalValue(v, oldSettings[k]) {
			changed = true
		}
	}
	if changed {
		newSettings["restart"] = true
		if err := r.client.Do(ctx, "PUT", "/networks/"+id, newSettings, nil); err != nil {
			resp.Diagnostics.AddError("Cannot update network settings", err.Error())
			return
		}
	}
	if !plan.Autostart.Equal(state.Autostart) {
		if err := r.client.Do(ctx, "PUT", "/networks/"+id+"/autostart", map[string]bool{"autostart": plan.Autostart.ValueBool()}, nil); err != nil {
			resp.Diagnostics.AddError("Cannot set autostart", err.Error())
			return
		}
	}

	// Reconcile DHCP reservations by MAC: delete removed, then modify / add.
	old := map[string]dhcpHostModel{}
	for _, h := range state.DHCPHosts {
		old[strings.ToLower(h.MAC.ValueString())] = h
	}
	wanted := map[string]dhcpHostModel{}
	for _, h := range plan.DHCPHosts {
		wanted[strings.ToLower(h.MAC.ValueString())] = h
	}
	for mac := range old {
		if _, keep := wanted[mac]; !keep {
			if err := r.client.Do(ctx, "DELETE", "/networks/"+id+"/hosts/"+url.PathEscape(mac), nil, nil); err != nil && !IsNotFound(err) {
				resp.Diagnostics.AddError("Cannot remove DHCP reservation "+mac, err.Error())
				return
			}
		}
	}
	for mac, h := range wanted {
		prev, exists := old[mac]
		var err error
		switch {
		case !exists:
			err = r.client.Do(ctx, "POST", "/networks/"+id+"/hosts", hostBody(h), nil)
		case !prev.IP.Equal(h.IP) || !prev.Name.Equal(h.Name):
			err = r.client.Do(ctx, "PUT", "/networks/"+id+"/hosts/"+url.PathEscape(mac), hostBody(h), nil)
		}
		if err != nil {
			resp.Diagnostics.AddError("Cannot set DHCP reservation "+mac, err.Error())
			return
		}
	}

	r.readInto(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func equalValue(a, b any) bool {
	pa, okA := a.(*string)
	pb, okB := b.(*string)
	if okA && okB {
		return (pa == nil && pb == nil) || (pa != nil && pb != nil && *pa == *pb)
	}
	return a == b
}

func (r *networkResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state networkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Do(ctx, "DELETE", "/networks/"+state.ID.ValueString(), nil, nil); err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete network", err.Error())
	}
}

func (r *networkResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
