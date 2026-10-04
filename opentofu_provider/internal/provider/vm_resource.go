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
	_ resource.ResourceWithConfigure   = &vmResource{}
	_ resource.ResourceWithImportState = &vmResource{}
)

const ipWaitTimeout = 5 * time.Minute

type vmResource struct{ client *Client }

type cloudInitModel struct {
	Username types.String `tfsdk:"username"`
	Password types.String `tfsdk:"password"`
	SSHKeys  types.List   `tfsdk:"ssh_keys"`
	Keyboard types.String `tfsdk:"keyboard"`
	UserData types.String `tfsdk:"user_data"`
}

type vmModel struct {
	ID           types.String    `tfsdk:"id"`
	UUID         types.String    `tfsdk:"uuid"`
	Name         types.String    `tfsdk:"name"`
	Description  types.String    `tfsdk:"description"`
	Memory       types.Int64     `tfsdk:"memory"`
	VCPU         types.Int64     `tfsdk:"vcpu"`
	DiskSize     types.Int64     `tfsdk:"disk_size"`
	CloudImageID types.String    `tfsdk:"cloud_image_id"`
	ISOPath      types.String    `tfsdk:"iso_path"`
	Network      types.String    `tfsdk:"network"`
	MACAddress   types.String    `tfsdk:"mac_address"`
	Autostart    types.Bool      `tfsdk:"autostart"`
	Running      types.Bool      `tfsdk:"running"`
	WaitForIP    types.Bool      `tfsdk:"wait_for_ip"`
	CloudInit    *cloudInitModel `tfsdk:"cloud_init"`
	Status       types.String    `tfsdk:"status"`
	IPAddresses  types.List      `tfsdk:"ip_addresses"`
	VNCPort      types.Int64     `tfsdk:"vnc_port"`
}

func NewVMResource() resource.Resource { return &vmResource{} }

func (r *vmResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_vm"
}

func (r *vmResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replaceStr := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	replaceInt := []planmodifier.Int64{int64planmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A KVM virtual machine. Hardware and boot source changes recreate the VM (its disks are deleted); " +
			"description, autostart and running are updated in place.",
		Attributes: map[string]schema.Attribute{
			"id":             schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"uuid":           schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"name":           schema.StringAttribute{Required: true, PlanModifiers: replaceStr, Description: "Also the guest hostname with cloud-init."},
			"description":    schema.StringAttribute{Optional: true},
			"memory":         schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(2048), PlanModifiers: replaceInt, Description: "MiB."},
			"vcpu":           schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(2), PlanModifiers: replaceInt},
			"disk_size":      schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(20), PlanModifiers: replaceInt, Description: "GiB. With a cloud image, the copy is grown to this size."},
			"cloud_image_id": schema.StringAttribute{Optional: true, PlanModifiers: replaceStr, Description: "vmmanager_cloud_image id to boot from."},
			"iso_path":       schema.StringAttribute{Optional: true, PlanModifiers: replaceStr, Description: "Install ISO volume path to attach as CD-ROM."},
			"network":        schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("default"), PlanModifiers: replaceStr},
			"mac_address": schema.StringAttribute{
				Optional: true, Computed: true, Description: "Fixed MAC (e.g. matching a DHCP reservation); generated if unset.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace(), stringplanmodifier.UseStateForUnknown()},
			},
			"autostart":   schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Start with the host."},
			"running":     schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Desired power state."},
			"wait_for_ip": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Wait (up to 5 min) for a DHCP address after starting."},
			"cloud_init": schema.SingleNestedAttribute{
				Optional:      true,
				Description:   "cloud-init settings (with cloud_image_id).",
				PlanModifiers: []planmodifier.Object{objectplanmodifier.RequiresReplace()},
				Attributes: map[string]schema.Attribute{
					"username":  schema.StringAttribute{Optional: true, Description: "User created with passwordless sudo."},
					"password":  schema.StringAttribute{Optional: true, Sensitive: true},
					"ssh_keys":  schema.ListAttribute{Optional: true, ElementType: types.StringType},
					"keyboard":  schema.StringAttribute{Optional: true, Description: "Console keyboard layout: us, fr, de, ..."},
					"user_data": schema.StringAttribute{Optional: true, Description: "Raw #cloud-config; replaces the fields above."},
				},
			},
			"status":       schema.StringAttribute{Computed: true},
			"ip_addresses": schema.ListAttribute{Computed: true, ElementType: types.StringType},
			"vnc_port":     schema.Int64Attribute{Computed: true},
		},
	}
}

func (r *vmResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *vmResource) get(ctx context.Context, id string) (*apiVM, error) {
	var vm apiVM
	if err := r.client.Do(ctx, "GET", "/vms/"+id, nil, &vm); err != nil {
		return nil, err
	}
	return &vm, nil
}

func (r *vmResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan vmModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{
		"name":         plan.Name.ValueString(),
		"description":  strPtr(plan.Description),
		"memory":       plan.Memory.ValueInt64(),
		"vcpu":         plan.VCPU.ValueInt64(),
		"disk_size":    plan.DiskSize.ValueInt64(),
		"iso_path":     strPtr(plan.ISOPath),
		"network_name": plan.Network.ValueString(),
		"mac_address":  strPtr(plan.MACAddress),
		"autostart":    plan.Autostart.ValueBool(),
		"start":        plan.Running.ValueBool(),
	}
	if id := strPtr(plan.CloudImageID); id != nil {
		n, err := strconv.ParseInt(*id, 10, 64)
		if err != nil {
			resp.Diagnostics.AddError("Invalid cloud_image_id", err.Error())
			return
		}
		body["cloud_image_id"] = n
	}
	if ci := plan.CloudInit; ci != nil {
		body["cloudinit_username"] = strPtr(ci.Username)
		body["cloudinit_password"] = strPtr(ci.Password)
		body["cloudinit_keyboard"] = strPtr(ci.Keyboard)
		body["cloudinit_userdata"] = strPtr(ci.UserData)
		if !ci.SSHKeys.IsNull() && !ci.SSHKeys.IsUnknown() {
			var keys []string
			resp.Diagnostics.Append(ci.SSHKeys.ElementsAs(ctx, &keys, false)...)
			body["cloudinit_ssh_keys"] = keys
		}
	}

	var created apiVM
	if err := r.client.Do(ctx, "POST", "/vms", body, &created); err != nil {
		resp.Diagnostics.AddError("Cannot create VM", err.Error())
		return
	}
	plan.ID = types.StringValue(strconv.FormatInt(created.ID, 10))
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), plan.ID)...)

	if plan.Running.ValueBool() && plan.WaitForIP.ValueBool() {
		if err := r.waitForIP(ctx, plan.ID.ValueString()); err != nil {
			resp.Diagnostics.AddWarning("VM has no IP address yet", err.Error())
		}
	}
	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *vmResource) waitForIP(ctx context.Context, id string) error {
	return Poll(ctx, 3*time.Second, ipWaitTimeout, func() (bool, error) {
		vm, err := r.get(ctx, id)
		if err != nil {
			return false, err
		}
		for _, iface := range vm.Interfaces {
			if len(iface.Addresses) > 0 {
				return true, nil
			}
		}
		return false, nil
	})
}

// refresh copies live values into m; returns false if the VM no longer exists.
func (r *vmResource) refresh(ctx context.Context, m *vmModel, d diags) bool {
	vm, err := r.get(ctx, m.ID.ValueString())
	if err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read VM", err.Error())
		return true
	}
	m.UUID = types.StringValue(vm.UUID)
	m.Name = types.StringValue(vm.Name)
	m.Description = strOrNull(vm.Description)
	m.Memory = types.Int64Value(vm.Memory)
	m.VCPU = types.Int64Value(vm.VCPU)
	m.Autostart = types.BoolValue(vm.Autostart)
	m.Status = types.StringValue(vm.Status)
	m.Running = types.BoolValue(vm.Status == "running" || vm.Status == "paused" || vm.Status == "blocked")
	if len(vm.Nics) > 0 {
		m.Network = strOrNull(vm.Nics[0].Network)
		m.MACAddress = strOrNull(vm.Nics[0].MAC)
	}
	ips := []string{}
	for _, iface := range vm.Interfaces {
		ips = append(ips, iface.Addresses...)
	}
	m.IPAddresses, _ = types.ListValueFrom(ctx, types.StringType, ips)
	if vm.Console != nil && vm.Console.Port != nil {
		m.VNCPort = types.Int64Value(*vm.Console.Port)
	} else {
		m.VNCPort = types.Int64Null()
	}
	return true
}

func (r *vmResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state vmModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !r.refresh(ctx, &state, &resp.Diagnostics) {
		resp.State.RemoveResource(ctx)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

func (r *vmResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state vmModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := state.ID.ValueString()
	plan.ID = state.ID

	patch := map[string]any{}
	if !plan.Description.Equal(state.Description) {
		patch["description"] = strPtr(plan.Description)
	}
	if !plan.Autostart.Equal(state.Autostart) {
		patch["autostart"] = plan.Autostart.ValueBool()
	}
	if len(patch) > 0 {
		if err := r.client.Do(ctx, "PATCH", "/vms/"+id, patch, nil); err != nil {
			resp.Diagnostics.AddError("Cannot update VM", err.Error())
			return
		}
	}

	if !plan.Running.Equal(state.Running) {
		if plan.Running.ValueBool() {
			if err := r.client.Do(ctx, "POST", "/vms/"+id+"/start", nil, nil); err != nil {
				resp.Diagnostics.AddError("Cannot start VM", err.Error())
				return
			}
			if plan.WaitForIP.ValueBool() {
				if err := r.waitForIP(ctx, id); err != nil {
					resp.Diagnostics.AddWarning("VM has no IP address yet", err.Error())
				}
			}
		} else if err := r.shutdown(ctx, id); err != nil {
			resp.Diagnostics.AddError("Cannot stop VM", err.Error())
			return
		}
	}

	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

// shutdown asks the guest to power off (ACPI) and forces it off after 2 minutes.
func (r *vmResource) shutdown(ctx context.Context, id string) error {
	if err := r.client.Do(ctx, "POST", "/vms/"+id+"/stop", nil, nil); err != nil {
		return err
	}
	err := Poll(ctx, 2*time.Second, 2*time.Minute, func() (bool, error) {
		vm, err := r.get(ctx, id)
		if err != nil {
			return false, err
		}
		return vm.Status == "shutoff", nil
	})
	if err == nil {
		return nil
	}
	if ferr := r.client.Do(ctx, "POST", "/vms/"+id+"/force_stop", nil, nil); ferr != nil {
		return fmt.Errorf("graceful shutdown: %v; force off: %w", err, ferr)
	}
	return nil
}

func (r *vmResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state vmModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.client.Do(ctx, "DELETE", "/vms/"+state.ID.ValueString()+"?delete_disks=true", nil, nil)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete VM", err.Error())
	}
}

func (r *vmResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
