-- Migration to add container_format column to partitions table
-- Stores detailed format information from disc image tools (e.g., "Acorn ADFS E", "FAT12")

-- Add the container_format column
ALTER TABLE partitions
ADD COLUMN IF NOT EXISTS container_format TEXT;

-- No data migration needed - existing partitions will have NULL container_format
-- New analyses will populate this field from DIM report output
