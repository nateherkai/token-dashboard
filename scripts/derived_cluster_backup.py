import json
import sys

from token_dashboard import helios_writer


def read_clusters(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT id,label,project_slug,CAST(centroid AS TEXT),size,built_at FROM dashboard.clusters ORDER BY id LIMIT 10001")
        clusters = [list(row) for row in cursor.fetchall()]
        cursor.execute("SELECT cluster_id,uuid,distance FROM dashboard.cluster_members ORDER BY cluster_id,uuid LIMIT 100001")
        members = [list(row) for row in cursor.fetchall()]
        if len(clusters) > 10000 or len(members) > 100000:
            raise ValueError("Derived cluster backup exceeds bounds")
        return {"clusters": clusters, "members": members}
    finally:
        cursor.close()


def clear_clusters(connection, expected):
    cursor = connection.cursor()
    try:
        cursor.execute("BEGIN")
        if read_clusters(connection) != expected:
            raise ValueError("Derived clusters changed since backup; refuse removal")
        cursor.execute("DELETE FROM dashboard.cluster_members")
        cursor.execute("DELETE FROM dashboard.clusters")
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        raise
    finally:
        cursor.close()


def restore_clusters(connection, saved):
    cursor = connection.cursor()
    try:
        cursor.execute("BEGIN")
        current = read_clusters(connection)
        if current["clusters"] or current["members"]:
            raise ValueError("Refuse to overwrite existing derived clusters")
        for row in saved["clusters"]:
            cursor.execute("INSERT INTO dashboard.clusters VALUES (%s,%s,%s,CAST(%s AS VECTOR(384)),%s,%s)", tuple(row))
        for row in saved["members"]:
            cursor.execute("INSERT INTO dashboard.cluster_members VALUES (%s,%s,%s)", tuple(row))
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        raise
    finally:
        cursor.close()


def main():
    connection = helios_writer._connect_raw()
    try:
        json.dump(read_clusters(connection), sys.stdout, ensure_ascii=True, allow_nan=False)
        sys.stdout.write("\n")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
