#!/usr/bin/env bash

set -u

REPOSITORY_PATH="${MARKET_DATA_REPOSITORY_PATH:-/home/ubuntu/market-data}"
SECRET_FILE="${MARKET_DATA_NASS_ENV_FILE:-/home/ubuntu/.config/market-data/nass.env}"
LOCK_FILE="${MARKET_DATA_SOYBEAN_LOCK_FILE:-/run/lock/soybean_crop_progress_update.lock}"
CONTAINER_NAME="${MARKET_DATA_SPREAD_CONTAINER:-spread-dashboard}"
CONTAINER_ENTRYPOINT="${MARKET_DATA_SOYBEAN_ENTRYPOINT:-/app/04_scripts/soybean_crop_progress/update_soybeans_crop_weekly.py}"

cleanup() {
    unset NASS_API_KEY MARKET_DATA_GIT_HEAD CONTROL_REPO_GIT_HEAD
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -d "${REPOSITORY_PATH}" ]]; then
    echo "正式仓库不存在：${REPOSITORY_PATH}" >&2
    exit 1
fi

for required_command in flock git docker; do
    if ! command -v "${required_command}" >/dev/null 2>&1; then
        echo "缺少运行命令：${required_command}" >&2
        exit 1
    fi
done

if ! exec 9>"${LOCK_FILE}"; then
    echo "无法打开更新锁文件：${LOCK_FILE}" >&2
    exit 1
fi
if ! flock -n 9; then
    echo "美豆周度更新已有实例运行，本次跳过。"
    exit 0
fi

if [[ ! -f "${SECRET_FILE}" || ! -r "${SECRET_FILE}" ]]; then
    echo "NASS_API_KEY密钥文件不存在或不可读。" >&2
    exit 1
fi

NASS_API_KEY=""
nass_key_lines=0
while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    case "${line}" in
        NASS_API_KEY=*)
            nass_key_lines=$((nass_key_lines + 1))
            NASS_API_KEY="${line#NASS_API_KEY=}"
            ;;
    esac
done <"${SECRET_FILE}"

if [[ "${nass_key_lines}" -ne 1 || -z "${NASS_API_KEY}" ]]; then
    echo "NASS_API_KEY缺失、为空或重复，未执行更新。" >&2
    exit 1
fi

if ! container_running="$(
    docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null
)"; then
    echo "容器不存在或无法检查：${CONTAINER_NAME}" >&2
    exit 1
fi
if [[ "${container_running}" != "true" ]]; then
    echo "容器未运行：${CONTAINER_NAME}" >&2
    exit 1
fi

if ! MARKET_DATA_GIT_HEAD="$(
    docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null
)"; then
    echo "无法读取运行容器Git身份，未执行更新。" >&2
    exit 1
fi
runtime_git_head_line_count="$(
    docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD 2>/dev/null | wc -l
)"
if [[ "${runtime_git_head_line_count}" != "1" || ! "${MARKET_DATA_GIT_HEAD}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "运行容器Git身份缺失或不是40位十六进制哈希，未执行更新。" >&2
    exit 1
fi
MARKET_DATA_GIT_HEAD="${MARKET_DATA_GIT_HEAD,,}"

CONTROL_REPO_GIT_HEAD=""
if CONTROL_REPO_GIT_HEAD="$(
    git -C "${REPOSITORY_PATH}" rev-parse HEAD 2>/dev/null
)" && [[ "${CONTROL_REPO_GIT_HEAD}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    CONTROL_REPO_GIT_HEAD="${CONTROL_REPO_GIT_HEAD,,}"
else
    CONTROL_REPO_GIT_HEAD="unavailable"
fi

identity_match=false
if [[ "${CONTROL_REPO_GIT_HEAD}" == "${MARKET_DATA_GIT_HEAD}" ]]; then
    identity_match=true
fi
echo "runtime_git_head=${MARKET_DATA_GIT_HEAD}"
echo "control_repo_git_head=${CONTROL_REPO_GIT_HEAD}"
echo "identity_match=${identity_match}"

export NASS_API_KEY MARKET_DATA_GIT_HEAD

docker exec \
    --env NASS_API_KEY \
    --env MARKET_DATA_GIT_HEAD \
    "${CONTAINER_NAME}" \
    python "${CONTAINER_ENTRYPOINT}" "$@"
exit_code=$?

exit "${exit_code}"
