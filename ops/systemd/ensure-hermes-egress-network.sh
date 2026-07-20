#!/usr/bin/env bash
set -euo pipefail

network_name="actverse-hermes-egress"
network_subnet="172.29.0.0/24"
network_gateway="172.29.0.1"
contract_label="restricted-v1"

if ! docker network inspect "$network_name" >/dev/null 2>&1; then
  docker network create \
    --driver bridge \
    --internal \
    --subnet "$network_subnet" \
    --gateway "$network_gateway" \
    --label "com.actverse.hermes-egress=$contract_label" \
    "$network_name" >/dev/null
fi

actual="$({
  docker network inspect \
    --format '{{.Internal}}|{{index .Labels "com.actverse.hermes-egress"}}|{{(index .IPAM.Config 0).Subnet}}|{{(index .IPAM.Config 0).Gateway}}' \
    "$network_name"
} 2>/dev/null)"

expected="true|$contract_label|$network_subnet|$network_gateway"
if [[ "$actual" != "$expected" ]]; then
  echo "refusing incompatible $network_name network: $actual" >&2
  exit 1
fi
