import asyncio
import json
import os
import time
from io import BufferedWriter, BufferedReader
from struct import pack, unpack
from threading import Lock
from typing import List, NamedTuple, Tuple

from aiortc import RTCRtpCodecParameters
from av.audio.frame import AudioFrame
from av.frame import Frame

from src.aiortc import MediaStreamTrack
from src.aiortc.codecs import Encoder


class Sample(NamedTuple):
    payloads: List[bytes]
    timestamp: int


samples_file: dict = {}
samples_file_lock = Lock()


class SamplesFile:
    __writer: BufferedWriter = None
    __reader: BufferedReader = None
    __serialized_header: bool = False
    __samples: List[ Sample ] = []
    clock_rate: int = 0
    __file_size: int = 0

    kind: str = ""

    @staticmethod
    def get_samples_reader(filename: str):
        with samples_file_lock:
            reader = samples_file.get(filename, None)
            if not reader:
                reader = SamplesFile()
                reader.open_read( filename)
                samples_file[filename] = reader

            return reader

    def __init__(self):
        return

    def open_write(self, filename: str):
        self.__writer = open(filename, 'wb')
        return

    def open_read(self, filename: str):
        self.__reader = open(filename, 'rb')
        file_size = os.fstat(self.__reader.fileno()).st_size
        header_len = unpack('H', self.__reader.read(2))[0]
        header_bytes = self.__reader.read(header_len)
        header = json.loads(header_bytes.decode())

        while file_size > self.__reader.tell():
            self._read_sample()

        self.clock_rate = header['clockRate']
        self.kind = "video" if header['mimeType'].startswith("video") else "audio"

    @property
    def samples_count(self):
        return len(self.__samples)

    def get_sample(self, sample_index: int) -> Sample:
        return self.__samples[sample_index]

    def _read_sample(self):
        timestamp = unpack('I', self.__reader.read(4))[0]
        payloads_count = unpack('H', self.__reader.read(2))[0]

        payloads_bytes = []
        for i in range(payloads_count):
            payload_len = unpack('I', self.__reader.read(4))[0]
            payload = self.__reader.read(payload_len)
            payloads_bytes.append(payload)

        self.__samples.append( Sample(payloads_bytes, timestamp) )

    def __del__(self):
        self.close()

    def write(self, codec: RTCRtpCodecParameters, payloads: List[bytes], timestamp: int):
        if not self.__serialized_header:
            ser = {
                "channels": codec.channels,
                "clockRate": codec.clockRate,
                "mimeType": codec.mimeType,
                "name": codec.name,
                "payloadType": codec.payloadType
            }
            ser = json.dumps(ser).encode()
            self.__writer.write(pack('H', len(ser)))
            self.__writer.write(ser)
            self.__serialized_header = True

        self.__writer.write(pack('I', timestamp))
        self.__writer.write(pack('H', len(payloads)))
        for payload in payloads:
            self.__writer.write(pack('I', len(payload)))
            self.__writer.write(payload)

    def close(self):
        if self.__writer:
            self.__writer.close()
            self.__writer = None
        if self.__reader:
            self.__reader.close()
            self.__reader = None


class PassthroughFrame(Frame):
    payloads: List[bytes] = []

class PassthroughMediaStreamTrack(MediaStreamTrack):
    _start: float = None
    reader: SamplesFile = None
    loop_count: int = -1

    def __init__(self, file_name):
        super().__init__()
        self.reader = SamplesFile.get_samples_reader(file_name)
        self.kind = self.reader.kind
        self.sample_index = 0
        self.timestamp_offset = 0

    #dummy implmentation - not used
    async def recv(self) -> Frame:
        frame = PassthroughFrame()

        if not self._start:
            self._start = time.time()

        sample = self.reader.get_sample(self.sample_index)
        timestamp = self.timestamp_offset + sample.timestamp

        time_passed = time.time()-self._start
        sample_time = float(timestamp) / self.reader.clock_rate

        diff = sample_time - time_passed
        if diff > 0:
            await asyncio.sleep(diff)

        self.sample_index += 1
        if self.sample_index == self.reader.samples_count or self.sample_index==self.loop_count:
            self.sample_index = 0
            self.timestamp_offset = timestamp

        frame.pts = timestamp
        frame.payloads = sample.payloads

        return frame


class PassthroughEncoder(Encoder):

    def __init__(self) -> None:
        return

    def __del__(self) -> None:
        return

    def encode(
            self, frame: Frame, force_keyframe: bool = False
    ) -> Tuple[List[bytes], int]:
        return frame.payloads, frame.pts
