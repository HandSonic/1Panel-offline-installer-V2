# 1Panel v2 离线包生成器

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-English-blue" alt="English"></a>
  <a href="https://github.com/1Panel-dev/1Panel"><img src="https://img.shields.io/badge/Upstream-1Panel-blue?logo=github" alt="Upstream"></a>
  <img src="https://img.shields.io/badge/Type-Offline%20Installer-orange" alt="Type">
  <img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License">
</p>

一个用于生成 1Panel v2 **全离线安装包**的专用工具。

该工具解决了在受限网络（内网/无互联网）环境中安装现代容器化软件的“鸡生蛋”问题：它将 Docker 引擎和 Docker Compose 二进制文件直接**内置**到 1Panel 安装包中，并修改安装逻辑以使用这些本地资源，而不再尝试下载。

## 📖 目录

- [工作原理](#-工作原理)
- [前置要求](#-前置要求)
- [使用指南](#-使用指南)
- [命令参考](#-命令参考)
- [输出目录](#-输出目录)
- [安装指南](#-安装指南)
- [开发者说明](#-开发者说明)

## 💡 工作原理

生成器执行“补丁与重打包”操作：

```mermaid
flowchart LR
    Start([开始]) --> Source{来源?}
    Source -- "官方" --> GetOff[下载官方包]
    Source -- "自定义" --> GetCust[下载自定义包]
    
    GetOff & GetCust --> GetDocker[下载 Docker 静态文件]
    GetDocker --> GetCompose[下载 Docker Compose 文件]
    
    GetCompose --> Patch[通过 Python 修改 install.sh]
    Patch --> Repack[重新打包成离线包]
    
    Repack --> End([完成])
    
    style Start fill:#f9f,stroke:#333
    style End fill:#f9f,stroke:#333
    style Patch fill:#ff9,stroke:#f66
```

## ✅ 前置要求

确保您的环境（Linux/macOS/WSL）中已安装以下工具：
*   `bash`: 脚本解释器。
*   `curl`: 用于下载上游资源。
*   `tar`: 用于解压和重打包。
*   `python3`: **关键组件**。用于安全地修补 `install.sh` 脚本而不破坏复杂的逻辑。

## 🛠️ 使用指南

### 1. 基础构建（推荐）
为 1Panel 最新稳定版生成离线包。这将下载官方在线包并进行转换。

```bash
cd v2
chmod +x prepare_offline.sh
./prepare_offline.sh
```

### 2. 高级构建
指定 1Panel 版本、Docker 版本，或一次性支持多个架构。

```bash
./prepare_offline.sh \
  --app_version v2.0.13 \
  --mode stable \
  --arch "amd64,arm64" \
  --docker_version 24.0.7 \
  --compose_version v2.23.0
```

## 📚 命令参考

| 标志 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--app_version` | *最新稳定版* | 要打包的 1Panel 版本 (例如 `v2.10.0`)。 |
| `--mode` | `stable` | 更新通道：`stable`, `beta`, 或 `dev`。 |
| `--arch` | *全部* | 目标架构。逗号或空格分隔 (例如 `amd64,arm64`)。 |
| `--source` | `both` | `official` (官方发布), `custom` (GitHub 发布), 或 `both`。 |
| `--custom_repo` | *默认仓库* | 获取自定义构建的 GitHub 仓库 (如果来源是 custom)。 |
| `--docker_version` | `24.0.7` | 要绑定的 Docker 静态二进制版本。 |
| `--compose_version` | `v2.23.0` | 要绑定的 Docker Compose 版本。 |
| `--allow-missing` | `false` | 如果为真，缺失架构构件时仅警告而不报错。 |
| `--interactive` | `false` | 交互模式以确认版本。 |

## 📂 输出目录

构建产物存储在 `build/` 目录中，按版本和来源组织。

```text
build/
└── v2.0.13/
    ├── checksums.txt                                      # SHA256 校验和
    ├── official/
    │   └── 1panel-v2.0.13-official-offline-linux-amd64.tar.gz
    └── custom/
        └── 1panel-v2.0.13-custom-offline-linux-amd64.tar.gz
```

## 💿 安装指南

### 终端用户安装（离线）

1.  **传输**：通过 USB、SCP 等方式将 `offline` 离线包复制到服务器。
2.  **安装**：
    ```bash
    tar -zxf 1panel-v2.0.13-official-offline-linux-amd64.tar.gz
    cd 1panel-v2.0.13-official-offline-linux-amd64
    sudo ./install.sh
    ```
    > 安装程序会自动检测内置的 Docker 二进制文件并自动安装。

### 终端用户升级（离线）

1.  **传输**：将相同的包复制到服务器。
2.  **升级**：
    ```bash
    tar -zxf ...tar.gz
    cd ...
    sudo ./upgrade.sh
    ```
    > **注意**：`upgrade.sh` 是此生成器添加的特殊脚本。它负责安全地停止服务、替换二进制文件和迁移配置。

## 👨‍💻 开发者说明

**注入机制**：
脚本使用 python 定位 `install.sh` 中的特定标记（如 `PASSWORD_MASK` 或 `Install_Docker` 函数定义），并注入代码指向本地的 `docker.tgz` 和 `docker-compose` 文件。这确保了即使官方 `install.sh` 发生细微变化，只要锚点存在，补丁即使在未来版本中也能保持稳健。

---
<p align="center">Made with ❤️ by the Open Source Community</p>
