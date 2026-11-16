UV ?= uv
CARGO ?= cargo
POWERSHELL ?= powershell

ARTIFACT_DIR ?= artifacts
PY_ARTIFACT_DIR := $(ARTIFACT_DIR)/python
LINUX_UV ?= artifacts/uv-linux-amd64
ENROLLMENT_FILE ?= secrets/build-enrollment.json

# Private builds pin the gateway endpoint the enrollment profile must match. It is
# given independently of the profile (ANX_ENROLLMENT_ENDPOINT in the environment,
# or ENROLLMENT_ENDPOINT=wss://<hub-domain> on the make command line); without it
# a private build fails instead of trusting the profile's own url.
ifdef ENROLLMENT_ENDPOINT
export ANX_ENROLLMENT_ENDPOINT := $(ENROLLMENT_ENDPOINT)
endif

# The private and the full (`make` / `make build`) builds ship every business
# plugin: a missing checkout under builtins/ is an error there, not a silent skip.
# Public targets (runtime, desktop, ubuntu, client, server, python) keep skipping.
private-desktop ubuntu-cloud check-client-inputs build: export ANX_REQUIRE_ALL_BUILTINS = 1

.PHONY: all check version-check version-bump python desktop runtime private-desktop ubuntu ubuntu-cloud client server build distributions verify verify-clients clean check-client-inputs

all: build

check: version-check runtime
	"$(UV)" run pytest
	"$(CARGO)" check --manifest-path desktop/src-tauri/Cargo.toml

# Single version source: agent_nexus/__init__.py (docs/design.md section 10).
version-check:
	"$(UV)" run --no-project python scripts/version.py check

# Usage: make version-bump VERSION=X.Y.Z
version-bump:
	"$(UV)" run --no-project python scripts/version.py set "$(VERSION)"

python:
	"$(UV)" run python scripts/build_wheels.py --out "$(PY_ARTIFACT_DIR)" --uv "$(UV)"

client: desktop
	"$(UV)" run python scripts/build_release.py client --out "$(ARTIFACT_DIR)" --uv "$(UV)"

server:
	"$(UV)" run python scripts/build_release.py server --out "$(ARTIFACT_DIR)"

runtime:
	"$(UV)" run python scripts/build_release.py runtime --out "$(ARTIFACT_DIR)" --uv "$(UV)"

private-desktop:
	"$(UV)" run python scripts/build_desktop.py --out "$(ARTIFACT_DIR)" --enrollment-file "$(ENROLLMENT_FILE)" --uv "$(UV)" --cargo "$(CARGO)"

ubuntu:
	"$(UV)" run python scripts/build_ubuntu.py --uv "$(LINUX_UV)" --out "$(ARTIFACT_DIR)/ubuntu"

ubuntu-cloud:
	"$(UV)" run python scripts/build_ubuntu.py --uv "$(LINUX_UV)" --out "$(ARTIFACT_DIR)/ubuntu" --enrollment-file "$(ENROLLMENT_FILE)"

# Public desktop: raw exe plus the NSIS installer (`cargo tauri build --bundles nsis`, Tauri CLI 2).
# SIGNING_KEY_DIR=<folder with updater.key and updater.key.password> also signs it for the updater;
# TAURI_SIGNING_PRIVATE_KEY / _PASSWORD in the environment work the same way (CI).
SIGNING_KEY_DIR ?=
desktop: runtime
	"$(UV)" run python scripts/build_desktop.py --out "$(ARTIFACT_DIR)" --runtime "$(ARTIFACT_DIR)/agent-nexus-runtime.zip" --uv "$(UV)" --cargo "$(CARGO)" $(if $(SIGNING_KEY_DIR),--signing-key-dir "$(SIGNING_KEY_DIR)")
	"$(UV)" run python scripts/verify_client_artifacts.py --out "$(ARTIFACT_DIR)" --desktop-only $(if $(SIGNING_KEY_DIR),--require-signature)

check-client-inputs:
	"$(UV)" run python scripts/check_client_build_inputs.py --uv "$(LINUX_UV)" --enrollment-file "$(ENROLLMENT_FILE)"

build: check-client-inputs
	$(MAKE) python client private-desktop ubuntu ubuntu-cloud server
	"$(UV)" run python scripts/verify_client_artifacts.py --out "$(ARTIFACT_DIR)" --enrollment-file "$(ENROLLMENT_FILE)" --complete

distributions: python client server

verify: distributions
	"$(UV)" run python scripts/verify_release.py --out "$(ARTIFACT_DIR)"

verify-clients: build

clean:
	"$(POWERSHELL)" -NoProfile -Command "Remove-Item '$(ARTIFACT_DIR)' -Recurse -Force -ErrorAction SilentlyContinue"
	"$(CARGO)" clean --manifest-path desktop/src-tauri/Cargo.toml
