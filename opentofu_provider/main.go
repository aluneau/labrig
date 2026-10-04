// OpenTofu / Terraform provider for VM Manager.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"

	"github.com/aluneau/vm-manager/opentofu_provider/internal/provider"
)

// Set at build time with -ldflags "-X main.version=..."
var version = "dev"

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run with support for debuggers like delve")
	flag.Parse()

	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.opentofu.org/local/vmmanager",
		Debug:   debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
