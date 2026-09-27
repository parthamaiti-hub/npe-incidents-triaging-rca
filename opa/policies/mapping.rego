# Generic INCIDENT_MAPPING_RULE evaluator. The policy itself never changes;
# both the candidate rules and the extracted incident signals are supplied
# as `input` on every query, so new mapping rules are added by inserting
# catalog rows, not by editing this file.
package npe.triage

import rego.v1

# input.signals: [{"signal_type": "table_name", "value": "DSNADEV.dcd_billing_summary"}, ...]
# input.rules:   [{"id", "source_system_id", "category", "signal_type", "signal_pattern", "priority", "action"}, ...]

matches contains rule if {
	some rule in input.rules
	some signal in input.signals
	rule.signal_type == signal.signal_type
	regex.match(rule.signal_pattern, signal.value)
}
