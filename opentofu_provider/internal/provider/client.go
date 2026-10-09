package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client is a minimal JSON client for the VM Manager REST API (/api/v1).
type Client struct {
	Endpoint string
	HTTP     *http.Client
}

// APIError carries the HTTP status and FastAPI's "detail" message.
type APIError struct {
	Status int
	Detail string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("VM Manager API: %s (HTTP %d)", e.Detail, e.Status)
}

func IsNotFound(err error) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound
}

func NewClient(endpoint string) *Client {
	return &Client{
		Endpoint: strings.TrimRight(endpoint, "/"),
		HTTP:     &http.Client{Timeout: 5 * time.Minute},
	}
}

// Do sends body as JSON (if non-nil) and decodes the response into out (if non-nil).
func (c *Client) Do(ctx context.Context, method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Endpoint+"/api/v1"+path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach VM Manager at %s: %w", c.Endpoint, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)

	if resp.StatusCode >= 400 {
		return &APIError{Status: resp.StatusCode, Detail: detail(data)}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

// detail extracts FastAPI's error message: {"detail": "..."} or {"detail": [{"loc": [...], "msg": "..."}]}
func detail(data []byte) string {
	var body struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(data, &body) != nil || body.Detail == nil {
		return strings.TrimSpace(string(data))
	}
	var msg string
	if json.Unmarshal(body.Detail, &msg) == nil {
		return msg
	}
	var items []struct {
		Loc []any  `json:"loc"`
		Msg string `json:"msg"`
	}
	if json.Unmarshal(body.Detail, &items) == nil {
		parts := make([]string, 0, len(items))
		for _, it := range items {
			loc := make([]string, 0, len(it.Loc))
			for _, l := range it.Loc[min(1, len(it.Loc)):] {
				loc = append(loc, fmt.Sprint(l))
			}
			parts = append(parts, strings.Join(loc, ".")+": "+it.Msg)
		}
		return strings.Join(parts, "; ")
	}
	return string(body.Detail)
}

// Poll calls check every interval until it returns done=true, an error, or the timeout expires.
func Poll(ctx context.Context, interval, timeout time.Duration, check func() (bool, error)) error {
	deadline := time.Now().Add(timeout)
	for {
		done, err := check()
		if err != nil || done {
			return err
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("timed out after %s", timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
}

// API payloads (only the fields the provider uses)

type apiTask struct {
	ID           int64   `json:"id"`
	Status       string  `json:"status"`
	ErrorMessage *string `json:"error_message"`
}

type apiCloudImage struct {
	ID               int64   `json:"id"`
	Name             string  `json:"name"`
	Distribution     string  `json:"distribution"`
	Version          string  `json:"version"`
	URL              string  `json:"url"`
	Path             *string `json:"path"`
	Size             int64   `json:"size"`
	Status           string  `json:"status"`
	DownloadProgress int64   `json:"download_progress"`
}

type apiDHCPHost struct {
	MAC  string  `json:"mac"`
	IP   string  `json:"ip"`
	Name *string `json:"name,omitempty"`
}

type apiNetwork struct {
	ID          int64   `json:"id"`
	Name        string  `json:"name"`
	BridgeName  *string `json:"bridge_name"`
	ForwardMode string  `json:"forward_mode"`
	ForwardDev  *string `json:"forward_dev"`
	VLAN        *int64  `json:"vlan"`
	Domain      *string `json:"domain"`
	IPAddress   *string `json:"ip_address"`
	Prefix      *int64  `json:"prefix"`
	DHCPEnabled bool    `json:"dhcp_enabled"`
	DHCPStart   *string `json:"dhcp_start"`
	DHCPEnd     *string `json:"dhcp_end"`
	Active      bool    `json:"active"`
	Autostart   bool    `json:"autostart"`
}

type apiNetworkConfig struct {
	Network apiNetwork    `json:"network"`
	Hosts   []apiDHCPHost `json:"hosts"`
}

type apiVM struct {
	ID          int64   `json:"id"`
	UUID        string  `json:"uuid"`
	Name        string  `json:"name"`
	Description *string `json:"description"`
	Memory      int64   `json:"memory"`
	VCPU        int64   `json:"vcpu"`
	Status      string  `json:"status"`
	Autostart   bool    `json:"autostart"`
	Interfaces  []struct {
		Addresses []string `json:"addresses"`
	} `json:"interfaces"`
	Nics []struct {
		Network   *string `json:"network"`
		MAC       *string `json:"mac"`
		Type      *string `json:"type"`
		Model     *string `json:"model"`
		LinkState *string `json:"link_state"`
		Pending   *string `json:"pending"`
		VF        bool    `json:"vf"`
		VLAN      *int64  `json:"vlan"`
	} `json:"nics"`
	Iommu *struct {
		Enabled bool  `json:"enabled"`
		Active  *bool `json:"active"`
	} `json:"iommu"`
	Console *struct {
		Port *int64 `json:"port"`
	} `json:"console"`
	Disks []apiVMDisk `json:"disks"`
	Cdrom *struct {
		Target *string `json:"target"`
		Path   *string `json:"path"`
	} `json:"cdrom"`
	Boot *struct {
		Order []string `json:"order"`
		Once  []string `json:"once"`
	} `json:"boot"`
}

type apiVMDisk struct {
	Device   *string `json:"device"`
	Path     *string `json:"path"`
	Target   *string `json:"target"`
	Bus      *string `json:"bus"`
	Format   *string `json:"format"`
	Capacity *int64  `json:"capacity"`
	Boot     bool    `json:"boot"`
	Pending  *string `json:"pending"`
}

// apiDeviceChange is the result of PUT /cdrom, PUT /boot and the /disks calls.
type apiDeviceChange struct {
	Message string  `json:"message"`
	Pending bool    `json:"pending"`
	Target  *string `json:"target"`
	Path    *string `json:"path"`
}
