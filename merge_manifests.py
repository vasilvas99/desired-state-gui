#!/usr/bin/env python3

import json
from collections import defaultdict
from pathlib import Path


def get_containers(manifest):
    for domain in manifest["payload"]["domains"]:
        if domain["id"] == "containers":
            return domain["components"]
    return Exception("No containers domain found!")


def merge(paths):
    parsed_manifests = []

    for p in paths:
        with open(p) as fh:
            parsed_manifests.append(json.load(fh))

    domain_components = defaultdict(lambda: [])
    domain_configs = defaultdict(lambda: [])

    # split components and configs
    for manifest in parsed_manifests:
        for domain in manifest["payload"]["domains"]:
            domain_components[domain["id"]] += domain["components"]
            try:
                domain_configs[domain["id"]] += domain["config"]
            except KeyError as _:
                print(
                    f'[WARNING] Domain with id = {domain["id"]} is missing a config array in one of the partial manifests.'
                )

    combined_domains = []

    # produce a combined desired state manifest
    for domain_id in domain_components.keys():
        combined_domains.append(
            {
                "id": domain_id,
                "config": domain_configs[domain_id],
                "components": domain_components[domain_id],
            }
        )

    desired_state_msg = {"activityId": "", "payload": {"domains": combined_domains}}

    return desired_state_msg
