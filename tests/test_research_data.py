import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import research_data

class ResearchDataTests(unittest.TestCase):
    def test_context_and_bars_on_empty_database_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'test.db')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(research_data.main(['--db', db, 'context', '--symbol', '002241']), 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value['stock']['15']['status'], 'insufficient')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                research_data.main(['--db', db, 'bars', '--symbol', '002241', '--timeframe', '15'])
            self.assertEqual(json.loads(output.getvalue())['count'], 0)

    def test_collect_reports_success_and_propagates_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['--db', str(Path(tmp) / 'test.db'), 'collect', '--kind', 'stock', '--symbol', '002241', '--timeframe', '15']
            with patch.object(research_data.data_collector, 'collect', return_value=7) as collect, contextlib.redirect_stdout(io.StringIO()) as output:
                research_data.main(args)
                self.assertEqual(json.loads(output.getvalue())['count'], 7)
                collect.assert_called_once()
            with patch.object(research_data.data_collector, 'collect', side_effect=OSError('unavailable')):
                with self.assertRaises(OSError): research_data.main(args)

    def test_rejects_unsupported_daily_board(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            research_data.main(['collect', '--kind', 'board', '--symbol', 'x', '--board-type', 'industry', '--timeframe', 'daily'])
