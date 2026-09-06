"""Build the Community Edition bundle and its machine-readable release receipt."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


IMAGE_REPOSITORY = "studioxvii/generator-fleet-sim"
PACKAGE_FILES = (
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
    "README.md",
    "CHANGELOG.md",
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ReleaseArtifacts:
    archive: Path
    checksum: Path
    receipt: Path


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _package_version(release_version: str) -> str:
    return re.sub(r"-(a|b|rc)\.(\d+)$", r"\1\2", release_version)


def _read_metadata(root: Path) -> tuple[str, str]:
    release_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = str(pyproject["project"]["version"])

    setup = configparser.ConfigParser()
    setup.read(root / "setup.cfg")
    setup_version = setup["metadata"]["version"]
    setup_license = setup["metadata"]["license"]

    expected_package_version = _package_version(release_version)
    if package_version != expected_package_version or setup_version != expected_package_version:
        raise ValueError(
            "Release version mismatch: "
            f"VERSION={release_version!r}, pyproject={package_version!r}, setup.cfg={setup_version!r}"
        )
    if pyproject["project"].get("license") != "MIT" or setup_license != "MIT":
        raise ValueError("Package metadata must declare the MIT license")
    if not (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"):
        raise ValueError("LICENSE is not the MIT license")
    return release_version, package_version


def _parse_generated_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _bundle_file_bytes(
    *,
    root: Path,
    relative_path: str,
    version: str,
    image_repository: str,
    image_digest: str,
) -> bytes:
    source = root / relative_path
    if relative_path != "docker-compose.yml":
        return source.read_bytes()

    compose = source.read_text(encoding="utf-8")
    tagged_reference = f"{image_repository}:${{GENSIM_IMAGE_TAG:-{version}}}"
    digest_reference = f"{image_repository}@{image_digest}"
    if compose.count(tagged_reference) != 1:
        raise ValueError("docker-compose.yml does not contain the expected release image template")
    return compose.replace(tagged_reference, digest_reference).encode("utf-8")


def _zip_info(name: str, generated_at: datetime, *, executable: bool = False) -> zipfile.ZipInfo:
    # ZIP cannot represent years before 1980 and only stores even-numbered seconds.
    normalized = generated_at.replace(second=generated_at.second - generated_at.second % 2, microsecond=0)
    if normalized.year < 1980:
        raise ValueError("generated_at must be 1980 or later")
    info = zipfile.ZipInfo(name, date_time=normalized.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = ((stat.S_IFREG | mode) & 0xFFFF) << 16
    return info


def build_release_bundle(
    *,
    root: Path,
    output_dir: Path,
    git_sha: str,
    image_repository: str,
    image_digest: str,
    generated_at: str,
    workflow_run_url: str = "",
) -> ReleaseArtifacts:
    root = root.resolve()
    output_dir = output_dir.resolve()
    if not SHA_PATTERN.fullmatch(git_sha):
        raise ValueError("git_sha must be a lowercase 40-character Git commit SHA")
    if not DIGEST_PATTERN.fullmatch(image_digest):
        raise ValueError("image_digest must be a sha256 OCI digest")
    if not image_repository or "@" in image_repository or ":" in image_repository:
        raise ValueError("image_repository must be an untagged OCI repository name")

    version, package_version = _read_metadata(root)
    generated = _parse_generated_at(generated_at)
    runtime_lock = root / "requirements.lock"
    if not runtime_lock.is_file():
        raise FileNotFoundError("requirements.lock is required for a release")
    package_bytes: dict[str, bytes] = {}
    missing: list[str] = []
    for relative_path in PACKAGE_FILES:
        try:
            package_bytes[relative_path] = _bundle_file_bytes(
                root=root,
                relative_path=relative_path,
                version=version,
                image_repository=image_repository,
                image_digest=image_digest,
            )
        except FileNotFoundError:
            missing.append(relative_path)
    if missing:
        raise FileNotFoundError(f"Release package files are missing: {', '.join(missing)}")

    receipt_data = {
        "schema": "studio-seventeen-release-receipt/v1",
        "product": "Generator Fleet Simulator Community Edition",
        "edition": "Community Edition",
        "version": version,
        "package_version": package_version,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "git_sha": git_sha,
        "image": {"repository": image_repository, "digest": image_digest},
        "runtime_lock": {"path": "requirements.lock", "sha256": _sha256(runtime_lock)},
        "license": "MIT",
        "workflow_run_url": workflow_run_url,
        "required_release_gates": [
            "locked dependency install and pip check",
            "credential guard and source compilation",
            "pytest suite",
            "Docker image release smoke with HTTP and Modbus",
            "multi-architecture image build with SBOM and provenance",
            "exact amd64 and arm64 digest runtime smoke",
            "keyless image signature",
            "keyless downloadable asset-manifest signature",
        ],
    }
    receipt_bytes = (json.dumps(receipt_data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"generator-fleet-simulator-community-edition-{version}"
    bundle_prefix = f"{base_name}/"
    archive = output_dir / f"{base_name}.zip"
    receipt = output_dir / f"{base_name}-release-receipt.json"
    receipt.write_bytes(receipt_bytes)

    with zipfile.ZipFile(archive, mode="w") as bundle:
        for relative_path in PACKAGE_FILES:
            info = _zip_info(
                bundle_prefix + relative_path,
                generated,
                executable=relative_path in {"start.sh"},
            )
            bundle.writestr(
                info,
                package_bytes[relative_path],
            )
        bundle.writestr(_zip_info(bundle_prefix + "RELEASE_RECEIPT.json", generated), receipt_bytes)

    checksum = output_dir / f"{archive.name}.sha256"
    checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return ReleaseArtifacts(archive=archive, checksum=checksum, receipt=receipt)


def _git_sha(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA", ""))
    parser.add_argument("--image-repository", default=IMAGE_REPOSITORY)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--generated-at", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--workflow-run-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    git_sha = args.git_sha or _git_sha(args.root)
    artifacts = build_release_bundle(
        root=args.root,
        output_dir=args.output_dir,
        git_sha=git_sha,
        image_repository=args.image_repository,
        image_digest=args.image_digest,
        generated_at=args.generated_at,
        workflow_run_url=args.workflow_run_url,
    )
    print(artifacts.archive)
    print(artifacts.checksum)
    print(artifacts.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
