# Community Edition Release Process

This process prepares the MIT-licensed Generator Fleet Simulator Community
Edition. The Docker image and launcher ZIP are separate distribution artifacts.
Repository visibility and public release publication require an explicit action.
Public source availability does not prove that public downloads are ready.

## Release Boundaries

- The simulator runs on the user's Docker host; it is not a hosted Studio
  Seventeen service.
- The web dashboard and Modbus TCP bind to localhost by default.
- Modbus must never be exposed to the public internet.
- Published Docker images must be anonymously pullable. Provide a source link
  with each release. The launcher bundle does not contain application source.
- The launcher displays MIT terms and requires security acknowledgement.
- Legal questions in `docs/legal-review-checklist.md` require explicit approval;
  repository documentation does not silently amend the license.

## External Preconditions

1. In Docker Hub, open `studioxvii/generator-fleet-sim`, choose
   **Settings -> Visibility settings -> Public**, and confirm. This is a manual
   account setting and is intentionally not automated by this repository.
2. In the same Docker Hub repository, open **Settings -> General -> Tag
   mutability settings**, choose **Specific tags are immutable**, and add
   `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`. This protects full SemVer
   and prerelease tags while leaving `latest`, major/minor aliases, and
   run-specific candidate tags mutable. This external setting is a launch gate
   and is intentionally not changed by the workflow.
3. Confirm the intended source publication with the owner. Review history and
   third-party notices before changing repository visibility.
4. Configure `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as GitHub Actions
   secrets with push permission to the image repository.
5. Confirm the Studio Seventeen website has stable destinations for the ZIP,
   checksum, release receipt, SPDX SBOM, release notes, feedback, and security
   contact paths.
6. Obtain the legal approvals recorded in `docs/legal-review-checklist.md`.

## Prepare A Release Candidate

1. Create a release branch; never commit or tag directly on `main` during
   preparation.
2. Create the version-specific acceptance record if it does not already exist:

   ```bash
   RELEASE_VERSION="$(cat VERSION)"
   cp docs/releases/ACCEPTANCE_TEMPLATE.md "docs/releases/v${RELEASE_VERSION}-acceptance.md"
   ```

3. Synchronize:
   - `VERSION` as SemVer, such as `1.1.0-rc.4`;
   - `pyproject.toml` and `setup.cfg` as normalized PEP 440, such as
     `1.1.0rc4`;
   - `docker-compose.yml`, `start.sh`, and `start.ps1` fallbacks;
   - `CHANGELOG.md`, bundle tests, website copy, and documentation examples.
4. If dependency inputs changed, regenerate and inspect both hashed locks:

   ```bash
   python3 -m pip install pip-tools
   pip-compile --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.txt
   pip-compile --allow-unsafe --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host -o requirements-dev.lock requirements-dev.txt
   ```

5. Run the local release gates:

   ```bash
   python3 -m venv .venv-release
   .venv-release/bin/python -m pip install --require-hashes -r requirements-dev.lock
   .venv-release/bin/python -m pip install --no-build-isolation --no-deps .
   .venv-release/bin/python -m pip check
   .venv-release/bin/python tools/security/secret_guard.py --root .
   PYTHONPYCACHEPREFIX=/tmp/gfs-release-pycache .venv-release/bin/python -m compileall -q .
   .venv-release/bin/python -m pytest
   ```

6. Build and smoke the exact Docker context using `LAUNCH_CHECKLIST.md`.
7. Merge the reviewed branch to `main`, then create a new immutable tag matching
   `v$(cat VERSION)`. Never move or overwrite an existing release tag.

## What The Tag Workflow Must Prove

The `Docker Publish` workflow must complete every step before website assets are
published:

- locked dependency installation, `pip check`, credential guard, compilation,
  and full pytest suite;
- local Docker image build plus HTTP, asset, API, and real Modbus smoke;
- `linux/amd64` and `linux/arm64` image publication;
- max-level BuildKit provenance and SBOM attestations;
- keyless cosign signature and immediate signature verification;
- a pull of the exact image digest using a fresh, empty Docker configuration;
- runtime smoke of the exact pushed digest on amd64 and arm64 under QEMU;
- deterministic Community Edition launcher ZIP, SHA-256 checksum, release
  receipt, and downloadable SPDX JSON SBOM;
- keyless signing and verification of a SHA-256 manifest covering every
  downloadable release asset;
- validated checksums/JSON, workflow artifact upload, and GitHub prerelease.

The workflow initially pushes only an unadvertised, run-specific candidate tag.
It stages validated assets in a draft GitHub release, fails closed if a full
version tag points elsewhere, and promotes the full immutable version tag last.
The per-tag workflow concurrency guard prevents two runs for the same Git ref
from racing.

The anonymous pull intentionally fails while the Docker Hub repository is
private. Treat that as a launch-blocking publisher configuration error, not as
a customer entitlement problem.

## Bundle Contract

`generator-fleet-simulator-community-edition-<version>.zip` contains only:

- `start.sh` and `start.ps1`
- `docker-compose.yml`
- `VERSION`
- `LICENSE`, `THIRD_PARTY_NOTICES.md`, `static/vendor/socket.io.LICENSE`, and `SECURITY.md`
- `CUSTOMER_ONBOARDING.md`, `WEBSITE_STARTUP_INSTRUCTIONS.md`, `INSTALL.md`,
  `OPERATIONS.md`, `MODBUS_REFERENCE.md`, `README.md`, and `CHANGELOG.md`
- `RELEASE_RECEIPT.json`

It does not contain Python, JavaScript, templates, tests, Git history, or other
source code. The public image contains compiled Python bytecode and required
runtime assets. Project code uses MIT; dependencies keep their own licenses.

The bundle builder replaces the repository's development tag template in
`docker-compose.yml` with the exact multi-architecture manifest digest produced
by the release workflow. The ZIP must never ship a mutable tag as its default
runtime reference.

## Verify Release Assets

Download the workflow assets into `dist/`, then run:

```bash
(cd dist && sha256sum --check *.zip.sha256)
jq empty dist/*-release-receipt.json dist/*-sbom.spdx.json
```

Verify the image by the digest in the receipt:

```bash
DOCKER_CONFIG="$(mktemp -d)" docker pull studioxvii/generator-fleet-sim@sha256:<digest>
cosign verify \
  --certificate-identity-regexp '^https://github.com/studioxvii/generator-fleet-simulator/.github/workflows/docker-publish.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  studioxvii/generator-fleet-sim@sha256:<digest>

cosign verify-blob \
  --bundle dist/generator-fleet-simulator-community-edition-v<VERSION>-SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github.com/studioxvii/generator-fleet-simulator/.github/workflows/docker-publish.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  dist/generator-fleet-simulator-community-edition-v<VERSION>-SHA256SUMS
(cd dist && sha256sum --check generator-fleet-simulator-community-edition-v<VERSION>-SHA256SUMS)
```

## Clean-Machine Acceptance

The workflow renders `docs/releases/RELEASE_NOTES_TEMPLATE.md` with the tagged
version and places its download, checksum, and startup instructions before the
generated changelog. Keep that template's host prerequisites synchronized with
`INSTALL.md`. Deliver launcher fixes through a new version and signed bundle;
do not replace artifacts on an existing published release.

Test the exact downloaded ZIP, with no cached image and an empty/signed-out
Docker configuration, on:

- Windows with Docker Desktop;
- Apple Silicon macOS (`arm64`);
- at least one `amd64` Docker host.

On every host, verify checksum, unzip, license display and security acknowledgement, anonymous image
pull, default/local-only binding, fleet configuration, dashboard access, logs,
stop, reset, and restart. Run the release smoke and the full 2,000-generator
acceptance in `LAUNCH_CHECKLIST.md`.

## Rollback Proof

1. Preserve the prior immutable image tag and its receipt.
2. Start the new release against a copy of the test volume.
3. Stop the new container, restore the prior Compose tag, and restart.
4. Confirm health, expected compatible state, dashboard, and Modbus operation.
5. Record any state-format limitation; never promise downgrade compatibility
   without this proof.

If a workflow fails after staging the draft release, inspect the draft and the
full Docker version tag before retrying. A tag that already matches the receipt
digest is resumable; a tag that points elsewhere must never be overwritten—cut
a new release-candidate version. If promotion succeeded but only the final
GitHub publish step failed, verify the draft assets and run:

```bash
gh release edit "v$(cat VERSION)" --draft=false
```

## Release Evidence

Complete `docs/releases/v<VERSION>-acceptance.md` with the tag/digest, Git SHA,
bundle checksum, SBOM and signature verification, workflow/website URLs,
platform acceptance, 2,000-generator evidence, anonymous-pull proof, and
rollback result before marking the release downloadable.

The release workflow rejects high and critical container vulnerabilities, including
unfixed findings. It checks the local smoke image before any push, then both exact
candidate architectures before promotion. A passing test suite does not override
this security gate. See `docs/security-readiness-audit.md` for the candidate status.
