"""Validate the audio formats accepted by the local transfer boundary."""

import io
import wave

import av

from ..common import Conflict


MAX_DURATION = 7200
FORMATS = {
    'audio/wav': '.wav',
    'audio/mp4': '.m4a',
    'audio/mpeg': '.mp3',
    'audio/amr': '.amr',
    'audio/amr-wb': '.amr',
}
COMPRESSED = {
    'audio/mp4': ('mov', {'aac', 'alac'}),
    'audio/mpeg': ('mp3', {'mp3', 'mp3float'}),
    'audio/amr': ('amr', {'amr_nb', 'amrnb'}),
    'audio/amr-wb': ('amr', {'amr_wb', 'amrwb'}),
}
PCM_PREVIEW_TYPES = {'audio/mp4', 'audio/amr', 'audio/amr-wb'}
MP3_BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}


def media_type(header):
    if header[:4] == b'RIFF' and header[8:12] == b'WAVE':
        return 'audio/wav'
    if header[4:8] == b'ftyp':
        return 'audio/mp4'
    if header.startswith(b'#!AMR-WB\n'):
        return 'audio/amr-wb'
    if header.startswith(b'#!AMR\n'):
        return 'audio/amr'
    if header.startswith(b'ID3') or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0):
        return 'audio/mpeg'
    raise Conflict('INVALID_AUDIO')


def pcm_duration(source):
    with wave.open(source, 'rb') as audio:
        frames = audio.getnframes()
        duration = frames / audio.getframerate()
        if not 0 < duration <= MAX_DURATION or audio.getnchannels() not in (1, 2):
            raise Conflict('INVALID_AUDIO')
        remaining = frames * audio.getnchannels() * audio.getsampwidth()
        while chunk := audio.readframes(65536):
            remaining -= len(chunk)
        if remaining:
            raise Conflict('INVALID_AUDIO')
        return duration


def check_mp4_boxes(source):
    file_size = source.seek(0, 2)
    source.seek(0)
    boxes = set()
    while source.tell() < file_size:
        offset = source.tell()
        header = source.read(8)
        if len(header) != 8:
            raise Conflict('INVALID_AUDIO')
        size = int.from_bytes(header[:4], 'big')
        if size == 1:
            size = int.from_bytes(source.read(8), 'big')
        elif size == 0:
            size = file_size - offset
        if size < source.tell() - offset or offset + size > file_size:
            raise Conflict('INVALID_AUDIO')
        if header[4:] in (b'moov', b'mdat'):
            boxes.add(header[4:])
        source.seek(offset + size)
    if boxes != {b'moov', b'mdat'}:
        raise Conflict('INVALID_AUDIO')
    source.seek(0)


def check_mp3_frame(packet):
    # The decoder can output a frame even if its final packet is cut short.
    header = bytes(packet)[:4]
    if len(header) < 4 or header[0] != 0xff or header[1] & 0xe6 != 0xe2:
        raise Conflict('INVALID_AUDIO')
    version = (header[1] >> 3) & 3
    rate_index = (header[2] >> 2) & 3
    if version == 1 or rate_index == 3:
        raise Conflict('INVALID_AUDIO')
    bitrate = MP3_BITRATES[3 if version == 3 else 2][header[2] >> 4]
    if not bitrate:
        raise Conflict('UNSUPPORTED_AUDIO_FORMAT')
    rate = (44100, 48000, 32000)[rate_index] // {3: 1, 2: 2, 0: 4}[version]
    multiplier = 144000 if version == 3 else 72000
    if packet.size != multiplier * bitrate // rate + ((header[2] >> 1) & 1):
        raise Conflict('INVALID_AUDIO')


def compressed_duration(source, kind, write_frame=None):
    demuxer, codecs = COMPRESSED[kind]
    options = {'protocol_whitelist': 'file'}
    if demuxer == 'mov':
        check_mp4_boxes(source)
        options['enable_drefs'] = '0'
    with av.open(source, format=demuxer, options=options, timeout=30) as container:
        if len(container.streams) != 1 or not container.streams.audio:
            raise Conflict('UNSUPPORTED_AUDIO_FORMAT')
        stream = container.streams.audio[0]
        codec = stream.codec_context
        if codec is None:
            raise Conflict('INVALID_AUDIO')
        if codec.name not in codecs:
            raise Conflict('UNSUPPORTED_AUDIO_FORMAT')
        codec.options = {'err_detect': 'explode', 'threads': '1'}
        samples = 0
        sample_rate = codec.sample_rate
        if sample_rate <= 0 or codec.channels not in (1, 2):
            raise Conflict('INVALID_AUDIO')
        for packet in container.demux(stream):
            if packet.is_corrupt:
                raise Conflict('INVALID_AUDIO')
            if demuxer == 'mp3' and packet.size:
                check_mp3_frame(packet)
            for frame in packet.decode():
                if frame.sample_rate != sample_rate or len(frame.layout.channels) != codec.channels:
                    raise Conflict('INVALID_AUDIO')
                samples += frame.samples
                if samples > MAX_DURATION * sample_rate:
                    raise Conflict('INVALID_AUDIO')
                if write_frame is not None:
                    write_frame(frame)
        if not samples:
            raise Conflict('INVALID_AUDIO')
        return samples / sample_rate


def audio_duration(source, suffix=None):
    if suffix is not None and suffix not in FORMATS.values():
        raise Conflict('UNSUPPORTED_AUDIO_FORMAT')
    kind = media_type(source.read(12))
    source.seek(0)
    if suffix is not None and FORMATS[kind] != suffix:
        raise Conflict('INVALID_AUDIO')
    try:
        if kind == 'audio/wav':
            return pcm_duration(source)
        return compressed_duration(source, kind)
    except (wave.Error, EOFError, ZeroDivisionError, av.error.FFmpegError):
        raise Conflict('INVALID_AUDIO') from None


def pcm_preview(data, kind):
    output = io.BytesIO()
    resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
    try:
        with wave.open(output, 'wb') as preview:
            preview.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))

            def write_frame(frame):
                for converted in resampler.resample(frame):
                    preview.writeframesraw(bytes(converted.planes[0])[:converted.samples * 2])

            compressed_duration(io.BytesIO(data), kind, write_frame)
            write_frame(None)
    except (ValueError, av.error.FFmpegError):
        raise Conflict('INVALID_AUDIO') from None
    return output.getvalue()


def pcm_clip(data, kind, start, end):
    if kind != 'audio/wav':
        # shortcut: decode the original in memory, optimize to segment decoding if long calls make this slow.
        data = pcm_preview(data, kind)
    output = io.BytesIO()
    with wave.open(io.BytesIO(data), 'rb') as source, wave.open(output, 'wb') as clip:
        rate = source.getframerate()
        first = round(start * rate)
        last = min(source.getnframes(), round(end * rate))
        if first >= last:
            raise Conflict('AUDIO_SEGMENT_UNAVAILABLE')
        source.setpos(first)
        clip.setparams(source.getparams())
        clip.writeframes(source.readframes(last - first))
    return output.getvalue()
