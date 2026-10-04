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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &cloudImageResource{}
	_ resource.ResourceWithImportState = &cloudImageResource{}
)

type cloudImageResource struct{ client *Client }

type cloudImageModel struct {
	ID            types.String `tfsdk:"id"`
	Distribution  types.String `tfsdk:"distribution"`
	Version       types.String `tfsdk:"version"`
	URL           types.String `tfsdk:"url"`
	Path          types.String `tfsdk:"path"`
	Size          types.Int64  `tfsdk:"size"`
	KeepOnDestroy types.Bool   `tfsdk:"keep_on_destroy"`
}

func NewCloudImageResource() resource.Resource { return &cloudImageResource{} }

func (r *cloudImageResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_cloud_image"
}

func (r *cloudImageResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	keep := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A cloud image (qcow2 with cloud-init) downloaded into the default storage pool. " +
			"An image that is already downloaded is adopted instead of downloaded again.",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true, PlanModifiers: keep},
			"distribution": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "ubuntu, debian, almalinux, rocky, centos, or any name with a custom url."},
			"version":      schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "e.g. 24.04, 13, 9, 10-stream."},
			"url": schema.StringAttribute{
				Optional: true, Computed: true, Description: "Download URL; defaults to the known URL for distribution/version.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace(), stringplanmodifier.UseStateForUnknown()},
			},
			"path": schema.StringAttribute{Computed: true, PlanModifiers: keep, Description: "Volume path on the host."},
			"size": schema.Int64Attribute{Computed: true, PlanModifiers: []planmodifier.Int64{int64planmodifier.UseStateForUnknown()}},
			"keep_on_destroy": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Only forget the image on destroy (it stays downloaded for other VMs and the web UI).",
			},
		},
	}
}

func (r *cloudImageResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *cloudImageResource) find(ctx context.Context, match func(apiCloudImage) bool) (*apiCloudImage, error) {
	var images []apiCloudImage
	if err := r.client.Do(ctx, "GET", "/storage/cloud-images", nil, &images); err != nil {
		return nil, err
	}
	for _, img := range images {
		if match(img) {
			return &img, nil
		}
	}
	return nil, nil
}

func (r *cloudImageResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan cloudImageModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	dist, ver := plan.Distribution.ValueString(), plan.Version.ValueString()
	same := func(img apiCloudImage) bool { return img.Distribution == dist && img.Version == ver }

	existing, err := r.find(ctx, func(img apiCloudImage) bool { return same(img) && img.Status == "ready" })
	if err != nil {
		resp.Diagnostics.AddError("Cannot list cloud images", err.Error())
		return
	}
	if existing == nil {
		body := map[string]any{"distribution": dist, "version": ver}
		if url := strPtr(plan.URL); url != nil {
			body["url"] = *url
		}
		var task apiTask
		if err := r.client.Do(ctx, "POST", "/storage/cloud-images", body, &task); err != nil {
			resp.Diagnostics.AddError("Cannot start cloud image download", err.Error())
			return
		}
		err = Poll(ctx, 3*time.Second, 60*time.Minute, func() (bool, error) {
			img, err := r.find(ctx, same)
			if err != nil || img == nil {
				return false, err
			}
			switch img.Status {
			case "ready":
				existing = img
				return true, nil
			case "error":
				var t apiTask
				_ = r.client.Do(ctx, "GET", fmt.Sprintf("/tasks/%d", task.ID), nil, &t)
				msg := "download failed"
				if t.ErrorMessage != nil {
					msg = *t.ErrorMessage
				}
				return false, fmt.Errorf("%s", msg)
			}
			return false, nil
		})
		if err != nil {
			resp.Diagnostics.AddError("Cloud image download failed", err.Error())
			return
		}
	}
	m := imageModel(*existing)
	m.KeepOnDestroy = plan.KeepOnDestroy
	if existing.URL == "" {
		m.URL = types.StringValue(plan.URL.ValueString())
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, m)...)
}

func imageModel(img apiCloudImage) cloudImageModel {
	return cloudImageModel{
		ID:            types.StringValue(strconv.FormatInt(img.ID, 10)),
		Distribution:  types.StringValue(img.Distribution),
		Version:       types.StringValue(img.Version),
		URL:           types.StringValue(img.URL),
		Path:          strOrNull(img.Path),
		Size:          types.Int64Value(img.Size),
		KeepOnDestroy: types.BoolValue(false),
	}
}

func (r *cloudImageResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state cloudImageModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	img, err := r.find(ctx, func(img apiCloudImage) bool { return strconv.FormatInt(img.ID, 10) == state.ID.ValueString() })
	if err != nil {
		resp.Diagnostics.AddError("Cannot read cloud image", err.Error())
		return
	}
	if img == nil || img.Status == "missing" || img.Status == "error" {
		resp.State.RemoveResource(ctx)
		return
	}
	m := imageModel(*img)
	m.KeepOnDestroy = state.KeepOnDestroy
	if img.URL == "" { // image adopted from an existing volume: keep what the config said
		m.URL = state.URL
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, m)...)
}

func (r *cloudImageResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	// Every argument forces replacement, nothing to update in place.
	var plan cloudImageModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *cloudImageResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state cloudImageModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if state.KeepOnDestroy.ValueBool() {
		return
	}
	err := r.client.Do(ctx, "DELETE", "/storage/cloud-images/"+state.ID.ValueString(), nil, nil)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete cloud image", err.Error())
	}
}

func (r *cloudImageResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
