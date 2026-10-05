package provider

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	dschema "github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---------------------------------------------------------------- pull secret

var _ resource.ResourceWithConfigure = &pullSecretResource{}

type pullSecretResource struct{ client *Client }

type pullSecretModel struct {
	ID         types.String `tfsdk:"id"`
	Content    types.String `tfsdk:"content"`
	Path       types.String `tfsdk:"path"`
	Registries types.List   `tfsdk:"registries"`
	Source     types.String `tfsdk:"source"`
}

type apiPullSecret struct {
	Configured bool     `json:"configured"`
	Registries []string `json:"registries"`
	Source     *string  `json:"source"`
}

func NewPullSecretResource() resource.Resource { return &pullSecretResource{} }

func (r *pullSecretResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_openshift_pull_secret"
}

func (r *pullSecretResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "The OpenShift pull secret stored on the server (one per server, needed by vmmanager_cluster type = \"openshift\"). " +
			"Give `content` (e.g. file(\"~/pull-secret.json\")) or `path`, a file on the server host. The server keeps it " +
			"mode 0600 and never returns it. Destroying the resource deletes it from the server.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"content": schema.StringAttribute{Optional: true, Sensitive: true,
				Description: "Pull secret JSON (console.redhat.com/openshift/install/pull-secret)."},
			"path": schema.StringAttribute{Optional: true,
				Description: "Path of the pull secret on the server host (read by the server, e.g. ~/pull-secret.json)."},
			"registries": schema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "Registries the secret has credentials for."},
			"source":     schema.StringAttribute{Computed: true, Description: "\"pasted\" or the path it was read from."},
		},
	}
}

func (r *pullSecretResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *pullSecretResource) put(ctx context.Context, m *pullSecretModel, d diags) {
	body := map[string]any{"content": strPtr(m.Content), "path": strPtr(m.Path)}
	if body["content"] == (*string)(nil) && body["path"] == (*string)(nil) {
		d.AddError("Missing pull secret", "set content or path")
		return
	}
	var out apiPullSecret
	if err := r.client.Do(ctx, "PUT", "/openshift/pull-secret", body, &out); err != nil {
		d.AddError("Cannot store the pull secret", err.Error())
		return
	}
	r.fill(ctx, m, &out)
}

func (r *pullSecretResource) fill(ctx context.Context, m *pullSecretModel, out *apiPullSecret) {
	m.ID = types.StringValue("pull-secret")
	m.Registries, _ = types.ListValueFrom(ctx, types.StringType, out.Registries)
	m.Source = strOrNull(out.Source)
}

func (r *pullSecretResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan pullSecretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *pullSecretResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state pullSecretModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out apiPullSecret
	if err := r.client.Do(ctx, "GET", "/openshift/pull-secret", nil, &out); err != nil {
		resp.Diagnostics.AddError("Cannot read the pull secret status", err.Error())
		return
	}
	if !out.Configured {
		resp.State.RemoveResource(ctx)
		return
	}
	r.fill(ctx, &state, &out)
	resp.Diagnostics.Append(resp.State.Set(ctx, state)...)
}

func (r *pullSecretResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan pullSecretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *pullSecretResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.client.Do(ctx, "DELETE", "/openshift/pull-secret", nil, nil); err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete the pull secret", err.Error())
	}
}

// ------------------------------------------------------------ release lookup

var _ datasource.DataSourceWithConfigure = &releaseDataSource{}

type releaseDataSource struct{ client *Client }

type releaseModel struct {
	Channel  types.String `tfsdk:"channel"`
	Version  types.String `tfsdk:"version"`
	Payload  types.String `tfsdk:"payload"`
	Versions types.List   `tfsdk:"versions"`
}

type apiRelease struct {
	Version string  `json:"version"`
	Payload *string `json:"payload"`
}

func NewReleaseDataSource() datasource.DataSource { return &releaseDataSource{} }

func (d *releaseDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_openshift_release"
}

func (d *releaseDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = dschema.Schema{
		Description: "OpenShift releases of an update channel (the server asks the upgrade graph): pin a cluster's version.",
		Attributes: map[string]dschema.Attribute{
			"channel":  dschema.StringAttribute{Required: true, Description: "e.g. stable-4.20, fast-4.21, candidate-4.22."},
			"version":  dschema.StringAttribute{Computed: true, Description: "Latest release of the channel."},
			"payload":  dschema.StringAttribute{Computed: true, Description: "Release image of `version`."},
			"versions": dschema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "All releases, newest first."},
		},
	}
}

func (d *releaseDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	client, ok := req.ProviderData.(*Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data", "expected *provider.Client")
		return
	}
	d.client = client
}

func (d *releaseDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m releaseModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out []apiRelease
	if err := d.client.Do(ctx, "GET", "/openshift/versions?channel="+m.Channel.ValueString(), nil, &out); err != nil {
		resp.Diagnostics.AddError("Cannot list OpenShift releases", err.Error())
		return
	}
	if len(out) == 0 {
		resp.Diagnostics.AddError("No release", fmt.Sprintf("channel %s has no release", m.Channel.ValueString()))
		return
	}
	names := make([]string, 0, len(out))
	for _, r := range out {
		names = append(names, r.Version)
	}
	m.Version = types.StringValue(out[0].Version)
	m.Payload = strOrNull(out[0].Payload)
	m.Versions, _ = types.ListValueFrom(ctx, types.StringType, names)
	resp.Diagnostics.Append(resp.State.Set(ctx, m)...)
}
