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
`console.sh`, and `console.ps1` to the GitHub Release. It then builds the
bootc appliance disk `genestack-console-appliance-<version>-amd64.qcow2.xz`
and the installer ISO `genestack-console-appliance-<version>-amd64.iso`,
and attaches those too. `version.json` names the disk in `appliance` and
the ISO in `iso`. The binary field is unchanged. Before the tag, add a `CHANGELOG.md` section
whose heading is that version. The workflow copies it onto the release. A
tag with no matching section fails. The disk build runs after the binary
is published. A failure there leaves the binary release in place.

Installers and the update check read:

- `https://github.com/PIndustries/genestack-console/releases/latest/download/console.sh`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/version.json`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-linux-amd64`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-appliance-<version>-amd64.qcow2.xz`
- `https://github.com/PIndustries/genestack-console/releases/latest/download/genestack-console-appliance-<version>-amd64.iso`

`https://get.genestack.dev/console.sh` redirects to the `console.sh` asset.
`genestack-console update` and a second run of `console.sh` read that `version.json`, download the Linux binary it names when it is newer, and restart both systemd units. They do not change `config.yaml`, the database, or `/opt/genestack`. A named release stays downloadable after a newer tag is latest: `console.sh --version 2026.10.04.3` installs that tag's binary. A production deploy host runs that published binary. `--from-source` is for a checkout where you are changing the console and testing that change. A checkout can run on a deploy host. The install we support in production is the binary.

A hyphen suffix such as `v2026.10.04.1-rc1` is published as a prerelease and is
not marked latest. You can also run the release workflow by hand with a
`version` input that matches `app/version.py`.
