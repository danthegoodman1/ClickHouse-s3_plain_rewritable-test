-- Writer
CREATE TABLE writer (
  id UInt64
) ORDER BY ()
SETTINGS
  table_disk = true,
  disk = disk(
    type = object_storage,
    object_storage_type = s3,
    endpoint = 'http://minio:9000/testbucket',
    metadata_type = plain_rewritable,
    region = 'us-east-1',
    access_key_id = 'user',
    secret_access_key = 'password'
  )
;

INSERT INTO writer (id) VALUES (1);
INSERT INTO writer (id) VALUES (2);

select * from writer;


-- Reader
CREATE TABLE reader (
  id UInt64
) ORDER BY ()
SETTINGS
  table_disk = true,
  refresh_parts_interval = 1, -- only in readonly mode
  disk = disk(
    type = object_storage,
    object_storage_type = s3,
    endpoint = 'http://minio:9000/testbucket',
    metadata_type = plain_rewritable,
    region = 'us-east-1',
    access_key_id = 'user',
    secret_access_key = 'password',
    readonly = true
  )
;

SELECT * FROM reader;


-- Writer
INSERT INTO writer (id) select number from numbers(100);


-- Reader
SELECT * FROM reader;


-- Writer

DETACH TABLE writer;

-- then run:
--  rm /var/lib/clickhouse/metadata/default/writer.sql
--  rm -rf /var/lib/clickhouse/data/default/writer

-- you have to use a new UUID, while the node is booted it has the old UUID in memory
select generateUUIDv4();

-- this comes from the /var/lib/clickhouse/metadata/default/writer.sql but uses the new UUID
ATTACH TABLE writer UUID '2e4d59c4-0547-41a6-a961-1b743cff35d7' (
  id UInt64
) ORDER BY ()
SETTINGS
  table_disk = true,
  disk = disk(
    type = object_storage,
    object_storage_type = s3,
    endpoint = 'http://minio:9000/testbucket',
    metadata_type = plain_rewritable,
    region = 'us-east-1',
    access_key_id = 'user',
    secret_access_key = 'password'
  )
  , index_granularity = 8192
;
