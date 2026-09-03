#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="${1:-}"
ARTIFACT_ROOT="${CI_ARTIFACT_DIR:-$ROOT/artifacts/ci}"
STAGE_ARTIFACTS="$ARTIFACT_ROOT/$STAGE"

if [[ -z "$STAGE" ]]; then
    echo "Usage: $0 <stage>" >&2
    exit 2
fi

mkdir -p "$STAGE_ARTIFACTS"
cd "$ROOT"

case "$STAGE" in
    "quality")
        uv lock --check
        uv run ruff check .
        uv run pyright
        uv run python tools/verify_frozen_whatsapp.py
        uv run python ci/validate_feature_flags.py \
            --root "$ROOT" \
            --registry ai_bridge/feature_flags.json \
            --output "$STAGE_ARTIFACTS/feature-flag-validation.json"
        uv run python ci/validate_s2s_inventory.py \
            --root "$ROOT" \
            --manifest migration/s2s-surface-v1.json \
            --output "$STAGE_ARTIFACTS/s2s-inventory-validation.json"
        uv run python ci/validate_cascade_characterization.py \
            --root "$ROOT" \
            --matrix migration/cascade-characterization-v1.json \
            --inventory migration/s2s-surface-v1.json \
            --output "$STAGE_ARTIFACTS/cascade-characterization-validation.json"
        uv run python release/evidence.py \
            reports/releases/m0-09-reference \
            --output "$STAGE_ARTIFACTS/release-evidence-validation.json"
        uv run python release/evidence.py \
            reports/releases/m0-clean-baseline \
            --output "$STAGE_ARTIFACTS/m0-clean-baseline-validation.json"
        uv run python -m phone_agent_gateway.release.report_signature verify \
            reports/quality/2026-09-03-m0-exit-evidence.json \
            --signature reports/quality/2026-09-03-m0-exit-evidence.signature.json \
            --public-key release/trust/m0-local-qualification-ed25519.pem \
            --output "$STAGE_ARTIFACTS/m0-exit-signature-validation.json"
        ;;
    "fast-unit")
        uv run pytest -q tests --ignore=tests/production -m "not device_integration"
        ;;
    "integration")
        uv run pytest -q \
            tests/production \
            tests/test_audio_loopback.py \
            tests/test_pipecat_pipeline_loopback.py \
            tests/test_remote_link.py \
            tests/test_voice_host_lifecycle.py \
            tests/test_web_server.py
        ;;
    "package")
        export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
        uv build --out-dir "$STAGE_ARTIFACTS"
        uv run python -m phone_agent_gateway.release.normalize_sdist \
            "$STAGE_ARTIFACTS"/*.tar.gz
        uv run python release/generate_manifest.py "$STAGE_ARTIFACTS" --version \
            "$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
        (cd "$STAGE_ARTIFACTS" && sha256sum --check SHA256SUMS)
        ;;
    "android-protocol")
        ./android_service_apk/build_and_install.sh --build-only
        ./android_service_apk/test_protocol_codec.sh
        ./android_service_apk/test_remote_link.sh
        cp android_service_apk/PhoneAgentGateway.apk "$STAGE_ARTIFACTS/"
        sha256sum "$STAGE_ARTIFACTS/PhoneAgentGateway.apk" > "$STAGE_ARTIFACTS/PhoneAgentGateway.apk.sha256"
        ;;
    "security")
        uv export --locked --all-extras --no-dev --no-emit-project \
            --no-emit-package en-core-web-sm --format requirements.txt \
            --output-file "$STAGE_ARTIFACTS/requirements.lock.txt" >/dev/null
        uv export --locked --all-extras --no-dev --no-emit-project \
            --no-emit-package en-core-web-sm --no-hashes --no-annotate --no-header \
            --format requirements.txt \
            --output-file "$STAGE_ARTIFACTS/requirements.advisory-source.txt" >/dev/null
        uv run python ci/prepare_audit_requirements.py \
            "$STAGE_ARTIFACTS/requirements.advisory-source.txt" \
            security/dependency-policy.json \
            "$STAGE_ARTIFACTS/requirements.audit.txt" \
            "$STAGE_ARTIFACTS/audit-version-normalizations.json"
        audit_status=0
        uv tool run --from pip-audit==2.10.1 pip-audit \
            --requirement "$STAGE_ARTIFACTS/requirements.audit.txt" \
            --no-deps --disable-pip --strict --progress-spinner off --format json \
            --output "$STAGE_ARTIFACTS/dependency-audit.json" >/dev/null || audit_status=$?
        if [[ "$audit_status" -gt 1 ]]; then
            echo "pip-audit failed operationally with status $audit_status" >&2
            exit "$audit_status"
        fi
        if [[ ! -s "$STAGE_ARTIFACTS/dependency-audit.json" ]]; then
            echo "pip-audit did not produce its required JSON report" >&2
            exit 1
        fi
        uv run python ci/validate_dependency_audit.py \
            "$STAGE_ARTIFACTS/dependency-audit.json" \
            security/dependency-audit-allowlist.json \
            --output "$STAGE_ARTIFACTS/dependency-audit-validation.json"
        ;;
    "licence-sbom")
        uv export --preview-features sbom-export --locked --all-extras --no-dev \
            --format cyclonedx1.5 --output-file "$STAGE_ARTIFACTS/cyclonedx-sbom.json" >/dev/null
        uv run python ci/validate_sbom.py "$STAGE_ARTIFACTS/cyclonedx-sbom.json" \
            --output "$STAGE_ARTIFACTS/sbom-validation.json"
        uv run --frozen --all-extras --no-dev --with pip-licenses==5.5.0 \
            pip-licenses --format=json \
            --output-file="$STAGE_ARTIFACTS/python-licenses.json"
        uv run python ci/validate_dependency_policy.py \
            --root "$ROOT" \
            --policy security/dependency-policy.json \
            --licenses "$STAGE_ARTIFACTS/python-licenses.json" \
            --output "$STAGE_ARTIFACTS/dependency-policy-validation.json"
        ;;
    "container")
        docker buildx build --check --file Dockerfile.cuda .
        if [[ "${PHONE_AGENT_FULL_CONTAINER_BUILD:-0}" == "1" ]]; then
            docker buildx build --file Dockerfile.cuda \
                --tag "${PHONE_AGENT_CONTAINER_TAG:-phoneagent-cascade:ci}" --load .
        fi
        ;;
    "eval")
        uv run pytest -q \
            tests/test_evalset.py \
            tests/test_agent_policy.py \
            tests/test_call_context.py \
            tests/test_conversation_repair.py \
            tests/test_personality_os.py \
            tests/test_realtime_tool_catalog.py
        uv run python -m phone_agent_gateway.qualification.performance_harness \
            --profile linux-x86_64-contract-ci \
            --output "$STAGE_ARTIFACTS/cascade-performance.json" \
            --require-qualified
        ;;
    "device-qualification")
        if [[ "${PHONE_AGENT_DEVICE_CI:-0}" != "1" ]]; then
            echo "Device qualification requires PHONE_AGENT_DEVICE_CI=1." >&2
            exit 2
        fi
        uv run phone-agent-qualify --ensure-forwards \
            --output "$STAGE_ARTIFACTS/device-qualification.json"
        ;;
    *)
        echo "Unknown CI stage: $STAGE" >&2
        exit 2
        ;;
esac
