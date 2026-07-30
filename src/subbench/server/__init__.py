"""Server side of SubBench: ingest pushed evidence, derive estimates, serve them.

Derivation is not reimplemented here. This package imports the same estimator the CLI
uses, so an improvement to it applies identically to local reports and to the dashboard.
"""
