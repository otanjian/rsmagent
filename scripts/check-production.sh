#!/usr/bin/env bash
# Local portion of the production-readiness gate. No Windows desktop test run.
set -u -o pipefail
cd "$(dirname "$0")/.." || exit 2
mode="${1:---local}"
if [[ "$mode" != "--local" && "$mode" != "--image" && "$mode" != "--all" ]]; then
    echo 'Usage: check-production.sh --local | --image IMAGE [--deployment-env FILE] | --all IMAGE [--deployment-env FILE]' >&2
    exit 2
fi
if [[ "$mode" != "--local" && -z "${2:-}" ]]; then
    echo 'A final candidate image is required for image checks.' >&2
    exit 2
fi
python_bin="${PYTHON_BIN:-$PWD/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    echo 'Missing Python environment; set PYTHON_BIN to the project interpreter.' >&2
    exit 2
fi
if [[ "$mode" == "--image" ]]; then
    exec "$python_bin" scripts/check-production-image.py "$2" "${@:3}"
fi
if ! command -v node >/dev/null || ! command -v npm >/dev/null || [[ ! -x desktop/node_modules/.bin/tsc ]]; then
    echo 'Missing Node/npm or desktop dependencies; install the locked desktop dependencies first.' >&2
    exit 2
fi
check_data=$(mktemp -d "${TMPDIR:-/tmp}/rsmagent-local-check.XXXXXX") || exit 2
trap 'rm -rf "$check_data"' EXIT
export COW_DATA_DIR="$check_data"
failed=0
run_check() {
    local label="$1"
    shift
    echo "Checking: $label"
    "$@"
    local result=$?
    if (( result != 0 )); then
        echo "FAIL: $label (exit $result)" >&2
        failed=1
    else
        echo "PASS: $label"
    fi
}
run_check 'Python production regressions' "$python_bin" -m pytest -q \
    tests/test_ratelimit.py tests/test_http_gate.py tests/test_http_policy.py \
    tests/test_route_registry.py tests/test_identity_auth_security.py \
    tests/test_identity_resource_authorization.py tests/test_identity_audit.py \
    tests/test_control_plane.py tests/test_action_approval_consumer.py \
    tests/test_execution_isolation.py tests/test_external_connections_api.py \
    tests/test_runtime_log_rotation.py tests/test_log_tail.py tests/test_cli_backup.py \
    tests/test_execution_sandbox.py tests/test_bash_background.py tests/test_bash_background_capacity.py tests/test_bash_streaming.py tests/test_bash_redaction.py \
    tests/test_search_files_tool.py tests/test_search_external_failure_fallback.py tests/test_search_files_powershell_quoting.py \
    tests/test_bash_exit_codes.py tests/test_instance_backup.py tests/test_readiness.py \
    tests/test_trusted_proxy.py tests/test_production_document_tools.py tests/test_image_content_gate.py tests/test_production_candidate.py tests/test_desktop_auth_flow.py tests/test_startup_hook_seam.py \
    tests/test_tenant_admin_tool_execution.py tests/test_rbac_execution_permission.py
run_check 'Route coverage' "$python_bin" scripts/check-route-coverage.py
run_check 'Web module seams' "$python_bin" scripts/check-web-module-seams.py
run_check 'Desktop typecheck and build' npm --prefix desktop run build
run_check 'Web and Desktop behavior' node --test \
    tests/test_audit_console_frontend.cjs \
    tests/test_desktop_relogin_frontend.cjs \
    tests/test_desktop_local_execution.cjs
run_check 'Native environment diagnostic (not production acceptance)' "$python_bin" -c 'from common.readiness import probe_execution, execution_checks; import json; ready = probe_execution(); print("Native production environment: " + ("PASS" if ready else "NOT VERIFIED / PREREQUISITE UNMET")); print(json.dumps(execution_checks()))'
if [[ "$mode" == "--all" ]]; then
    if (( failed == 0 )); then
        run_check 'Final Linux image smoke' "$python_bin" scripts/check-production-image.py "$2" "${@:3}"
    else
        echo 'Final Linux image smoke / deployment pin: NOT RUN (local checks failed).'
    fi
else
    echo 'Linux image acceptance: NOT RUN (use --image IMAGE or --all IMAGE).'
fi
echo 'Remote service / final installed Desktop package / target capacity: NOT RUN by this script.'
echo 'Instance recovery tests use synthetic offline data; real process-manager stop proof still requires the target environment.'
echo 'Windows desktop testing: deferred (environment unavailable).'
exit "$failed"
