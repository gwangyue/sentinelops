-- 调查、审计、版本、观测索引（01 §2 第⑤层）。checkpoint 表由 PostgresSaver.setup() 自己建。
-- 只用 CREATE ... IF NOT EXISTS：启动时执行，可以重复执行。表结构改动时新增 002_*.sql。
CREATE TABLE IF NOT EXISTS investigations (
    id              text PRIMARY KEY,
    target          text NOT NULL,
    mode            text NOT NULL,
    agent_version   text NOT NULL,
    status          text NOT NULL,
    created_at      timestamptz NOT NULL,
    findings        jsonb NOT NULL DEFAULT '[]',
    fact_sheets     jsonb NOT NULL DEFAULT '[]',
    context_bundle  jsonb,
    diagnosis       jsonb,
    escalate_reason text
);

-- 只追加（07 篇）：应用账号没有 UPDATE / DELETE 权限这件事在 M3 补上；这里先靠哈希链发现篡改
CREATE TABLE IF NOT EXISTS audit_events (
    seq             bigserial PRIMARY KEY,
    id              text UNIQUE NOT NULL,
    ts              timestamptz NOT NULL,
    investigation_id text,
    agent_version   text,
    actor           text NOT NULL,
    type            text NOT NULL,
    payload         jsonb NOT NULL,
    prev_hash       text NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_inv ON audit_events (investigation_id);

CREATE TABLE IF NOT EXISTS agent_versions (
    seq         integer PRIMARY KEY,
    hash        text UNIQUE NOT NULL,
    parent      text,
    created_at  timestamptz NOT NULL,
    note        text NOT NULL CHECK (note <> ''),
    components  jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id               text PRIMARY KEY,
    investigation_id text NOT NULL,
    created_at       timestamptz NOT NULL,
    body             jsonb NOT NULL           -- Observation 契约的完整 JSON
);
CREATE INDEX IF NOT EXISTS observations_inv ON observations (investigation_id);
