import argparse
import sys

import os

import requests
import yaml
from dotenv import load_dotenv
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
import pynetbox

load_dotenv()

DEVICETYPE_LIBRARY = "netbox-community/devicetype-library"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_API = "https://api.github.com"


def parse_args():
    parser = argparse.ArgumentParser(
        description="SSH to a Cisco IOS/IOS-XE device and add it to NetBox."
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--username", required=True, help="SSH username")
    parser.add_argument("--password", required=True, help="SSH password")
    return parser.parse_args()


def _interface_type(name):
    n = name.lower()
    if n.startswith(("tengigabit", "te")):
        return "10gbase-t"
    if n.startswith(("gigabitethernet", "gi")):
        return "1000base-t"
    if n.startswith(("fastethernet", "fa")):
        return "100base-tx"
    if n.startswith(("loopback", "lo", "tunnel")):
        return "virtual"
    return "other"


def gather_device_info(host, username, password, device_type="cisco_xe"):
    device = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
    }
    print(f"Connecting to {host}...")
    connection = ConnectHandler(**device)

    version_data = connection.send_command("show version", use_textfsm=True)
    iface_data = connection.send_command("show interfaces", use_textfsm=True)

    connection.disconnect()

    data = version_data[0] if isinstance(version_data, list) and version_data else {}

    interfaces = []
    if isinstance(iface_data, list):
        interfaces = [
            {"name": i["interface"], "type": _interface_type(i["interface"])}
            for i in iface_data
            if i.get("interface")
        ]

    return {
        "hostname": data.get("hostname") or host,
        "os_version": data.get("version", "unknown"),
        "model": data.get("hardware", ["unknown"])[0] if data.get("hardware") else "unknown",
        "serial": data.get("serial", ["unknown"])[0] if data.get("serial") else "unknown",
        "ip": host,
        "interfaces": interfaces,
    }


def find_in_devicetype_library(model):
    """Search the NetBox device type library on GitHub for the given model."""
    candidates = [model, model.split("/")[0]]
    for candidate in candidates:
        url = f"{GITHUB_RAW}/{DEVICETYPE_LIBRARY}/master/device-types/Cisco/{candidate}.yaml"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            print(f"Found '{candidate}' in NetBox device type library.")
            return yaml.safe_load(r.text)

    r = requests.get(
        f"{GITHUB_API}/search/code",
        params={"q": f"{model} repo:{DEVICETYPE_LIBRARY} path:device-types/Cisco"},
        headers={"Accept": "application/vnd.github.v3+json"},
        timeout=10,
    )
    if r.status_code == 200:
        items = r.json().get("items", [])
        if items:
            raw_url = (
                items[0]["html_url"]
                .replace("github.com", "raw.githubusercontent.com")
                .replace("/blob/", "/")
            )
            r2 = requests.get(raw_url, timeout=10)
            if r2.status_code == 200:
                print(f"Found '{items[0]['name']}' in NetBox device type library via search.")
                return yaml.safe_load(r2.text)

    return None


def _ensure_manufacturer(nb, name):
    manufacturer = nb.dcim.manufacturers.get(name=name)
    if not manufacturer:
        manufacturer = nb.dcim.manufacturers.create(
            name=name, slug=name.lower().replace(" ", "-")
        )
    return manufacturer


def import_device_type_from_library(nb, yaml_data):
    """Create device type in NetBox from library YAML including all components."""
    manufacturer = _ensure_manufacturer(nb, yaml_data.get("manufacturer", "Cisco"))
    model = yaml_data["model"]
    slug = yaml_data.get("slug", model.lower().replace(" ", "-").replace("/", "-"))

    device_type = nb.dcim.device_types.create(
        manufacturer=manufacturer.id,
        model=model,
        slug=slug,
        u_height=yaml_data.get("u_height", 1),
        is_full_depth=yaml_data.get("is_full_depth", True),
        part_number=yaml_data.get("part_number", ""),
    )
    print(f"Imported device type '{model}' from library.")

    for iface in yaml_data.get("interfaces", []):
        nb.dcim.interface_templates.create(
            device_type=device_type.id,
            name=iface["name"],
            type=iface.get("type", "other"),
            mgmt_only=iface.get("mgmt_only", False),
        )

    for port in yaml_data.get("console-ports", []):
        nb.dcim.console_port_templates.create(
            device_type=device_type.id,
            name=port["name"],
            type=port.get("type", "other"),
        )

    for port in yaml_data.get("power-ports", []):
        kwargs = {
            "device_type": device_type.id,
            "name": port["name"],
            "type": port.get("type", "other"),
        }
        if port.get("maximum_draw"):
            kwargs["maximum_draw"] = port["maximum_draw"]
        if port.get("allocated_draw"):
            kwargs["allocated_draw"] = port["allocated_draw"]
        nb.dcim.power_port_templates.create(**kwargs)

    return device_type


def create_custom_device_type(nb, info):
    """Create a device type with interface templates built from live device data."""
    manufacturer = _ensure_manufacturer(nb, "Cisco")
    slug = info["model"].lower().replace("/", "-").replace(" ", "-")

    device_type = nb.dcim.device_types.create(
        manufacturer=manufacturer.id,
        model=info["model"],
        slug=slug,
    )
    print(f"Created custom device type '{info['model']}'.")

    for iface in info.get("interfaces", []):
        nb.dcim.interface_templates.create(
            device_type=device_type.id,
            name=iface["name"],
            type=iface["type"],
        )

    return device_type


def sync_to_netbox(info, netbox_url, netbox_token, verify=True):
    nb = pynetbox.api(netbox_url, token=netbox_token)

    if not verify:
        nb.verify = False

    device_type = nb.dcim.device_types.get(model=info["model"])
    if not device_type:
        yaml_data = find_in_devicetype_library(info["model"])
        if yaml_data:
            device_type = import_device_type_from_library(nb, yaml_data)
        else:
            print(f"'{info['model']}' not found in library. Creating custom device type.")
            device_type = create_custom_device_type(nb, info)

    site = nb.dcim.sites.get(name="Default")
    if not site:
        site = nb.dcim.sites.create(name="Default", slug="default", status="active")
        print("Created site: Default")

    role = nb.dcim.device_roles.get(name="Network Device")
    if not role:
        role = nb.dcim.device_roles.create(
            name="Network Device", slug="network-device", color="0000ff"
        )
        print("Created device role: Network Device")

    existing = nb.dcim.devices.get(name=info["hostname"])
    payload = {
        "name": info["hostname"],
        "device_type": device_type.id,
        "role": role.id,
        "site": site.id,
        "serial": info["serial"],
        "comments": f"OS Version: {info['os_version']}",
    }

    if existing:
        existing.update(payload)
        print(f"Updated device '{info['hostname']}' in NetBox.")
    else:
        nb.dcim.devices.create(**payload)
        print(f"Created device '{info['hostname']}' in NetBox.")


def main():
    args = parse_args()

    netbox_url = os.getenv("NETBOX_URL")
    netbox_token = os.getenv("NETBOX_TOKEN")
    if not netbox_url or not netbox_token:
        print("Error: NETBOX_URL and NETBOX_TOKEN must be set in .env")
        sys.exit(1)

    try:
        info = gather_device_info(args.host, args.username, args.password)
    except NetmikoAuthenticationException:
        print("Error: Authentication failed. Check your username/password.")
        sys.exit(1)
    except NetmikoTimeoutException:
        print(f"Error: Connection to {args.host} timed out.")
        sys.exit(1)

    print(f"  Hostname  : {info['hostname']}")
    print(f"  IP        : {info['ip']}")
    print(f"  Model     : {info['model']}")
    print(f"  Serial    : {info['serial']}")
    print(f"  OS Version: {info['os_version']}")
    print(f"  Interfaces: {len(info['interfaces'])} found")

    sync_to_netbox(info, netbox_url, netbox_token, verify=False)


if __name__ == "__main__":
    main()
