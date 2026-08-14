#!/usr/bin/env bash
# 内网运维管理平台 · 离线部署包制作脚本
# 在【有外网的打包机】上运行（本机 docker 走代理时先 export 代理，见下）。
# 产出：deploy/offline/ops-platform-offline-<日期>.tar.gz，拷入内网服务器即可部署。
#
# 代理（本机 Clash）：如拉镜像/下载依赖失败，取消下面两行注释
# export http_proxy=http://127.0.0.1:7897
# export https_proxy=http://127.0.0.1:7897
set -euo pipefail

# 项目根目录（脚本位于 deploy/ 下）
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE_DIR="$ROOT_DIR/deploy/offline"
PKG_NAME="ops-platform-offline-$(date +%Y%m%d)"
STAGE_DIR="$OFFLINE_DIR/$PKG_NAME"

DB_IMAGE="${OPS_DB_IMAGE:-timescale/timescaledb:latest-pg15}"

echo "==> [1/6] 清理并创建离线打包目录：$STAGE_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/wheels"

echo "==> [2/6] 下载 Python 依赖 wheel（供裸机部署或重建镜像用，可选但建议带上）"
# 打包机可能没有全局 pip：优先 PATH 里的 pip，退回项目 venv 的 pip，再退回 python3 -m pip
if command -v pip >/dev/null 2>&1; then
    PIP="pip"
elif [ -x "$ROOT_DIR/backend/.venv/bin/pip" ]; then
    PIP="$ROOT_DIR/backend/.venv/bin/pip"
else
    PIP="python3 -m pip"
fi
echo "    使用：$PIP"
# --python-version 311 匹配生产镜像 python:3.11-slim，使 wheels 真正可用于内网裸机/离线重建镜像
# （否则下载的是打包机 Python 版本的平台 wheel，如 cp313，与生产 py3.11 不匹配装不上）
$PIP download --python-version 311 --only-binary=:all: -r "$ROOT_DIR/backend/requirements.txt" -d "$STAGE_DIR/wheels"

echo "==> [3/6] 拉取数据库镜像：$DB_IMAGE"
# 代理不可用时 pull 会失败；本地已有该镜像则直接用本地版（离线重打场景常见）
docker pull "$DB_IMAGE" || docker image inspect "$DB_IMAGE" >/dev/null

echo "==> [4/6] 构建 api / web 镜像"
docker compose -f "$ROOT_DIR/deploy/docker-compose.full.yml" build api web

echo "==> [5/6] 导出全部镜像为 images.tar"
docker save -o "$STAGE_DIR/images.tar" \
    "$DB_IMAGE" \
    ops-platform-api:latest \
    ops-platform-web:latest

echo "==> [6/6] 拷贝部署文件并打 tar.gz"
cp "$ROOT_DIR/deploy/docker-compose.full.yml" "$STAGE_DIR/docker-compose.yml"
cp "$ROOT_DIR/deploy/.env.example"           "$STAGE_DIR/.env.example"
cp "$ROOT_DIR/deploy/nginx.conf"             "$STAGE_DIR/nginx.conf"
cp "$ROOT_DIR/deploy/Dockerfile.api"         "$STAGE_DIR/Dockerfile.api"
cp "$ROOT_DIR/deploy/Dockerfile.web"         "$STAGE_DIR/Dockerfile.web"
cp "$ROOT_DIR/docs/管理员手册.docx"          "$STAGE_DIR/管理员手册.docx"
cp "$ROOT_DIR/docs/用户手册.docx"            "$STAGE_DIR/用户手册.docx"
cp "$ROOT_DIR/docs/管理员手册.md"            "$STAGE_DIR/管理员手册.md"
cp "$ROOT_DIR/docs/用户手册.md"              "$STAGE_DIR/用户手册.md"
cp "$ROOT_DIR/docs/部署手册.md"              "$STAGE_DIR/部署手册.md"
tar -C "$OFFLINE_DIR" -czf "$OFFLINE_DIR/$PKG_NAME.tar.gz" "$PKG_NAME"

echo ""
echo "完成：$OFFLINE_DIR/$PKG_NAME.tar.gz"
echo "内网部署：上传 tar.gz → 解压 → cd $PKG_NAME → docker load -i images.tar → 详见 管理员手册.docx"
