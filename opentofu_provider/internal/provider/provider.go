package provider

import (
	"context"
	"os"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

const defaultEndpoint = "http://127.0.0.1:8000"

type vmManagerProvider struct {
	version string
}

type providerModel struct {
	Endpoint types.String `tfsdk:"endpoint"`
}

func New(version string) func() provider.Provider {
	return func() provider.Provider { return &vmManagerProvider{version: version} }
}

func (p *vmManagerProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "vmmanager"
	resp.Version = p.version
}

func (p *vmManagerProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manage KVM virtual machines, networks and cloud images through the VM Manager API.",
		Attributes: map[string]schema.Attribute{
			"endpoint": schema.StringAttribute{
				Description: "VM Manager base URL. Defaults to $VMMANAGER_ENDPOINT, then " + defaultEndpoint + ".",
				Optional:    true,
			},
		},
	}
}

func (p *vmManagerProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var config providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	endpoint := defaultEndpoint
	if env := os.Getenv("VMMANAGER_ENDPOINT"); env != "" {
		endpoint = env
	}
	if !config.Endpoint.IsNull() && !config.Endpoint.IsUnknown() {
		endpoint = config.Endpoint.ValueString()
	}
	client := NewClient(endpoint)
	resp.ResourceData = client
	resp.DataSourceData = client
}

func (p *vmManagerProvider) Resources(_ context.Context) []func() resource.Resource {
	return []func() resource.Resource{NewCloudImageResource, NewNetworkResource, NewVMResource, NewClusterResource}
}

func (p *vmManagerProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return nil
}

// configureClient is shared by all resources' Configure methods.
func configureClient(req resource.ConfigureRequest, resp *resource.ConfigureResponse) *Client {
	if req.ProviderData == nil {
		return nil // provider not configured yet (validation phase)
	}
	client, ok := req.ProviderData.(*Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data", "expected *provider.Client")
		return nil
	}
	return client
}

// helpers for optional API values

func strOrNull(s *string) types.String {
	if s == nil || *s == "" {
		return types.StringNull()
	}
	return types.StringValue(*s)
}

func strPtr(v types.String) *string {
	if v.IsNull() || v.IsUnknown() || v.ValueString() == "" {
		return nil
	}
	s := v.ValueString()
	return &s
}
