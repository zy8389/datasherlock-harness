# Architecture Documentation

Use this directory as the map from the system-level design to the contracts
owned by individual components.

| Document | Purpose |
| --- | --- |
| [System Overview](system_overview.md) | Current-main scope, components, runtime flow, state machine, recovery, repair, and benchmark architecture |
| [Planner](planner.md) | Structured planning inputs, outputs, model boundary, fallback, and semantic validation |
| [Planner Evidence-Source Coverage](planner_evidence_coverage.md) | Per-hypothesis step coverage and canonical source inference introduced after PR #20 |
| [Data Quality Tools](data_quality_tools.md) | Tri-state DQ result contract and schema-history behavior |
| [Ground Truth Evidence](evidence.md) | Offline Ground Truth evidence paths and catalog alignment |
| [Runtime Evidence](runtime_evidence.md) | Fail-closed runtime admission, typed SQL rules, polarity, and provenance |

`system_overview.md` explains how these components fit together. The specialist
documents remain the source for detailed contracts and should not be read as
separate runtime implementations.
