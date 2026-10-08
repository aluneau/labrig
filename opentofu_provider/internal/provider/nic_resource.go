package provider

import (
	"context"
	"net/url"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &nicResource{}
	_ resource.ResourceWithImportState = &nicResource{}
)

type nicResource struct{ client *Client }

type nicModel struct {
	ID        types.String `tfsdk:"id"`
	VMID      types.String `tfsdk:"vm_id"`
	Network   types.String `tfsdk:"network"`
	Model     types.String `tfsdk:"model"`
	MAC       types.String `tfsdk:"mac"`
	LinkState types.String `tfsdk:"link_state"`
	VF        types.Bool   `tfsdk:"vf"`
	VLAN      types.Int64  `tfsdk:"vlan"`
}

func NewNicResource() resource.Resource { return &nicResource{} }

func (r *nicResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_nic"
}

func (r *nicResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "An extra network interface of a vmmanager_vm, hot-plugged when the VM runs. network and link_state " +
			"change in place (live); model / mac recreate the NIC. On an SR-IOV VF pool network (forward mode hostdev) " +
			"the VM gets a VF of the host instead of an emulated NIC.",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "<vm_id>/<mac>"},
			"vm_id":   schema.StringAttribute{Required: true, PlanModifiers: replace},
			"network": schema.StringAttribute{Required: true, Description: "libvirt network. Changed in place (live reconnect), except to / from a VF pool."},
			"model": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("virtio"), PlanModifiers: replace,
				Description: "virtio, e1000e, igb (Intel 82576: emulated SR-IOV, up to 7 VFs in the guest), e1000, rtl8139. Ignored on VF pools."},
			"mac": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: append(replace, keep...),
				Description: "Fixed MAC; generated if unset."},
			"link_state": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("up"),
				Description: "up, or down (cable unplugged). Changed in place, live."},
			"vlan": schema.Int64Attribute{Optional: true, PlanModifiers: []planmodifier.Int64{int64planmodifier.RequiresReplace()},
				Description: "SR-IOV VF pool networks only: VLAN tag the PF applies to this VF (overrides the pool's). Changing it recreates the NIC."},
			"vf": schema.BoolAttribute{Computed: true, PlanModifiers: []planmodifier.Bool{boolplanmodifier.UseStateForUnknown()}, Description: "True when the NIC is an SR-IOV VF passed through from the host."},
		},
	}
}

func (r *nicResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *nicResource) path(m nicModel) string {
	return "/vms/" + m.VMID.ValueString() + "/nics/" + url.PathEscape(m.MAC.ValueString())
}

func (r *nicResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan nicModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{
		"network":    plan.Network.ValueString(),
		"model":      plan.Model.ValueString(),
		"mac":        strPtr(plan.MAC),
		"link_state": plan.LinkState.ValueString(),
	}
	if !plan.VLAN.IsNull() && !plan.VLAN.IsUnknown() {
		body["vlan"] = plan.VLAN.ValueInt64()
	}
	var change apiDeviceChange
	if err := r.client.Do(ctx, "POST", "/vms/"+plan.VMID.ValueString()+"/nics", body, &change); err != nil {
		resp.Diagnostics.AddError("Cannot add NIC", err.Error())
		return
	}
	if change.Target == nil {
		resp.Diagnostics.AddError("Cannot add NIC", "the API returned no MAC")
		return
	}
	if change.Pending {
		resp.Diagnostics.AddWarning("NIC attached at the next start", change.Message)
	}
	plan.MAC = types.StringValue(strings.ToLower(*change.Target))
	plan.ID = types.StringValue(plan.VMID.ValueString() + "/" + plan.MAC.ValueString())
	if !r.refresh(ctx, &plan, &resp.Diagnostics) && !resp.Diagnostics.HasError() {
		resp.Diagnostics.AddError("Cannot read NIC", "the new NIC is not in the VM")
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

// refresh copies live values into m; returns false if the VM or the NIC no longer exists.
func (r *nicResource) refresh(ctx context.Context, m *nicModel, d diags) bool {
	var vm apiVM
	if err := r.client.Do(ctx, "GET", "/vms/"+m.VMID.ValueString(), nil, &vm); err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read VM", err.Error())
		return true
	}
	for _, nic := range vm.Nics {
		if nic.MAC == nil || !strings.EqualFold(*nic.MAC, m.MAC.ValueString()) {
			continue
		}
		if nic.Pending != nil && *nic.Pending == "detach" {
			return false // being removed: gone at the next shutdown
		}
		m.Network = strOrNull(nic.Network)
		m.VF = types.BoolValue(nic.VF)
		if nic.VLAN != nil {
			m.VLAN = types.Int64Value(*nic.VLAN)
		} else {
			m.VLAN = types.Int64Null()
		}
		if nic.Model != nil {
			m.Model = types.StringValue(*nic.Model)
		} else if m.Model.IsNull() || m.Model.IsUnknown() {
			m.Model = types.StringValue("virtio") // VF: no model, keep the configured value
		}
		if nic.LinkState != nil {
			m.LinkState = types.StringValue(*nic.LinkState)
		}
		return true
	}
	return false
}

func (r *nicResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state nicModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !r.refresh(ctx, &state, &resp.Diagnostics) {
		if !resp.Diagnostics.HasError() {
			resp.State.RemoveResource(ctx)
		}
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

func (r *nicResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state nicModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	plan.ID, plan.MAC, plan.VF, plan.VLAN = state.ID, state.MAC, state.VF, state.VLAN
	body := map[string]any{}
	if !plan.Network.Equal(state.Network) {
		body["network"] = plan.Network.ValueString()
	}
	if !plan.LinkState.Equal(state.LinkState) {
		body["link_state"] = plan.LinkState.ValueString()
	}
	if len(body) > 0 {
		var change apiDeviceChange
		if err := r.client.Do(ctx, "PUT", r.path(state), body, &change); err != nil {
			resp.Diagnostics.AddError("Cannot update NIC", err.Error())
			return
		}
		if change.Pending {
			resp.Diagnostics.AddWarning("NIC change applies at the next start", change.Message)
		}
	}
	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *nicResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state nicModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var change apiDeviceChange
	err := r.client.Do(ctx, "DELETE", r.path(state), nil, &change)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot remove NIC", err.Error())
		return
	}
	if err == nil && change.Pending {
		resp.Diagnostics.AddWarning("NIC still plugged in the running VM", change.Message)
	}
}

// ImportState takes "<vm_id>/<mac>", e.g. "12/52:54:00:12:34:56".
func (r *nicResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	vmID, mac, ok := strings.Cut(req.ID, "/")
	if !ok || vmID == "" || mac == "" {
		resp.Diagnostics.AddError("Invalid import id", "expected <vm_id>/<mac>, e.g. 12/52:54:00:12:34:56")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("vm_id"), vmID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("mac"), strings.ToLower(mac))...)
}
