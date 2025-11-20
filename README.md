# 1Panel v2 离线安装包制作

准备好的 `prepare_offline.sh` 会拉取 v2 在线安装包，并把 Docker 与 docker-compose 一起打进离线包，安装脚本会优先使用本地的 `docker.tgz` 与 `docker-compose`，无需外网即可完成部署。

## 环境要求
- `bash`、`curl`、`tar`、`python3`
- 写入权限（脚本会在 `build/` 下缓存下载内容并输出离线包）

## 制作步骤
```bash
cd v2
chmod +x prepare_offline.sh
# 示范：只打 amd64，使用 stable 渠道的 v2.0.13
./prepare_offline.sh --app_version v2.0.13 --mode stable --arch amd64 --docker_version 24.0.7 --compose_version v2.23.0
```

- `--arch` 支持空格或逗号分隔，默认同时生成 `amd64 arm64 armv7 ppc64le s390x`。
- 产物位置：`build/<version>/1panel-<version>-offline-linux-<arch>.tar.gz`，同目录生成 `checksums.txt`。
- 下载缓存：`build/cache/`，可复用后续构建。

## 离线安装
在目标机器解压对应架构的离线包后，直接执行其中的 `install.sh`，脚本会自动安装本地内置的 Docker 与 docker-compose，并继续原有的交互式安装流程（需 root）。

## 离线升级
- 目标机器解压离线包后，直接执行 `upgrade.sh`（需 root），它会保留原有端口、账户信息、入口路径并替换最新二进制/语言包，随后自动重启服务。
