"""AgentCore Platform v1.0"""

# SVC-C2-018 — runtime configuration loader.
#
# Two configuration files ship with this template and they have different jobs:
#
#   config/agent.yaml   static manifest — identity, entry point, declared
#                       secrets/extras, required trust level. Read by the
#                       platform registry, never by node code.
#   config/config.yaml  runtime parameters — max_retry, timeout_s, the routing
#                       taxonomy. Passed to the graph as ``Graph(config=...)``
#                       and read by the nodes that consume it.
#
# Everything in this module reads config/config.yaml ONLY. A reader pointed at
# the manifest would silently receive an empty mapping and degrade to defaults
# without any signal, so the split is enforced here in one place rather than
# repeated at each call site.

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# src/config_loader.py -> parents[1] == repo root.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"

# Built-in routing taxonomy — the ALLOWED service categories and their target
# queues/handlers, used when config/config.yaml is absent or declares no
# routing block. Routing destinations exist ONLY here and in config/config.yaml;
# they are never derived from the request text.
DEFAULT_TAXONOMY_VERSION = "svc-routing-v1"
DEFAULT_CATEGORY_ID = "general_inquiry"
DEFAULT_CATEGORIES: List[Dict[str, str]] = [
    {
        "id": "it_support",
        "label": "IT Support",
        "queue": "itsm_queue",
        "handler": "IT Service Desk",
        "default_priority": "medium",
    },
    {
        "id": "hr_request",
        "label": "HR Request",
        "queue": "hrsd_queue",
        "handler": "HR Service Desk",
        "default_priority": "medium",
    },
    {
        "id": "facilities",
        "label": "Facilities & Maintenance",
        "queue": "facilities_queue",
        "handler": "Facilities Management",
        "default_priority": "low",
    },
    {
        "id": "finance_billing",
        "label": "Finance & Billing",
        "queue": "finance_queue",
        "handler": "Finance Support",
        "default_priority": "medium",
    },
    {
        "id": "customer_complaint",
        "label": "Customer Complaint",
        "queue": "customer_care_queue",
        "handler": "Customer Care",
        "default_priority": "high",
    },
    {
        "id": "general_inquiry",
        "label": "General Inquiry",
        "queue": "general_triage_queue",
        "handler": "General Triage",
        "default_priority": "low",
    },
]


def load_runtime_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read config/config.yaml and return it as a mapping.

    Returns an empty mapping when the file is absent or unreadable — the graph
    then runs on framework defaults rather than failing to construct.
    """
    target = path or CONFIG_PATH
    try:
        with target.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_routing_taxonomy(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the routing taxonomy: ``{taxonomy_version, default_category, categories}``.

    Sourced from the ``routing`` block of config/config.yaml when that block is
    present and well formed (every entry needs an ``id`` and a ``queue``);
    otherwise the built-in default above. The result always carries a
    ``source`` key — ``"config"`` or ``"builtin_default"`` — so the audit trail
    records which table actually resolved the request.
    """
    routing = load_runtime_config(path).get("routing")
    if isinstance(routing, dict):
        categories = routing.get("categories")
        if (
            isinstance(categories, list)
            and categories
            and all(isinstance(c, dict) and c.get("id") and c.get("queue") for c in categories)
        ):
            return {
                "taxonomy_version": str(routing.get("taxonomy_version", DEFAULT_TAXONOMY_VERSION)),
                "default_category": str(routing.get("default_category", DEFAULT_CATEGORY_ID)),
                "categories": [dict(c) for c in categories],
                "source": "config",
            }
    return {
        "taxonomy_version": DEFAULT_TAXONOMY_VERSION,
        "default_category": DEFAULT_CATEGORY_ID,
        "categories": [dict(c) for c in DEFAULT_CATEGORIES],
        "source": "builtin_default",
    }
