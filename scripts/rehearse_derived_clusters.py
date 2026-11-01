import copy
import hashlib
import json
from pathlib import Path
import sys
import time

import pg8000.dbapi

from derived_cluster_backup import clear_clusters, read_clusters, restore_clusters


def main(path):
    payload = Path(path).read_bytes()
    saved = json.loads(payload)
    for attempt in range(30):
        try:
            connection = pg8000.dbapi.connect(
                host="127.0.0.1", port=5432, user="postgres", password="isolated-derived-rehearsal-only",
                database="heliosdb", timeout=3,
            )
            break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("Derived rehearsal database is not ready")
    connection.autocommit = True
    cursor = connection.cursor()
    cursor.execute("CREATE SCHEMA dashboard")
    cursor.execute("CREATE TABLE dashboard.clusters (id BIGINT PRIMARY KEY,label TEXT NOT NULL,project_slug TEXT,centroid VECTOR(384),size INT NOT NULL,built_at FLOAT8 NOT NULL)")
    cursor.execute("CREATE TABLE dashboard.cluster_members (cluster_id BIGINT NOT NULL,uuid TEXT NOT NULL,distance FLOAT8,PRIMARY KEY(cluster_id,uuid))")
    restore_clusters(connection, saved)
    assert read_clusters(connection) == saved
    clear_clusters(connection, saved)
    assert read_clusters(connection) == {"clusters": [], "members": []}
    restore_clusters(connection, saved)
    assert read_clusters(connection) == saved
    altered = copy.deepcopy(saved)
    altered["clusters"][0][1] = "different synthetic label"
    try:
        clear_clusters(connection, altered)
    except ValueError:
        pass
    else:
        raise AssertionError("Changed backup should not clear derived data")
    assert read_clusters(connection) == saved
    try:
        restore_clusters(connection, saved)
    except ValueError:
        pass
    else:
        raise AssertionError("Restore must not overwrite existing derived data")
    cursor.execute("BEGIN")
    cursor.execute("DELETE FROM dashboard.cluster_members")
    cursor.execute("ROLLBACK")
    assert read_clusters(connection) == saved, "Rollback did not preserve derived data"
    print(json.dumps({"complete": True, "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
                      "clusters": len(saved["clusters"]), "members": len(saved["members"]),
                      "roundtrip_matches": True, "changed_backup_guard": True,
                      "nonempty_restore_guard": True, "rollback_verified": True}), flush=True)
    connection.close()


if __name__ == "__main__":
    main(sys.argv[1])
