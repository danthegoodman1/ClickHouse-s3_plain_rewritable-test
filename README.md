# ClickHouse s3_plain_rewritable test

Shows how to create a single writer, and an unlimited number of reader clickhouse nodes using s3 for storage wholly (nothing more than the create table statement needed).

```
docker compose up -d
```

In two terminals:

```
docker exec -it ch-w clickhouse-server
```

```
docker exec -it ch-r clickhouse-server
```

Then do the instructions in `test.sql`

Then when done:

```
docker compose down -v
```
