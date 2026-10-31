package provider

import (
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Host resource budget (docs/budget.md): the server refuses a create that doesn't fit on the host with HTTP 409
// and a plain-words message (surfaced as the apply error); force_budget = ?force=true creates it anyway.

func forceBudgetAttribute() schema.BoolAttribute {
	return schema.BoolAttribute{
		Optional: true, Computed: true, Default: booldefault.StaticBool(false),
		Description: "Create even when the host resource budget refuses it (not enough free RAM / disk on the host: " +
			"HTTP 409 with the numbers otherwise). Client-side only: changing it changes nothing on the server.",
	}
}

func forceBudgetQuery(force types.Bool) string {
	if force.ValueBool() {
		return "?force=true"
	}
	return ""
}
