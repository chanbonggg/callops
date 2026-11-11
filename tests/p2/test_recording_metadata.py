from datetime import datetime, timezone

from tests.support import ProductCase


class RecordingMetadataTests(ProductCase):
    def save(self, recording, csrf, *, counterparty='가상 거래처 A', started='2029-12-31T23:30'):
        return self.http('POST', '/recordings/' + recording + '/metadata',
                         data={'csrf': csrf, 'counterparty': counterparty,
                               'call_started_at': started})

    def test_metadata_requires_owner_session_csrf_and_does_not_approve_ai(self):
        recording = self.ingest()
        self.assertEqual(self.save(recording, '').status_code, 401)
        csrf = self.login()
        self.assertEqual(self.save(recording, 'wrong').status_code, 403)
        self.assertEqual(self.save(recording, csrf).status_code, 302)
        row = self.row(recording)
        self.assertEqual(row['counterparty'], '가상 거래처 A')
        self.assertEqual(row['call_started_at'], datetime(2029, 12, 31, 14, 30, tzinfo=timezone.utc))
        self.assertEqual(row['call_time_source'], 'USER_CONFIRMED')
        self.assertTrue(row['call_time_trusted'])
        self.assertEqual(row['classification_status'], 'PENDING')
        self.assertEqual(self.jobs(recording), [])
        self.assertIn('2029-12-31T23:30', self.http('GET', '/').text)

    def test_empty_metadata_and_invalid_values_preserve_existing_information(self):
        recording = self.ingest()
        csrf = self.login()
        self.assertEqual(self.save(recording, csrf).status_code, 302)
        for counterparty, started in (('x' * 101, '2029-12-31T23:30'),
                                      ('가상\n거래처', '2029-12-31T23:30'),
                                      ('가상', '2029-02-30T12:00'),
                                      ('가상', '2029-12-31T23:30+09:00'),
                                      ('가상', '2029-12-31'),
                                      ('가상', '0001-01-01T00:00')):
            with self.subTest(kind='invalid metadata'):
                self.assertEqual(self.save(recording, csrf, counterparty=counterparty,
                                           started=started).status_code, 409)
                self.assertEqual(self.row(recording)['counterparty'], '가상 거래처 A')
        self.assertEqual(self.save(recording, csrf, counterparty='  ', started='').status_code, 302)
        row = self.row(recording)
        self.assertIsNone(row['counterparty'])
        self.assertIsNone(row['call_started_at'])
        self.assertIsNone(row['call_time_source'])
        self.assertFalse(row['call_time_trusted'])

    def test_counterparty_and_date_filters_use_actual_call_time_and_korean_day_boundaries(self):
        recordings = [self.ingest() for _ in range(4)]
        csrf = self.login()
        for recording, counterparty, started in (
                (recordings[0], '가상 %_거래처', '2029-12-31T00:00'),
                (recordings[1], '가상 %_거래처', '2029-12-31T23:59:59'),
                (recordings[2], '가상 %_거래처', '2030-01-01T00:00')):
            self.assertEqual(self.save(recording, csrf, counterparty=counterparty,
                                       started=started).status_code, 302)
        result = self.http('GET', '/', params={'counterparty': '%_', 'from_date': '2029-12-31',
                                                    'to_date': '2029-12-31'})
        self.assertEqual(result.status_code, 200)
        for recording in recordings[:2]:
            self.assertIn(recording, result.text)
        for recording in recordings[2:]:
            self.assertNotIn(recording, result.text)
        result = self.http('GET', '/', params={'counterparty': "' OR 1=1 --"})
        self.assertEqual(result.status_code, 200)
        for recording in recordings:
            self.assertNotIn(recording, result.text)
        for filters in ({'from_date': '2030-02-30'}, {'to_date': 'bad-date'},
                        {'from_date': '0001-01-01'}, {'to_date': '9999-12-31'},
                        {'from_date': '2030-01-02', 'to_date': '2030-01-01'}):
            self.assertEqual(self.http('GET', '/', params=filters).status_code, 409)
        self.assertIn(recordings[3], self.http('GET', '/').text)

    def test_approval_locks_metadata_and_private_or_expired_copies_lose_metadata(self):
        approved, private, expired = [self.ingest() for _ in range(3)]
        csrf = self.login()
        for recording in (approved, private, expired):
            self.assertEqual(self.save(recording, csrf).status_code, 302)
        self.service.classify(approved, 'BUSINESS_APPROVED')
        self.assertEqual(self.save(approved, csrf, counterparty='다른 거래처').status_code, 409)
        self.assertEqual(self.row(approved)['counterparty'], '가상 거래처 A')
        self.service.classify(private, 'PRIVATE_EXCLUDED')
        self.assertIsNone(self.row(private)['counterparty'])
        self.assertIsNone(self.row(private)['call_started_at'])
        self.assertEqual(self.save(private, csrf).status_code, 409)
        self.advance(hours=72)
        csrf = self.login()
        # GET hides expired metadata without changing its stored lifecycle state.
        result = self.http('GET', '/', params={'counterparty': '가상 거래처 A'})
        self.assertIn(approved, result.text)
        self.assertNotIn(private, result.text)
        self.assertNotIn(expired, result.text)
        self.assertEqual(self.row(expired)['classification_status'], 'PENDING')
        self.assertEqual(self.save(expired, csrf).status_code, 409)
        self.service.maintain()
        self.assertIsNone(self.row(expired)['counterparty'])
        self.assertIsNone(self.row(expired)['call_started_at'])

    def test_names_are_escaped_and_worker_tokens_cannot_edit_metadata(self):
        recording = self.ingest()
        path = '/recordings/' + recording + '/metadata'
        self.assertEqual(self.http('POST', path, headers={'Authorization': 'Bearer ' + self.tokens['one']},
                                   data={'counterparty': '가상', 'call_started_at': ''}).status_code, 401)
        csrf = self.login()
        self.assertEqual(self.save(recording, csrf, counterparty='<script>alert(1)</script>',
                                   started='').status_code, 302)
        result = self.http('GET', '/')
        self.assertNotIn('<script>alert(1)</script>', result.text)
        self.assertIn('&lt;script&gt;', result.text)
        self.assertEqual(self.http('GET', path).status_code, 405)
