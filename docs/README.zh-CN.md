# OpenWrt Traffic Stats

[English](README.md) | **简体中文**

基于 nftables 的逐设备流量统计插件，支持 IPv4 / IPv6、多 LAN/WAN、多语言和手机浏览器。

## 截图

<img src="images/overview.png" alt="概览" width="1000">

<img src="images/history.png" alt="历史记录" width="1000">

## 安装与配置

安装 `luci-app-traffic-stats` 会自动安装后端 `traffic-stats` 及依赖，无需 pip 包。升级时请同时更新前后端，采集服务会自动重启。

在 **服务 → 流量统计 → 设置** 中选择 LAN/WAN 设备，启用并保存应用。使用 `br-lan`、`eth0` 等实际网络设备名，两侧不能重叠。

配置文件：`/etc/config/traffic-stats`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `0` | 默认禁用采集 |
| `lan_devices` | `br-lan` | LAN 设备列表 |
| `wan_devices` | 空 | WAN 设备列表，启用前填写 |
| `interval` | `5` | 采样间隔，1–3600 秒 |
| `commit_interval` | `60` | 数据库写入间隔，秒；不短于采样间隔，最长 24 小时 |
| `retention` | `3` | 保留的自然月数，包含当月；1–120，`0` 无限保存 |
| `proxy_containers` | 空 | 本机代理容器名称列表 |
| `storage_dir` | `/etc/traffic-stats` | 存储目录 |

界面的数据库写入间隔可填 `30s`、`5m`、`1h`。高级设置可多选 Docker 代理容器，自动跟踪地址和映射端口；排除 host 网络模式，没有可选容器时隐藏。

## 使用

- **概览**：实时速率与本月累计，默认按累计用量降序排列。多个 LAN 时可筛选设备；筛选不改变总量或设备历史。
- **交互**：刷新间隔默认 3 秒，与采样间隔独立；刷新和接口选择保存在浏览器中。状态图标悬停查看、点击固定，长名称和地址可横向滑动。
- **历史**：月历按日浏览，曲线每 30 分钟一个点。仅显示已保存数据，空心点表示记录不完整。
- **保留**：保存 30 分钟明细和每日汇总。保留 3 个月即当月和前两个月，过期明细与汇总一起删除。
- **名称**：点击概览设备名编辑，支持中文和空格。匹配优先级为 **MAC + IP > MAC > IP**，同级按规则顺序；同名设备不会合并。

## 统计范围

统计所选 LAN/WAN 间的转发、本机 WAN 流量，以及客户端与所选代理容器的全部通信。代理流量按客户端归属，排除容器另一侧的重复计数，不涵盖所有 Docker 或 VPN 流量。

同一 MAC 的地址归入同一设备，无法归属的流量列为“未识别设备”。流量卸载、硬件加速或绕过所选路径可能造成漏计；IP 层用量不等同于运营商账单。

首次采集不补记此前流量。正常停止会保存暂存数据，断电或写入失败可能丢失未保存用量。

## 存储与备份

存储目录内，`traffic.db` 保存流量，`devices.json` 保存名称规则。目录自动创建，但父目录必须存在，外置存储需先挂载。更换目录时会复制可找到的原数据，已有目标文件优先使用。

默认目录包含在 OpenWrt 备份中，自定义目录需单独备份。备份或升级固件前执行 `/etc/init.d/traffic-stats stop`；手动备份完成后执行 `/etc/init.d/traffic-stats start`。

## 排查

```sh
/etc/init.d/traffic-stats status
ubus call traffic-stats status '{}'
logread -e traffic-stats
```

## 许可证

[GNU GPL v3.0 only](../LICENSE)（`GPL-3.0-only`）。
