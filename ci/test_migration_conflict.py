import unittest

from ci.check_migration_conflict import find_conflicts, get_new_migrations


class MigrationConflictTests(unittest.TestCase):
    def test_renamed_existing_migration_is_not_treated_as_new(self):
        target_migrations = [
            {
                'filename': '20260420_1112_add_image_artefact_type.py',
                'revision': '000069e60a13',
                'down_revision': '000069e74e9c',
            },
        ]
        local_migrations = [
            {
                'filename': '20260423_0001_add_image_artefact_type.py',
                'revision': '000069e60a13',
                'down_revision': '000069e74e9c',
            },
        ]

        self.assertEqual(
            get_new_migrations(local_migrations, target_migrations),
            [],
        )

    def test_new_migration_conflicting_on_same_parent_is_reported(self):
        target_migrations = [
            {
                'filename': '20260420_1112_add_image_artefact_type.py',
                'revision': '000069e60a13',
                'down_revision': '000069e74e9c',
            },
        ]
        new_migrations = [
            {
                'filename': '20260423_0113_add_detect_track_density.py',
                'revision': '000069e9644a',
                'down_revision': '000069e74e9c',
            },
        ]

        errors = find_conflicts(
            new_migrations, target_migrations, 'origin/master'
        )

        self.assertEqual(len(errors), 1)
        self.assertIn('20260423_0113_add_detect_track_density.py', errors[0])
        self.assertIn('20260420_1112_add_image_artefact_type.py', errors[0])


if __name__ == '__main__':
    unittest.main()
