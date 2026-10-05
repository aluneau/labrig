package provider

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var (
	_ resource.ResourceWithConfigure   = &clusterResource{}
	_ resource.ResourceWithImportState = &clusterResource{}
)

const clusterTimeout = 30 * time.Minute

type clusterResource struct{ client *Client }

type clusterModel struct {
	ID             types.String `tfsdk:"id"`
	Name           types.String `tfsdk:"name"`
	Type           types.String `tfsdk:"type"`
	Version        types.String `tfsdk:"version"`
	Ctlplanes      types.Int64  `tfsdk:"ctlplanes"`
	Workers        types.Int64  `tfsdk:"workers"`
	CtlplaneMemory types.Int64  `tfsdk:"ctlplane_memory"`
	CtlplaneVCPU   types.Int64  `tfsdk:"ctlplane_vcpu"`
	CtlplaneDisk   types.Int64  `tfsdk:"ctlplane_disk_size"`
	WorkerMemory   types.Int64  `tfsdk:"worker_memory"`
	WorkerVCPU     types.Int64  `tfsdk:"worker_vcpu"`
	WorkerDisk     types.Int64  `tfsdk:"worker_disk_size"`
	CloudImageID   types.String `tfsdk:"cloud_image_id"`
	Domain         types.String `tfsdk:"domain"`
	Network        types.String `tfsdk:"network"`
	CIDR           types.String `tfsdk:"cidr"`
	ExtraArgs      types.String `tfsdk:"extra_args"`
	Username       types.String `tfsdk:"username"`
	Password       types.String `tfsdk:"password"`
	SSHKeys        types.List   `tfsdk:"ssh_keys"`
	Running        types.Bool   `tfsdk:"running"`
	Status         types.String `tfsdk:"status"`
	APIHostname    types.String `tfsdk:"api_hostname"`
	APIEndpoint    types.String `tfsdk:"api_endpoint"`
	NodeIPs        types.Map    `tfsdk:"node_ips"`
	Kubeconfig     types.String `tfsdk:"kubeconfig"`
}

type apiClusterNode struct {
	Name  string  `json:"name"`
	Role  string  `json:"role"`
	IP    *string `json:"ip"`
	State string  `json:"state"`
}

type apiCluster struct {
	ID            int64            `json:"id"`
	Name          string           `json:"name"`
	Type          string           `json:"type"`
	Version       *string          `json:"version"`
	Network       string           `json:"network"`
	Domain        string           `json:"domain"`
	APIHostname   string           `json:"api_hostname"`
	APIEndpoint   *string          `json:"api_endpoint"`
	Status        string           `json:"status"`
	StatusMessage *string          `json:"status_message"`
	TaskID        *int64           `json:"task_id"`
	TaskRunning   bool             `json:"task_running"`
	HasKubeconfig bool             `json:"has_kubeconfig"`
	Ctlplanes     int64            `json:"ctlplanes"`
	Workers       int64            `json:"workers"`
	Spec          map[string]any   `json:"spec"`
	Nodes         []apiClusterNode `json:"nodes"`
}

func NewClusterResource() resource.Resource { return &clusterResource{} }

func (r *clusterResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_cluster"
}

func (r *clusterResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replaceStr := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	replaceInt := []planmodifier.Int64{int64planmodifier.RequiresReplace()}
	keepStr := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	replaceKeepStr := []planmodifier.String{stringplanmodifier.RequiresReplace(), stringplanmodifier.UseStateForUnknown()}
	intAttr := func(def int64, desc string) schema.Int64Attribute {
		return schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(def),
			PlanModifiers: replaceInt, Description: desc}
	}
	resp.Schema = schema.Schema{
		Description: "A Kubernetes cluster (k3s for now) made of VMs on its own NAT network (or an existing one). " +
			"Creation waits until every node is Ready. `workers` and `running` change in place; anything else recreates the cluster.",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keepStr},
			"name":    schema.StringAttribute{Required: true, PlanModifiers: replaceStr, Description: "DNS label; nodes are <name>-ctlplane-N / <name>-worker-N."},
			"type":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("k3s"), PlanModifiers: replaceStr, Description: "k3s (kubeadm and openshift: not supported yet)."},
			"version": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "k3s release, e.g. v1.33.5+k3s1. Default: stable channel (the installed version is read back)."},
			"ctlplanes": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1), PlanModifiers: replaceInt,
				Description: "Control planes: 1, or 3/5 with embedded etcd."},
			"workers":            schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(2), Description: "Workers; changed in place (added / drained and removed)."},
			"ctlplane_memory":    intAttr(2048, "MiB."),
			"ctlplane_vcpu":      intAttr(2, ""),
			"ctlplane_disk_size": intAttr(20, "GiB."),
			"worker_memory":      intAttr(2048, "MiB."),
			"worker_vcpu":        intAttr(2, ""),
			"worker_disk_size":   intAttr(20, "GiB."),
			"cloud_image_id":     schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "vmmanager_cloud_image id. Default: Debian 13, else AlmaLinux 9."},
			"domain":             schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("lab"), PlanModifiers: replaceStr, Description: "Base domain: the API is api.<name>.<domain>."},
			"network":            schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "Existing network for the nodes. Default: a new NAT network vmm-k-<name>, deleted with the cluster."},
			"cidr":               schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "Subnet of the new network. Default: first free /24 of the server's CLUSTER_SUBNET_POOL."},
			"extra_args":         schema.StringAttribute{Optional: true, PlanModifiers: replaceStr, Description: "Extra k3s server flags, e.g. \"--disable traefik\"."},
			"username":           schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("admin"), PlanModifiers: replaceStr},
			"password":           schema.StringAttribute{Optional: true, Sensitive: true, PlanModifiers: replaceStr},
			"ssh_keys": schema.ListAttribute{Optional: true, ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{listplanmodifier.RequiresReplace()}},
			"running":      schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Desired power state of all nodes."},
			"status":       schema.StringAttribute{Computed: true},
			"api_hostname": schema.StringAttribute{Computed: true, PlanModifiers: keepStr},
			"api_endpoint": schema.StringAttribute{Computed: true, PlanModifiers: keepStr, Description: "https://<first control plane IP>:6443, reachable from the host."},
			"node_ips":     schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Node name -> IP."},
			"kubeconfig":   schema.StringAttribute{Computed: true, Sensitive: true, PlanModifiers: keepStr, Description: "Admin kubeconfig pointing at api_endpoint."},
		},
	}
}

func (r *clusterResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.client = configureClient(req, resp)
}

func (r *clusterResource) get(ctx context.Context, id string) (*apiCluster, error) {
	var c apiCluster
	if err := r.client.Do(ctx, "GET", "/clusters/"+id, nil, &c); err != nil {
		return nil, err
	}
	return &c, nil
}

// waitIdle polls until no task runs on the cluster; fails if it ends in "error".
func (r *clusterResource) waitIdle(ctx context.Context, id string) error {
	return Poll(ctx, 5*time.Second, clusterTimeout, func() (bool, error) {
		c, err := r.get(ctx, id)
		if err != nil {
			return false, err
		}
		if c.TaskRunning {
			return false, nil
		}
		if c.Status == "error" {
			msg := "unknown error"
			if c.StatusMessage != nil {
				msg = *c.StatusMessage
			}
			return false, fmt.Errorf("cluster %s: %s", c.Name, msg)
		}
		return true, nil
	})
}

func (r *clusterResource) kubeconfig(ctx context.Context, id string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", r.client.Endpoint+"/api/v1/clusters/"+id+"/kubeconfig", nil)
	if err != nil {
		return "", err
	}
	resp, err := r.client.HTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var b strings.Builder
	buf := make([]byte, 32*1024)
	for {
		n, rerr := resp.Body.Read(buf)
		b.Write(buf[:n])
		if rerr != nil {
			break
		}
	}
	if resp.StatusCode >= 400 {
		return "", &APIError{Status: resp.StatusCode, Detail: detail([]byte(b.String()))}
	}
	return b.String(), nil
}

func (r *clusterResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan clusterModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{
		"name":       plan.Name.ValueString(),
		"type":       plan.Type.ValueString(),
		"version":    strPtr(plan.Version),
		"ctlplanes":  plan.Ctlplanes.ValueInt64(),
		"workers":    plan.Workers.ValueInt64(),
		"ctlplane":   map[string]any{"memory": plan.CtlplaneMemory.ValueInt64(), "vcpu": plan.CtlplaneVCPU.ValueInt64(), "disk_size": plan.CtlplaneDisk.ValueInt64()},
		"worker":     map[string]any{"memory": plan.WorkerMemory.ValueInt64(), "vcpu": plan.WorkerVCPU.ValueInt64(), "disk_size": plan.WorkerDisk.ValueInt64()},
		"domain":     plan.Domain.ValueString(),
		"network":    strPtr(plan.Network),
		"cidr":       strPtr(plan.CIDR),
		"extra_args": strPtr(plan.ExtraArgs),
		"username":   strPtr(plan.Username),
		"password":   strPtr(plan.Password),
	}
	if id := strPtr(plan.CloudImageID); id != nil {
		n, err := strconv.ParseInt(*id, 10, 64)
		if err != nil {
			resp.Diagnostics.AddError("Invalid cloud_image_id", err.Error())
			return
		}
		body["cloud_image_id"] = n
	}
	if !plan.SSHKeys.IsNull() && !plan.SSHKeys.IsUnknown() {
		var keys []string
		resp.Diagnostics.Append(plan.SSHKeys.ElementsAs(ctx, &keys, false)...)
		body["ssh_keys"] = keys
	}

	var created apiCluster
	if err := r.client.Do(ctx, "POST", "/clusters", body, &created); err != nil {
		resp.Diagnostics.AddError("Cannot create cluster", err.Error())
		return
	}
	plan.ID = types.StringValue(strconv.FormatInt(created.ID, 10))
	// Saved right away so a failed provisioning can still be destroyed
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), plan.ID)...)

	if err := r.waitIdle(ctx, plan.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Cluster provisioning failed", err.Error())
		return
	}
	if !plan.Running.ValueBool() {
		if err := r.power(ctx, plan.ID.ValueString(), "stop"); err != nil {
			resp.Diagnostics.AddError("Cannot stop cluster", err.Error())
			return
		}
	}
	plan.Kubeconfig = types.StringNull()
	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func (r *clusterResource) power(ctx context.Context, id, action string) error {
	if err := r.client.Do(ctx, "POST", "/clusters/"+id+"/"+action, nil, nil); err != nil {
		return err
	}
	return r.waitIdle(ctx, id)
}

// refresh copies live values into m; returns false if the cluster no longer exists.
func (r *clusterResource) refresh(ctx context.Context, m *clusterModel, d diags) bool {
	c, err := r.get(ctx, m.ID.ValueString())
	if err != nil {
		if IsNotFound(err) {
			return false
		}
		d.AddError("Cannot read cluster", err.Error())
		return true
	}
	m.Name = types.StringValue(c.Name)
	m.Type = types.StringValue(c.Type)
	m.Version = strOrNull(c.Version)
	m.Ctlplanes = types.Int64Value(c.Ctlplanes)
	m.Workers = types.Int64Value(c.Workers)
	m.Domain = types.StringValue(c.Domain)
	m.Network = types.StringValue(c.Network)
	if cidr, ok := c.Spec["cidr"].(string); ok && cidr != "" {
		m.CIDR = types.StringValue(cidr)
	}
	if img, ok := c.Spec["cloud_image_id"].(float64); ok {
		m.CloudImageID = types.StringValue(strconv.FormatInt(int64(img), 10))
	}
	m.Status = types.StringValue(c.Status)
	m.Running = types.BoolValue(c.Status != "stopped" && c.Status != "stopping")
	m.APIHostname = types.StringValue(c.APIHostname)
	m.APIEndpoint = strOrNull(c.APIEndpoint)
	ips := map[string]string{}
	for _, n := range c.Nodes {
		if n.IP != nil {
			ips[n.Name] = *n.IP
		}
	}
	m.NodeIPs, _ = types.MapValueFrom(ctx, types.StringType, ips)
	if (m.Kubeconfig.IsNull() || m.Kubeconfig.IsUnknown()) && c.HasKubeconfig {
		kc, err := r.kubeconfig(ctx, m.ID.ValueString())
		if err != nil {
			d.AddWarning("Cannot fetch kubeconfig", err.Error())
			m.Kubeconfig = types.StringNull()
		} else {
			m.Kubeconfig = types.StringValue(kc)
		}
	} else if m.Kubeconfig.IsUnknown() {
		m.Kubeconfig = types.StringNull()
	}
	return true
}

func (r *clusterResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state clusterModel
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

func (r *clusterResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state clusterModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := state.ID.ValueString()
	plan.ID = state.ID
	plan.Kubeconfig = state.Kubeconfig

	if plan.Running.ValueBool() && !state.Running.ValueBool() {
		if err := r.power(ctx, id, "start"); err != nil {
			resp.Diagnostics.AddError("Cannot start cluster", err.Error())
			return
		}
	}

	want, have := plan.Workers.ValueInt64(), state.Workers.ValueInt64()
	if want > have {
		if err := r.client.Do(ctx, "POST", "/clusters/"+id+"/workers", map[string]any{"count": want - have}, nil); err != nil {
			resp.Diagnostics.AddError("Cannot add workers", err.Error())
			return
		}
		if err := r.waitIdle(ctx, id); err != nil {
			resp.Diagnostics.AddError("Adding workers failed", err.Error())
			return
		}
	} else if want < have {
		c, err := r.get(ctx, id)
		if err != nil {
			resp.Diagnostics.AddError("Cannot read cluster", err.Error())
			return
		}
		// Remove the highest-numbered workers first
		workers := []string{}
		for _, n := range c.Nodes {
			if n.Role == "worker" {
				workers = append(workers, n.Name)
			}
		}
		sort.Slice(workers, func(i, j int) bool { return nodeIndex(workers[i]) > nodeIndex(workers[j]) })
		for _, name := range workers[:have-want] {
			if err := r.client.Do(ctx, "DELETE", "/clusters/"+id+"/nodes/"+name, nil, nil); err != nil {
				resp.Diagnostics.AddError("Cannot remove worker "+name, err.Error())
				return
			}
			if err := r.waitIdle(ctx, id); err != nil {
				resp.Diagnostics.AddError("Removing worker "+name+" failed", err.Error())
				return
			}
		}
	}

	if !plan.Running.ValueBool() && state.Running.ValueBool() {
		if err := r.power(ctx, id, "stop"); err != nil {
			resp.Diagnostics.AddError("Cannot stop cluster", err.Error())
			return
		}
	}

	r.refresh(ctx, &plan, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, plan)...)
}

func nodeIndex(name string) int {
	i, err := strconv.Atoi(name[strings.LastIndex(name, "-")+1:])
	if err != nil {
		return -1
	}
	return i
}

func (r *clusterResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state clusterModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.client.Do(ctx, "DELETE", "/clusters/"+state.ID.ValueString(), nil, nil)
	if err != nil && !IsNotFound(err) {
		resp.Diagnostics.AddError("Cannot delete cluster", err.Error())
	}
}

func (r *clusterResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
