import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(__file__)
sys.path.insert(0, ROOT)

import app


class TotalAttendanceDaysTests(unittest.TestCase):
    @patch("app.qone")
    def test_total_attendance_days_supports_filters(self, mock_qone):
        mock_qone.return_value = {"total_days": 7}

        result = app.total_attendance_days(
            MagicMock(),
            dept="BCA",
            faculty_id="2",
            semester="1",
        )

        self.assertEqual(result, 7)
        sql = mock_qone.call_args[0][1]
        self.assertIn("s.faculty_id", sql)
        self.assertIn("s.semester::text", sql)
        self.assertIn("s.department", sql)

    @patch("app.qone")
    def test_total_attendance_days_without_filters_uses_global_count(self, mock_qone):
        mock_qone.return_value = {"total_days": 12}

        result = app.total_attendance_days(MagicMock())

        self.assertEqual(result, 12)
        sql = mock_qone.call_args[0][1]
        self.assertNotIn("WHERE", sql)


if __name__ == "__main__":
    unittest.main()
