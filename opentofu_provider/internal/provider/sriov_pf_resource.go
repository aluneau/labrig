package provider

import (
	"context"
	"net/url"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &sriovPFResource{}
	_ resource.ResourceWithImportState = &sriovPFResource{}
)

// vmmanager_sriov_pf: host-level SR-IOV settings of one physical function (VF count, VF trust /
// spoof checking on every VF, kept across host reboots), applied by the app's privileged helper.
type sriovPFResource struct{ client *Client }

type sriovPFModel struct {
	ID         types.String `tfsdk:"id"`
	Name       types.String `tfsdk:"name"`
	NumVFs     types.Int64  `tfsdk:"num_vfs"`
	Trust      types.Bool   `tfsdk:"trust"`
	Spoofchk   types.Bool   `tfsdk:"spoofchk"`
	Persistent types.Bool   `tfsdk:"persistent"`
	PCI        types.String `tfsdk:"pci"`
	Driver     types.String `tfsdk:"driver"`
	TotalVFs   types.Int64  `tfsdk:"total_vfs"`
}

type apiSriovPF struct {
	Name       string  `json:"name"`
	PCI        *string `json:"pci"`
	Driver     *string `json:"driver"`
	TotalVFs   int64   `json:"total_vfs"`
	NumVFs     int64   `json:"num_vfs"`
	Persistent bool    `json:"persistent"`
	Trust      *bool   `json:"trust"`
	Spoofchk   *bool   `json:"spoofchk"`
}

type apiSriovStatus struct {
	PFs []apiSriovPF `json:"pfs"`
}

func NewSriovPFResource() resource.Resource { return &sriovPFResource{} }

func (r *sriovPFResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_sriov_pf"
}

func (r *sriovPFResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "SR-IOV settings of a host physical function (GET /api/v1/hosts/sriov lists them): number of VFs, VF " +
			"trust / spoof checking (applied to every VF) and whether they are restored when the host reboots. Changing " +
			"num_vfs is refused while one of the PF's VFs is passed through to a running VM. Destroy sets 0 VFs and turns " +
			"persistence off. Use it with a vmmanager_network in mode \"hostdev\" (VF pool) on the same PF.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"name": schema.StringAttribute{Required: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}, Description: "PF network interface, e.g. ens1f0."},
			"num_vfs": schema.Int64Attribute{Required: true,
				Description: "Number of VFs (0 to total_vfs)."},
			"trust": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "VF trust: the guest may change its MAC and use promiscuous / all-multicast (bonding, OpenShift)."},
			"spoofchk": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true),
				Description: "MAC anti-spoofing on every VF; false for bonding / failover MAC moves."},
			"persistent": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Restore the VF count and options at host boot (vm-manager-sriov.service)."},
			"pci":       schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"driver":    schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"total_vfs": schema.Int64Attribute{Computed: true, PlanModifiers: []planmodifier.Int64{int64planmodifier.UseStateForUnknown()}, Description: "Firmware limit (sriov_totalvfs)."},
		},
	}
}

func (r *sriovPFResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *sriovPFResource) put(ctx context.Context, name string, body map[string]any, d diags) bool {
	if err := r.client.Do(ctx, "PUT", "/hosts/sriov/"+url.PathEscape(name), body, nil); err != nil {
		d.AddError("Cannot update SR-IOV PF "+name, err.Error())
		return false
	}
	return true
}

// readInto refreshes m from GET /hosts/sriov; returns false when the PF is gone.
func (r *sriovPFResource) readInto(ctx context.Context, m *sriovPFModel, d diags) bool {
	var status apiSriovStatus
	if err := r.client.Do(ctx, "GET", "/hosts/sriov", nil, &status); err != nil {
		d.AddError("Cannot read SR-IOV status", err.Error())
		return true
	}
	for _, pf := range status.PFs {
		if pf.Name != m.Name.ValueString() {
			continue
		}
		m.ID = types.StringValue(pf.Name)
		m.NumVFs = types.Int64Value(pf.NumVFs)
		m.TotalVFs = types.Int64Value(pf.TotalVFs)
		m.PCI = strOrNull(pf.PCI)
		m.Driver = strOrNull(pf.Driver)
		m.Persistent = types.BoolValue(pf.Persistent)
		m.Trust = types.BoolValue(pf.Trust != nil && *pf.Trust)
		m.Spoofchk = types.BoolValue(pf.Spoofchk == nil || *pf.Spoofchk)
		return true
	}
	return false
}

func (r *sriovPFResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan sriovPFModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{
		"num_vfs": plan.NumVFs.ValueInt64(), "trust": plan.Trust.ValueBool(), "spoofchk": plan.Spoofchk.ValueBool(),
		"persistent": plan.Persistent.ValueBool(),
	}
	if !r.put(ctx, plan.Name.ValueString(), body, &resp.Diagnostics) {
		return
	}
	if !r.readInto(ctx, &plan, &resp.Diagnostics) && !resp.Diagnostics.HasError() {
		resp.Diagnostics.AddError("SR-IOV PF not found", plan.Name.ValueString()+" is not an SR-IOV interface of the host")
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *sriovPFResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state sriovPFModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !r.readInto(ctx, &state, &resp.Diagnostics) {
		if !resp.Diagnostics.HasError() {
			resp.State.RemoveResource(ctx)
		}
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

func (r *sriovPFResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state sriovPFModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{}
	if !plan.NumVFs.Equal(state.NumVFs) {
		body["num_vfs"] = plan.NumVFs.ValueInt64()
	}
	if !plan.Trust.Equal(state.Trust) || !plan.Spoofchk.Equal(state.Spoofchk) {
		body["trust"], body["spoofchk"] = plan.Trust.ValueBool(), plan.Spoofchk.ValueBool()
	}
	if !plan.Persistent.Equal(state.Persistent) || body["num_vfs"] != nil {
		body["persistent"] = plan.Persistent.ValueBool()
	}
	if len(body) > 0 && !r.put(ctx, plan.Name.ValueString(), body, &resp.Diagnostics) {
		return
	}
	r.readInto(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *sriovPFResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state sriovPFModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.client.Do(ctx, "PUT", "/hosts/sriov/"+url.PathEscape(state.Name.ValueString()),
		map[string]any{"num_vfs": 0, "persistent": false}, nil)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot remove the VFs of "+state.Name.ValueString(), err.Error())
	}
}

// ImportState takes the PF name, e.g. "ens1f0".
func (r *sriovPFResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), req.ID)...)
}
