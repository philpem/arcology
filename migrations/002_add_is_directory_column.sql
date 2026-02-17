-- Migration to add is_directory column to extracted_files table
-- This distinguishes ADFS directories (2KB entries) from actual files

-- Add the is_directory column with default value FALSE
ALTER TABLE extracted_files
ADD COLUMN IF NOT EXISTS is_directory BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for faster filtering
CREATE INDEX IF NOT EXISTS ix_extracted_files_is_directory
ON extracted_files(is_directory);

-- Mark existing 2KB files as directories (ADFS directories are exactly 2048 bytes)
-- This is a heuristic - ADFS directories are stored as 2KB files
UPDATE extracted_files
SET is_directory = TRUE
WHERE file_size = 2048
AND is_directory = FALSE;

-- Optionally, mark files with RISC OS directory filetype 'ddc' as directories
UPDATE extracted_files
SET is_directory = TRUE
WHERE risc_os_filetype = 'ddc'
AND is_directory = FALSE;
