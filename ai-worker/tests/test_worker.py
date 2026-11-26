from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import worker, worker_engine


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.copies = worker.Copies(self.root / 'worker')
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.data = b'synthetic bytes'
        self.job = {
            'stage': 'STT', 'job_id': str(uuid4()), 'copy_key': str(uuid4()), 'lease_token': str(uuid4()),
            'input_artifact_id': None, 'input_artifact_version': 1, 'input_sha256': hashlib.sha256(self.data).hexdigest(),
            'lease_until': self.after(300), 'preparation_deadline': self.after(600),
        }

    def after(self, seconds):
        return (self.now + timedelta(seconds=seconds)).isoformat()

    def reply(self, **values):
        return worker.Reply(values, self.now, 0)

    def rag_job(self, stage='RAG_EMBED'):
        job = dict(self.job, stage=stage, schema_version=1,
                   input_snapshot_id=str(uuid4()), input_snapshot_version=1)
        del job['input_artifact_id'], job['input_artifact_version']
        return job

    def test_claim_advertises_only_a_supported_integer_version(self):
        for version in (None, True, '1', 1.0, 2, 1):
            with self.subTest(version=version, kind=type(version)):
                api = Mock()
                ready = dict(real_results=True)
                if version is not None:
                    ready['rag_contract_version'] = version
                api.json.return_value = self.reply(**ready)
                api.post.side_effect = [worker.Reply([], self.now, 0), worker.Reply(None, self.now, 0)]
                self.assertIsNone(worker.Worker(api, self.copies, Mock()).once())
                if type(version) is int and version == 1:
                    api.post.assert_called_with('claim', json={'rag_contract_version': 1})
                else:
                    api.post.assert_called_with('claim')

    def test_unnegotiated_rag_claim_never_downloads_input(self):
        api = Mock()
        api.json.return_value = self.reply(real_results=True)
        api.post.side_effect = [worker.Reply([], self.now, 0), worker.Reply(self.rag_job(), self.now, 0)]
        model = Mock()
        with self.assertRaisesRegex(worker.WorkerError, 'RAG_NOT_NEGOTIATED'):
            worker.Worker(api, self.copies, model).once()
        api.download.assert_not_called()
        model.assert_not_called()
        self.assertEqual(list(self.copies.work.iterdir()), [])

    def test_invalid_rag_claim_binding_stops_before_download(self):
        for fields in ({'schema_version': True}, {'input_snapshot_id': '../input'},
                       {'input_snapshot_version': 0}, {'input_snapshot_version': True},
                       {'input_sha256': 'F' * 64}):
            with self.subTest(fields=fields):
                api = Mock()
                job = self.rag_job() | fields
                with self.assertRaises(ValueError):
                    worker.Worker(api, self.copies, Mock()).process(worker.Reply(job, self.now, 0))
                api.download.assert_not_called()
                self.assertEqual(list(self.copies.work.iterdir()), [])

    def test_rag_snapshot_download_rejects_wrong_headers_hash_and_size(self):
        job = self.rag_job()
        target = self.copies.work_dir(job['copy_key']) / 'input'
        good = {'X-Input-Snapshot-ID': job['input_snapshot_id'], 'X-Input-Snapshot-Version': '1'}
        oversized = b'x' * (80 * 1024 + 1)
        for headers, data, sha in (
            (good | {'X-Input-Snapshot-ID': str(uuid4())}, self.data, job['input_sha256']),
            (good | {'X-Input-Snapshot-Version': '2'}, self.data, job['input_sha256']),
            (good, b'incorrect', job['input_sha256']),
            (good, oversized, hashlib.sha256(oversized).hexdigest()),
        ):
            response = Mock(status_code=200, headers=headers)
            response.iter_bytes.return_value = [data]

            @contextmanager
            def stream(method, url, **kwargs):
                yield response

            with self.subTest(headers=headers, size=len(data)), self.assertRaisesRegex(worker.WorkerError, 'INPUT_CORRUPT'):
                api = worker.WorkerAPI(stream, 'synthetic-token', transport_errors=(OSError,))
                api.download(job | {'input_sha256': sha}, target, lambda: None)
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_rag_restart_replays_same_envelope_until_result_id_is_acknowledged(self):
        job = self.rag_job('RAG_ANSWER')
        api = Mock()
        api.monotonic.return_value = 0
        api.json.return_value = self.reply(real_results=True, rag_contract_version=1)
        api.post.side_effect = [
            worker.Reply([], self.now, 0), worker.Reply(job, self.now, 0),
            self.reply(execution_deadline=self.after(120)), self.reply(upload_deadline=self.after(120)),
            worker.Offline('SERVER_UNAVAILABLE'),
        ]
        output = {'schema_version': 1, 'answer': '가상 답변', 'reason': None, 'segment_ids': ['C1:S1']}
        model = Mock(return_value=output)
        with patch.object(worker.Lease, 'heartbeat'), self.assertRaises(worker.Offline):
            worker.Worker(api, self.copies, model).once()
        model.assert_called_once()
        _, report, original = next(self.copies.pending())
        self.assertEqual(json.loads(original), {
            'schema_version': 1, 'job_id': job['job_id'], 'attempt_id': job['copy_key'],
            'input_snapshot_id': job['input_snapshot_id'], 'input_snapshot_version': 1,
            'input_sha256': job['input_sha256'], 'output': output,
        })
        late_api = Mock()
        late_api.monotonic.return_value = 1
        late_api.post.side_effect = [
            self.reply(accepted=False, status='UPLOADING', upload_deadline=self.after(120), lease_until=self.after(300)),
            self.reply(accepted=True, artifact_id=str(uuid4())),
        ]
        model.reset_mock()
        with patch.object(worker.Lease, 'heartbeat'), self.assertRaisesRegex(worker.WorkerError, 'INVALID_ACK'):
            worker.Worker(late_api, self.copies, model).once()
        late_api.post.assert_any_call('status', job, json={'request_id': report['request_id']})
        late_api.post.assert_called_with('result', job, report=report, content=original)
        self.assertEqual(next(self.copies.pending())[2], original)

        ack = {'accepted': True, 'result_id': str(uuid4())}
        late_api.reset_mock()
        late_api.post.side_effect = [self.reply(**ack), worker.Reply([], self.now, 0)]
        self.assertEqual(worker.Worker(late_api, self.copies, model).once(), ack)
        self.assertEqual([call.args[0] for call in late_api.post.call_args_list], ['status', 'cleanup'])
        model.assert_not_called()
        self.assertEqual(list(self.copies.work.iterdir()), [])
        self.assertEqual(list(self.copies.spool.iterdir()), [])

    def test_rag_result_over_one_mib_is_not_prepared(self):
        job = self.rag_job('RAG_ANSWER')
        api = Mock()
        api.monotonic.return_value = 0
        api.post.return_value = self.reply(execution_deadline=self.after(120))
        model = Mock(return_value={'schema_version': 1, 'answer': 'x' * (1024 * 1024)})
        with patch.object(worker.Lease, 'heartbeat'), self.assertRaisesRegex(worker.WorkerError, 'MODEL_ERROR'):
            worker.Worker(api, self.copies, model).process(worker.Reply(job, self.now, 0))
        self.assertEqual([call.args[0] for call in api.post.call_args_list], ['start'])
        self.assertEqual(list(self.copies.spool.iterdir()), [])

    def test_rag_context_exit_reports_terminal_failure_and_cleans(self):
        job = self.rag_job('RAG_ANSWER')
        api = Mock()
        api.monotonic.return_value = 0
        api.json.return_value = self.reply(real_results=True, rag_contract_version=1)
        item = {'copy_key': job['copy_key'], 'version': 1}
        api.post.side_effect = [
            worker.Reply([], self.now, 0), worker.Reply(job, self.now, 0),
            self.reply(execution_deadline=self.after(120)), self.reply(status='STOP_AND_CLEAN'),
            worker.Reply([item], self.now, 0), worker.Reply([], self.now, 0),
        ]
        engine = worker_engine.StageProcess(self.copies.engine_guard, None, self.root)
        process = Mock(returncode=worker_engine.RAG_CONTEXT_EXIT)
        process.poll.return_value = process.returncode
        with (patch.object(worker.Lease, 'heartbeat'),
              patch.object(worker_engine.subprocess, 'Popen', return_value=process),
              patch.object(worker_engine, 'stop_group'),
              self.assertRaisesRegex(worker.WorkerError, 'RAG_CONTEXT_TOO_LONG')):
            worker.Worker(api, self.copies, engine.run).once()
        api.post.assert_any_call('failed', job, json={'code': 'RAG_CONTEXT_TOO_LONG'})
        api.post.assert_called_with('cleanup', json={**item, 'stopped': True, 'removed': True})
        self.assertEqual(list(self.copies.work.iterdir()), [])
        self.assertEqual(list(self.copies.spool.iterdir()), [])

    def test_restart_replays_prepared_bytes_or_recovers_a_lost_ack_without_a_model_call(self):
        for already_accepted in (True, False):
            with self.subTest(already_accepted=already_accepted):
                api = Mock()
                api.monotonic.return_value = 0
                api.json.return_value = self.reply(real_results=True)
                api.post.side_effect = [
                    worker.Reply([], self.now, 0), worker.Reply(self.job, self.now, 0),
                    self.reply(execution_deadline=self.after(3600)), self.reply(upload_deadline=self.after(1800)),
                    worker.Offline('SERVER_UNAVAILABLE'),
                ]
                run_model = Mock(return_value={'segments': []})
                with patch.object(worker.Lease, 'heartbeat'):
                    with self.assertRaises(worker.Offline):
                        worker.Worker(api, self.copies, run_model).once()
                run_model.assert_called_once()
                job, report, data = next(self.copies.pending())
                self.assertEqual(data, api.post.call_args.kwargs['content'])
                self.assertEqual(report, api.post.call_args.kwargs['report'])
                self.assertEqual(os.stat(next(self.copies.spool.iterdir())).st_mode & 0o777, 0o600)

                replacement = Mock()
                replacement.monotonic.return_value = 10
                ack = {'accepted': True, 'artifact_id': str(uuid4())}
                cleanup = [{'copy_key': job['copy_key'], 'version': 1}]
                tail = [worker.Reply(cleanup, self.now, 10), worker.Reply([], self.now, 10)]
                if already_accepted:
                    replacement.post.side_effect = [self.reply(**ack), *tail]
                else:
                    replacement.post.side_effect = [
                        self.reply(accepted=False, status='UPLOADING', lease_until=self.after(290),
                                   upload_deadline=self.after(1700)),
                        self.reply(**ack), *tail,
                    ]
                run_model.reset_mock()
                with patch.object(worker.Lease, 'heartbeat'):
                    self.assertEqual(worker.Worker(replacement, self.copies, run_model).once(), ack)
                run_model.assert_not_called()
                results = [call for call in replacement.post.call_args_list if call.args[0] == 'result']
                self.assertEqual(len(results), int(not already_accepted))
                if results:
                    self.assertEqual(results[0].kwargs, {'report': report, 'content': data})
                self.assertEqual(list(self.copies.spool.iterdir()), [])
                self.assertEqual(list(self.copies.work.iterdir()), [])

    def test_download_rejects_wrong_binding_and_hash_before_publishing(self):
        target = self.copies.work_dir(self.job['copy_key']) / 'input'
        for version, content in (('2', self.data), ('1', b'wrong bytes')):
            response = Mock(status_code=200, headers={'X-Input-Artifact-ID': '', 'X-Input-Artifact-Version': version})
            response.iter_bytes.return_value = [content]

            @contextmanager
            def stream(method, url, **kwargs):
                self.assertEqual(kwargs['headers']['X-Lease-Token'], self.job['lease_token'])
                yield response

            with self.subTest(version=version), self.assertRaisesRegex(worker.WorkerError, 'INPUT_CORRUPT'):
                api = worker.WorkerAPI(stream, 'synthetic-token', transport_errors=(OSError,))
                api.download(self.job, target, lambda: None)
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_resuming_analysis_with_missing_input_reports_failure_and_cleans(self):
        job = dict(self.job, stage='ANALYSIS', input_artifact_id=str(uuid4()))
        self.copies.work_dir(job['copy_key'])
        data = b'{"synthetic":false}'
        report = {
            'request_id': str(uuid4()), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'spool_created_at': self.now.isoformat(),
        }
        self.copies.save(job['copy_key'], job, report, data)
        api = Mock()
        api.monotonic.return_value = 0
        api.json.return_value = self.reply(real_results=True)
        api.post.side_effect = [
            self.reply(accepted=False, status='UPLOADING', lease_until=self.after(300), upload_deadline=self.after(600)),
            worker.Rejected('INPUT_MISSING'), self.reply(status='STOP_AND_CLEAN'),
            worker.Reply([{'copy_key': job['copy_key'], 'version': 1}], self.now, 0),
            worker.Reply([], self.now, 0), worker.Reply([], self.now, 0), worker.Reply(None, self.now, 0),
        ]
        run_model = Mock()
        with patch.object(worker.Lease, 'heartbeat'):
            self.assertIsNone(worker.Worker(api, self.copies, run_model).once())
        api.post.assert_any_call('failed', job, json={'code': 'INPUT_MISSING'})
        run_model.assert_not_called()
        self.assertEqual(list(self.copies.spool.iterdir()), [])
        self.assertEqual(list(self.copies.work.iterdir()), [])

    def test_expired_copies_are_purged_even_without_a_server_connection(self):
        directory = self.copies.work_dir(self.job['copy_key'])
        (directory / 'input').write_bytes(self.data)
        shadow = self.copies.spool / (self.job['copy_key'] + '.part')
        shadow.write_bytes(self.data)
        for path in (shadow, directory):
            os.utime(path, (0, 0))
        api = Mock()
        api.post.side_effect = worker.Offline('SERVER_UNAVAILABLE')
        with self.assertRaises(worker.Offline):
            worker.Worker(api, self.copies, Mock(), wall_time=lambda: 24 * 60 * 60 + 1).once()
        self.assertEqual(list(self.copies.work.iterdir()), [])
        self.assertEqual(list(self.copies.spool.iterdir()), [])

    def test_engine_timeout_keeps_guard_until_exit_is_confirmed(self):
        directory = self.copies.work_dir(self.job['copy_key'])
        engine = worker_engine.StageProcess(self.copies.engine_guard, self.root, self.root)
        process = Mock()
        process.poll.return_value = None
        check = Mock(side_effect=worker.WorkerError('STAGE_TIMEOUT'))
        for confirmed in (True, False):
            stop_error = None if confirmed else worker.WorkerError('ENGINE_STOP_UNCONFIRMED')
            with (self.subTest(confirmed=confirmed),
                  patch.object(worker_engine.subprocess, 'Popen', return_value=process) as launch,
                  patch.object(worker_engine, 'stop_group', side_effect=stop_error) as stop):
                with self.assertRaisesRegex(worker.WorkerError, str(stop_error) if stop_error else 'STAGE_TIMEOUT'):
                    engine.run(self.job, directory, check)
                self.assertTrue(launch.call_args.kwargs['start_new_session'])
                self.assertNotIn('CALLOPS_WORKER_TOKEN', launch.call_args.kwargs['env'])
                stop.assert_called_once_with(process)
                self.assertEqual(self.copies.engine_guard.exists(), not confirmed)
        api = Mock()
        with self.assertRaisesRegex(worker.WorkerError, 'ENGINE_STOP_UNCONFIRMED'):
            worker.Worker(api, self.copies, Mock()).once()
        self.assertEqual(api.mock_calls, [])
        self.assertTrue(directory.exists())

    def test_hard_stop_targets_only_the_started_group(self):
        process = Mock(pid=1234)
        with (patch.object(worker_engine.os, 'killpg') as kill,
              patch.object(worker_engine, 'live_group', return_value=False),
              patch.object(worker_engine.time, 'monotonic', return_value=0)):
            worker_engine.stop_group(process, grace=0)
        self.assertEqual([item.args for item in kill.call_args_list],
                         [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])

    def test_heartbeat_extension_cannot_extend_the_execution_deadline(self):
        api = Mock()
        api.monotonic.return_value = 400
        lease = worker.Lease(api, self.job, worker.Reply(self.job, self.now, 0), 30)
        lease.until = 700
        with self.assertRaisesRegex(worker.WorkerError, 'STAGE_TIMEOUT'):
            lease.check(400, 'STAGE_TIMEOUT')
