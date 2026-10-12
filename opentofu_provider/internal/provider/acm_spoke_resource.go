package provider

import (
	"context"
	"fmt"
	"strconv"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.ResourceWithConfigure = &acmSpokeResource{}

// acmSpokeResource: a single-node OpenShift the ACM hub provisions on a BMC member of its lab group
// (POST /clusters/{hub}/acm/spokes). The spoke is a cluster of the app: cluster_id works wherever a cluster id does.
type acmSpokeResource struct{ client *Client }

type acmSpokeModel struct {
	ID         types.String `tfsdk:"id"`
	HubID      types.String `tfsdk:"hub_id"`
	Name       types.String `tfsdk:"name"`
	Machine    types.String `tfsdk:"machine"`
	Version    types.String `tfsdk:"version"`
	ClusterID  types.String `tfsdk:"cluster_id"`
	Status     types.String `tfsdk:"status"`
	Kubeconfig types.String `tfsdk:"kubeconfig"`
	ConsoleURL types.String `tfsdk:"console_url"`
}

func NewAcmSpokeResource() resource.Resource { return &acmSpokeResource{} }

func (r *acmSpokeResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_acm_spoke"
}

func (r *acmSpokeResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A single-node OpenShift provisioned by an ACM hub (vmmanager_cluster with openshift.acm = true) on a BMC " +
			"member of the hub's lab group (vmmanager_group member with bmc = true): metal3 boots it from the discovery ISO " +
			"through the emulated BMC, the assisted service installs it, ACM manages it. Create waits for the install; " +
			"destroy deletes it through the hub (the machine stays). Every change recreates it.",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "The spoke's cluster id."},
			"hub_id":  schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "The hub cluster's id."},
			"name":    schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Cluster name (DNS label)."},
			"machine": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "BMC member of the hub's group (16 GiB / 8 vCPU at least)."},
			"version": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: append(replace, keep...),
				Description: "OpenShift release (default: the hub's)."},
			"cluster_id":  schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "Same as id: the spoke is a cluster of the app."},
			"status":      schema.StringAttribute{Computed: true},
			"kubeconfig":  schema.StringAttribute{Computed: true, Sensitive: true, PlanModifiers: keep, Description: "Admin kubeconfig (server = the router's forwarded API port)."},
			"console_url": schema.StringAttribute{Computed: true, PlanModifiers: keep},
		},
	}
}

func (r *acmSpokeResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *acmSpokeResource) clusters() *clusterResource { return &clusterResource{client: r.client} }

func (r *acmSpokeResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan acmSpokeModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{"name": plan.Name.ValueString(), "machine": plan.Machine.ValueString(), "version": strPtr(plan.Version)}
	var c apiCluster
	if err := r.client.Do(ctx, "POST", "/clusters/"+plan.HubID.ValueString()+"/acm/spokes", body, &c); err != nil {
		resp.Diagnostics.AddError("Cannot provision the spoke", err.Error())
		return
	}
	id := strconv.FormatInt(c.ID, 10)
	plan.ID, plan.ClusterID = types.StringValue(id), types.StringValue(id)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...) // tracked even if the install fails
	if err := r.clusters().waitIdle(ctx, id); err != nil {
		resp.Diagnostics.AddError("Spoke install failed", err.Error())
		return
	}
	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *acmSpokeResource) refresh(ctx context.Context, m *acmSpokeModel, d diags) bool {
	cr := r.clusters()
	c, err := cr.get(ctx, m.ID.ValueString())
	if err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read the spoke", err.Error())
		return true
	}
	m.Status = types.StringValue(c.Status)
	m.ConsoleURL = strOrNull(c.ConsoleURL)
	if c.Version != nil {
		m.Version = types.StringValue(*c.Version)
	}
	if c.HasKubeconfig && (m.Kubeconfig.IsNull() || m.Kubeconfig.IsUnknown()) {
		if k, err := cr.kubeconfig(ctx, m.ID.ValueString()); err == nil {
			m.Kubeconfig = types.StringValue(k)
		} else {
			d.AddWarning("No kubeconfig yet", err.Error())
		}
	}
	if m.Kubeconfig.IsUnknown() {
		m.Kubeconfig = types.StringNull()
	}
	if m.ConsoleURL.IsUnknown() {
		m.ConsoleURL = types.StringNull()
	}
	return true
}

func (r *acmSpokeResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state acmSpokeModel
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

func (r *acmSpokeResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan acmSpokeModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *acmSpokeResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state acmSpokeModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Do(ctx, "DELETE", "/clusters/"+state.ID.ValueString(), nil, nil); err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete the spoke", fmt.Sprint(err))
	}
}
