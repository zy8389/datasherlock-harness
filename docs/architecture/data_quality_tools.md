# Data Quality Tool Result Contract

Data quality tools use a status envelope and a tri-state `passed` field. The
two fields must be interpreted together:

| Result | Meaning |
| --- | --- |
| `status="success", passed=True` | The tool completed and found no anomaly. |
| `status="success", passed=False` | The tool completed and found a structured anomaly. |
| `status="success", passed=None` | The tool completed but could not reach a conclusion. |
| `status="error"` | The tool contract or execution failed. |

An inconclusive result is not an execution failure. The executor therefore
propagates every successful data quality result as `ToolExecutionResult.success=True`,
including results where `passed=None`. Runtime evidence interpretation remains
fail-closed: only a scoped, structured anomaly with `passed=False` can support
a hypothesis. Passed and inconclusive checks are neutral by default.

## Schema Drift History

`detect_schema_drift` compares the two latest rows in `schema_snapshots`. If
the query succeeds but fewer than two snapshots exist, it returns:

```text
status="success"
passed=None
assessment="insufficient_history"
```

The evidence also records `snapshot_count` and `required_snapshot_count=2`.
This neutral observation allows the investigation to continue without
inventing a baseline snapshot. Missing tables, SQL failures, timeouts,
unexpected result shapes, invalid arguments, and malformed `schema_json`
remain errors.
