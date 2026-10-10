# 1Panel v2 离线包生成器

[English](README.md)

本仓库负责构建、校验、打包和离线安装 1Panel v2。生成器内置按架构锁定的 Docker、Compose，修改已识别的安装接口，并保留上游应用资源。

## 构建方式

使用 GitHub Actions 中的 **Build 1Panel v2 Offline** 工作流（`.github/workflows/build-offline-v2.yml`）。选择通道和应用版本；普通构建的版本留空时，会解析该通道的最新版本。

- `build`：解析本次输入，独立构建各产品分支，通过对应原生验收后发布。
- `validate-repair`：构建、校验明确指定版本和标签的修复候选包，不修改公开发布。
- `repair-existing`：经过同一套门禁后，以保留备份的方式修复指定公开发布。

每次运行自动解析源码提交、配置原文、生产者凭据、官方及企业版资源可用性和架构清单。后续步骤必须使用该次运行经过认证的计划及其 SHA-256。仓库不维护逐应用版本的白名单、发布矩阵、内嵌配置登记表或旧验证器快照；新版本无需新增版本配置。

official、custom、enterprise-original、enterprise-docker 是独立产品分支。发现或下载失败必须保留为明确失败，不能伪装成资源不存在。只有通过本产品必需校验的资产可以发布；失败分支保留在 Actions 和回执中。没有任何合格产品时发布失败。

`docker-sources.json` 和 `compose-sources.json` 仅保存通用依赖的按架构 URL、版本、字节数及 SHA-256。依赖更新需要验证真实文件和 ELF 架构。

旧 CNB 配方、独立的已发布包 smoke 工作流、仅刷新回执模式，以及独立修改发布资产的旧脚本已退出。支持的构建发布入口是 GitHub 工作流，历史源码保留在 Git 历史中。这次清理不代表完成了 CNB 原生运行验证。

## 包内容与离线安装

社区包名为 `1panel-<版本>-<来源>-offline-linux-<架构>.tar.gz`，包含应用、Docker 所需二进制、Compose、服务文件、安装升级脚本及记录来源和载荷哈希的 `offline-manifest.json`。

enterprise-original 保留上游压缩包原始字节；enterprise-docker 保留上游内部目录、升级脚本和其他原文件，补充 Docker/Compose 并修改安装脚本。是否需要 AppStore 由认证包的内容及安装接口决定。发布校验逐文件核对原件是否保持不变。

下载合格资产和校验文件，验证 SHA-256 后传入目标机器并解压，在解压目录执行：

```bash
sudo ./install.sh
```

升级已有安装时使用包内 `upgrade.sh`。社区包使用本仓库的离线升级器；企业版保留上游升级器。升级前备份，并选择与目标机器架构相符的包。

全新 Docker 默认网络安装要求 systemd 和已经离线安装的 iptables。健康的本地 Docker 引擎会保留。冲突的 `DOCKER_HOST`、`DOCKER_CONTEXT`、非默认 context 或自定义 socket 需要手动准备。安装器不会覆盖 `daemon.json`，也不会在线安装缺失的系统依赖。

## 开发与验证

打包脚本只消费工作流计划器绑定的 `ONEPANEL_RESOLVED_PLAN` 与 `ONEPANEL_RESOLVED_PLAN_SHA256`。仅放入本地 JSON 文件不会启用旧配置回退。具体流程见[发布说明](docs/publication-runbook.md)。

```bash
python3 -m pip install -r requirements-validation.txt
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n prepare_offline.sh upgrade_offline.sh quick_start.sh
```

测试使用合成认证输入及按内容哈希命名的历史安装脚本文本，覆盖异常输入、压缩包/路径/ELF、精确生产配置、来源与清单篡改、离线 shell 行为和发布门禁。它们不执行下载的应用二进制，也不等同于真实原生安装通过。

真实安装仅在一次性 GitHub-hosted amd64/arm64 主机上进行，覆盖已有和全新 Docker。社区升级还需通过认证前驱版本测试。其他架构进行静态验证，不隐含原生运行通过。参见[原生验证](docs/native-runtime-validation.md)、[打包流程](docs/package-matrix.md)和[发布并发](docs/workflow-concurrency.md)。
