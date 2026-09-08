#!/bin/bash
# Install a verified 1Panel release. Downloads are resumable by retrying this
# script; cached and fresh archives always pass the same checksum gate.
set -uo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
fetch() {
    local url=$1 dest=$2
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 1800 --output "$dest" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -T 30 -t 3 -O "$dest" "$url"
    else return 1; fi
}
sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | awk '{print $NF}'
    else return 1; fi
}

case "$(uname -m)" in
    x86_64|amd64) architecture=amd64;; aarch64|arm64) architecture=arm64;;
    armv7*|armv6*|armhf) architecture=armv7;; ppc64le) architecture=ppc64le;;
    s390x) architecture=s390x;; riscv64) architecture=riscv64;;
    loongarch64|loong64) architecture=loong64;;
    *) fail '暂不支持的系统架构，请选择受支持的安装包。';;
esac
INSTALL_MODE=${INSTALL_MODE:-stable}
case "$INSTALL_MODE" in stable|beta|dev) ;; *) fail '请输入正确的安装模式（dev、beta 或 stable）';; esac

# An explicit mirror may carry additional architectures. It must expose the
# upstream latest/version/release layout and HTTPS checksums, not a new format.
RESOURCE_BASE=${PANEL_RESOURCE_BASE:-https://resource.fit2cloud.com/1panel/package/v2}
[[ "$RESOURCE_BASE" == https://* ]] || fail '下载源必须使用 HTTPS'
RESOURCE_BASE=${RESOURCE_BASE%/}
metadata_dir=$(mktemp -d "${PWD}/.1panel-download.XXXXXX") || fail '无法创建下载临时目录'
cleanup() { rm -rf -- "$metadata_dir"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
fetch "$RESOURCE_BASE/$INSTALL_MODE/latest" "$metadata_dir/latest" || fail '获取最新版本失败，请稍后重试'
VERSION=$(tr -d '\r\n' < "$metadata_dir/latest")
[[ "$VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([.+_-][a-zA-Z0-9._-]+)?$ ]] || fail '下载源返回了无效版本号'
package_dir="1panel-${VERSION}-linux-${architecture}"
package_file_name="$package_dir.tar.gz"
release_url="$RESOURCE_BASE/$INSTALL_MODE/$VERSION/release"
fetch "$release_url/checksums.txt" "$metadata_dir/checksums" || fail '获取校验文件失败，请稍后重试'
# Exact basename match supports SHA256SUMS paths from older builders too.
expected_hash=$(awk -v name="$package_file_name" '
    NF == 2 { n=$2; sub(/^\*/, "", n); sub(/^.*\//, "", n);
              if (n == name && length($1) == 64 && $1 !~ /[^0-9a-fA-F]/) print tolower($1) }
' "$metadata_dir/checksums")
[[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || fail "没有找到 $package_file_name 的唯一 SHA256 校验值；下载源可能尚未发布此架构，请稍后重试或指定完整镜像"

actual_hash=""
if [[ -f "$package_file_name" ]]; then actual_hash=$(sha256 "$package_file_name") || fail '没有可用的 SHA256 校验工具'; fi
if [[ "$actual_hash" != "$expected_hash" ]]; then
    printf '下载 1Panel %s（%s）\n' "$VERSION" "$architecture"
    fetch "$release_url/$package_file_name" "$metadata_dir/package.tar.gz" || fail '下载安装包失败，请稍后重试'
    actual_hash=$(sha256 "$metadata_dir/package.tar.gz") || fail '没有可用的 SHA256 校验工具'
    [[ "$actual_hash" == "$expected_hash" ]] || fail '安装包校验失败，未执行安装；请稍后重试'
    mv -f -- "$metadata_dir/package.tar.gz" "$package_file_name" || fail '无法保存已校验安装包'
else printf '安装包校验通过，使用已有下载\n'; fi

# Extract into a fresh directory to avoid mixing stale files into a new release.
# Reject absolute/traversal entries before invoking tar.
tar -tzf "$package_file_name" > "$metadata_dir/entries" || fail '无法读取安装包'
while IFS= read -r entry; do
    entry=${entry#./}
    [[ "$entry" != /* && "/$entry/" != *'/../'* && ( "$entry" == "$package_dir" || "$entry" == "$package_dir/"* ) ]] || fail '安装包包含异常路径'
done < "$metadata_dir/entries"
mkdir "$metadata_dir/extracted" || fail '无法创建解压目录'
tar -xzf "$package_file_name" -C "$metadata_dir/extracted" || fail '解压失败'
[[ -f "$metadata_dir/extracted/$package_dir/install.sh" ]] || fail '安装包缺少 install.sh'
cd "$metadata_dir/extracted/$package_dir" || fail '无法进入安装目录'
/bin/bash install.sh
status=$?
exit "$status"
