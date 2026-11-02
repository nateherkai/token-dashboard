import json


TABLE_COLUMNS = {
    "messages": ("uuid", "prompt_text"),
    "tool_results": ("tool_use_id", "body_text"),
}


def change_vector(connection, record, restore=False):
    table = record["table"]
    identifier, text_column = TABLE_COLUMNS[table]
    if not all(isinstance(record[key], str) for key in ["id", "text", "vector"]):
        raise ValueError("Quarantine record requires original identifier, source text and text-cast vector")
    cursor = connection.cursor()
    try:
        if restore:
            cursor.execute(
                "UPDATE dashboard." + table + " SET body_vec=CAST(%s AS VECTOR(384)) WHERE "
                + identifier + "=%s AND " + text_column + "=%s AND body_vec IS NULL",
                (record["vector"], record["id"], record["text"]),
            )
        else:
            cursor.execute(
                "UPDATE dashboard." + table + " SET body_vec=NULL WHERE " + identifier
                + "=%s AND " + text_column + "=%s AND CAST(body_vec AS TEXT)=%s",
                (record["id"], record["text"], record["vector"]),
            )
        affected = cursor.rowcount
        if affected not in (0, 1):
            raise RuntimeError("Unexpected affected-row count; stop and inspect journal")
        return affected
    finally:
        cursor.close()


def records(path):
    with open(path, encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record["kind"] == "row":
                yield record
