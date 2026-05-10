import argparse
import re
import sys

from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
import pynetbox


def parse_args():
    parser = argparse.ArgumentParser(
        description="SSH to a Cisco IOS/IOS-XE device and add it to NetBox."
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--username", required=True, help="SSH username")
    parser.add_argument("--password", required=True, help="SSH password")
    parser.add_argument("--netbox-url", required=True, help="NetBox base URL (e.g. http://netbox.local)")
    parser.add_argument("--netbox-token", required=True, help="NetBox API token")
    return parser.parse_args()


def gather_device_info(host, username, password):
    device = {
        "device_type": "cisco_ios",
        "host": host,
        "username": username,
        "password": password,
    }
    print(f"Connecting to {host}...")
    connection = ConnectHandler(**device)
    output = connection.send_command("show version")
    connection.disconnect()

    info = {}

    match = re.search(r"^(\S+)\s+uptime", output, re.MULTILINE)
    info["hostname"] = match.group(1) if match else host

    match = re.search(r"Cisco IOS.*?Version\s+([\w().]+)", output)
    info["os_version"] = match.group(1) if match else "unknown"

    match = re.search(r"(?:Model Number|cisco)\s*:\s*(\S+)|^[Cc]isco\s+(\S+)\s+\(", output, re.MULTILINE)
    if match:
        info["model"] = match.group(1) or match.group(2)
    else:
        info["model"] = "unknown"

    match = re.search(r"Processor board ID\s+(\S+)", output)
    info["serial"] = match.group(1) if match else "unknown"

    info["ip"] = host

    return info


def sync_to_netbox(info, netbox_url, netbox_token):
    nb = pynetbox.api(netbox_url, token=netbox_token)

    # Ensure the device type exists
    device_type = nb.dcim.device_types.get(model=info["model"])
    if not device_type:
        manufacturer = nb.dcim.manufacturers.get(name="Cisco")
        if not manufacturer:
            manufacturer = nb.dcim.manufacturers.create(name="Cisco", slug="cisco")
        device_type = nb.dcim.device_types.create(
            model=info["model"],
            slug=info["model"].lower().replace(" ", "-"),
            manufacturer=manufacturer.id,
        )
        print(f"Created device type: {info['model']}")

    # Ensure a default site exists
    site = nb.dcim.sites.get(name="Default")
    if not site:
        site = nb.dcim.sites.create(name="Default", slug="default", status="active")
        print("Created site: Default")

    # Ensure a default role exists
    role = nb.dcim.device_roles.get(name="Network Device")
    if not role:
        role = nb.dcim.device_roles.create(
            name="Network Device", slug="network-device", color="0000ff"
        )
        print("Created device role: Network Device")

    # Create or update the device
    existing = nb.dcim.devices.get(name=info["hostname"])
    payload = {
        "name": info["hostname"],
        "device_type": device_type.id,
        "role": role.id,
        "site": site.id,
        "serial": info["serial"],
        "custom_fields": {},
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

    sync_to_netbox(info, args.netbox_url, args.netbox_token)


if __name__ == "__main__":
    main()
