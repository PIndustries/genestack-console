# Releasing

Branch pushes do not build a binary. A public release is a git tag. Pull requests land on `main`. `main` is the development line. The installer follows the latest tag, not `main`. A release branch is cut from a tag only when a fix must ship without the rest of `main`.

1. Set `VERSION` in `app/version.py` to `YYYY.MM.DD.B`. `B` is the build for that day, starting at `1`.
2. Commit that change on `main`.
3. Tag the same commit and push the tag:

```bash
git tag v2026.10.04.1
git push origin v2026.10.04.1
```

The tag is `v` plus the exact `VERSION` string. The release workflow builds
`genestack-console-linux-amd64` and attaches that file, `version.json`,
`console.sh`, and `console.ps1` to the GitHub Release.

Installers and the update check read:

- `https://github.com/PIndustries/genestack-console/releases/latest/download/console.sh`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/version.json`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-linux-amd64`

`https://get.genestack.dev/console.sh` redirects to the `console.sh` asset.

A hyphen suffix such as `v2026.10.04.1-rc1` is published as a prerelease and is
not marked latest. You can also run the release workflow by hand with a
`version` input that matches `app/version.py`.
