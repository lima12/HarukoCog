-- HLL account-link and independent RCON statistics constraints.
-- Run as a PostgreSQL role allowed to alter the slhhll tables.
-- The transaction aborts without changing constraints when invalid legacy rows exist.

BEGIN;

LOCK TABLE slhhll."Discord", slhhll."RCON_DATA" IN ACCESS EXCLUSIVE MODE;

-- Existing null counters are safe to normalize before making them required.
UPDATE slhhll."RCON_DATA"
SET
    "Kill" = COALESCE("Kill", 0),
    "Dead" = COALESCE("Dead", 0)
WHERE "Kill" IS NULL OR "Dead" IS NULL;

-- Refuse to silently discard or merge identity data.
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM slhhll."RCON_DATA"
        WHERE "EOS_Id" IS NULL OR btrim("EOS_Id"::text) = ''
    ) THEN
        RAISE EXCEPTION 'RCON_DATA contains a null or blank EOS_Id';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM slhhll."RCON_DATA"
        GROUP BY "EOS_Id"
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'RCON_DATA contains duplicate EOS_Id values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM slhhll."Discord"
        WHERE "Discord_Id" IS NULL OR btrim("Discord_Id"::text) = ''
    ) THEN
        RAISE EXCEPTION 'Discord contains a null or blank Discord_Id';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM slhhll."Discord"
        WHERE "EOS_Id" IS NULL OR btrim("EOS_Id"::text) = ''
    ) THEN
        RAISE EXCEPTION 'Discord contains a null or blank EOS_Id';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM slhhll."Discord"
        GROUP BY "Discord_Id"
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Discord contains duplicate Discord_Id values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM slhhll."Discord"
        GROUP BY "EOS_Id"
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Discord contains duplicate EOS_Id values';
    END IF;
END
$migration$;

ALTER TABLE slhhll."RCON_DATA"
    ALTER COLUMN "EOS_Id" SET NOT NULL,
    ALTER COLUMN "Kill" TYPE bigint USING COALESCE("Kill", 0)::bigint,
    ALTER COLUMN "Kill" SET DEFAULT 0,
    ALTER COLUMN "Kill" SET NOT NULL,
    ALTER COLUMN "Dead" TYPE bigint USING COALESCE("Dead", 0)::bigint,
    ALTER COLUMN "Dead" SET DEFAULT 0,
    ALTER COLUMN "Dead" SET NOT NULL;

ALTER TABLE slhhll."Discord"
    ALTER COLUMN "Discord_Id" SET NOT NULL,
    ALTER COLUMN "EOS_Id" SET NOT NULL;

-- Make the two stable identity columns the primary keys. Abort instead of
-- replacing an unexpected existing primary key.
DO $migration$
DECLARE
    eos_attnum smallint;
    primary_key_columns smallint[];
BEGIN
    SELECT attnum
    INTO eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."RCON_DATA"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    SELECT conkey
    INTO primary_key_columns
    FROM pg_constraint
    WHERE conrelid = 'slhhll."RCON_DATA"'::regclass
      AND contype = 'p';

    IF primary_key_columns IS NULL THEN
        ALTER TABLE slhhll."RCON_DATA"
            ADD CONSTRAINT rcon_data_pkey PRIMARY KEY ("EOS_Id");
    ELSIF primary_key_columns <> ARRAY[eos_attnum]::smallint[] THEN
        RAISE EXCEPTION
            'RCON_DATA already has a primary key on columns other than EOS_Id';
    END IF;
END
$migration$;

DO $migration$
DECLARE
    discord_attnum smallint;
    primary_key_columns smallint[];
BEGIN
    SELECT attnum
    INTO discord_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."Discord"'::regclass
      AND attname = 'Discord_Id'
      AND NOT attisdropped;

    SELECT conkey
    INTO primary_key_columns
    FROM pg_constraint
    WHERE conrelid = 'slhhll."Discord"'::regclass
      AND contype = 'p';

    IF primary_key_columns IS NULL THEN
        ALTER TABLE slhhll."Discord"
            ADD CONSTRAINT discord_pkey PRIMARY KEY ("Discord_Id");
    ELSIF primary_key_columns <> ARRAY[discord_attnum]::smallint[] THEN
        RAISE EXCEPTION
            'Discord already has a primary key on columns other than Discord_Id';
    END IF;
END
$migration$;

-- One game account may be linked to at most one Discord account.
DO $migration$
DECLARE
    eos_attnum smallint;
BEGIN
    SELECT attnum
    INTO eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."Discord"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'slhhll."Discord"'::regclass
          AND contype IN ('p', 'u')
          AND conkey = ARRAY[eos_attnum]::smallint[]
    ) THEN
        ALTER TABLE slhhll."Discord"
            ADD CONSTRAINT discord_eos_id_key UNIQUE ("EOS_Id");
    END IF;
END
$migration$;

-- Ensure every existing Discord link has a parent statistics row.
INSERT INTO slhhll."RCON_DATA" ("EOS_Id", "Kill", "Dead")
SELECT DISTINCT "EOS_Id", 0, 0
FROM slhhll."Discord"
ON CONFLICT ("EOS_Id") DO NOTHING;

-- Remove only the EOS_Id relationship in the wrong direction, regardless of
-- its old constraint name.
DO $migration$
DECLARE
    reverse_fk_name name;
    rcon_eos_attnum smallint;
    discord_eos_attnum smallint;
BEGIN
    SELECT attnum
    INTO rcon_eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."RCON_DATA"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    SELECT attnum
    INTO discord_eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."Discord"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    FOR reverse_fk_name IN
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid = 'slhhll."RCON_DATA"'::regclass
          AND confrelid = 'slhhll."Discord"'::regclass
          AND conkey = ARRAY[rcon_eos_attnum]::smallint[]
          AND confkey = ARRAY[discord_eos_attnum]::smallint[]
    LOOP
        EXECUTE format(
            'ALTER TABLE slhhll."RCON_DATA" DROP CONSTRAINT %I',
            reverse_fk_name
        );
    END LOOP;
END
$migration$;

-- Add Discord.EOS_Id -> RCON_DATA.EOS_Id if that exact relationship is absent.
DO $migration$
DECLARE
    discord_eos_attnum smallint;
    rcon_eos_attnum smallint;
BEGIN
    SELECT attnum
    INTO discord_eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."Discord"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    SELECT attnum
    INTO rcon_eos_attnum
    FROM pg_attribute
    WHERE attrelid = 'slhhll."RCON_DATA"'::regclass
      AND attname = 'EOS_Id'
      AND NOT attisdropped;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid = 'slhhll."Discord"'::regclass
          AND confrelid = 'slhhll."RCON_DATA"'::regclass
          AND conkey = ARRAY[discord_eos_attnum]::smallint[]
          AND confkey = ARRAY[rcon_eos_attnum]::smallint[]
    ) THEN
        ALTER TABLE slhhll."Discord"
            ADD CONSTRAINT discord_eos_id_fkey
            FOREIGN KEY ("EOS_Id")
            REFERENCES slhhll."RCON_DATA" ("EOS_Id")
            ON UPDATE CASCADE
            ON DELETE RESTRICT;
    END IF;
END
$migration$;

COMMIT;

-- Expected final relationship:
-- slhhll."RCON_DATA" (one) <- (zero or one) slhhll."Discord"
