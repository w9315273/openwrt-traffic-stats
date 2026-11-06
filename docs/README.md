# OpenWrt Traffic Stats

**English** | [简体中文](README.zh-CN.md)

Per-device traffic statistics for OpenWrt, using nftables. Supports IPv4, IPv6, multiple LAN/WAN network devices, multiple languages, and mobile browsers. The interface follows the LuCI language setting.

## Screenshots

<img src="images/overview.png" alt="Overview" width="1000">

<img src="images/history.png" alt="History" width="1000">

## Installation

The plugin consists of `traffic-stats` and `luci-app-traffic-stats`. The backend requires `python3-light`, `python3-sqlite3`, `nftables-json`, `uci`, `ubus` and `rpcd`; the frontend requires `luci-base`. No pip packages are needed.

Install these packages and their dependencies using your firmware's package manager.

## Configuration

Collection is disabled by default. Open **Network → Traffic Statistics → Settings**, select the LAN/WAN devices, enable collection, then save and apply. Use actual Linux network device names such as `br-lan`, `eth0` or `pppoe-wan`. Each side accepts multiple devices, but the two lists must not overlap.

Configuration is stored in `/etc/config/traffic-stats`:

| Option | Default | Description |
| --- | --- | --- |
| `enabled` | `0` | Enable collection |
| `lan_devices` | `br-lan` | LAN device list |
| `wan_devices` | Empty | WAN device list; configure before enabling collection |
| `interval` | `5` | Sampling interval, 1–3600 seconds |
| `commit_interval` | `60` | Database write interval in seconds; at least the sampling interval, up to 24 hours |
| `retention` | `3` | Calendar months to retain, including the current month; 1–120, or `0` to keep history forever |
| `proxy_containers` | Empty | Names of proxy containers on this router |
| `storage_dir` | `/etc/traffic-stats` | Persistent storage directory |

In LuCI, the database write interval accepts values such as `30s`, `5m` or `1h`, converted to seconds when saved. The overview's 1/3/5/10-second refresh options only control page refreshes.

Advanced settings let you select multiple local Docker proxy containers. This requires a working Docker service and CLI. Container addresses and published host ports are tracked automatically. Host network mode and manual IP entry are not supported.

## Usage and History

- **Overview**: Shows current rates and monthly usage, including both saved and pending usage. Devices are sorted by combined monthly upload and download usage in descending order by default. The router is included in the same sort order.
- **History**: Browse days in the calendar and view a full-day chart from 00:00 to 24:00, with one point every 30 minutes. Only saved data is queried, so history may temporarily lag behind the overview.
- **Retention**: Stores 30-minute detail records and daily totals; monthly totals are calculated from daily records. Retaining 3 months means the current month and the previous two. Expired detail records and daily totals are deleted together. The database is not split into monthly files.

Accounting covers forwarding between the selected LAN/WAN devices, the router's own WAN traffic, and communication between LAN clients and selected proxy containers. Proxy usage is attributed at the client side, excluding duplicate forwarding counts on the container's other side. Other client communication with a selected container is also counted. This is not general monitoring of all Docker or VPN traffic.

Recognized addresses with the same MAC belong to one device. Traffic that cannot be attributed is listed under “Unidentified devices”. Flow offloading, hardware acceleration or connections bypassing the selected paths may cause undercounting. Usage measures IP-layer bytes and may differ from your ISP's bill.

On first use, collection establishes a baseline; earlier traffic is not added retroactively. A normal stop saves pending data. Power loss or persistent write failures may result in lost unsaved usage. Clock changes, counter resets and collection interruptions are marked; hollow chart points indicate incomplete records.

## Storage and Device Names

Within the storage directory, `traffic.db` stores usage and `devices` stores naming rules in UCI format. The default directory is created automatically. A custom directory must already exist, and external storage must be mounted first. When changing directories, existing data is copied if the destination file is missing and the original can be found. Existing destination files take precedence.

OpenWrt backups include the default `/etc/traffic-stats/` directory. Stop the collector with `/etc/init.d/traffic-stats stop` before backing up or upgrading firmware to keep the database consistent; restart it after a manual backup. Back up custom storage directories separately.

Click a device name in the overview to edit it. Chinese characters and spaces are supported. Rules can match MAC, IP or both, with priority **MAC + IP > MAC > IP**. Rules with equal priority follow configuration order. Names only affect display; devices with the same name are not merged.

## Troubleshooting

```sh
/etc/init.d/traffic-stats status
ubus call traffic-stats status '{}'
logread -e traffic-stats
```

## License

Licensed under [GNU GPL v3.0 only](../LICENSE) (`GPL-3.0-only`).
