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

# Fails if any __PLACEHOLDER__ token is still unreplaced. Run from the
# NodeLocalDataStores directory before applying anything.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ONLY manifests/ IS SCANNED, deliberately. Everything that gets applied to a
# cluster lives there, so it is the only place a leftover token can actually
# break something.
#
# Scanning the whole tree instead, with a list of files to exclude, is what this
# script used to do and it failed even on a correct substitution. Prose outside
# manifests/ names these tokens on purpose: README.md and RESULTS.md document
# them, and agent/Dockerfile and agent/sync_agent.py each carry a comment
# pointing at __IMAGE_REPOSITORY__ and __LABEL_DOMAIN__ to say where they are
# set. Those are references to the tokens, not uses of them, so matching them
# was a false positive. Keeping the scan scoped to what is applied means the
# exclude list cannot drift out of date as documentation is added.
manifests="$root/manifests"

if [[ ! -d "$manifests" ]]; then
  echo "No manifests directory found at $manifests" >&2
  exit 1
fi

if hits=$(grep -rn '__[A-Z0-9_]\+__' "$manifests" 2>/dev/null); then
  echo "Unreplaced placeholders found:"
  echo "$hits"
  exit 1
fi

echo "No unreplaced placeholders in manifests/."
