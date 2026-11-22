<!-- plugin-list: en w9315273/openwrt-traffic-stats -->
<table>
  <tr><th colspan="3">My plugin list</th></tr>
  <tr valign="top">
    <td><a href="https://github.com/w9315273/openwrt-traffic-stats"><img src="https://img.shields.io/github/stars/w9315273/openwrt-traffic-stats?style=flat&amp;label=%E2%98%85&amp;color=57606a" height="16" alt="OpenWrt Traffic Stats · GitHub stars"></a></td>
    <td><strong>OpenWrt Traffic Stats</strong></td>
    <td width="360"><details><summary>Per-device traffic</summary><a href="https://raw.githubusercontent.com/w9315273/openwrt-traffic-stats/HEAD/docs/images/overview.png"><img src="https://raw.githubusercontent.com/w9315273/openwrt-traffic-stats/HEAD/docs/images/overview.png" width="100%" alt="OpenWrt Traffic Stats · screenshot"></a></details></td>
  </tr>
  <tr valign="top">
    <td><a href="https://github.com/w9315273/luci-app-adguardhome"><img src="https://img.shields.io/github/stars/w9315273/luci-app-adguardhome?style=flat&amp;label=%E2%98%85&amp;color=57606a" height="16" alt="AdGuard Home for LuCI · GitHub stars"></a></td>
    <td><strong><a href="https://github.com/w9315273/luci-app-adguardhome">AdGuard Home for LuCI</a></strong></td>
    <td width="360"><details><summary>AdGuard Home management</summary><a href="https://raw.githubusercontent.com/w9315273/luci-app-adguardhome/HEAD/docs/screenshots/base-en.png"><img src="https://raw.githubusercontent.com/w9315273/luci-app-adguardhome/HEAD/docs/screenshots/base-en.png" width="100%" alt="AdGuard Home for LuCI · screenshot"></a></details></td>
  </tr>
  <tr valign="top">
    <td><a href="https://github.com/w9315273/luci-app-pulse"><img src="https://img.shields.io/github/stars/w9315273/luci-app-pulse?style=flat&amp;label=%E2%98%85&amp;color=57606a" height="16" alt="LuCI Pulse · GitHub stars"></a></td>
    <td><strong><a href="https://github.com/w9315273/luci-app-pulse">LuCI Pulse</a></strong></td>
    <td width="360"><details><summary>System monitor</summary><a href="https://raw.githubusercontent.com/w9315273/luci-app-pulse/HEAD/docs/screenshot-en.png"><img src="https://raw.githubusercontent.com/w9315273/luci-app-pulse/HEAD/docs/screenshot-en.png" width="100%" alt="LuCI Pulse · screenshot"></a></details></td>
  </tr>
</table>
<!-- /plugin-list -->

# OpenWrt Traffic Stats

**English** | [简体中文](README.zh-CN.md)

Per-device traffic statistics powered by nftables, with IPv4 / IPv6, multiple LAN/WAN devices, multiple languages, and mobile support.

## Screenshots

<img src="images/overview.png" alt="Overview" width="1000">

<img src="images/history.png" alt="History" width="1000">

## Installation and Configuration

Install `luci-app-traffic-stats` to pull in the `traffic-stats` backend and dependencies automatically. No pip packages are needed. Update both packages when upgrading; collection restarts automatically.

Open **Services → Traffic Statistics → Settings**, select LAN/WAN devices, enable collection, then save and apply. Use actual network device names such as `br-lan` or `eth0`; the two lists must not overlap.

Configuration file: `/etc/config/traffic-stats`.

| Option | Default | Description |
| --- | --- | --- |
| `enabled` | `0` | Collection disabled by default |
| `lan_devices` | `br-lan` | LAN device list |
| `wan_devices` | Empty | WAN device list; configure before enabling |
| `interval` | `5` | Sampling interval, 1–3600 seconds |
| `commit_interval` | `60` | Database write interval in seconds; between the sampling interval and 24 hours |
| `retention` | `3` | Calendar months including the current month; 1–120, or `0` for unlimited retention |
| `proxy_containers` | Empty | Local proxy container names |
| `storage_dir` | `/etc/traffic-stats` | Storage directory |

The database write interval accepts `30s`, `5m`, or `1h` in LuCI. Advanced settings offer Docker proxy container selection with automatic address and published-port tracking. Host networking is excluded; the option is hidden when no eligible containers exist.

## Usage

- **Overview**: Current rates and monthly usage, sorted by total usage by default. With multiple LANs, filter devices without changing global totals or device history.
- **Interaction**: Page refresh defaults to 3 seconds, independently of sampling. Refresh and LAN selections are remembered in the browser. Hover over the status icon for details or click to pin; long names and addresses scroll horizontally.
- **History**: Browse days in the calendar, with one chart point every 30 minutes. Only saved data is shown; hollow points indicate incomplete records.
- **Retention**: Stores 30-minute details and daily totals. Three months means the current month and the previous two; expired details and totals are deleted together.
- **Names**: Click a device name in the overview to edit it. Unicode and spaces are supported. Matching priority is **MAC + IP > MAC > IP**, then rule order. Identical names do not merge devices.

## Accounting Scope

Counts forwarding between selected LAN/WAN devices, the router's WAN traffic, and all client communication with selected proxy containers. Proxy traffic is attributed to clients without counting the container's outgoing forwarding twice. This does not cover all Docker or VPN traffic.

Addresses sharing a MAC belong to one device; unattributed traffic appears under “Unidentified devices”. Flow offloading, hardware acceleration, or traffic bypassing selected paths may cause undercounting. IP-layer usage may differ from ISP billing.

Collection does not backfill earlier traffic. A normal stop saves pending data; power loss or write failures may lose unsaved usage.

## Storage and Backups

The storage directory contains `traffic.db` for usage and `devices.json` for naming rules. It is created automatically, but its parent must exist and external storage must be mounted first. Changing directories copies available original data; existing destination files take precedence.

OpenWrt backups include the default directory; back up custom directories separately. Run `/etc/init.d/traffic-stats stop` before backups or firmware upgrades, and `/etc/init.d/traffic-stats start` after a manual backup.

## Troubleshooting

```sh
/etc/init.d/traffic-stats status
ubus call traffic-stats status '{}'
logread -e traffic-stats
```

## License

[GNU GPL v3.0 only](../LICENSE) (`GPL-3.0-only`).
