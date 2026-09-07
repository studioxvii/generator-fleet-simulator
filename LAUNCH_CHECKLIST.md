# Launch Checklist

Use this checklist before publishing a self-service Generator Fleet Simulator
Community Edition package.

## Release Image

- Merge the launch branch to `main`.
- Confirm `VERSION`, `pyproject.toml`, `setup.cfg`, Compose, and both launcher
  fallbacks identify the same normalized release version.
- Regenerate `requirements.lock` and `requirements-dev.lock` only when an
  abstract dependency changes; review the resolution diff and verify every
  requirement has hashes.
- Confirm every third-party GitHub Action is pinned to a full commit SHA.
- Confirm the image starts through `gunicorn --worker-class gthread --workers 1`
  and `wsgi:app`, not the Flask development server.
- Build the release image from the merged commit.
- Tag the image with the package version, for example
  `studioxvii/generator-fleet-sim:1.1.0-rc.5`.
- Push the image to `studioxvii/generator-fleet-sim` on Docker Hub.
- In Docker Hub, open the `studioxvii/generator-fleet-sim` repository,
  choose **Settings -> Visibility settings -> Public**, and confirm the change.
  This is a manual external-account action; the release workflow does not alter
  repository visibility.
- Under **Settings -> General -> Tag mutability settings**, choose **Specific
  tags are immutable** and add
  `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`; confirm the full release tag
  cannot be replaced while candidate and moving alias tags remain usable.
- Confirm the publish workflow adds max-level provenance and an SBOM
  attestation, signs the immutable digest with GitHub Actions OIDC, and verifies
  the signature before packaging.
- Confirm the exact pushed digest passes release smoke on amd64 and arm64 before
  official SemVer aliases are promoted.
- Confirm an existing full version tag fails instead of being overwritten.
- Verify the signed `SHA256SUMS` asset manifest and Sigstore bundle before
  publishing the ZIP to the website.
- Confirm the workflow produces the launcher ZIP, `.zip.sha256` checksum,
  release receipt, downloadable SPDX JSON SBOM, workflow artifact, and GitHub
  prerelease.
- Run `(cd dist && sha256sum --check *.zip.sha256)` against downloaded workflow
  artifacts before publishing them to the website.
- Inspect the release receipt and confirm its version, Git SHA, image digest,
  runtime-lock hash, and workflow URL match the completed release run.
- Confirm the workflow's post-logout anonymous pull gate succeeds for the exact
  image digest.
- Independently verify the digest from a clean unsigned Docker configuration:
  `DOCKER_CONFIG="$(mktemp -d)" docker pull studioxvii/generator-fleet-sim@sha256:<digest>`.
- Confirm no Docker Hub account, approval, token, or `docker login` is required.
- Avoid reusing a version tag after customers have already pulled it. If a
  previously published tag was stale, ship the corrected package under the next
  patch version.
- Do not move `latest`, `{{major}}`, or `{{major}}.{{minor}}` for a prerelease
  tag. The workflow enables those aliases only for a stable version without a
  prerelease suffix.

## Clean-Machine Startup

Run the Community Edition launcher on a machine without a cached image or existing
Docker volume:

- macOS/Linux: `./start.sh`
- Windows PowerShell: `.\start.ps1`

Validate:

- The downloaded launcher ZIP passes its SHA-256 check before extraction.
- `RELEASE_RECEIPT.json` inside the ZIP matches the separately delivered
  receipt.
- Docker install/running guidance appears when Docker is unavailable.
- License acceptance requires `yes`.
- Security acknowledgement requires `understood`.
- Local-only mode binds host ports to `127.0.0.1`.
- Trusted LAN integration mode requires the `trusted-lan` confirmation.
- Default, custom, and configure-later fleet paths work.
- `/api/live` returns HTTP 200 after the web process starts.
- `/api/ready` returns HTTP 200 after terminal or browser startup.
- `python tools/release_smoke.py --base-url http://localhost:5001 --modbus-port 5021 --startup-modbus-port 5020 \
  --generators 2000`
  completes successfully against the running package. The `--modbus-port`
  value is the published host port; `--startup-modbus-port` is the container
  port the app should bind internally.
- Dashboard opens at `http://localhost:5001`.
- Modbus listens on `localhost:5021`.

## Large-Fleet Acceptance

For the launch demo path, validate a 2,000-generator startup:

- Select custom counts in the launcher.
- Confirm the configured fleet count is 2,000.
- Open `Fleet Status` and verify paging, filters, and bulk controls.
- Open `One-Line SCADA` and drill from fleet to group/size/range/unit.
- Confirm kW rollups render at each hierarchy level.
- Confirm alarm/info popouts work without losing hierarchy context.
- Run `Utility Failure All`, `Restore Utility All`, `Transfer Mode All`, and
  `Parallel Mode All`.
- Confirm restore utility returns the fleet to utility breakers closed and
  generator breakers open.

## Website And Support Copy

- Confirm the website startup copy matches `WEBSITE_STARTUP_INSTRUCTIONS.md`.
- Publish the ZIP, `.zip.sha256`, release receipt, and SPDX JSON SBOM as one
  versioned delivery set. Link source at the matching release revision.
- State clearly: “Free download. MIT-licensed project code.”
- Explain that the exact pinned image is public and anonymously pullable.
- Explain that `use` is only for a known cached image or support-preloaded
  image.
- Distinguish simulator readiness from generator units running.
- Include the stop, log, and reset commands.

## Support Handoff

Capture these before launch:

- Release image tag and digest.
- Git commit SHA used for the image.
- Release workflow URL and GitHub prerelease URL.
- Launcher ZIP SHA-256, runtime-lock SHA-256, SPDX SBOM, and signature
  verification command/result.
- Evidence of an anonymous pull from a clean Docker configuration.
- Known support command for logs:
  `docker compose --file docker-compose.yml logs --tail=100 generator`
- Known clean reset command:
  `docker compose --file docker-compose.yml down --volumes`

- [ ] Both exact image architectures pass the high/critical vulnerability gate.
  Do not bypass unresolved findings. See `docs/security-readiness-audit.md`.
