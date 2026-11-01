# Contributing

Esri welcomes contributions from anyone and everyone. Please see our [guidelines for contributing](https://github.com/esri/contributing).

## Maintained deployment source

The Helm chart in `charts/node-local-data` and the documented values files in
`examples/` are the only maintained deployment source. Do not add generated
manifest copies; validate changes by rendering the chart.

## Required validation environment

Validation requires Helm 3.0.0 or later, Python 3, and exactly rclone `1.75.0`.
The chart test dependencies are pinned in `tests/chart/requirements-dev.txt`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --requirement tests/chart/requirements-dev.txt
```

If a trusted system package already provides exactly rclone `1.75.0`, it may be
used. Otherwise, the following reproducible setup installs the official pinned
release for macOS or Linux on amd64 or arm64 into `~/.local/bin` and verifies
its published checksum:

```bash
set -eu

case "$(uname -s)-$(uname -m)" in
  Darwin-x86_64) platform=osx-amd64 ;;
  Darwin-arm64) platform=osx-arm64 ;;
  Linux-x86_64) platform=linux-amd64 ;;
  Linux-aarch64|Linux-arm64) platform=linux-arm64 ;;
  *) echo "Unsupported platform; provide rclone 1.75.0 on PATH." >&2; exit 1 ;;
esac

version=1.75.0
archive="rclone-v${version}-${platform}.zip"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
curl --fail --location --silent --show-error \
  --output "$tmp_dir/$archive" \
  "https://downloads.rclone.org/v${version}/${archive}"
curl --fail --location --silent --show-error \
  --output "$tmp_dir/SHA256SUMS" \
  "https://downloads.rclone.org/v${version}/SHA256SUMS"
expected_checksum="$(awk -v file="$archive" '$2 == file { print $1 }' "$tmp_dir/SHA256SUMS")"
test -n "$expected_checksum"
actual_checksum="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$tmp_dir/$archive")"
test "$actual_checksum" = "$expected_checksum"
unzip -q "$tmp_dir/$archive" -d "$tmp_dir"
mkdir -p "$HOME/.local/bin"
install -m 0755 "$tmp_dir/rclone-v${version}-${platform}/rclone" \
  "$HOME/.local/bin/rclone"
export PATH="$HOME/.local/bin:$PATH"
```

Before running pytest, enforce the exact required version:

```bash
required_rclone_version='v1.75.0'
if ! command -v rclone >/dev/null 2>&1; then
  echo 'Validation requires rclone 1.75.0 on PATH; absence is a failure.' >&2
  exit 1
fi
actual_rclone_version="$(rclone version | sed -n '1s/^rclone //p')"
if [ "$actual_rclone_version" != "$required_rclone_version" ]; then
  echo "Validation requires rclone 1.75.0; found ${actual_rclone_version:-unknown}." >&2
  exit 1
fi
```

An absent or wrong-version rclone is a validation failure. The real-rclone
addition, modification, deletion, configured-exclude, and interrupted-pass
retry scenarios are mandatory and must not be skipped.

Run the complete required validation set from the repository root:

```bash
helm lint --strict charts/node-local-data -f examples/values-example.yaml
pytest -q tests/chart agent/tests
./scripts/check-placeholders.sh
```
