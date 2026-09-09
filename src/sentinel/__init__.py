"""Sentinel - an AI-powered self-healing data platform.

Sentinel runs a small CPG (consumer packaged goods) data warehouse, watches every
pipeline run for quality regressions, detects anomalies with an ML model, and lets
an LLM agent diagnose the root cause and apply a whitelisted remediation.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
