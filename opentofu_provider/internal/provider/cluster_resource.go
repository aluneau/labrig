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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
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

// OpenShift: image build + install + operators (agent-based installer)
const openshiftTimeout = 4 * time.Hour

type clusterResource struct{ client *Client }

type clusterModel struct {
	ID             types.String    `tfsdk:"id"`
	Name           types.String    `tfsdk:"name"`
	Type           types.String    `tfsdk:"type"`
	Version        types.String    `tfsdk:"version"`
	Ctlplanes      types.Int64     `tfsdk:"ctlplanes"`
	Workers        types.Int64     `tfsdk:"workers"`
	CtlplaneMemory types.Int64     `tfsdk:"ctlplane_memory"`
	CtlplaneVCPU   types.Int64     `tfsdk:"ctlplane_vcpu"`
	CtlplaneDisk   types.Int64     `tfsdk:"ctlplane_disk_size"`
	WorkerMemory   types.Int64     `tfsdk:"worker_memory"`
	WorkerVCPU     types.Int64     `tfsdk:"worker_vcpu"`
	WorkerDisk     types.Int64     `tfsdk:"worker_disk_size"`
	CloudImageID   types.String    `tfsdk:"cloud_image_id"`
	Domain         types.String    `tfsdk:"domain"`
	Network        types.String    `tfsdk:"network"`
	GroupID        types.String    `tfsdk:"group_id"`
	RouterMemory   types.Int64     `tfsdk:"router_memory"`
	CIDR           types.String    `tfsdk:"cidr"`
	ExtraArgs      types.String    `tfsdk:"extra_args"`
	Username       types.String    `tfsdk:"username"`
	Password       types.String    `tfsdk:"password"`
	SSHKeys        types.List      `tfsdk:"ssh_keys"`
	Running        types.Bool      `tfsdk:"running"`
	Status         types.String    `tfsdk:"status"`
	APIHostname    types.String    `tfsdk:"api_hostname"`
	APIEndpoint    types.String    `tfsdk:"api_endpoint"`
	NodeIPs        types.Map       `tfsdk:"node_ips"`
	Kubeconfig     types.String    `tfsdk:"kubeconfig"`
	OpenShift      *openshiftModel `tfsdk:"openshift"`
	ConsoleURL     types.String    `tfsdk:"console_url"`
	KubeadminPass  types.String    `tfsdk:"kubeadmin_password"`
}

type openshiftModel struct {
	Channel         types.String `tfsdk:"channel"`
	Topology        types.String `tfsdk:"topology"`
	Storage         types.String `tfsdk:"storage"`
	StorageDiskSize types.Int64  `tfsdk:"storage_disk_size"`
	Operators       types.List   `tfsdk:"operators"`
	SRIOV           types.Bool   `tfsdk:"sriov"`
	SRIOVNics       types.Int64  `tfsdk:"sriov_nics"`
	SRIOVVFs        types.Int64  `tfsdk:"sriov_vfs"`
	SRIOVDeviceType types.String `tfsdk:"sriov_device_type"`
	MetalLB         types.Bool   `tfsdk:"metallb"`
	MetalLBAddrs    types.Int64  `tfsdk:"metallb_addresses"`
	MetalLBDemo     types.Bool   `tfsdk:"metallb_demo"`
	MetalLBMode     types.String `tfsdk:"metallb_mode"`
	DisableUpdates  types.Bool   `tfsdk:"disable_updates"`
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
	GroupID       *int64           `json:"group_id"`
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
	ConsoleURL    *string          `json:"console_url"`
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
	keepReplaceInt := []planmodifier.Int64{int64planmodifier.RequiresReplace(), int64planmodifier.UseStateForUnknown()}
	// Server-side defaults (k3s / kubeadm: 2048 MiB, 2 vCPU, 20 GiB; OpenShift: per topology), read back
	intAttr := func(desc string) schema.Int64Attribute {
		return schema.Int64Attribute{Optional: true, Computed: true, PlanModifiers: keepReplaceInt, Description: desc}
	}
	// openshift block: what the add-on API applies live changes in place (operators, MetalLB),
	// the rest recreates the cluster
	// Optional + computed: unset fields show the server's values (read back), and keep them
	osStr := func(desc string) schema.StringAttribute {
		return schema.StringAttribute{Optional: true, Computed: true, Description: desc,
			PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown(), stringplanmodifier.RequiresReplace()}}
	}
	osInt := func(desc string) schema.Int64Attribute {
		return schema.Int64Attribute{Optional: true, Computed: true, Description: desc,
			PlanModifiers: []planmodifier.Int64{int64planmodifier.UseStateForUnknown(), int64planmodifier.RequiresReplace()}}
	}
	osBool := func(desc string) schema.BoolAttribute {
		return schema.BoolAttribute{Optional: true, Computed: true, Description: desc,
			PlanModifiers: []planmodifier.Bool{boolplanmodifier.UseStateForUnknown(), boolplanmodifier.RequiresReplace()}}
	}
	liveStr := func(desc string) schema.StringAttribute {
		return schema.StringAttribute{Optional: true, Computed: true, Description: desc,
			PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}}
	}
	liveBool := func(desc string) schema.BoolAttribute {
		return schema.BoolAttribute{Optional: true, Computed: true, Description: desc,
			PlanModifiers: []planmodifier.Bool{boolplanmodifier.UseStateForUnknown()}}
	}
	resp.Schema = schema.Schema{
		Description: "A Kubernetes cluster made of VMs. k3s: on its own NAT network (no router) or an existing one. " +
			"kubeadm: inside a lab group (an existing one, group_id, or one created for the cluster and deleted with it) " +
			"whose router serves the DNS records and load-balances the API (haproxy) on its uplink address. " +
			"Creation waits until every node is Ready. `workers` and `running` change in place; anything else recreates the cluster.",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keepStr},
			"name":    schema.StringAttribute{Required: true, PlanModifiers: replaceStr, Description: "DNS label; nodes are <name>-ctlplane-N / <name>-worker-N."},
			"type":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("k3s"), PlanModifiers: replaceStr, Description: "k3s (standalone network), kubeadm or openshift (in a lab group with a router)."},
			"version": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "k3s: release, e.g. v1.33.5+k3s1 (default: stable channel). kubeadm: Kubernetes minor or patch, e.g. v1.37 or v1.37.1 (default: the server's pinned minor). openshift: release, e.g. 4.20.39 (default: latest of openshift.channel). The installed version is read back."},
			"ctlplanes": schema.Int64Attribute{Optional: true, Computed: true, PlanModifiers: keepReplaceInt,
				Description: "Control planes: 1, or 3/5 with embedded etcd (default 1). openshift: set by openshift.topology."},
			"workers": schema.Int64Attribute{Optional: true, Computed: true, PlanModifiers: []planmodifier.Int64{int64planmodifier.UseStateForUnknown()},
				Description: "Workers (default 2; openshift: HA topology only); changed in place for k3s / kubeadm (added / drained and removed)."},
			"ctlplane_memory":    intAttr("MiB."),
			"ctlplane_vcpu":      intAttr(""),
			"ctlplane_disk_size": intAttr("GiB."),
			"worker_memory":      intAttr("MiB."),
			"worker_vcpu":        intAttr(""),
			"worker_disk_size":   intAttr("GiB."),
			"cloud_image_id":     schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "vmmanager_cloud_image id. Default: Debian 13, else AlmaLinux 9."},
			"domain":             schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "Base domain: the API is api.<name>.<domain>. Default: lab; kubeadm in an existing group: the group's domain."},
			"network":            schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "k3s: existing network for the nodes. Default: a new NAT network vmm-k-<name>, deleted with the cluster. kubeadm: the group's network (computed)."},
			"group_id": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr,
				Description: "kubeadm: id of an existing vmmanager_group to put the nodes in (deleting the cluster leaves the group and its members). Default: a group named after the cluster, deleted with it."},
			"router_memory": schema.Int64Attribute{Optional: true, PlanModifiers: replaceInt,
				Description: "kubeadm with an auto-created group: router memory in MiB (default: the server's group default, 512)."},
			"cidr":       schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: replaceKeepStr, Description: "Subnet of the new network. Default: first free /24 of the server's CLUSTER_SUBNET_POOL."},
			"extra_args": schema.StringAttribute{Optional: true, PlanModifiers: replaceStr, Description: "Extra k3s server flags, e.g. \"--disable traefik\"."},
			"username":   schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("admin"), PlanModifiers: replaceStr},
			"password":   schema.StringAttribute{Optional: true, Sensitive: true, PlanModifiers: replaceStr},
			"ssh_keys": schema.ListAttribute{Optional: true, ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{listplanmodifier.RequiresReplace()}},
			"running":            schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Desired power state of all nodes."},
			"status":             schema.StringAttribute{Computed: true},
			"api_hostname":       schema.StringAttribute{Computed: true, PlanModifiers: keepStr},
			"api_endpoint":       schema.StringAttribute{Computed: true, PlanModifiers: keepStr, Description: "Reachable from the host: k3s https://<first control plane IP>:6443, kubeadm https://<group router uplink IP>:<port> (haproxy in front of every control plane)."},
			"node_ips":           schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Node name -> IP."},
			"kubeconfig":         schema.StringAttribute{Computed: true, Sensitive: true, PlanModifiers: keepStr, Description: "Admin kubeconfig pointing at api_endpoint (openshift: with tls-server-name = api_hostname)."},
			"console_url":        schema.StringAttribute{Computed: true, PlanModifiers: keepStr, Description: "openshift: web console (resolvable through the group router's DNS, e.g. over WireGuard)."},
			"kubeadmin_password": schema.StringAttribute{Computed: true, Sensitive: true, PlanModifiers: keepStr, Description: "openshift: kubeadmin password."},
			"openshift": schema.SingleNestedAttribute{
				Optional: true,
				Description: "type = openshift (agent-based installer, needs vmmanager_openshift_pull_secret). Unset fields take the " +
					"server defaults. Changed in place on an installed cluster: adding `operators`, enabling `metallb` / " +
					"`metallb_demo`, switching `metallb_mode`; anything else recreates the cluster.",
				Attributes: map[string]schema.Attribute{
					"channel":           osStr("e.g. stable-4.20 (default)."),
					"topology":          osStr("sno (default), compact (3 schedulable masters) or ha (3 masters + workers)."),
					"storage":           osStr("none (default), lvms or odf (>= 3 nodes); adds a disk per storage node."),
					"storage_disk_size": osInt("GiB (default 100)."),
					"operators": schema.ListAttribute{Optional: true, Computed: true, ElementType: types.StringType,
						PlanModifiers: []planmodifier.List{listplanmodifier.UseStateForUnknown()},
						Description:   "OLM package names (default channel, redhat-operators). Adding one installs it in place; removing is not supported (uninstall it in the cluster first)."},
					"sriov":             osBool("Emulated SR-IOV: vIOMMU + igb NICs, SR-IOV Network Operator, sample policy."),
					"sriov_nics":        osInt("igb NICs per node (default 1)."),
					"sriov_vfs":         osInt("VFs per NIC (default 4, max 7)."),
					"sriov_device_type": osStr("netdevice (default) or vfio-pci."),
					"metallb":           liveBool("MetalLB with an address pool (enabled in place on an installed cluster; can't be disabled)."),
					"metallb_addresses": osInt("Pool size (default 16)."),
					"metallb_demo":      liveBool("Deploy the MetalLB lab demo (hello.<group domain>), default true."),
					"metallb_mode":      liveStr("l2 (default: pool in the group network, ARP) or bgp (a /27 announced to the group router over BGP, ECMP; enables BGP on the router)."),
					"disable_updates":   osBool("Clear the update channel (default true)."),
				},
			},
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
	timeout := clusterTimeout
	if c, err := r.get(ctx, id); err == nil && c.Type == "openshift" {
		timeout = openshiftTimeout
	}
	return Poll(ctx, 10*time.Second, timeout, func() (bool, error) {
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
		"name":    plan.Name.ValueString(),
		"type":    plan.Type.ValueString(),
		"version": strPtr(plan.Version),
		"network": strPtr(plan.Network),
		"router_memory": func() any {
			if plan.RouterMemory.IsNull() || plan.RouterMemory.IsUnknown() {
				return nil
			}
			return plan.RouterMemory.ValueInt64()
		}(),
		"cidr":       strPtr(plan.CIDR),
		"extra_args": strPtr(plan.ExtraArgs),
		"username":   strPtr(plan.Username),
		"password":   strPtr(plan.Password),
	}
	if v := intPtr(plan.Ctlplanes); v != nil {
		body["ctlplanes"] = *v
	}
	if v := intPtr(plan.Workers); v != nil {
		body["workers"] = *v
	}
	for key, attrs := range map[string][3]types.Int64{
		"ctlplane": {plan.CtlplaneMemory, plan.CtlplaneVCPU, plan.CtlplaneDisk},
		"worker":   {plan.WorkerMemory, plan.WorkerVCPU, plan.WorkerDisk},
	} {
		res := map[string]any{}
		for i, field := range []string{"memory", "vcpu", "disk_size"} {
			if v := intPtr(attrs[i]); v != nil {
				res[field] = *v
			}
		}
		if len(res) > 0 {
			body[key] = res
		}
	}
	if os := plan.OpenShift; os != nil || plan.Type.ValueString() == "openshift" {
		opts := map[string]any{}
		if os != nil {
			if v := strPtr(os.Channel); v != nil {
				opts["channel"] = *v
			}
			if v := strPtr(os.Topology); v != nil {
				opts["topology"] = *v
			}
			if v := strPtr(os.Storage); v != nil {
				opts["storage"] = *v
			}
			if v := intPtr(os.StorageDiskSize); v != nil {
				opts["storage_disk_size"] = *v
			}
			if !os.Operators.IsNull() && !os.Operators.IsUnknown() {
				var names []string
				resp.Diagnostics.Append(os.Operators.ElementsAs(ctx, &names, false)...)
				ops := []map[string]any{}
				for _, n := range names {
					ops = append(ops, map[string]any{"name": n})
				}
				opts["operators"] = ops
			}
			sriov := map[string]any{}
			if !os.SRIOV.IsNull() && !os.SRIOV.IsUnknown() {
				sriov["enabled"] = os.SRIOV.ValueBool()
			}
			if v := intPtr(os.SRIOVNics); v != nil {
				sriov["nics"] = *v
			}
			if v := intPtr(os.SRIOVVFs); v != nil {
				sriov["vfs"] = *v
			}
			if v := strPtr(os.SRIOVDeviceType); v != nil {
				sriov["device_type"] = *v
			}
			if len(sriov) > 0 {
				opts["sriov"] = sriov
			}
			mlb := map[string]any{}
			if !os.MetalLB.IsNull() && !os.MetalLB.IsUnknown() {
				mlb["enabled"] = os.MetalLB.ValueBool()
			}
			if v := intPtr(os.MetalLBAddrs); v != nil {
				mlb["addresses"] = *v
			}
			if !os.MetalLBDemo.IsNull() && !os.MetalLBDemo.IsUnknown() {
				mlb["demo"] = os.MetalLBDemo.ValueBool()
			}
			if v := strPtr(os.MetalLBMode); v != nil {
				mlb["mode"] = os.MetalLBMode.ValueString()
			}
			if len(mlb) > 0 {
				opts["metallb"] = mlb
			}
			if !os.DisableUpdates.IsNull() && !os.DisableUpdates.IsUnknown() {
				opts["disable_updates"] = os.DisableUpdates.ValueBool()
			}
		}
		if v := strPtr(plan.Version); v != nil {
			opts["version"] = *v
		}
		delete(body, "version")
		body["openshift"] = opts
	}
	if d := strPtr(plan.Domain); d != nil {
		body["domain"] = *d
	}
	if gid := strPtr(plan.GroupID); gid != nil {
		n, err := strconv.ParseInt(*gid, 10, 64)
		if err != nil {
			resp.Diagnostics.AddError("Invalid group_id", err.Error())
			return
		}
		body["group_id"] = n
		delete(body, "network")
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
	plan.KubeadminPass = types.StringNull()
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
	if c.GroupID != nil {
		m.GroupID = types.StringValue(strconv.FormatInt(*c.GroupID, 10))
	} else {
		m.GroupID = types.StringNull()
	}
	if cidr, ok := c.Spec["cidr"].(string); ok && cidr != "" {
		m.CIDR = types.StringValue(cidr)
	}
	if img, ok := c.Spec["cloud_image_id"].(float64); ok {
		m.CloudImageID = types.StringValue(strconv.FormatInt(int64(img), 10))
	}
	for key, attrs := range map[string][3]*types.Int64{
		"ctlplane": {&m.CtlplaneMemory, &m.CtlplaneVCPU, &m.CtlplaneDisk},
		"worker":   {&m.WorkerMemory, &m.WorkerVCPU, &m.WorkerDisk},
	} {
		res, _ := c.Spec[key].(map[string]any)
		for i, field := range []string{"memory", "vcpu", "disk_size"} {
			if v, ok := res[field].(float64); ok {
				*attrs[i] = types.Int64Value(int64(v))
			} else if attrs[i].IsUnknown() {
				*attrs[i] = types.Int64Null()
			}
		}
	}
	if c.Type == "openshift" && m.OpenShift != nil {
		fillOpenShift(ctx, m.OpenShift, c.Spec)
	} else if c.Type != "openshift" {
		m.OpenShift = nil
	}
	if u, ok := c.Spec["username"].(string); ok && u != "" {
		m.Username = types.StringValue(u) // imported clusters: the server's value, not null
	}
	m.ConsoleURL = strOrNull(c.ConsoleURL)
	if c.Type == "openshift" && (m.KubeadminPass.IsNull() || m.KubeadminPass.IsUnknown()) {
		var creds struct {
			Password *string `json:"password"`
		}
		if err := r.client.Do(ctx, "GET", "/clusters/"+m.ID.ValueString()+"/openshift/credentials", nil, &creds); err == nil {
			m.KubeadminPass = strOrNull(creds.Password)
		} else {
			m.KubeadminPass = types.StringNull()
		}
	} else if m.KubeadminPass.IsUnknown() {
		m.KubeadminPass = types.StringNull()
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
	plan.KubeadminPass = state.KubeadminPass

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

	if plan.Type.ValueString() == "openshift" {
		if !r.openshiftAddons(ctx, id, state.OpenShift, plan.OpenShift, &resp.Diagnostics) {
			return
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
	// an empty openshift block: Read fills it from the server (dropped again for other types)
	empty := openshiftModel{Operators: types.ListNull(types.StringType)}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("openshift"), &empty)...)
}

// fillOpenShift copies the server's openshift options (spec.openshift) into m
func fillOpenShift(ctx context.Context, m *openshiftModel, spec map[string]any) {
	o, _ := spec["openshift"].(map[string]any)
	if o == nil {
		return
	}
	str := func(v any) types.String {
		if s, ok := v.(string); ok && s != "" {
			return types.StringValue(s)
		}
		return types.StringNull()
	}
	num := func(v any) types.Int64 {
		if f, ok := v.(float64); ok {
			return types.Int64Value(int64(f))
		}
		return types.Int64Null()
	}
	flag := func(v any) types.Bool {
		if b, ok := v.(bool); ok {
			return types.BoolValue(b)
		}
		return types.BoolNull()
	}
	sriov, _ := o["sriov"].(map[string]any)
	mlb, _ := o["metallb"].(map[string]any)
	m.Channel, m.Topology, m.Storage = str(o["channel"]), str(o["topology"]), str(o["storage"])
	m.StorageDiskSize = num(o["storage_disk_size"])
	m.SRIOV, m.SRIOVNics, m.SRIOVVFs, m.SRIOVDeviceType = flag(sriov["enabled"]), num(sriov["nics"]), num(sriov["vfs"]), str(sriov["device_type"])
	m.MetalLB, m.MetalLBAddrs, m.MetalLBDemo = flag(mlb["enabled"]), num(mlb["addresses"]), flag(mlb["demo"])
	m.MetalLBMode = str(mlb["mode"])
	if m.MetalLB.ValueBool() && m.MetalLBMode.IsNull() {
		m.MetalLBMode = types.StringValue("l2")
	}
	m.DisableUpdates = flag(o["disable_updates"])
	names := []string{}
	if ops, ok := o["operators"].([]any); ok {
		for _, op := range ops {
			if e, ok := op.(map[string]any); ok {
				if n, ok := e["name"].(string); ok {
					names = append(names, n)
				}
			}
		}
	}
	m.Operators, _ = types.ListValueFrom(ctx, types.StringType, names)
}

// openshiftAddons applies the in-place part of an openshift block change through the add-on API
// (one task at a time). Returns false on error.
func (r *clusterResource) openshiftAddons(ctx context.Context, id string, old, cur *openshiftModel, d diags) bool {
	if cur == nil {
		return true
	}
	if old == nil {
		old = &openshiftModel{}
	}
	listOf := func(l types.List) []string {
		var names []string
		if !l.IsNull() && !l.IsUnknown() {
			if errs := l.ElementsAs(ctx, &names, false); errs.HasError() {
				d.AddError("Invalid operators list", fmt.Sprint(errs))
			}
		}
		return names
	}
	had := map[string]bool{}
	for _, n := range listOf(old.Operators) {
		had[n] = true
	}
	want := map[string]bool{}
	added := []string{}
	for _, n := range listOf(cur.Operators) {
		want[n] = true
		if !had[n] {
			added = append(added, n)
		}
	}
	for n := range had {
		if !want[n] {
			d.AddError("Removing an operator is not supported",
				fmt.Sprintf("%s: uninstall it in the cluster (Subscription + CSV), then remove it from the configuration", n))
			return false
		}
	}
	on := func(b types.Bool, def bool) bool {
		if b.IsNull() || b.IsUnknown() {
			return def
		}
		return b.ValueBool()
	}
	mode := func(m *openshiftModel) string {
		if m.MetalLBMode.IsNull() || m.MetalLBMode.ValueString() == "" {
			return "l2"
		}
		return m.MetalLBMode.ValueString()
	}
	addon := func(body map[string]any, what string) bool {
		if err := r.client.Do(ctx, "POST", "/clusters/"+id+"/openshift/addons", body, nil); err != nil {
			d.AddError("Cannot add "+what, err.Error())
			return false
		}
		if err := r.waitIdle(ctx, id); err != nil {
			d.AddError("Adding "+what+" failed", err.Error())
			return false
		}
		return true
	}
	for _, n := range added {
		if !addon(map[string]any{"kind": "operator", "operator": map[string]any{"name": n}}, "operator "+n) {
			return false
		}
	}
	wasOn, isOn := on(old.MetalLB, false), on(cur.MetalLB, false)
	if wasOn && !isOn {
		d.AddError("Disabling MetalLB is not supported", "uninstall it in the cluster, or recreate the cluster")
		return false
	}
	demo := on(cur.MetalLBDemo, true)
	if isOn && (!wasOn || mode(old) != mode(cur)) {
		mlb := map[string]any{"enabled": true, "mode": mode(cur), "demo": demo}
		if v := intPtr(cur.MetalLBAddrs); v != nil {
			mlb["addresses"] = *v
		}
		if !addon(map[string]any{"kind": "metallb", "metallb": mlb}, "MetalLB ("+mode(cur)+")") {
			return false
		}
		if !wasOn && demo && !addon(map[string]any{"kind": "metallb-demo"}, "the MetalLB demo") {
			return false
		}
	} else if isOn && demo && !on(old.MetalLBDemo, true) {
		if !addon(map[string]any{"kind": "metallb-demo"}, "the MetalLB demo") {
			return false
		}
	}
	return true
}
