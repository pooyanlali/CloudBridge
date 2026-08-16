"""
core/reporter.py
Keeps a list of everything that could not be migrated cleanly.

Nothing here talks to a cloud. The mapper and the generator call these methods
whenever they change something, drop something, or find something they do not
understand. At the end of the run we write the whole list out as JSON so the
user can see exactly what the migration did and did not do.

A "gap" is one entry in that list. Every gap has the same six fields so the
report is easy to read and easy to search.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("core.reporter")

# The three levels we use, in the order we want them counted in the summary.
SEVERITIES = ["High", "Medium", "Low"]


class MigrationAuditReporter:

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.gaps: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # The one method that actually records something
    # ------------------------------------------------------------------

    def log_gap(self, resource_id: str, attribute: str, value: Any,
                gap_type: str, severity: str, notes: str) -> None:
        """Adds one entry to the report and prints it to the console."""

        # Guard against a typo in a severity string quietly breaking the
        # summary counts later on.
        if severity not in SEVERITIES:
            logger.debug(f"Unknown severity '{severity}', recording it as High.")
            severity = "High"

        self.gaps.append({
            "resource_id": resource_id,
            "attribute": attribute,
            "original_value": str(value),
            "gap_type": gap_type,
            "severity": severity,
            "notes": notes,
        })

        logger.warning(f"[GAP] [{severity}] {resource_id} -> {gap_type} ({attribute})")

    # ------------------------------------------------------------------
    # Short helpers, so the mapper does not have to repeat itself
    # ------------------------------------------------------------------

    def log_architectural_shift(self, resource: str, feature: str,
                                original: str, replacement: str) -> None:
        """The feature exists on both clouds but works differently."""
        self.log_gap(resource, feature, original, "Architectural Shift",
                     "Medium", f"Translated to {replacement}.")

    def log_naming_mutation(self, resource: str, original: str,
                            new_name: str, rule: str) -> None:
        """The name was not legal on the target cloud, so we changed it."""
        self.log_gap(resource, "name", original, "Naming Mutation", "Medium",
                     f"Renamed to '{new_name}'. {rule}")

    def log_regional_fallback(self, resource: str, original: str,
                              fallback: str) -> None:
        """We had no mapping for the source region, so we picked a default."""
        self.log_gap(resource, "region", original, "Regional Fallback", "Low",
                     f"Region not in the mapping table. Falling back to {fallback}.")

    def log_dropped(self, resource: str, attribute: str, value: Any,
                    reason: str, severity: str = "High") -> None:
        """The setting was thrown away and nothing on the target replaces it."""
        self.log_gap(resource, attribute, value, "Dropped Configuration",
                     severity, reason)

    # ------------------------------------------------------------------
    # Writing the report out
    # ------------------------------------------------------------------

    def count(self, severity: str) -> int:
        """How many gaps of one severity we have collected."""
        return len([g for g in self.gaps if g["severity"] == severity])

    def write_reports(self) -> Path:
        """Writes migration_gap_report.json and returns where it went."""
        report_path = self.output_dir / "migration_gap_report.json"

        payload = {
            "summary": {
                "total_gaps": len(self.gaps),
                "high": self.count("High"),
                "medium": self.count("Medium"),
                "low": self.count("Low"),
            },
            "gaps": self.gaps,
        }

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        logger.info(f"Gap report written to {report_path} "
                    f"({len(self.gaps)} entries, {self.count('High')} high severity).")
        return report_path
