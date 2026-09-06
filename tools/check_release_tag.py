"""Fail closed when an immutable Docker release tag already points elsewhere."""

from __future__ import annotations

import argparse
import json
import re
import subprocess


DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
NOT_FOUND_MARKERS = ("manifest unknown", "not found")


def inspect_tag_digest(reference: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest}}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        try:
            digest = json.loads(result.stdout)["digest"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Registry returned an invalid manifest for {reference}") from exc
        if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
            raise RuntimeError(f"Registry returned an invalid digest for {reference}")
        return digest

    error = f"{result.stdout}\n{result.stderr}".lower()
    if any(marker in error for marker in NOT_FOUND_MARKERS):
        return None
    raise RuntimeError(f"Could not verify immutable release tag {reference}: {error.strip()}")


def release_tag_status(reference: str, expected_digest: str) -> str:
    if not DIGEST_PATTERN.fullmatch(expected_digest):
        raise ValueError("expected_digest must be a sha256 OCI digest")
    current_digest = inspect_tag_digest(reference)
    if current_digest is None:
        return "absent"
    if current_digest != expected_digest:
        raise RuntimeError(
            f"Immutable release tag {reference} already points to {current_digest}, not {expected_digest}"
        )
    return "matches"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--expected-digest", required=True)
    args = parser.parse_args()
    print(release_tag_status(args.reference, args.expected_digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
