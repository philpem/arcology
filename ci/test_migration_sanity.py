import unittest

from ci.check_migration_sanity import check_header_consistency, parse_header_metadata


class MigrationSanityTests(unittest.TestCase):
    def test_parse_header_metadata_extracts_revision_fields(self):
        source = '''"""Example migration

Revision ID: abc123
Revises: def456
Create Date: 2026-04-23
"""'''

        self.assertEqual(
            parse_header_metadata(source),
            {
                'header_revision': 'abc123',
                'header_down_revision': 'def456',
            },
        )

    def test_parse_header_metadata_normalises_none_like_revises(self):
        source = '''"""Initial migration

Revision ID: root123
Revises: None
"""'''

        self.assertEqual(
            parse_header_metadata(source),
            {
                'header_revision': 'root123',
                'header_down_revision': None,
            },
        )

    def test_check_header_consistency_warns_on_mismatches(self):
        migrations = [
            {
                'filename': 'example.py',
                'revision': 'abc123',
                'down_revision': 'def456',
                'header_revision': 'abc999',
                'header_down_revision': 'def999',
            },
        ]

        warnings = check_header_consistency(migrations)

        self.assertEqual(len(warnings), 2)
        self.assertIn("Header 'Revision ID: abc999'", warnings[0])
        self.assertIn("Header 'Revises: def999'", warnings[1])

    def test_check_header_consistency_ignores_matching_or_missing_headers(self):
        migrations = [
            {
                'filename': 'matching.py',
                'revision': 'abc123',
                'down_revision': 'def456',
                'header_revision': 'abc123',
                'header_down_revision': 'def456',
            },
            {
                'filename': 'missing.py',
                'revision': 'ghi789',
                'down_revision': None,
                'header_revision': None,
                'header_down_revision': None,
            },
        ]

        self.assertEqual(check_header_consistency(migrations), [])


if __name__ == '__main__':
    unittest.main()
