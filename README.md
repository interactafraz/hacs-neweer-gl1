# Neewer WiFi — Home Assistant Integration

Home Assistant custom integration for Neewer GL1 Pro WiFi key lights, distributed via HACS.

Controls lights over the reverse-engineered UDP protocol on port **5052** ([braintapper/neewer-gl1](https://github.com/braintapper/neewer-gl1), [mglatt/neewer-wifi-python](https://github.com/mglatt/neewer-wifi-python)). Includes **subnet auto-discovery** so you do not need to enter IP addresses manually.

## About this fork

This fork exists because the upstream setup/discovery probe (`async_probe_light` in
`protocol.py`) binds to an ephemeral UDP port. The GL1 Pro, however, always replies
to the *fixed* port 5052 rather than to the sender's actual source port — so the
probe can never see the response, causing config flow to fail with
`No Neewer response from <host>` even though the handshake itself reaches the light
(confirmed via packet capture). This is unrelated to routing/firewall setup and
reproduces on any network.

**Fix applied here:** both UDP endpoints (`NeewerProtocol.async_setup` and
`async_probe_light`) now bind `DEFAULT_PORT` (5052) with `reuse_port=True`, so the
probe actually receives the light's reply, and multiple probes/sessions can share
the port.

Note for cross-subnet setups (light and Home Assistant on different, routed
subnets): the handshake also embeds the client's IP as ASCII text in the UDP
payload (see `build_handshake` in `protocol.py`). This embedded value must match
the packet's real source IP as seen by the light, or it silently ignores the
request. If routing this traffic requires NAT, make sure the NAT'd source address
is the same address Home Assistant's own network-adapter/route detection reports
as its `client_ip` (e.g. via a locally-owned IP alias + a specific host route with
a matching `src`), not an address invented purely at the firewall.

### Network setup required for a routed/isolated subnet

Fixing the port-binding bug above was only half the story. If Home Assistant and
the light sit on **different subnets separated by a router** (e.g. a second
router/AP handing out its own isolated subnet, connected to the main LAN), the
following also had to be set up before the light could be added at all —
otherwise the UDP handshake either never reaches the light, or the light's reply
never makes it back:

1. **A route from the Home Assistant host to the light's subnet**, via the second
   router's LAN-side IP. Without this, Home Assistant can't send anything to the
   light at all.
   ```bash
   # on the Home Assistant host (persisted via NetworkManager in our case)
   nmcli connection modify "<connection>" +ipv4.routes "<light-subnet>/24 <second-router-ip>"
   ```

2. **A firewall rule on the second router allowing forwarding from the Home
   Assistant side into the light's subnet** (`wan`→`lan`-style zone rule, ACCEPT,
   at least UDP port 5052). Ping working is *not* sufficient proof this is open —
   ICMP and the app's UDP port can be filtered independently.

3. **A locally-owned IP alias on the Home Assistant host, inside the light's own
   subnet, plus a specific host route to the light's exact IP using that alias as
   source.** This is required because of the payload/source-IP matching behavior
   described above: Home Assistant's own adapter/route detection must report an
   address that is *actually* in the light's subnet as the `client_ip`, and
   traffic to the light must actually leave with that same address as its source.
   A generic NAT/SNAT rule on the router is **not enough** on its own, because it
   only fixes the IP-layer source — the ASCII client IP baked into the UDP
   payload is computed by Home Assistant itself from its own interfaces and would
   still mismatch.
   ```bash
   # on the Home Assistant host
   nmcli connection modify "<connection>" +ipv4.addresses "<alias-ip>/32"
   nmcli connection modify "<connection>" +ipv4.routes "<light-ip>/32 <second-router-ip> src=<alias-ip>"
   ```

4. **A specific host route on the second router directing that alias IP back out
   towards Home Assistant**, instead of treating it as on-link on the light's own
   subnet. The alias IP is numerically inside the light's subnet but physically
   lives elsewhere, so without this the router tries (and fails) to ARP for it
   locally, causing intermittent `ICMP host unreachable` and dropped packets.
   ```bash
   # on the second router (OpenWrt example)
   uci add network route
   uci set network.@route[-1].interface='<uplink-interface>'
   uci set network.@route[-1].target='<alias-ip>'
   uci set network.@route[-1].netmask='255.255.255.255'
   uci commit network
   /etc/init.d/network reload
   ```

Only after all four of these were in place, combined with the port-binding fix
above, did the light actually respond to the setup handshake.

## Tested device

- **Neewer GL1 Pro** (WiFi control path)

Other Neewer WiFi models that share the same UDP protocol may work but are not verified.

## Features

- HACS installable custom component
- Config flow with local subnet discovery (UDP handshake probe)
- Manual IP fallback when discovery fails
- One `light` entity per device: on/off, brightness, color temperature (2900K–7000K)
- Shared UDP session management with heartbeat and periodic re-handshake

## Installation (HACS)

1. Open **HACS** → **Integrations** → **Custom repositories**
2. Add repository URL: `https://github.com/interactafraz/hacs-neweer-gl1`
3. Category: **Integration**
4. Install **Neewer WiFi**
5. Restart Home Assistant
6. Go to **Settings** → **Devices & Services** → **Add Integration** → **Neewer WiFi**

## Manual installation

Copy `custom_components/neewer_wifi` into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

## Usage

1. Add the integration and choose **Discover lights on local network** (scan may take 10–30 seconds on a typical `/24` subnet).
2. Select one or more discovered lights, or use **Enter light IP manually**.
3. Each light appears as a `light` entity (e.g. `light.neewer_gl1_pro_142`).

### Entity attributes (example)

```yaml
light.neewer_gl1_pro_142:
  state: on
  brightness: 128
  color_temp_kelvin: 5600
  supported_color_modes:
    - color_temp
  min_color_temp_kelvin: 2900
  max_color_temp_kelvin: 7000
```

## Discovery behavior

Discovery enumerates **private RFC1918** IPv4 subnets from Home Assistant network adapters and the system routing table (`/proc/net/route`), then probes each host with:

1. UDP handshake (client IP embedded, checksummed)
2. Wakeup packet
3. Heartbeat packet

Routed subnets (for example a light VLAN reachable via a gateway) are included automatically when a matching private route exists. Virtual interfaces such as Docker bridges are skipped. Routes broader than `/16` are ignored to avoid huge scans.

A host is identified as a Neewer light when a plausible protocol response is received (heartbeat ack `80 03 00 83`). Probes use ephemeral source ports with limited concurrency (30 workers) and per-host timeouts (~2s).

Discovery does **not** require DHCP reservations, but a stable IP helps avoid stale entries after network changes.

## Session conflict

Only **one controller** can hold the UDP session per light at a time. Close the Neewer mobile/desktop app before using Home Assistant. If commands stop working, reload the integration or restart Home Assistant to re-handshake.

## No device-reported state

The light does not report its state over UDP. The integration tracks state locally. Physical button changes or other apps will desync entity state until you toggle from Home Assistant again.

## Troubleshooting

| Issue | Action |
|-------|--------|
| Discovery finds nothing | Confirm light is on WiFi; enter a subnet on the discovery screen (e.g. `192.168.103.0/24`); try manual IP |
| Cannot connect | Close Neewer app; ensure Home Assistant has a network adapter on the same subnet as the light |
| Port 5052 in use | Another process bound UDP 5052; stop conflicting service |
| Wrong brightness/temp | State is local-only; turn light off/on from HA to resync |

### Logging

The integration logs key events at **info** level (discovery, connections, light control). For per-packet UDP detail, enable debug logging in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.neewer_wifi: debug
```

## Development

```bash
# Protocol / discovery unit tests (no HA runtime required)
python -m pytest tests/ -v

# Lint (optional)
ruff check custom_components tests
```

### Manual test plan

- [ ] Install via HACS or manual copy; restart HA
- [ ] Add integration → Discover → wait for scan; at least one GL1 Pro listed
- [ ] Select light → entity appears under Devices
- [ ] Turn off / on from HA UI
- [ ] Adjust brightness slider
- [ ] Adjust color temperature
- [ ] Add second light via Discover (multi-select or second add)
- [ ] Manual path: enter IP when discovery disabled or light on different subnet
- [ ] Reload integration; entity remains and controls work
- [ ] Open Neewer app while HA controls → expect conflict; close app and reload integration

## Credits

- [braintapper/neewer-gl1](https://github.com/braintapper/neewer-gl1) — UDP protocol
- [mglatt/neewer-wifi-python](https://github.com/mglatt/neewer-wifi-python) — Python reference server

## License

MIT
