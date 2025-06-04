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

select uuid from system.tables where name = 'writer'; -- need this for <UUID>


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
