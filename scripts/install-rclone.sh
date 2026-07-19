#!/usr/bin/env bash
set -euo pipefail

RCLONE_VERSION="1.74.4"
RCLONE_ARCHIVE="rclone-v${RCLONE_VERSION}-linux-amd64.zip"
RCLONE_SHA256="fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
INSTALL_ROOT="${RUNNER_TEMP:?RUNNER_TEMP is required}/rclone-v${RCLONE_VERSION}"
ARCHIVE_PATH="${INSTALL_ROOT}/${RCLONE_ARCHIVE}"
BIN_PATH="${INSTALL_ROOT}/bin"

mkdir -p "$INSTALL_ROOT" "$BIN_PATH"

curl \
  --fail \
  --location \
  --silent \
  --show-error \
  "https://downloads.rclone.org/v${RCLONE_VERSION}/${RCLONE_ARCHIVE}" \
  --output "$ARCHIVE_PATH"

echo "${RCLONE_SHA256}  ${ARCHIVE_PATH}" | sha256sum --check --status
unzip -q "$ARCHIVE_PATH" -d "$INSTALL_ROOT"
install \
  -m 0755 \
  "${INSTALL_ROOT}/rclone-v${RCLONE_VERSION}-linux-amd64/rclone" \
  "${BIN_PATH}/rclone"

echo "$BIN_PATH" >> "${GITHUB_PATH:?GITHUB_PATH is required}"
