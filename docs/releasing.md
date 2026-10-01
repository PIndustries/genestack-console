# Releasing

Branch pushes do not build a binary. A public release is a git tag.

1. Set `VERSION` in `app/version.py` to `YYYY.MM.DD`.
2. Commit that change on `main`.
3. Tag the same commit and push the tag:

```bash
git tag v2026.10.01
git push origin v2026.10.01
```

The tag is `v` plus the exact `VERSION` string. The release workflow builds
`genestack-console-linux-amd64` and attaches that file and `version.json` to
the GitHub Release.

Installers and the update check read:

- `https://github.com/PIndustries/genestack-console/releases/latest/download/version.json`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-linux-amd64`

A hyphen suffix such as `v2026.10.01-rc1` is published as a prerelease and is
not marked latest. You can also run the release workflow by hand with a
`version` input that matches `app/version.py`.
