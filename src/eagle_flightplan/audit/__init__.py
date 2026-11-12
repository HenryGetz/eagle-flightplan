"""UNW degree-audit (Ellucian Degree Works) client and parser."""

from .client import AuditClient, audit_for_session
from .models import (
    Audit,
    AuditParseError,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
    RequirementStatus,
)
from .parser import parse_audit

__all__ = [
    "Audit",
    "AuditClient",
    "AuditParseError",
    "CourseRef",
    "GroupKind",
    "RequirementBlock",
    "RequirementGroup",
    "RequirementStatus",
    "audit_for_session",
    "parse_audit",
]
