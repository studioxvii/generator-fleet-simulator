import configparser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
import zipfile
from pathlib import Path

from packaging.version import Version
import pytest

from tools.build_release_bundle import build_release_bundle
from tools import check_release_tag


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELEASE_VERSION = "1.1.0-rc.4"
EXPECTED_PACKAGE_VERSION = "1.1.0rc4"
EXPECTED_BUNDLE_FILES = {
    "docker-compose.yml",
    "start.sh",
    "start.ps1",
    "VERSION",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "static/vendor/socket.io.LICENSE",
    "SECURITY.md",
    "CUSTOMER_ONBOARDING.md",
    "WEBSITE_STARTUP_INSTRUCTIONS.md",
    "INSTALL.md",
    "OPERATIONS.md",
    "MODBUS_REFERENCE.md",
    "README.md",
    "CHANGELOG.md",
    "RELEASE_RECEIPT.json",
}


def test_release_version_and_license_metadata_are_consistent():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setup = configparser.ConfigParser()
    setup.read(ROOT / "setup.cfg")

    assert version == EXPECTED_RELEASE_VERSION
    assert pyproject["project"]["version"] == EXPECTED_PACKAGE_VERSION
    assert setup["metadata"]["version"] == EXPECTED_PACKAGE_VERSION
    assert Version(version) == Version(pyproject["project"]["version"])
    assert Version(version) == Version(setup["metadata"]["version"])
    assert pyproject["project"]["license"] == "MIT"
    assert setup["metadata"]["license"] == "MIT"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")

    release_surfaces = (
        "docker-compose.yml",
        "start.sh",
        "start.ps1",
        "CHANGELOG.md",
        "docs/website-product-download-copy.md",
        f"docs/releases/v{EXPECTED_RELEASE_VERSION}-acceptance.md",
    )
    for relative_path in release_surfaces:
        assert EXPECTED_RELEASE_VERSION in (ROOT / relative_path).read_text(encoding="utf-8"), relative_path


def test_docker_and_ci_use_hashed_dependency_locks():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    assert "COPY requirements.lock ." in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in dockerfile
    assert "tools/" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for workflow in (ci, publish):
        assert "pip install --require-hashes -r requirements-dev.lock" in workflow
        assert "pip install --no-build-isolation --no-deps ." in workflow
        assert "pip install --require-hashes -r requirements.lock" in workflow


def test_github_actions_are_pinned_to_commit_shas():
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    uses_pattern = re.compile(r"^\s*-?\s*uses:\s*([^\s]+)", re.MULTILINE)

    external_uses = []
    for workflow in workflows:
        for value in uses_pattern.findall(workflow.read_text(encoding="utf-8")):
            if not value.startswith("./"):
                external_uses.append((workflow.name, value))

    assert external_uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for _, value in external_uses), external_uses


def test_publish_workflow_builds_signed_attested_release_assets():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "cosign sign --yes" in workflow
    assert "tools/build_release_bundle.py" in workflow
    assert "anchore/sbom-action" in workflow
    assert "gh release create" in workflow
    assert "actions/upload-artifact" in workflow


def test_public_image_is_verified_without_docker_hub_authentication():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    anonymous_step = workflow.index("Verify anonymous image pull")
    release_step = workflow.index("Publish GitHub release")
    assert 'DOCKER_CONFIG="$ANONYMOUS_DOCKER_CONFIG" docker pull --platform linux/amd64' in workflow
    assert 'DOCKER_CONFIG="$ANONYMOUS_DOCKER_CONFIG" docker pull --platform linux/arm64' in workflow
    assert anonymous_step < release_step


def test_publish_workflow_does_not_promote_prerelease_aliases():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    assert "type=semver,pattern={{version}}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}},enable=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "type=semver,pattern={{major}},enable=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "python -m pip_audit --requirement requirements.lock" in workflow


def test_ci_audits_runtime_lock_on_supported_python():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "python -m pip install --require-hashes -r requirements-audit.lock" in workflow
    assert "python -m pip_audit --requirement requirements.lock" in workflow
    assert (ROOT / "requirements-audit.lock").exists()
    assert "pip-audit" in (ROOT / "requirements-audit.txt").read_text(encoding="utf-8")


def test_publish_workflow_smokes_digest_before_promoting_official_tags():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    push_step = workflow.index("Build and push release candidate")
    amd64_smoke = workflow.index("Smoke exact amd64 release digest")
    arm64_smoke = workflow.index("Smoke exact arm64 release digest under QEMU")
    asset_signature = workflow.index("Sign and verify release asset manifest")
    draft_release = workflow.index("Stage draft GitHub release with validated assets")
    promotion = workflow.index("Promote validated digest to official release tags")
    release = workflow.index("Publish GitHub release")

    assert "CANDIDATE_TAG: candidate-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert 'tags: ${{ env.IMAGE_REPOSITORY }}:${{ env.CANDIDATE_TAG }}' in workflow
    assert "group: docker-publish-${{ github.ref }}" in workflow
    assert "python tools/check_release_tag.py" in workflow
    assert "VERSION_STATUS" in workflow
    assert 'docker run --rm --platform linux/amd64' in workflow
    assert 'docker run --rm --platform linux/arm64' in workflow
    assert "cosign sign-blob --yes" in workflow
    assert "cosign verify-blob" in workflow
    assert "docker buildx imagetools create --tag" in workflow
    assert push_step < amd64_smoke < arm64_smoke < asset_signature < draft_release < promotion < release


def _completed_process(*, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_release_tag_guard_accepts_absent_and_matching_tags(monkeypatch):
    digest = "sha256:" + "a" * 64
    responses = iter(
        [
            _completed_process(returncode=1, stderr="manifest unknown"),
            _completed_process(returncode=0, stdout=json.dumps({"digest": digest})),
        ]
    )
    monkeypatch.setattr(check_release_tag.subprocess, "run", lambda *args, **kwargs: next(responses))

    assert check_release_tag.release_tag_status("example/image:1.0.0", digest) == "absent"
    assert check_release_tag.release_tag_status("example/image:1.0.0", digest) == "matches"


def test_release_tag_guard_rejects_conflicting_or_unverifiable_tags(monkeypatch):
    expected = "sha256:" + "a" * 64
    conflicting = "sha256:" + "b" * 64
    responses = iter(
        [
            _completed_process(returncode=0, stdout=json.dumps({"digest": conflicting})),
            _completed_process(returncode=1, stderr="unauthorized: registry unavailable"),
        ]
    )
    monkeypatch.setattr(check_release_tag.subprocess, "run", lambda *args, **kwargs: next(responses))

    with pytest.raises(RuntimeError, match="already points"):
        check_release_tag.release_tag_status("example/image:1.0.0", expected)
    with pytest.raises(RuntimeError, match="Could not verify"):
        check_release_tag.release_tag_status("example/image:1.0.0", expected)


def test_launchers_treat_pull_denial_as_publication_failure_not_entitlement():
    launchers = [
        (ROOT / "start.sh").read_text(encoding="utf-8"),
        (ROOT / "start.ps1").read_text(encoding="utf-8"),
    ]

    for launcher in launchers:
        lowered = launcher.lower()
        assert "docker login" not in lowered
        assert "purchased access" not in lowered
        assert "anonymously pullable" in lowered
        assert "publication" in lowered


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_bash_launcher(tmp_path: Path, *, mode: str, input_text: str,
                       missing_tools: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    for relative_path in ("start.sh", "docker-compose.yml", "VERSION", "LICENSE", "SECURITY.md"):
        shutil.copy2(ROOT / relative_path, tmp_path / relative_path)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = "info" ]; then
  if [ "$FAKE_DOCKER_MODE" = "permission-denied" ]; then
    printf 'permission denied while connecting to unix:///var/run/docker.sock\n' >&2
    exit 1
  fi
  if [ "$FAKE_DOCKER_MODE" = "engine-unavailable" ]; then
    printf 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n' >&2
    exit 1
  fi
  exit 0
fi
if [ "$1" = "compose" ] && [[ "$*" == *" version" ]]; then exit 0; fi
if [ "$1" = "compose" ] && [[ "$*" == *" config --images" ]]; then
  if [ "$FAKE_DOCKER_MODE" = "reference-error" ]; then exit 1; fi
  printf 'studioxvii/generator-fleet-sim@sha256:%064d\n' 0
  exit 0
fi
if [ "$1" = "compose" ] && [[ "$*" == *" pull generator" ]]; then
  if [ "$FAKE_DOCKER_MODE" = "pull-success" ]; then exit 0; fi
  exit 1
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  if [ "$FAKE_DOCKER_MODE" = "cached" ]; then exit 0; fi
  exit 1
fi
if [ "$1" = "compose" ] && [[ "$*" == *" up -d generator" ]]; then exit 0; fi
exit 0
""",
    )
    _write_executable(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")

    if missing_tools:
        # Keep only declared test tools on PATH so the host installation cannot
        # accidentally satisfy a prerequisite that this case intentionally omits.
        for command in ("bash", "dirname", "sed", "python3", "cat", "rm", "mktemp", "sleep", "uname"):
            if command not in missing_tools:
                (fake_bin / command).symlink_to(shutil.which(command))
        for command in missing_tools:
            (fake_bin / command).unlink(missing_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) if missing_tools else f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_DOCKER_MODE": mode,
        }
    )
    return subprocess.run(
        [shutil.which("bash"), str(tmp_path / "start.sh")],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=15,
        env=env,
        check=False,
    )


def test_bash_launcher_pull_success_reaches_dashboard(tmp_path):
    result = _run_bash_launcher(
        tmp_path,
        mode="pull-success",
        input_text="1\nunderstood\n1\n3\n\n2\n",
    )

    assert result.returncode == 0, result.stderr
    assert "Generator Fleet Simulator is ready" in result.stdout
    assert "up -d generator" in (tmp_path / "docker.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode,expected", [
    ("permission-denied", "This account does not have permission to access Docker."),
    ("engine-unavailable", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
])
def test_bash_launcher_preserves_docker_failure_and_exits_cleanly(tmp_path, mode, expected):
    result = _run_bash_launcher(tmp_path, mode=mode, input_text="1\nquit\n")
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "Docker daemon is not running" not in result.stdout
    if mode == "permission-denied":
        assert "permission denied while connecting" in result.stdout
        assert "Docker access denied" in result.stdout
        assert "Start Docker Desktop" not in result.stdout
    assert "up -d generator" not in (tmp_path / "docker.log").read_text()


@pytest.mark.parametrize("missing,expected", [
    (("python3",), "Python 3 is required"),
    (("curl",), "curl is required"),
    (("python3", "curl"), "Python 3 is required"),
])
def test_bash_launcher_checks_host_tools_before_docker(tmp_path, missing, expected):
    (tmp_path / "RELEASE_RECEIPT.json").write_text("{}")
    result = _run_bash_launcher(tmp_path, mode="pull-success", input_text="1\n", missing_tools=missing)
    assert result.returncode != 0
    assert expected in result.stderr
    assert "See INSTALL.md, Prerequisites" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def test_generated_release_instructions_match_bundle_names():
    version = EXPECTED_RELEASE_VERSION
    template = (ROOT / "docs/releases/RELEASE_NOTES_TEMPLATE.md").read_text()
    rendered = template.replace("{{VERSION}}", version)
    assert "{{VERSION}}" not in rendered
    zip_name = f"generator-fleet-simulator-community-edition-{version}.zip"
    assert f"sha256sum -c {zip_name}.sha256" in rendered
    assert f"shasum -a 256 -c {zip_name}.sha256" in rendered
    assert f"/releases/download/v{version}/{zip_name}" in rendered
    assert 'throw "Checksum mismatch' in rendered
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    assert 'docs/releases/RELEASE_NOTES_TEMPLATE.md > "$RUNNER_TEMP/gfs-release-notes.md"' in workflow
    assert '--notes-file "$RUNNER_TEMP/gfs-release-notes.md"' in workflow


def test_bash_launcher_uses_exact_cached_image_after_pull_failure(tmp_path):
    result = _run_bash_launcher(
        tmp_path,
        mode="cached",
        input_text="1\nunderstood\n1\n3\nuse\n\n2\n",
    )

    assert result.returncode == 0, result.stderr
    assert "Using cached Generator Fleet image" in result.stdout
    assert "Generator Fleet Simulator is ready" in result.stdout


def test_bash_launcher_rejects_mismatched_release_receipt(tmp_path):
    receipt = {
        "version": "9.9.9",
        "image": {"repository": "studioxvii/generator-fleet-sim", "digest": "sha256:" + "c" * 64},
    }
    (tmp_path / "RELEASE_RECEIPT.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = _run_bash_launcher(
        tmp_path,
        mode="pull-success",
        input_text="1\nyes\nunderstood\n1\n3\n\n2\n",
    )

    assert result.returncode != 0
    assert "does not match VERSION" in result.stderr + result.stdout


def test_bash_launcher_reference_failure_reaches_publication_help(tmp_path):
    result = _run_bash_launcher(
        tmp_path,
        mode="reference-error",
        input_text="1\nunderstood\n1\n3\nquit\n\n2\n",
    )

    assert result.returncode == 0, result.stderr
    assert "publication or tag configuration failure" in result.stdout
    assert "intended to be anonymously pullable" in result.stdout


def test_community_edition_positioning_reaches_package_and_dashboard():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert "Community Edition" in pyproject["project"]["description"]
    assert "Generator Fleet Simulator Community Edition" in dashboard
    assert "Free download" in readme or "free-to-download" in readme
    assert "MIT" in normalized_readme


def test_website_handoff_and_safety_copy_cover_self_service_contract():
    website_copy = (ROOT / "docs" / "website-product-download-copy.md").read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "website-integration-checklist.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    legal = (ROOT / "docs" / "legal-review-checklist.md").read_text(encoding="utf-8")
    normalized_website_copy = " ".join(website_copy.split())
    normalized_integration = " ".join(integration.split())

    assert "Free download. MIT-licensed project code." in website_copy
    assert "Docker Hub account or sign-in is not required" in website_copy
    assert "do not include application authentication" in normalized_website_copy
    assert "does not include telemetry" in normalized_website_copy
    assert "Docker Hub repository visibility is **Public**" in integration
    assert "source publication" in normalized_integration
    assert "Do not publish Modbus TCP ports directly to the public internet" in security
    assert "Ownership" in legal


def test_release_bundle_contains_receipt_and_matching_checksum(tmp_path):
    digest = "sha256:" + "b" * 64
    artifacts = build_release_bundle(
        root=ROOT,
        output_dir=tmp_path,
        git_sha="a" * 40,
        image_repository="studioxvii/generator-fleet-sim",
        image_digest=digest,
        generated_at="2026-07-12T00:00:00Z",
    )

    archive_bytes = artifacts.archive.read_bytes()
    expected_checksum = hashlib.sha256(archive_bytes).hexdigest()
    assert artifacts.checksum.read_text(encoding="utf-8").split()[0] == expected_checksum

    receipt = json.loads(artifacts.receipt.read_text(encoding="utf-8"))
    assert receipt["version"] == EXPECTED_RELEASE_VERSION
    assert receipt["package_version"] == EXPECTED_PACKAGE_VERSION
    assert receipt["edition"] == "Community Edition"
    assert receipt["git_sha"] == "a" * 40
    assert receipt["image"] == {
        "repository": "studioxvii/generator-fleet-sim",
        "digest": digest,
    }
    assert receipt["runtime_lock"]["path"] == "requirements.lock"
    assert len(receipt["runtime_lock"]["sha256"]) == 64
    assert receipt["required_release_gates"]

    with zipfile.ZipFile(artifacts.archive) as bundle:
        members = set(bundle.namelist())
        prefix = f"generator-fleet-simulator-community-edition-{EXPECTED_RELEASE_VERSION}/"
        assert members == {prefix + path for path in EXPECTED_BUNDLE_FILES}
        assert prefix + "RELEASE_RECEIPT.json" in members
        assert prefix + "docker-compose.yml" in members
        assert prefix + "start.sh" in members
        assert prefix + "start.ps1" in members
        assert prefix + "LICENSE" in members
        assert not any(member.endswith(".py") for member in members)
        bundled_compose = bundle.read(prefix + "docker-compose.yml").decode("utf-8")
        assert f"studioxvii/generator-fleet-sim@{digest}" in bundled_compose
        assert "GENSIM_IMAGE_TAG" not in bundled_compose
        embedded_receipt = bundle.read(prefix + "RELEASE_RECEIPT.json")
        assert embedded_receipt == artifacts.receipt.read_bytes()
        assert json.loads(embedded_receipt)["image"]["digest"] == digest
        for member in members:
            member_mode = bundle.getinfo(member).external_attr >> 16
            assert stat.S_IFMT(member_mode) == stat.S_IFREG
        assert (bundle.getinfo(prefix + "start.sh").external_attr >> 16) & 0o111
        assert not ((bundle.getinfo(prefix + "README.md").external_attr >> 16) & 0o111)
        assert "255" in bundle.read(prefix + "MODBUS_REFERENCE.md").decode("utf-8")
        # A downloaded ZIP must not direct customers to absent local Markdown
        # files. Maintainer-only references may link to the public repository.
        for member in members:
            if not member.endswith(".md"):
                continue
            for target in re.findall(r"\]\(([^)]+)\)", bundle.read(member).decode("utf-8")):
                if re.match(r"https?://|mailto:|#", target):
                    continue
                local = (Path(member).parent / target.split("#")[0]).as_posix()
                assert local in members, (member, target)

    assert artifacts.archive.name == f"generator-fleet-simulator-community-edition-{EXPECTED_RELEASE_VERSION}.zip"

    second = build_release_bundle(
        root=ROOT,
        output_dir=tmp_path / "second",
        git_sha="a" * 40,
        image_repository="studioxvii/generator-fleet-sim",
        image_digest=digest,
        generated_at="2026-07-12T00:00:00Z",
    )
    assert second.archive.read_bytes() == archive_bytes
