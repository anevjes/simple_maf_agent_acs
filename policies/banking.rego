package banking

import rego.v1

allow := {"decision": "allow"}

input_verdict := {
    "decision": "deny",
    "reason": "prompt_injection_pattern",
    "message": "The demo policy blocks this instruction-bypass pattern.",
} if {
    regex.match(
        `(?i)(ignore (all )?(previous|prior) instructions|bypass (the )?(banking |acs )?polic(y|ies))`,
        sprintf("%v", [input.policy_target.value.content]),
    )
} else := allow

owned_accounts := {"checking-001", "savings-001"}
registered_payees := {"utility-company", "landlord"}
permitted_tools := {"get_balance", "transfer_money", "get_account_profile"}

valid_amount if {
    is_number(input.policy_target.value.amount_cents)
    input.policy_target.value.amount_cents > 0
    floor(input.policy_target.value.amount_cents) == input.policy_target.value.amount_cents
}

valid_request_id if {
    is_string(input.policy_target.value.request_id)
    trim_space(input.policy_target.value.request_id) != ""
}

tool_verdict := {
    "decision": "deny",
    "reason": "tool_not_permitted",
} if {
    not input.tool.name in permitted_tools
} else := {
    "decision": "deny",
    "reason": "account_not_owned",
} if {
    not input.policy_target.value.account_id in owned_accounts
} else := {
    "decision": "deny",
    "reason": "invalid_amount",
} if {
    input.tool.name == "transfer_money"
    not valid_amount
} else := {
    "decision": "deny",
    "reason": "invalid_request_id",
} if {
    input.tool.name == "transfer_money"
    not valid_request_id
} else := {
    "decision": "deny",
    "reason": "unregistered_payee",
} if {
    input.tool.name == "transfer_money"
    not input.policy_target.value.payee_id in registered_payees
} else := {
    "decision": "deny",
    "reason": "absolute_transfer_limit",
    "message": "Transfers above USD 5,000 cannot be approved in this demo.",
} if {
    input.tool.name == "transfer_money"
    input.policy_target.value.amount_cents > 500000
} else := {
    "decision": "deny",
    "reason": "large_transfer_requires_approval",
    "message": "Transfers above USD 1,000 require a trusted operator.",
    "approval": {"role": "demo-bank-operator"},
} if {
    input.tool.name == "transfer_money"
    input.policy_target.value.amount_cents > 100000
} else := {
    "decision": "allow",
    "warnings": [{
        "reason": "transfer_near_approval_limit",
        "message": "Review this transfer: it is at least USD 500.",
    }],
} if {
    input.tool.name == "transfer_money"
    input.policy_target.value.amount_cents >= 50000
} else := allow

tool_result_verdict := {
    "decision": "transform",
    "reason": "account_number_removed_from_tool_result",
    "transform": {"path": "$target.account_number", "value": "[ACCOUNT-REDACTED]"},
} if {
    input.tool.name == "get_account_profile"
    is_string(input.policy_target.value.account_number)
} else := allow

output_verdict := {
    "decision": "transform",
    "reason": "account_number_removed_from_output",
    "transform": {
        "path": "$target.content",
        "value": regex.replace(
            input.policy_target.value.content,
            `ACCT-[0-9]{12}`,
            "[ACCOUNT-REDACTED]",
        ),
    },
} if {
    is_string(input.policy_target.value.content)
    regex.match(`ACCT-[0-9]{12}`, input.policy_target.value.content)
} else := {
    "decision": "deny",
    "reason": "sensitive_output_shape_not_supported",
} if {
    not is_string(input.policy_target.value.content)
    regex.match(`ACCT-[0-9]{12}`, sprintf("%v", [input.policy_target.value]))
} else := allow
