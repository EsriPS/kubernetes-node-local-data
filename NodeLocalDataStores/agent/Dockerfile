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

# Sync agent image. Sample, unsupported.
#
# This is NOT an ArcGIS image and nothing here touches one. The agent is a
# customer-owned DaemonSet that stages data onto nodes; ArcGIS service pods run
# their own unmodified images.
#
# Build and push to a registry your cluster can pull from, then set
# __IMAGE_REPOSITORY__ in ../manifests/07-sync-agent-daemonset.yaml to match:
#
#     docker build --platform linux/amd64 -t geodata-sync-agent:0.4.0 .
#     docker tag geodata-sync-agent:0.4.0 <registry>/geodata-sync-agent:0.4.0
#     docker push <registry>/geodata-sync-agent:0.4.0
#
# --platform linux/amd64 matters, because the node pool is x86_64 and this is
# often built on an arm64 workstation.

FROM python:3.12-slim-bookworm

# The copy engine. Taken from the upstream image at a pinned tag rather than
# from Debian, whose packaged version predates multi-thread copy for
# local-to-local pairs, which is the feature the throughput result rests on.
# Pin it: rclone's defaults have moved between releases.
COPY --from=rclone/rclone:1.75.0 /usr/local/bin/rclone /usr/local/bin/rclone

# kubectl removes the bootstrap taint and writes the generation labels.
# Pin to your cluster's minor version; kubectl supports one minor of skew.
ARG KUBECTL_VERSION=v1.34.9
# Populated by BuildKit from the target platform. Left without a default so a
# native build for local testing does not silently pull an amd64 kubectl.
ARG TARGETARCH

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client=true; \
    rclone version | head -1; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/geodata-sync

COPY sync_agent.py /opt/geodata-sync/

# Unbuffered, so pod logs appear as the pass progresses rather than at exit.
# A several-hundred-gigabyte first sync is a long silence otherwise.
ENV PYTHONUNBUFFERED=1

# The copy preserves ownership onto a host path. Also set explicitly in the
# DaemonSet's securityContext.
USER root

ENTRYPOINT ["python3", "/opt/geodata-sync/sync_agent.py"]
