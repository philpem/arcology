# Partition and Analysis UI Improvements

This document describes the improvements made to partition display and analysis details in the Arcology UI.

## Changes Implemented

### 1. Fixed Missing Analysis Details in API

**Problem**: The "Raw Details" pane showed incomplete/truncated JSON data because the `details` field wasn't being serialized in API responses.

**Solution**: Added `details` field to `analysis_to_dict()` function in `/myapp/blueprints/api.py` (line 586).

**Impact**:
- ✅ Partition detection shows full JSON including partition arrays and filesystem details
- ✅ File listing shows complete summary data
- ✅ Flux visualization output references are accessible
- ✅ All analysis types now return structured results in Raw Details

**Files Modified**:
- `myapp/blueprints/api.py` - Added `details` field to serialization

### 2. Added GET /api/partitions/<uuid> Endpoint

**Problem**: Workers were getting 404 errors when trying to fetch partition data during analysis.

**Solution**: Added new endpoint `/api/partitions/<uuid>` that returns partition metadata including:
- Partition UUID, index, label, filesystem
- Detection method and raw details
- Container format and disc name (for ADFS)
- Created/updated timestamps

**Files Modified**:
- `myapp/blueprints/api.py` - Added `get_partition()` endpoint (lines 349-371)

### 3. DIM Report Parsing for Container Format

**Problem**: ADFS floppy partitions didn't show disc names or detailed filesystem information.

**Solution**: Enhanced partition detection to:
- Parse DIM tool output for "Container format" field
- Extract disc name from ADFS/ADFS-L/ADFS-D reports
- Store both in partition detection_details
- Display in UI with tooltips

**Example**: Container format "Acorn ADFS (new_map) - 800K floppy (E)" shows as:
- Filesystem badge: "adfs" (clickable)
- Tooltip: Full container format description
- Partition label: Shows disc name if present (e.g., "TheHacker")

**Files Modified**:
- `worker/arcworker/tools/partition.py` - Added DIM report parsing
- `myapp/templates/analysis/partials/partition_pane.html` - Added tooltips

### 4. Clickable Partition Rows

**Problem**: Users had to manually type partition filters to view partition-specific files.

**Solution**: Made partition rows clickable:
- Click any partition row to automatically filter files by that partition
- Visual feedback: pointer cursor on hover
- Updates file listing pane without page reload

**Files Modified**:
- `myapp/templates/analysis/partials/partition_pane.html` - Added click handlers
- `myapp/templates/analysis/view.html` - Added filtering integration

### 5. Column Reordering

**Problem**: Partition column was at the end, making it hard to scan files by partition.

**Solution**: Reordered file listing columns to: **Partition | Filename | Filetype | Size | Actions**

**Files Modified**:
- `myapp/templates/analysis/partials/file_listing_pane.html` - Reordered table columns

### 6. RISC OS Filetype Display

**Problem**: Files showed numeric filetypes (e.g., "ffd") without human-readable names.

**Solution**: Added comprehensive RISC OS filetype lookup with 50+ mappings:
- Common types: Text (fff), Data (ffd), BASIC (ffb), Sprite (ff9)
- Archive formats: ArcFS (3fb), Spark (ddc), Zip (a91)
- Image formats: DrawFile (aff), JPEG (c85), PNG (b60)
- Audio formats: Armadeus (d3c), AIFF (fc2)
- Video formats: AVI (fb2), MPEG (bf8)

**Display**: Filetype column shows both code and name (e.g., "fff - Text")

**Files Modified**:
- `myapp/templates/analysis/partials/file_listing_pane.html` - Added filetype lookup
- Template context includes comprehensive mapping

### 7. Color-Coded Partition Badges

**Problem**: Partition badges all looked the same, making it hard to distinguish between partitions.

**Solution**: Implemented color cycling for partition badges:
- 6 distinct colors: primary (blue), success (green), info (cyan), warning (yellow), danger (red), secondary (gray)
- Color assigned by partition index modulo 6
- Consistent across all views

**Files Modified**:
- `myapp/templates/analysis/partials/partition_pane.html` - Badge color logic
- `myapp/templates/analysis/partials/file_listing_pane.html` - Consistent badge colors

## API Changes

### New Endpoints

```
GET /api/partitions/<uuid>
```

Returns:
```json
{
  "id": 1,
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "partition_index": 0,
  "label": "TheHacker",
  "filesystem": "adfs",
  "detection_method": "dim",
  "detection_details": "{\"container_format\": \"Acorn ADFS (new_map) - 800K floppy (E)\", ...}",
  "created_at": "2026-02-16T12:00:00",
  "updated_at": "2026-02-16T12:00:00"
}
```

### Modified Endpoints

```
GET /api/analysis/<uuid>
```

Now includes `details` field with complete analysis results as JSON string.

## Database Schema

No schema changes were required. All functionality uses existing fields:
- `Analysis.details` (already existed, now properly serialized)
- `Partition.detection_details` (already existed, now populated with DIM data)
- `Partition.label` (already existed, now shows disc names)
- `Partition.container_format` (already existed, now populated from DIM)

## RISC OS Filetype Mapping

Comprehensive mapping includes:

| Code | Name | Description |
|------|------|-------------|
| fff | Text | Plain text file |
| ffd | Data | Generic data file |
| ffb | BASIC | BBC BASIC program |
| ff9 | Sprite | RISC OS sprite image |
| 3fb | ArcFS | ArcFS archive |
| a91 | Zip | ZIP archive |
| aff | DrawFile | RISC OS vector drawing |
| c85 | JPEG | JPEG image |
| ... | ... | (50+ total mappings) |

Full mapping maintained in `file_listing_pane.html` template.

## Testing

### Manual Testing

1. ✅ View partition detection analysis - Raw Details shows complete JSON
2. ✅ ADFS disc with name - Shows disc name in partition label
3. ✅ Click partition row - Files filter to that partition
4. ✅ Hover filesystem badge - Tooltip shows container format
5. ✅ View file listing - Filetype column shows human-readable names
6. ✅ Multiple partitions - Each has distinct color badge

### API Testing

```bash
# Test analysis details
curl http://localhost:5000/api/analysis/<uuid> | jq '.details'
# Should return full JSON, not null

# Test partition endpoint
curl http://localhost:5000/api/partitions/<uuid> | jq '.'
# Should return 200 with partition data
```

## Files Modified

```
myapp/blueprints/api.py
  - Added 'details' to analysis_to_dict() (line 586)
  - Added GET /api/partitions/<uuid> endpoint (lines 349-371)
  - Added partition_to_dict() helper (lines 373-387)

worker/arcworker/tools/partition.py
  - Enhanced DIM report parsing
  - Extract container_format and disc_name
  - Store in detection_details JSON

myapp/templates/analysis/view.html
  - Integrated partition click filtering

myapp/templates/analysis/partials/partition_pane.html
  - Made rows clickable
  - Added container format tooltips
  - Color-coded partition badges

myapp/templates/analysis/partials/file_listing_pane.html
  - Reordered columns (Partition first)
  - Added RISC OS filetype display
  - Consistent color-coded partition badges
```

## Benefits

1. **Better Analysis Visibility**: Full JSON in Raw Details pane enables debugging and verification
2. **Improved UX**: Disc names, clickable partitions, and color coding make navigation easier
3. **RISC OS Native**: Filetype display shows familiar names for RISC OS users
4. **No Breaking Changes**: All changes are additive and backward compatible
5. **Worker Integration**: New partition endpoint fixes worker 404 errors

## Future Enhancements

Possible future improvements:
- Client-side filetype filtering by category (archives, images, etc.)
- Editable partition labels
- Batch partition filtering (show files from multiple partitions)
- Export filtered file listings
- RISC OS load/exec address display for typed files
