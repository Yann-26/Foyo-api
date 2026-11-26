"""Balance rules (spec §13): an expense lowers its account, an income raises it,
a transfer moves money between two accounts and never changes monthly totals."""


def balance_deltas(op_type: str, amount: int, account_id: str, to_account_id: str | None) -> dict[str, int]:
    match op_type:
        case "expense":
            return {account_id: -amount}
        case "income":
            return {account_id: amount}
        case "transfer":
            assert to_account_id
            return {account_id: -amount, to_account_id: amount}
    raise ValueError(op_type)
