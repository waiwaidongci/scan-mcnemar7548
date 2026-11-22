"""生成两个扫描批次的确定性匿名验收样本。"""


def demo_rows():
    rows = []
    for batch_id, document_type, counts in (
        ("CS-FP-1121-AM", "增值税发票", (3, 24, 8, 2)),
        ("CS-XP-1121-AM", "零售小票", (0, 18, 7, 2)),
    ):
        for (a_pass, b_pass), count in zip(
            ((True, False), (True, True), (False, True), (False, False)),
            counts,
        ):
            for _ in range(count):
                rows.append({
                    "document_id": f"CS-20261121-{len(rows) + 1:03d}",
                    "batch_id": batch_id,
                    "document_type": document_type,
                    "a_pass": a_pass,
                    "b_pass": b_pass,
                })
    return rows
