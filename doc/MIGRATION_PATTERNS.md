# Migration patterns

Reference implementations for hand-written migrations.  The original worked
examples lived in the pre-squash migration chain (47 migrations consolidated
into `20260604_223020_initial_migration.py`); the reusable patterns are
preserved here.

## ArtefactType downgrade cascade

PostgreSQL cannot remove an enum value once added, so a migration that adds an
`ArtefactType` value must, on downgrade, delete every artefact of that type
**and all dependent rows in FK-safe order** (see CLAUDE.md, "Adding a new
analysis type", for the full rationale).  Copy this helper into the migration
(originally from `20260324_201523_add_arc_artefact_type.py`, revision
`000069c2f0db`, now squashed away — recover the full file with
`git log --all --diff-filter=D -- 'migrations/versions/*add_arc*'`):

```python
def _cascade_sql(type_names):
    """Build the FK-safe DELETE cascade for the given artefact type names.

    PostgreSQL DO blocks cannot accept bind parameters, so type names are
    inlined as quoted SQL literals.  Names are hardcoded enum identifiers
    (no user input) so this is safe.
    """
    types_in = ', '.join(f"'{name}'" for name in type_names)
    return f"""
    DO $$
    DECLARE _ids INTEGER[];
    BEGIN
        SELECT array_agg(id) INTO _ids FROM artefacts
            WHERE artefact_type IN ({types_in});
        IF _ids IS NULL THEN RETURN; END IF;

        UPDATE artefacts SET parent_artefact_id = NULL
            WHERE parent_artefact_id = ANY(_ids);
        UPDATE artefacts SET derived_from_analysis_id = NULL
            WHERE derived_from_analysis_id IN (
                SELECT id FROM analyses WHERE artefact_id = ANY(_ids));

        DELETE FROM extracted_file_restrictions
            WHERE extracted_file_id IN (
                SELECT ef.id FROM extracted_files ef
                JOIN partitions p ON ef.partition_id = p.id
                WHERE p.artefact_id = ANY(_ids));

        DELETE FROM extracted_files
            WHERE partition_id IN (SELECT id FROM partitions WHERE artefact_id = ANY(_ids));
        DELETE FROM recognised_products
            WHERE partition_id IN (SELECT id FROM partitions WHERE artefact_id = ANY(_ids));
        DELETE FROM partitions WHERE artefact_id = ANY(_ids);
        DELETE FROM analyses WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_protection  WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_mastering   WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_restrictions WHERE artefact_id = ANY(_ids);
        DELETE FROM riscos_modules WHERE artefact_id = ANY(_ids);
        DELETE FROM user_artefact_bypasses WHERE artefact_id = ANY(_ids);
        DELETE FROM artefact_tags WHERE artefact_id = ANY(_ids);
        DELETE FROM artefacts WHERE id = ANY(_ids);
    END $$
"""


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    op.execute(sa.text(_cascade_sql(['MY_NEW_TYPE'])))
```

> The original (pre-squash) version wrapped some DELETEs in
> `information_schema.tables` existence checks because the referenced tables
> were created later in the chain.  After the squash all tables exist from the
> initial migration onward, so new migrations can use unconditional DELETEs as
> above.  `user_artefact_bypasses` post-dates the original helper and is
> included here.

## Squashed-chain archaeology

The 47 pre-squash migration files were deleted in the consolidation commit.
To inspect any of them:

```bash
git log --oneline --diff-filter=D -- migrations/versions/   # find the deletion commit
git show <commit>^:migrations/versions/<filename>           # view a deleted migration
```

# vim: ts=4 sw=4 et
