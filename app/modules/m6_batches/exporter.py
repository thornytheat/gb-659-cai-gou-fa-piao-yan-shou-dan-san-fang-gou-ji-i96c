import csv
import io

FIELDS = [
    "business_key", "doc_no", "line_no", "reason_code", "currency",
    "original_amount", "amount_cny", "cost_cny", "payable_cny", "fx_difference_cny",
]


def differences_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
