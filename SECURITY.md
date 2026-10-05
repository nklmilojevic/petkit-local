# Security

## Reporting a vulnerability

Open a [GitHub security advisory](https://github.com/nklmilojevic/petkit-local/security/advisories/new),
or a normal issue if the problem is not sensitive. There is no formal SLA — this
is a hobby project — but security reports are read first.

## What this app is, in security terms

It impersonates a cloud service to hardware on your LAN, and it can modify that
hardware. That is the point of it, and it means the threat model is unusual
enough to be worth stating plainly.

### Things that are unauthenticated by design

| Surface | Why |
|---|---|
| Device API (host `80`) | The firmware has no credential to offer beyond an `X-Device` header it also computes itself. Anything on the LAN can talk to it. |
| Media bucket (host `9000`) | It stands in for Aliyun OSS, whose credentials this app issues to the device. Object keys are containment-checked, so an upload cannot escape the media root — but anyone on the LAN can upload. |
| Web panel (container `8099`) | Unmapped by default and reached through Home Assistant Ingress, which authenticates. **Map it to a host port and the whole API — device settings, commands, pet records, the patchers — is available with no login.** |

Web Bluetooth provisioning needs a secure context. Serving the panel a second
time over HTTPS with a self-signed certificate would have given it one, and was
rejected: it means a second unauthenticated surface, on a port nobody expects,
presenting a certificate nobody can verify. Provisioning asks the operator for a
real certificate in front of the ordinary panel port instead. (Confirmed on
hardware: it works through Ingress when Home Assistant itself is served over a
valid certificate.)

### Things that are deliberately permissive

- **`mqtt_strict_auth` defaults to `false`**, so the device-facing MQTT broker
  accepts any CONNECT. A signature nuance silently locking a device out is a
  worse failure than an open broker on a LAN that already exposes the device
  API. Turn it on if your network is shared.
- **Proxy mode does not verify the upstream's TLS certificate**
  (`mqtt/upstream.py`). The Aliyun IoT endpoint chains to a private root that is
  in no public store, is never sent, and has no AIA to fetch it from — the
  device itself is in the same position. So the connection is encrypted but not
  authenticated, and anything on the path could pose as the cloud. The guards
  that strip firmware pushes and shell commands out of a cloud reply still
  apply, and do not rest on server identity.
- **The patchers modify firmware on a rooted device.** They verify the binary is
  a MIPS executable and check free space first, but a patched device is outside
  its warranty and outside PetKit's update path.

### Captures contain secrets

Traffic capture writes verbatim payloads — nothing in a capture is filtered,
because it is only useful if it is exact. Any capture can contain your Wi-Fi
SSID, your LAN topology and the device's signing secret, and one taken in proxy
mode also carries the PetKit account credentials their cloud issues. **Read a
capture before attaching it to an issue**, and attach only what the question
needs; `requests.jsonl`, `state_report.jsonl` and `mqtt.jsonl` usually answer it
on their own.

### Supported versions

The latest release only. This is a single-maintainer project with no backport
branch.
