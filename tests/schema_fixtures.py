"""仅供临时测试数据库构造旧 schema；不是生产迁移或降级入口。"""


def restore_v8_layout(connection):
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构夹具需要关闭外键")
    connection.execute("DROP TABLE IF EXISTS expression_heads")
    connection.execute("PRAGMA user_version = 8")


def restore_v7_layout(connection):
    restore_v8_layout(connection)
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构夹具需要关闭外键")
    connection.execute("DROP TABLE IF EXISTS conversations")
    connection.execute("PRAGMA user_version = 7")


def restore_v6_layout(connection):
    restore_v7_layout(connection)
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构夹具必须使用独立且关闭外键的临时连接")
    connection.execute("DROP TABLE IF EXISTS relationship_receipts")
    connection.execute("DROP TABLE IF EXISTS relationships")
    connection.execute("PRAGMA user_version = 6")


def restore_v5_layout(connection):
    restore_v6_layout(connection)
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构夹具必须使用独立且关闭外键的临时连接")
    columns=("memory_id,agent_id,scope_id,kind,visibility,speaker_id,about_id,claim_type,provenance,"
             "topic_key,target_start_at,target_end_at,plan_status,sort_key,revision,record_json")
    connection.execute("""CREATE TABLE memories_v5 (
        memory_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        scope_id TEXT NOT NULL, kind TEXT NOT NULL, visibility TEXT NOT NULL, speaker_id TEXT, about_id TEXT,
        claim_type TEXT NOT NULL, provenance TEXT NOT NULL, topic_key TEXT NOT NULL,
        target_start_at TEXT, target_end_at TEXT, plan_status TEXT, sort_key TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL)""")
    connection.execute(f"INSERT INTO memories_v5 ({columns}) SELECT {columns} FROM memories")
    connection.execute("DROP TABLE memories")
    connection.execute("ALTER TABLE memories_v5 RENAME TO memories")
    connection.execute("CREATE INDEX memory_recent_idx ON memories (agent_id,scope_id,sort_key)")
    connection.execute("CREATE INDEX memory_subject_idx ON memories (agent_id,scope_id,about_id,claim_type,sort_key)")
    connection.execute("CREATE INDEX memory_merge_idx ON memories (agent_id,scope_id,about_id,claim_type,kind,provenance,sort_key)")
    connection.execute("CREATE INDEX memory_target_idx ON memories (agent_id,scope_id,about_id,claim_type,target_end_at,memory_id)")
    connection.execute("PRAGMA user_version = 5")


def restore_v4_layout(connection):
    restore_v6_layout(connection)
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构夹具必须使用独立且关闭外键的临时连接")
    for table in ("memory_terms","memory_sources","memory_revisions","memory_receipts","memories"):
        connection.execute(f'DROP TABLE "{table}"')
    connection.execute("PRAGMA user_version = 4")


def restore_v3_layout(connection):
    if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("旧结构测试夹具需要使用独立且未启用外键的临时连接")
    restore_v4_layout(connection)
    connection.execute("DROP TABLE feedback_heads")
    connection.execute("""CREATE TABLE activity_instances_v3 (
        activity_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        definition_json TEXT NOT NULL, instance_json TEXT NOT NULL)""")
    connection.execute("INSERT INTO activity_instances_v3 SELECT activity_id,agent_id,revision,definition_json,instance_json FROM activity_instances")
    connection.execute("DROP TABLE activity_instances")
    connection.execute("ALTER TABLE activity_instances_v3 RENAME TO activity_instances")
    connection.execute("PRAGMA user_version = 3")
