"""Dependency-free provider policy exceptions shared with local accounting tools."""


class ProductionProviderError(ValueError):
    """Provider or persisted provenance cannot safely identify this runtime."""
