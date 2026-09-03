#!/usr/bin/env bash
#
# Copyright 2026 Esri
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Render the documented example exactly as an install dry run so Helm includes
# both release manifests and NOTES, then reject any unreplaced placeholder.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chart="$root/charts/node-local-data"
values="$root/examples/values-example.yaml"

rendered="$(helm install placeholder-check "$chart" \
  --namespace placeholder-check \
  --values "$values" \
  --dry-run \
  --debug 2>&1)"

if hits="$(grep -En '__[A-Z0-9_]+__' <<<"$rendered")"; then
  echo "Unreplaced placeholders found in rendered manifests or NOTES:" >&2
  echo "$hits" >&2
  exit 1
fi

echo "No unreplaced placeholders in rendered manifests or NOTES."
