#!/usr/bin/env bash
set -euo pipefail

network_name="actverse-hermes-egress"
network_subnet="172.29.0.0/24"
network_gateway="172.29.0.1"
contract_label="restricted-v1"
proxy_network_name="actverse-hermes-proxy-egress"
proxy_network_subnet="172.30.0.0/24"
proxy_network_gateway="172.30.0.1"

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

if ! docker network inspect "$proxy_network_name" >/dev/null 2>&1; then
  docker network create \
    --driver bridge \
    --subnet "$proxy_network_subnet" \
    --gateway "$proxy_network_gateway" \
    --label "com.actverse.hermes-proxy-egress=$contract_label" \
    "$proxy_network_name" >/dev/null
fi

proxy_actual="$({
  docker network inspect \
    --format '{{.Internal}}|{{index .Labels "com.actverse.hermes-proxy-egress"}}|{{(index .IPAM.Config 0).Subnet}}|{{(index .IPAM.Config 0).Gateway}}' \
    "$proxy_network_name"
} 2>/dev/null)"
proxy_expected="false|$contract_label|$proxy_network_subnet|$proxy_network_gateway"
if [[ "$proxy_actual" != "$proxy_expected" ]]; then
  echo "refusing incompatible $proxy_network_name network: $proxy_actual" >&2
  exit 1
fi

# The external proxy container may call only the host-bound Antigravity OpenAI
# endpoint. Keep this rule narrower than generic Docker-to-host access and place
# it before the host's final reject rule. The tinyproxy destination allowlist is
# a second, independent boundary.
antigravity_port="8765"
host_rule=(
  -s "$proxy_network_subnet"
  -d "$proxy_network_gateway"
  -p tcp --dport "$antigravity_port"
  -m conntrack --ctstate NEW
  -j ACCEPT
)
if ! iptables -C INPUT "${host_rule[@]}" >/dev/null 2>&1; then
  iptables -I INPUT 5 "${host_rule[@]}"
fi
