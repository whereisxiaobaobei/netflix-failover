# Netflix 出口自动切换

定时任务每小时通过实际的 Xray 出站检查 Netflix 非自制剧目录。初始主节点为 ByteVirt，备用节点为 NiiHost。之后每次先检查当前主节点。只有主节点连续两轮显示受限、备用节点连续两轮显示可用，脚本才交换 `Netflix` 分流组的 `selector` 与 `fallbackTag`。两个节点都受限时保留当前顺序。

## 怎么判断

脚本为每个出口建立临时本地 SOCKS 端口，匿名请求 Netflix 英文片名页。以《Stranger Things》为对照，检查《LEGO Ninjago》和《Breaking Bad》。对照正常且至少一部非自制剧的片名、影片 ID 和 Netflix 视频元数据匹配时，判为 `unlocked`。对照正常且两部非自制剧页面均明确提示地区受限时，判为 `restricted`。单个页面遇到超时、验证页或证据不足时记为 `unknown`。汇总结果不足以判定可用或受限，或两轮汇总结果不一致时，保留当前顺序。

这个探测反映匿名片名页展示的目录信息。账户实际播放、订阅套餐和 DRM 授权需要另行验证。片库或页面格式变动可能影响判断。

## 部署条件

- Linux 使用 systemd，已安装 Python 3.8 或更新版本、curl 和 Docker。服务由 root 运行。
- 已运行名为 `xray` 的容器，使用 host 网络，主机目录 `/opt/xray/config` 挂载到容器的 `/etc/xray`，容器内 Xray 位于 `/usr/local/bin/xray`。
- `/opt/xray/config/config.json` 已有名为 `Netflix` 的 balancer，Netflix 路由使用该 balancer。两个出口标签应与 `netflix-failover.json` 对应，每个标签作为前缀只匹配一个出站。节点连接参数由本机已有的 Xray 配置提供。

## 安装

如果这台机器已有管理同一 Netflix 分流组的任务，先停用旧定时器，等待正在运行的旧服务结束，并检查旧实例的状态目录。若仍有 `pending.json`，先让旧实例完成恢复并确认 Xray 正常，再启用本版本。两个任务使用各自的锁和状态目录，同时运行会争用同一份生产配置。

在仓库目录以 root 执行。

```sh
install -d /usr/local/lib/netflix-failover
install -m 644 netflix-failover.py netflix_probe.py /usr/local/lib/netflix-failover/
install -m 600 netflix-failover.json /etc/netflix-failover.json
install -m 644 netflix-failover.service netflix-failover.timer /etc/systemd/system/
systemctl daemon-reload
```

已有这对主备节点时，先手动探测两端，再启用每小时任务。

```sh
python3 /usr/local/lib/netflix-failover/netflix-failover.py --check-all
systemctl enable --now netflix-failover.timer
```

`--check-all` 本轮只探测两端，不根据探测结果切换。启动时若发现上一次切换未完成，脚本会先尝试恢复原配置并重启 Xray，再继续探测。

首次接入已有其他主备配置的 `Netflix` 分流组时，可在启用定时器前显式运行 `python3 /usr/local/lib/netflix-failover/netflix-failover.py --initialize`。该命令会按 JSON 中的初始主备开始探测，**可能写入生产配置并重启 Xray**。若 ByteVirt 受限而 NiiHost 可用，最终主节点可以是 NiiHost。

## 维护与测试

| 用途 | 命令或文件 |
| --- | --- |
| 查看下一次检查 | `systemctl list-timers netflix-failover.timer` |
| 立即执行一次自动检查，可触发切换 | `systemctl start netflix-failover.service` |
| 查看执行日志 | `journalctl -u netflix-failover.service -n 30 --no-pager` |
| 查看最近探测详情 | `/var/lib/netflix-failover/latest.json` |
| 更换探测影片 | 修改 `/etc/netflix-failover.json`，保留一部对照和两部非自制剧，使用对应的英文片名及影片 ID |
| 关闭定时检查 | `systemctl disable --now netflix-failover.timer` |
| 在仓库目录运行离线测试 | `python3 -B -m unittest discover -v` |

切换前脚本会用相同镜像校验候选配置，并把生产配置备份到 `/var/lib/netflix-failover/config-*.json`。重启后会核对容器读到的配置，自动切换时还会复测新主节点。校验失败时尝试恢复备份。备份包含完整的本机 Xray 配置，应保存在本机。
