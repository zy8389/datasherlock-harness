# Generated Benchmark Cases

`variants.yaml` is the only parameter source for the concrete benchmark set.
The canonical taxonomy and semantics remain in `config/fault_catalog.yaml` and
`benchmark/ground_truth/Fxx-001.yaml`.

Regenerate all manifests with:

```text
python -m benchmark.case_generator
```

Check for deterministic drift without changing the repository with:

```text
python -m benchmark.case_generator --check
```

Generated `Fxx-yyy.yaml` files are machine-readable outputs and must not be
edited by hand.  Materialization is on demand and does not write data unless
an output directory is explicitly provided:

```text
python -m benchmark.case_generator --materialize F01-003 --output /tmp/F01-003
```
