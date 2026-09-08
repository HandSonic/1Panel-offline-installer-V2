# 1Panel v2 自适应离线包

[English](README.md)

将官方包或 HandSonic/1Panel-Build-v2 自编译包与 Docker、Compose 合并，生成可直接传入离线服务器的安装/升级包。

## 构建

需要联网的 Linux/macOS/WSL、Bash、Python 3.9+。不执行下载的二进制；Python 标准库校验 ELF 架构、归档路径和完整性。

```bash
bash prepare_offline.sh --source both --arch amd64,arm64 --allow-missing
# 精确指定应用版本；Docker/Compose 指定值是优先选择，不可用时自动回退
bash prepare_offline.sh --app_version v2.2.5 --docker_version 27.5.1 --compose_version v2.30.3 --allow-missing
```

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| --mode | stable | stable / beta / dev |
| --app_version | 通道最新 | 1Panel 版本精确匹配，绝不静默降到历史版本 |
| --source | both | official / custom / both |
| --custom_repo | HandSonic/1Panel-Build-v2 | 自编译 GitHub Release 来源 |
| --custom_dist | 不启用 | 直接使用本地 Build 仓库的 dist，需显式 --app_version；校验 manifest、SHA256 和架构 |
| --arch | amd64 arm64 armv7 ppc64le s390x loong64 riscv64 | 空格/逗号分隔，loongarch64 自动归一化 |
| --docker_version / --compose_version | latest | 首选版本 → 多源 → 实际可用 Release 资产 → 有效缓存 → 兼容历史版本 |
| --allow-missing | 关闭 | 缺失的来源/架构单独跳过，保留其他成功包 |
| --interactive | 关闭 | 终端中确认或覆盖应用版本 |

下载失败自动重试、切换来源；损坏缓存自动修复。缓存按 URL 隔离，换 custom_repo 或通道不会复用同名错误文件。没有再使用固定 8MB 阈值。API 暂时不可用时可使用缓存的发现结果；有 GitHub token 时自动认证以减少限流。

## 安装

```bash
sha256sum -c checksums.txt
tar -xzf 1panel-v2.2.5-official-offline-linux-amd64.tar.gz
cd 1panel-v2.2.5-official-offline-linux-amd64
sudo bash install.sh
```

安装入口由本仓库维护，原上游脚本完整保存在 install-upstream.sh。入口先准备本地 Docker/Compose，再把参数原样传给上游；不再匹配上游函数名、提示语或缩进。已有可用 Docker/Compose 直接沿用，不强制覆盖；尚未安装时支持 systemd/OpenRC/SysV 启动。上游安装器的普通 curl/wget 调用被离线环境快速阻止，避免公网探测长时间等待。

主机仍需具备内核、cgroups、iptables 等 Docker 运行条件。此包包含面板与容器引擎，不包含应用商店全部镜像；镜像需另外导入。quick_start.sh 是校验哈希的官方**在线**安装入口，不是离线入口。

## 升级和恢复

```bash
sudo bash upgrade.sh
# 启动较慢的主机可延长健康检查等待
sudo PANEL_HEALTH_TIMEOUT=120 bash upgrade.sh
```

先停服并备份数据库（含 WAL）、配置、服务文件、二进制及资源，再执行替换。两个服务持续健康后才成功；实质更新失败自动还原完整备份。短暂启动错误会等待恢复，缺少可选语言/GeoIP 时保留已安装资源。已有服务配置与未知 1pctl 配置项优先保留。Docker/Compose 不随面板升级。

默认阻止明确降级；有兼容备份时可用 --force。PANEL_BASE_DIR_OVERRIDE 可修正安装目录识别；沿用上游 1pctl 的配置格式，不承诺修复其原生 shell 特殊路径限制。备份会保留在升级包目录，升级包不能放在将被备份/替换的 db/conf/config/geo 目录内。

## 产物和自动发布

产物位于 build/<版本>/<official|custom>/。总 manifest.json 逐项记录 built/skipped、原因、实际依赖版本、来源、SHA256、构建指纹；包内也有 manifest.json。checksums.txt 使用 Release 附件的 basename。

GitHub 和 CNB 使用相同的解析、打包和 staging 代码。GitHub 只有完整 manifest、指纹和所有附件一致时才跳过；组件版本、脚本或 custom 资产变化会触发重建。部分成功可以发布，缺失项由后续任务重试；零产物不创建空 Release。新 GitHub Release 先以草稿上传验证，manifest 最后写入；修补已有 Release 时先撤销旧完成标记，中断后自动重试。补历史版本不会故意覆盖较新的 latest。

CNB 不再以 tag 存在判断成功，每次任务都重新检查并构建可用产物，使用原生附件上传。上传中断会在后续任务重试。已有 Release 的更新不是 GitHub/CNB 提供的原子事务，消费者应以当前 manifest/checksums 为准。

修复分支和 PR 会跑无发布的回归测试；修复分支还包含实际构建与 VM 验收工作流。GitHub 正式发布仅发生在 master。手动在其他分支运行构建会保留 Actions 工件供检查。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖上游布局变化、下载 404/截断/错架构、缓存恢复、备用版本、部分发布、升级失败回滚和在线入口哈希校验。常规脚本格式和资产版本变化自动适配；上游若改变安装协议、必需文件含义或运行时要求，仍可能需要兼容更新，不能把不可用产物标为成功。

`e2e-offline.yml` 消费配套 Build 仓库 `e2e-build.yml` 的实际 Actions 产物，使用 `--custom_dist` 打包，避免复用旧 Release。当前验收固定 v2.2.4 → v2.2.5 以便复现；正式构建仍默认自动发现最新版本。

VM 验收先在一次性 Ubuntu 24.04 amd64 虚拟机中准备 OS 依赖，第二次启动移除网卡，真实验证全新安装、Docker/Compose 容器、面板 HTTP、跨版本升级和服务启动失败后的回滚。只对本次需启动的服务清除 systemd 失败计数，避免恢复旧二进制后仍被启动限流。产物清单、构建日志、VM 网络信息和服务日志保存在 Actions 工件中；七架构打包检查不等于七架构启动验收。

本地复现（运行主机需安装 QEMU、cloud-image-utils、curl 和 Python 3.11+）：

```bash
bash prepare_offline.sh --source custom --app_version v2.2.5 --arch amd64 --custom_dist ../1Panel-Build-v2/dist
bash scripts/e2e/run-vm.sh TARGET_OFFLINE.tar.gz BASELINE_OFFLINE.tar.gz ./e2e-results
```
