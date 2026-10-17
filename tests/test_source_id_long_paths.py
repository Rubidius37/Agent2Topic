import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hierarchical_classifier as classifier
from atomic_io import filesystem_path


class SourceIdPathTests(unittest.TestCase):
    def test_failure_includes_file_stage_and_original_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'note.md'
            source.write_text('# Unknown\nBody\n', encoding='utf-8')
            plan = classifier.SourceMarkerPlan(str(source), 'Daily_Logs/note.md',
                '# Unknown\nBody\n', [(1, '1234567890abcdef')], 1, 0)
            with patch.object(classifier.shutil, 'copy2', side_effect=PermissionError('denied')):
                with self.assertRaisesRegex(RuntimeError, 'File: Daily_Logs/note.md; stage: back up original; PermissionError: denied'):
                    classifier.persist_source_marker_plans([plan], str(Path(directory) / 'backups'), directory)
            self.assertEqual(source.read_text(), '# Unknown\nBody\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths')
    def test_long_backup_and_rebuild_without_long_path_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'Daily_Logs/note.md'
            source.parent.mkdir()
            original = '# Unknown\nBody\n'
            source.write_bytes(original.encode('utf-8'))
            backup = Path(directory) / ('a' * 100) / ('b' * 100) / 'source_id_backups'
            plan = classifier.SourceMarkerPlan(str(source), 'Daily_Logs/note.md',
                original, [(1, '1234567890abcdef')], 1, 0)
            real_copy = classifier.shutil.copy2

            def legacy_windows_copy(src, dst, *args, **kwargs):
                # Simulate Windows rejecting ordinary paths beyond MAX_PATH.
                for path in (src, dst):
                    if len(str(path)) >= 260 and not str(path).startswith('\\\\?\\'):
                        raise OSError('MAX_PATH exceeded')
                return real_copy(src, dst, *args, **kwargs)

            with patch.object(classifier.shutil, 'copy2', side_effect=legacy_windows_copy):
                result = classifier.persist_source_marker_plans([plan], str(backup), directory)
                self.assertEqual(result[0], 1)
                saved = Path(result[4]) / 'Daily_Logs/note.md'
                self.assertGreater(len(str(saved)), 260)
                with open(filesystem_path(saved), encoding='utf-8') as stream:
                    self.assertEqual(stream.read(), original)
                changed, _ = classifier.rebuild_source_id_markers(str(source.parent), directory, str(backup))
                self.assertEqual(changed, 1)
                self.assertNotIn('research-notes-source-id', source.read_text())

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths')
    def test_unc_and_existing_extended_paths(self):
        self.assertEqual(filesystem_path(r'\\server\share\note.md'), r'\\?\UNC\server\share\note.md')
        self.assertEqual(filesystem_path(r'\\?\C:\note.md'), r'\\?\C:\note.md')


if __name__ == '__main__':
    unittest.main()
