# Releasing

The version lives in `custom_components/network_info/manifest.json` — nowhere else; the
manifest is what Home Assistant and HACS read, so the tag follows it.

1. Bump the version on the feature branch when the PR is raised — fixes bump the patch
   digit, features the minor.
2. Merge the PR into `main`.
3. **Validate** runs (HACS, hassfest, build & import checks).
4. When it passes, **Release** reads the manifest version and creates the `v<version>`
   tag and GitHub release with generated notes. An already-existing tag or a red
   Validate means no release.
