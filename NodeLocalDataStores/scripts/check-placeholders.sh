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

# Exclude this script and the README, which document the tokens rather than use
# them.
if hits=$(grep -rn '__[A-Z0-9_]\+__' "$root" \
            --exclude-dir=scripts \
            --exclude-dir=.venv \
            --exclude-dir=__pycache__ \
            --exclude=README.md \
            --exclude=RESULTS.md 2>/dev/null); then
  echo "Unreplaced placeholders found:"
  echo "$hits"
  exit 1
fi

echo "No unreplaced placeholders."
