"""tests/evals/__init__ — eval-harness test package (issue #9).

Workstreams:
- task-failures: test_failures_jsonl.py (the failures.jsonl pin + hook)
- task-gates: test_gates.py, test_failure_classes.py (gates 1–5 + taxonomy)
- task-slice: test_slice_gate.py (gate 6 slice dry-run delegation)
- task-region: test_region_gate.py (gate 7 region containment)
- task-golden-set: test_golden_set_seed.py, test_prompt_pins.py (the 20-case
  seed + hash-pinned prompt files)
- task-harness: test_promptfoo_harness.py, test_judge_protocol.py,
  test_adversarial_cases.py (promptfoo YAML + report + judge)
"""
