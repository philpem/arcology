-- Migration to add unique constraint for partitions and cleanup duplicates
-- This prevents duplicate partitions with the same artefact_id and partition_index

-- First, clean up any existing duplicates (keep only the most recent partition for each artefact_id + partition_index)
-- This uses a CTE to identify duplicates and delete all but the newest one

WITH RankedPartitions AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY artefact_id, partition_index ORDER BY created_at DESC) as rn
    FROM partitions
)
DELETE FROM partitions
WHERE id IN (
    SELECT id FROM RankedPartitions WHERE rn > 1
);

-- Now add the unique constraint (skip if it already exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_partition_artefact_index'
    ) THEN
        ALTER TABLE partitions
        ADD CONSTRAINT uq_partition_artefact_index
        UNIQUE (artefact_id, partition_index);
    END IF;
END $$;
