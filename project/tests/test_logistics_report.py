import unittest

from project.agents.logistics_report import build_report, render_html, render_markdown


class ReportRendererTests(unittest.TestCase):
    def test_scope_mismatch_and_unavailable_tool_are_rejected(self):
        response = {'status': 'error', 'data': None}
        with self.assertRaises(ValueError):
            build_report('run', response, response, response)
        response = {'status': 'ok', 'data': {}, 'run_id': 'other', 'snapshot_id': 's',
                    'fingerprint': 'f', 'business_as_of': 't', 'metric_version': 'v'}
        with self.assertRaises(ValueError):
            build_report('run', response, response, response)

    def test_render_escapes_source_markup(self):
        report = {'run_id': '<script>alert(1)</script>', 'scope': {'city': '[x](javascript:alert(1))',
                  'region_ids': ['5'], 'business_as_of': 'T', 'snapshot_id': 's', 'metric_version': 'v'},
                  'summary': {'metrics': {}, 'missing_window_count': 0}, 'focus_tasks': [],
                  'task_list': {'total_rows': 0, 'returned_rows': 0, 'truncated': False},
                  'summary_evidence_id': 'e', 'tasks_evidence_id': 'e', 'quality_evidence_id': 'e',
                  'quality': {'quality': {}}, 'limitations': ['<img src=x>'], 'follow_up': []}
        page = render_html(report)
        markdown = render_markdown(report)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img src=x>', page)
        self.assertNotIn('[x](javascript:', markdown)
        self.assertNotIn('<script>', markdown)
        self.assertEqual(render_markdown(report), markdown)


if __name__ == '__main__':
    unittest.main()
