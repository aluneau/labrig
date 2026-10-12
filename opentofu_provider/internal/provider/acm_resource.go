package provider

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.ResourceWithConfigure = &acmManagedClusterResource{}

// acmManagedClusterResource: another cluster of the app imported into an ACM hub (POST /clusters/{hub}/acm/managed)
type acmManagedClusterResource struct{ client *Client }

type acmManagedClusterModel struct {
	ID        types.String `tfsdk:"id"`
	HubID     types.String `tfsdk:"hub_id"`
	ClusterID types.String `tfsdk:"cluster_id"`
	Method    types.String `tfsdk:"method"`
	Name      types.String `tfsdk:"name"`
	State     types.String `tfsdk:"state"`
	Used      types.String `tfsdk:"import_method"`
}

type apiAcmManaged struct {
	Name      string  `json:"name"`
	ClusterID *int64  `json:"cluster_id"`
	State     string  `json:"state"`
	Method    *string `json:"method"`
	Message   *string `json:"message"`
}

type apiAcmStatus struct {
	Enabled bool            `json:"enabled"`
	Managed []apiAcmManaged `json:"managed"`
}

const acmImportTimeout = 30 * time.Minute

func NewAcmManagedClusterResource() resource.Resource { return &acmManagedClusterResource{} }

func (r *acmManagedClusterResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_acm_managed_cluster"
}

func (r *acmManagedClusterResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A cluster of the app (kubeadm, k3s, OpenShift) imported into an ACM hub (vmmanager_cluster type openshift " +
			"with openshift.acm = true). Create waits until the ManagedCluster is Joined and Available; destroy detaches it " +
			"(the hub removes the klusterlet). Every change recreates it.",
		Attributes: map[string]schema.Attribute{
			"id":         schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "<hub_id>/<name>"},
			"hub_id":     schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "The hub cluster's id."},
			"cluster_id": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "The cluster to import."},
			"method": schema.StringAttribute{Optional: true, PlanModifiers: replace,
				Description: "auto (default: auto-import-secret when the hub reaches the cluster's API, else the app applies the import manifests), secret or manifests."},
			"name":          schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "ManagedCluster name (the cluster's name)."},
			"state":         schema.StringAttribute{Computed: true, Description: "available | unavailable | unknown | ..."},
			"import_method": schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "How it was imported: secret or manifests."},
		},
	}
}

func (r *acmManagedClusterResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *acmManagedClusterResource) find(ctx context.Context, hubID, name string, clusterID int64) (*apiAcmManaged, error) {
	var st apiAcmStatus
	if err := r.client.Do(ctx, "GET", "/clusters/"+hubID+"/acm", nil, &st); err != nil {
		return nil, err
	}
	for i, m := range st.Managed {
		if (name != "" && m.Name == name) || (clusterID != 0 && m.ClusterID != nil && *m.ClusterID == clusterID) {
			return &st.Managed[i], nil
		}
	}
	return nil, nil
}

// waitTask waits for the hub's running task (the import / detach) to end
func (r *acmManagedClusterResource) waitTask(ctx context.Context, hubID string) error {
	return Poll(ctx, 10*time.Second, acmImportTimeout, func() (bool, error) {
		var c struct {
			TaskRunning bool `json:"task_running"`
		}
		if err := r.client.Do(ctx, "GET", "/clusters/"+hubID, nil, &c); err != nil {
			return false, err
		}
		return !c.TaskRunning, nil
	})
}

func (r *acmManagedClusterResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan acmManagedClusterModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	hub := plan.HubID.ValueString()
	cid, err := strconv.ParseInt(plan.ClusterID.ValueString(), 10, 64)
	if err != nil {
		resp.Diagnostics.AddError("Invalid cluster_id", err.Error())
		return
	}
	method := "auto"
	if v := strPtr(plan.Method); v != nil {
		method = *v
	}
	if err := r.client.Do(ctx, "POST", "/clusters/"+hub+"/acm/managed", map[string]any{"cluster_id": cid, "method": method}, nil); err != nil {
		resp.Diagnostics.AddError("Cannot import the cluster", err.Error())
		return
	}
	if err := r.waitTask(ctx, hub); err != nil {
		resp.Diagnostics.AddError("Import did not finish", err.Error())
		return
	}
	m, err := r.find(ctx, hub, "", cid)
	if err != nil || m == nil {
		resp.Diagnostics.AddError("Import failed", fmt.Sprintf("no managed cluster entry on hub %s (%v)", hub, err))
		return
	}
	if m.State != "available" {
		msg := m.State
		if m.Message != nil {
			msg += ": " + *m.Message
		}
		resp.Diagnostics.AddError("Import failed", msg)
		return
	}
	plan.ID = types.StringValue(hub + "/" + m.Name)
	plan.Name = types.StringValue(m.Name)
	plan.State = types.StringValue(m.State)
	plan.Used = strOrNull(m.Method)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *acmManagedClusterResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state acmManagedClusterModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	m, err := r.find(ctx, state.HubID.ValueString(), state.Name.ValueString(), 0)
	if err != nil {
		if IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Cannot read the hub's managed clusters", err.Error())
		return
	}
	if m == nil {
		resp.State.RemoveResource(ctx)
		return
	}
	state.State = types.StringValue(m.State)
	if m.Method != nil {
		state.Used = types.StringValue(*m.Method)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

// Update is never called with a change (every attribute recreates it)
func (r *acmManagedClusterResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan acmManagedClusterModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *acmManagedClusterResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state acmManagedClusterModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	hub := state.HubID.ValueString()
	err := r.client.Do(ctx, "DELETE", "/clusters/"+hub+"/acm/managed/"+url.PathEscape(state.Name.ValueString()), nil, nil)
	if err != nil {
		if !IsNotFound(err) {
			resp.Diagnostics.AddError("Cannot detach the cluster", err.Error())
		}
		return
	}
	if err := r.waitTask(ctx, hub); err != nil {
		resp.Diagnostics.AddError("Detach did not finish", err.Error())
		return
	}
	if m, err := r.find(ctx, hub, state.Name.ValueString(), 0); err == nil && m != nil {
		msg := m.State
		if m.Message != nil {
			msg += ": " + *m.Message
		}
		resp.Diagnostics.AddError("Detach failed", msg)
	}
}
