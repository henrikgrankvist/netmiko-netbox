# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

## Running the script

```powershell
.\venv\Scripts\python main.py `
  --host <device-ip> `
  --username <user> `
  --password <pass> `
  --netbox-url http://<netbox-host> `
  --netbox-token <api-token>
```

## Architecture

All logic lives in `main.py`. The flow is:

1. **`gather_device_info`** — Opens a single Netmiko SSH session to the Cisco IOS/IOS-XE device, runs `show version` and `show interfaces` with `use_textfsm=True`, and returns a dict with `hostname`, `model`, `serial`, `os_version`, `ip`, and `interfaces` (list of `{name, type}` dicts).

2. **`sync_to_netbox`** — Entry point for all NetBox writes. Checks if the device type already exists; if not, delegates to the library lookup or custom creation path, then upserts the device record.

3. **Device type resolution** (called from `sync_to_netbox`):
   - `find_in_devicetype_library(model)` — Tries a direct raw GitHub URL first (`/device-types/Cisco/{model}.yaml`), then falls back to the GitHub code search API. Returns parsed YAML or `None`.
   - `import_device_type_from_library(nb, yaml_data)` — Creates the device type plus interface templates, console port templates, and power port templates from the library YAML.
   - `create_custom_device_type(nb, info)` — Used when the model is absent from the library. Creates a minimal device type and populates interface templates from the live `interfaces` list gathered by SSH.

4. **`_ensure_manufacturer`** — Helper used by both device type paths to get-or-create the manufacturer in NetBox.

## Git commits

After every change, commit with a short, descriptive message that states what changed and why — enough context to understand the change from `git log` alone and to safely roll back if needed. Commit each logical change separately rather than batching unrelated edits together.

## Key details

- `requests` and `pyyaml` are transitive dependencies (via `pynetbox` and `netmiko`) — do not add them to `requirements.txt` explicitly.
- `_interface_type(name)` maps Cisco IOS interface name prefixes to NetBox interface type slugs (`10gbase-t`, `1000base-t`, `100base-tx`, `virtual`, `other`).
- The GitHub library lookup targets only the `Cisco/` subdirectory of `netbox-community/devicetype-library`. Other manufacturers are not supported yet.
- NetBox objects that are auto-created if missing: manufacturer (`Cisco`), site (`Default`), device role (`Network Device`).
