"""Whitelisted remediation actions and the executor that applies them."""

from sentinel.remediation.actions import ACTIONS, ActionContext, ActionDefinition
from sentinel.remediation.executor import RemediationExecutor

__all__ = ["ACTIONS", "ActionContext", "ActionDefinition", "RemediationExecutor"]
