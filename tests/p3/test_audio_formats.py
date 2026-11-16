import io
import os
import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import av

from callops.calls.audio import pcm_preview
from callops.common import Conflict, digest
from callops.processing.fake_worker import FakeWorker
from tests.support import ProductCase


def compressed_audio(container, codec, layout='mono', blocks=8):
    output = io.BytesIO()
    with av.open(output, mode='w', format=container) as target:
        stream = target.add_stream(codec, rate=48000)
        stream.layout = layout
        for _ in range(blocks):
            frame = av.AudioFrame(format='s16', layout=layout, samples=1024)
            frame.sample_rate = 48000
            frame.planes[0].update(bytes(frame.planes[0].buffer_size))
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    return output.getvalue()


class AudioFormatTests(ProductCase):
    def test_completed_recordings_open_in_web_and_run_only_after_approval(self):
        csrf = self.login()
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        samples = (
            ('pcm.wav', self.wav(), 'audio/wav'),
            ('aac.m4a', compressed_audio('ipod', 'aac'), 'audio/mp4'),
            ('alac.m4a', compressed_audio('ipod', 'alac'), 'audio/mp4'),
            ('mp3.mp3', compressed_audio('mp3', 'libmp3lame'), 'audio/mpeg'),
            ('nb.amr', b'#!AMR\n' + (b'\x04' + bytes(12)) * 10, 'audio/amr'),
            ('wb.amr', b'#!AMR-WB\n' + (b'\x04' + bytes(17)) * 10, 'audio/amr-wb'),
        )
        for filename, data, media_type in samples:
            with self.subTest(filename=filename):
                renamed = 'renamed-' + filename
                phone = Path(self.temp.name) / ('phone-original-' + filename)
                phone.write_bytes(data)
                partial = self.service.safe_path('sync-inbox', filename + '.part')
                partial.write_bytes(data[:len(data) // 2])
                with self.assertRaisesRegex(Conflict, 'TRANSFER_INCOMPLETE'):
                    self.service.ingest(partial.name, digest(data))
                self.assertFalse(any(row['sha256'] == digest(data) for row in self.service.list_recordings()))
                self.assertIsNone(worker.once())

                partial.write_bytes(data)
                partial.replace(self.service.safe_path('sync-inbox', filename))
                recording = self.service.ingest(filename, digest(data))
                self.recordings.append(recording)
                self.assertEqual(self.ingest(data, renamed), recording)
                self.assertEqual(self.row(recording)['classification_status'], 'PENDING')
                self.assertEqual(self.jobs(recording), [])
                path = '/recordings/' + recording
                self.assertIn(path, self.http('GET', '/').text)
                self.assertEqual(self.http('POST', path + '/metadata', data={
                    'csrf': csrf, 'counterparty': '가상 거래처', 'call_started_at': '2030-01-01T09:30',
                }).status_code, 302)
                self.assertIn(path, self.http('GET', '/', params={
                    'counterparty': '가상 거래처', 'from_date': '2030-01-01', 'to_date': '2030-01-01',
                }).text)
                audio = self.http('GET', path + '/audio')
                self.assertEqual(audio.status_code, 200)
                self.assertEqual(audio.headers['content-type'], media_type)
                self.assertEqual(audio.content, data)
                fallback = path + '/audio?play=pcm'
                has_fallback = media_type in ('audio/mp4', 'audio/amr', 'audio/amr-wb')
                if has_fallback:
                    self.assertEqual(self.http('GET', fallback).status_code, 410)

                for _ in range(2):
                    self.assertEqual(self.http('POST', path + '/classify', data={
                        'csrf': csrf, 'decision': 'BUSINESS_APPROVED',
                    }).status_code, 302)
                self.assertEqual([job['stage'] for job in self.jobs(recording)], ['STT'])
                if has_fallback:
                    audio = self.http('GET', fallback)
                    self.assertEqual(audio.status_code, 200)
                    self.assertEqual(audio.headers['content-type'], 'audio/wav')
                    with wave.open(io.BytesIO(audio.content), 'rb') as pcm:
                        self.assertAlmostEqual(pcm.getnframes() / pcm.getframerate(),
                                               self.row(recording)['duration_seconds'],
                                               delta=1 / pcm.getframerate())
                transcript = worker.once()
                self.assertIsNotNone(transcript)
                self.assertIn('synthetic test result', self.http('GET', path).text)
                self.assertIsNotNone(worker.once())
                if media_type == 'audio/amr':
                    def convert_then_exclude(data, kind):
                        converted = pcm_preview(data, kind)
                        self.service.classify(recording, 'PRIVATE_EXCLUDED')
                        return converted

                    with patch('callops.calls.routes.pcm_preview', side_effect=convert_then_exclude):
                        self.assertEqual(self.http('GET', fallback).status_code, 410)
                else:
                    self.service.classify(recording, 'PRIVATE_EXCLUDED')
                self.assertEqual(self.http('GET', path + '/audio').status_code, 410)
                if has_fallback:
                    self.assertEqual(self.http('GET', fallback).status_code, 410)
                self.assertFalse(self.service.audio_path(self.row(recording)).exists())
                self.assertFalse(self.service.safe_path('sync-inbox', filename).exists())
                self.assertFalse(self.service.safe_path('sync-inbox', renamed).exists())
                self.assertEqual(phone.read_bytes(), data)

    def test_compressed_format_rejections_do_not_create_work(self):
        m4a = compressed_audio('ipod', 'aac', blocks=12)
        mp3 = compressed_audio('mp3', 'libmp3lame')
        amr = b'#!AMR\n' + (b'\x04' + bytes(12)) * 10
        candidates = (
            ('disguised.mp3', m4a, 'INVALID_AUDIO'),
            ('truncated.m4a', m4a[:len(m4a) // 2], 'INVALID_AUDIO'),
            ('missing-tail.m4a', m4a[:-1], 'INVALID_AUDIO'),
            ('missing-tail.mp3', mp3[:-1], 'INVALID_AUDIO'),
            ('missing-tail.amr', amr[:-1], 'INVALID_AUDIO'),
            ('three-channels.m4a', compressed_audio('ipod', 'aac', '2.1'), 'INVALID_AUDIO'),
            ('unsupported-codec.m4a', compressed_audio('mp4', 'libmp3lame'), 'UNSUPPORTED_AUDIO_FORMAT'),
            ('wrong-extension.bin', m4a, 'UNSUPPORTED_AUDIO_FORMAT'),
        )
        for name, data, error in candidates:
            with self.subTest(name=name), self.assertRaisesRegex(Conflict, error):
                self.ingest(data, name)
        with patch.object(self.service, 'MAX_AUDIO', len(m4a) - 1), self.assertRaises(Conflict):
            self.ingest(m4a, 'oversize.m4a')
        with patch('callops.calls.audio.MAX_DURATION', 0.1), self.assertRaisesRegex(Conflict, 'INVALID_AUDIO'):
            self.ingest(m4a, 'overlong.m4a')
        with self.assertRaisesRegex(Conflict, 'TRANSFER_INCOMPLETE'):
            self.service.ingest('oversize.m4a', digest(m4a[:-1]))
        self.assertFalse(list(self.service.safe_path('quarantine').iterdir()))
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        self.assertIsNone(worker.once())

        command = [sys.executable, str(Path(__file__).resolve().parents[2] / 'scripts/manage.py'),
                   'ingest', '--name', 'oversize.m4a', '--sha256']
        env = {**os.environ, 'CALLOPS_DATABASE_URL': self.settings.database_url,
               'CALLOPS_DATA_ROOT': self.temp.name}
        mismatch = subprocess.run([*command, digest(m4a[:-1])], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual((mismatch.returncode, mismatch.stderr.strip()), (1, 'FAIL: TRANSFER_INCOMPLETE'))
        accepted = subprocess.run([*command, digest(m4a)], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        recording = accepted.stdout.removeprefix('Accepted recording: ').strip()
        self.recordings.append(recording)
        self.assertEqual(self.row(recording)['classification_status'], 'PENDING')
        self.assertEqual(self.jobs(recording), [])
