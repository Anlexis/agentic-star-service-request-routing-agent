"""AgentCore Platform v1.0"""

# SVC-C2-018 — Service layer (integration seam).
#
# The service layer is the template's seam for external integrations (domain
# queries, external API wrappers, data aggregation). It must NOT contain
# business logic, routing, or credentials; nodes call into it and it in turn
# calls shared/services/ for external integrations.
#
# v1 has NO external service dependency: classification is deterministic and
# routing is resolved entirely from the config taxonomy (see BuildContextNode /
# RouteResolveNode), so there is no external lookup to implement. The module is
# retained as the documented integration seam — add concrete methods here when a
# future version requires an external data source. (Previously this shipped an
# unimplemented `fetch()` stub that raised NotImplementedError; removed as dead
# code per source review.)


class Service:
    """Domain service seam for SVC-C2-018.

    Intentionally has no methods in v1 — the classification & routing pipeline
    is self-contained (deterministic classifier + config-taxonomy routing) and
    needs no external data source. Add integration methods here when a future
    version requires external lookups; keep them free of business logic,
    routing, and credentials.
    """
