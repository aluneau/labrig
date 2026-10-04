# Build and install the provider where OpenTofu finds it (implied local mirror),
# so configs can use: source = "local/vmmanager"
VERSION  ?= 0.1.0
OS_ARCH  := $(shell go env GOOS)_$(shell go env GOARCH)
PLUGINS  := $(HOME)/.terraform.d/plugins/registry.opentofu.org/local/vmmanager/$(VERSION)/$(OS_ARCH)
BINARY   := terraform-provider-vmmanager

.PHONY: build install test clean

build:
	go build -ldflags "-X main.version=$(VERSION)" -o $(BINARY) .

install: build
	mkdir -p $(PLUGINS)
	cp $(BINARY) $(PLUGINS)/$(BINARY)_v$(VERSION)
	@echo "Installed to $(PLUGINS)"

test:
	go vet ./...

clean:
	rm -f $(BINARY)
