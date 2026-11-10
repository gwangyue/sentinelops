-- L3 变更层的配置快照（04 §3.4）：每次巡检写一份，下一次巡检和它比较。只追加，按时间取最新。
CREATE TABLE IF NOT EXISTS snapshots (
    id      bigserial PRIMARY KEY,
    target  text NOT NULL,
    tool    text NOT NULL,
    ts      timestamptz NOT NULL,
    raw     jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots (target, tool, ts DESC);
