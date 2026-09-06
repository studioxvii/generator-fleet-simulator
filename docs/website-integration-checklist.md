# Studio Seventeen Website Integration Checklist

The website repository is not present in this workspace. This checklist defines
the handoff without guessing its framework, host, payment stack, or deployment
mechanics.

## Product Page

- [ ] Publish the approved copy from `docs/website-product-download-copy.md`.
- [ ] Display the exact version from `VERSION` and release date.
- [ ] Show “Free download. MIT-licensed project code.”
  adjacent to the primary CTA.
- [ ] Link requirements, security warning, privacy statement, support boundary,
  release notes, and legal terms before download.
- [ ] Do not add checkout, login, entitlement, approval, or Docker-account
  language to the normal journey.

## Versioned Assets

- [ ] Host the exact workflow-produced ZIP; do not rebuild it in the website
  pipeline.
- [ ] Host the matching `.zip.sha256`, release receipt, and SPDX JSON SBOM.
- [ ] Host the signed `SHA256SUMS` manifest and `.sigstore.json` bundle with the
  same versioned delivery set.
- [ ] Publish the image tag and immutable digest from the receipt.
- [ ] Publish the cosign verification command and BuildKit SBOM/provenance note.
- [ ] Publish the identity-constrained `cosign verify-blob` command from
  `OPERATIONS.md`.
- [ ] Keep old release assets available for rollback and support; never replace
  a file in place under the same version URL.
- [ ] Point the CTA to a versioned URL, not a mutable `latest` URL.

## Operational Links

- [ ] Set `[FEEDBACK_URL]` to a monitored Studio Seventeen destination.
- [ ] Set `[SECURITY_CONTACT_URL]` to a private vulnerability-reporting path.
- [ ] Set `[RELEASE_NOTES_URL]` to the immutable release notes.
- [ ] Set `[ASSET_MANIFEST_URL]` and `[ASSET_SIGNATURE_BUNDLE_URL]` to the
  matching versioned files.
- [ ] Confirm downloads return the expected content type, filename, content
  length, and cache headers.
- [ ] Verify desktop and mobile layouts and keyboard access for the CTA/links.

## Publication Gate

- [ ] Docker Hub repository visibility is **Public**. Confirm source publication
  with the owner and verify the source link.
- [ ] Docker Hub full SemVer tags are protected by the specific immutable-tag
  rule in `RELEASE_PROCESS.md`.
- [ ] A clean `DOCKER_CONFIG` can pull the exact digest without credentials.
- [ ] Windows, Apple Silicon macOS, and amd64 acceptance records are complete.
- [ ] Release smoke, 2,000-generator acceptance, and rollback evidence are
  recorded in the release acceptance file.
- [ ] Tom/legal approved all items in `docs/legal-review-checklist.md` that
  affect public claims or license terms.
- [ ] Download the website-hosted ZIP and re-run its checksum after deployment.
