# Native ClickHouse
Ubuntu 22.04 amd64; official LTS packages pinned by version and SHA-256.
HTTPS only on the selected private address. Separate administrator, writer and
read-only credentials; caller must provide three secrets of at least 24 characters.
Persistent data and native backup directory beneath `/srv/clickhouse`.
Logical event uniqueness is exposed through `analytics.events FINAL` and the
`analytics.measurements` view; this does not claim physical exactly-once insertion.
Verification covers authentication, authorization, exact payloads, duplicates,
service/VM persistence and a native backup restored into an isolated database.
