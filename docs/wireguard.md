# Remote access to a lab group (WireGuard)

Connect a laptop (or any device) to a lab group: it gets an address on a WireGuard tunnel to the group's
router and reaches the group network, its DNS zone (`<member>.<domain>`) and the router's load balancers
(e.g. a kubeadm cluster API) — the rest of its traffic stays as it is (split tunnel).

```
laptop ──udp──► host:<host_port> ──(app relay)──► router <uplink_ip>:<listen_port> wg0 ──► group network
 10.44.N.2                         51820…51869                   10.44.N.1 (also DNS)       10.42.x.0/24
```

## Enable it

- New group: check **Remote access (WireGuard)** in *Create group*.
- Existing group: *Remote access* tab → **Enable remote access** (applied live; an older router installs
  `wireguard-tools` first, which takes a minute).
- API: `PUT /api/v1/groups/{id}/wireguard {"enabled": true}`; OpenTofu: `wireguard = true` on `vmmanager_group`.

The app assigns a tunnel subnet (first free /24 of `WG_SUBNET_POOL`, default `10.44.0.0/16`) and a host UDP
port (first free one of `WG_HOST_PORTS`, default `51820-51869`). The router generates its key pair once
(`/etc/wireguard/private.key`, never leaves the router). Disabling keeps keys and devices.

## Add a device

*Remote access* tab → **Add device** → name (e.g. `laptop`) → **Create config**. You get the client config
as a file download, a QR code and a copy button. Without a public key the app generates the key pair: the
private key is **only in this config** (not stored, shown once). With *Use my own key pair* you paste your
device's public key (`wg genkey | tee private.key | wg pubkey`) and add `PrivateKey` to the config yourself.

The **Endpoint** is this host as the device reaches it: `WG_ENDPOINT_HOST` (in `backend/.env`), else the
host's primary LAN address; you can change it per device in the dialog (e.g. a DNS name, or a public
address with a port forward on your router to `udp/<host_port>` of this host).

The config:

```ini
[Interface]
PrivateKey = …
Address = 10.44.0.2/32
DNS = 10.44.0.1, case-12345.lab       # the router; the domain becomes a search domain

[Peer]
PublicKey = …                          # the router's
Endpoint = 192.168.1.20:51820          # this host, relay port
AllowedIPs = 10.42.7.0/24, 10.44.0.0/24, 192.168.122.50/32   # group, tunnel, router uplink (LB / k8s API)
                                       # + the BGP announce ranges when BGP is on (docs/bgp.md)
PersistentKeepalive = 25
```

## On the laptop

**Fedora / RHEL / any NetworkManager desktop:**

```bash
nmcli connection import type wireguard file wg-case-12345.conf   # the file name is the interface name
nmcli connection up wg-case-12345        # it is also brought up by the import
nmcli connection down wg-case-12345      # disconnect;  nmcli connection delete … to remove it
```

With systemd-resolved (Fedora), names of the lab domain are asked to the router and the rest to your usual
DNS. Without it (RHEL 9 default), NetworkManager puts the router first in `/etc/resolv.conf` while connected:
the router answers the lab zone and forwards everything else.

**Other Linux:** `sudo wg-quick up ./wg-case-12345.conf` (`down` to disconnect).
**Windows / macOS:** WireGuard app → *Import tunnel(s) from file*. **Phones:** WireGuard app → scan the QR code.

Then: `ping web1.case-12345.lab`, `ssh admin@web1`, and for a kubeadm cluster in the group the kubeconfig
downloaded from the cluster page works as is (its server, `https://<uplink_ip>:6443`, goes through the tunnel).

The *Remote access* tab lists the devices with their last handshake and traffic (read from `wg show` on the
router; a green handshake = less than 3 minutes ago). **Remove** cuts a device off at once. **Config**
downloads its config again, without private key.

## How it works / requirements on the host

- The router's uplink is on a libvirt NAT network: the LAN can't reach it, and libvirt's firewall rejects new
  inbound connections to NAT networks. The app itself relays UDP `host_port` → `uplink_ip:listen_port`
  (host → guest traffic is allowed by libvirt). No root, nothing to re-apply after a reboot; the relay runs
  while the app runs.
- The host firewall must let the `WG_HOST_PORTS` range in (UDP). `scripts/setup.sh` does it when ufw or
  firewalld is active (`--wg-ports A-B` to change it, `--wg-ports none` to skip). By hand:
  `sudo ufw allow 51820:51869/udp`, or
  `sudo firewall-cmd --permanent --add-port=51820-51869/udp && sudo firewall-cmd --reload`.
- Settings (`backend/.env`): `WG_HOST_PORTS`, `WG_RELAY_LISTEN` (default `0.0.0.0`), `WG_ENDPOINT_HOST`,
  `WG_SUBNET_POOL`.
- Throughput is what the relay forwards (a few hundred Mbit/s): fine for kubectl, SSH, consoles, web UIs.

## Troubleshooting

| Symptom | Check |
|---|---|
| "relay not listening" in the tab | Another process holds the UDP port (change `WG_HOST_PORTS`), or the router's uplink address isn't known yet (start the group). |
| No handshake (`never`) | From the laptop, is the endpoint the right address of this host? Host firewall: `sudo ufw status` / `sudo firewall-cmd --list-ports`. A laptop on another network needs a port forward to this host. `sudo journalctl -u vm-manager` shows relay errors. |
| Handshake OK, names don't resolve | `resolvectl status` (Fedora) / `cat /etc/resolv.conf`: the router's tunnel address must be a DNS server of the connection. `dig @10.44.N.1 web1.<domain>`. |
| Handshake OK, no ping | The member's own firewall; `ip route get <member ip>` on the laptop must say `dev wg-…`. Another network of the laptop may overlap the group CIDR or the tunnel subnet (pick another group CIDR / `WG_SUBNET_POOL`). |
| Router side | Router console: `wg show`, `systemctl status wg-quick@wg0`, `cat /etc/wireguard/wg0.conf`. *Router* tab → *Apply again* re-pushes the config. |

## IPv6

In a group with IPv6 (docs/ipv6.md) the tunnel also gets a /64 (`subnet6`): devices get `<subnet6>::<n>` and route the
lab /64, the tunnel /64 and the IPv6 announce ranges. Download the config again for devices added before IPv6 was on.
