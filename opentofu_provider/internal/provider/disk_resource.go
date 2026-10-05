package provider

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &diskResource{}
	_ resource.ResourceWithImportState = &diskResource{}
	_ resource.ResourceWithModifyPlan  = &diskResource{}
)

const gib = int64(1024 * 1024 * 1024)

type diskResource struct{ client *Client }

type diskModel struct {
	ID     types.String `tfsdk:"id"`
	VMID   types.String `tfsdk:"vm_id"`
	SizeGB types.Int64  `tfsdk:"size_gb"`
	Pool   types.String `tfsdk:"pool"`
	Bus    types.String `tfsdk:"bus"`
	Format types.String `tfsdk:"format"`
	Target types.String `tfsdk:"target"`
	Path   types.String `tfsdk:"path"`
}

func NewDiskResource() resource.Resource { return &diskResource{} }

func (r *diskResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_disk"
}

func (r *diskResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "An extra disk attached to a vmmanager_vm (volume <vm>-disk<N>), hot-plugged when the VM runs. " +
			"size_gb can grow in place (live); destroy detaches the disk and deletes its volume.",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "<vm_id>/<target>"},
			"vm_id":   schema.StringAttribute{Required: true, PlanModifiers: replace},
			"size_gb": schema.Int64Attribute{Required: true, Description: "GiB. Can only grow (in place)."},
			"pool": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: append(replace, keep...),
				Description: "Storage pool (default: the server's default pool)."},
			"bus": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("virtio"), PlanModifiers: replace,
				Description: "virtio (hot-plug) or sata (attached at the next start when running)."},
			"format": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("qcow2"), PlanModifiers: replace,
				Description: "qcow2 or raw."},
			"target": schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "Device name in the VM: vdb, vdc, ..."},
			"path":   schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "Volume path."},
		},
	}
}

func (r *diskResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

// ModifyPlan refuses shrinking (it would need recreating the disk, losing its data).
func (r *diskResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	if req.State.Raw.IsNull() || req.Plan.Raw.IsNull() {
		return
	}
	var plan, state diskModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() || plan.SizeGB.IsUnknown() {
		return
	}
	if plan.SizeGB.ValueInt64() < state.SizeGB.ValueInt64() {
		resp.Diagnostics.AddAttributeError(path.Root("size_gb"), "Disks can only grow",
			fmt.Sprintf("size_gb %d is smaller than the current %d GiB.", plan.SizeGB.ValueInt64(), state.SizeGB.ValueInt64()))
	}
}

func (r *diskResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan diskModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{
		"size_gb": plan.SizeGB.ValueInt64(),
		"pool":    strPtr(plan.Pool),
		"bus":     plan.Bus.ValueString(),
		"format":  plan.Format.ValueString(),
	}
	var change apiDeviceChange
	if err := r.client.Do(ctx, "POST", "/vms/"+plan.VMID.ValueString()+"/disks", body, &change); err != nil {
		resp.Diagnostics.AddError("Cannot add disk", err.Error())
		return
	}
	if change.Target == nil {
		resp.Diagnostics.AddError("Cannot add disk", "the API returned no target")
		return
	}
	if change.Pending {
		resp.Diagnostics.AddWarning("Disk attached at the next start", change.Message)
	}
	plan.Target = types.StringValue(*change.Target)
	plan.ID = types.StringValue(plan.VMID.ValueString() + "/" + *change.Target)
	if !r.refresh(ctx, &plan, &resp.Diagnostics) && !resp.Diagnostics.HasError() {
		resp.Diagnostics.AddError("Cannot read disk", "the new disk is not in the VM")
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

// refresh copies live values into m; returns false if the VM or the disk no longer exists.
func (r *diskResource) refresh(ctx context.Context, m *diskModel, d diags) bool {
	var vm apiVM
	if err := r.client.Do(ctx, "GET", "/vms/"+m.VMID.ValueString(), nil, &vm); err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read VM", err.Error())
		return true
	}
	for _, disk := range vm.Disks {
		if disk.Target == nil || *disk.Target != m.Target.ValueString() || disk.Device == nil || *disk.Device != "disk" {
			continue
		}
		if disk.Pending != nil && *disk.Pending == "detach" {
			return false // being detached: gone at the next shutdown
		}
		if disk.Capacity != nil {
			m.SizeGB = types.Int64Value((*disk.Capacity + gib - 1) / gib)
		}
		m.Path = strOrNull(disk.Path)
		if disk.Bus != nil {
			m.Bus = types.StringValue(*disk.Bus)
		}
		if disk.Format != nil {
			m.Format = types.StringValue(*disk.Format)
		}
		if m.Pool.IsNull() || m.Pool.IsUnknown() {
			m.Pool = types.StringNull()
		}
		return true
	}
	return false
}

func (r *diskResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state diskModel
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

func (r *diskResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state diskModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	plan.ID, plan.Target, plan.Path, plan.Pool = state.ID, state.Target, state.Path, state.Pool
	if !plan.SizeGB.Equal(state.SizeGB) {
		err := r.client.Do(ctx, "PUT", "/vms/"+state.VMID.ValueString()+"/disks/"+state.Target.ValueString(),
			map[string]any{"size_gb": plan.SizeGB.ValueInt64()}, nil)
		if err != nil {
			resp.Diagnostics.AddError("Cannot resize disk", err.Error())
			return
		}
	}
	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *diskResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state diskModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var change apiDeviceChange
	err := r.client.Do(ctx, "DELETE", "/vms/"+state.VMID.ValueString()+"/disks/"+state.Target.ValueString()+"?delete_volume=true", nil, &change)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot detach disk", err.Error())
		return
	}
	if err == nil && change.Pending {
		resp.Diagnostics.AddWarning("Disk still plugged in the running VM", change.Message)
	}
}

// ImportState takes "<vm_id>/<target>", e.g. "12/vdb".
func (r *diskResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	vmID, target, ok := strings.Cut(req.ID, "/")
	if !ok || vmID == "" || target == "" {
		resp.Diagnostics.AddError("Invalid import id", "expected <vm_id>/<target>, e.g. 12/vdb")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("vm_id"), vmID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("target"), target)...)
}
