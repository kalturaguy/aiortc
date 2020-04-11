import argparse
import asyncio
import logging
import random
import string
import sys

import websockets as ws
import json

from src.aiortc import RTCPeerConnection, RTCSessionDescription
from src.aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack
from src.aiortc.contrib.media import MediaPlayer
from src.aiortc.samplesfile import PassthroughMediaStreamTrack

logger = logging.getLogger('echo')


class WebSocketClient():

    def __init__(self, url='ws://localhost:8188/'):
        self._url = url
        self.connection = None
        self._transactions = {}

    async def connect(self):
        self.connection = await ws.connect(self._url,
                                           subprotocols=['janus-protocol'],
                                           ping_interval=10,
                                           ping_timeout=10,
                                           compression=None)
        if self.connection.open:
            asyncio.ensure_future(self.receiveMessage())
            logger.info('WebSocket connected')
            return self

    def transaction_id(self):
        return ''.join(random.choice(string.ascii_letters) for x in range(12))

    async def send(self, message):
        tx_id = self.transaction_id()
        message.update({'transaction': tx_id})
        tx = asyncio.get_event_loop().create_future()
        tx_in = {'tx': tx, 'request': message['janus']}
        self._transactions[tx_id] = tx_in
        try:
            await asyncio.gather(self.connection.send(json.dumps(message)), tx)
        except Exception as e:
            tx.set_result(e)
        return tx.result()

    async def receiveMessage(self):
        try:
            async for message in self.connection:
                data = json.loads(message)
                tx_id = data.get('transaction')
                response = data['janus']

                # Handle ACK
                if tx_id and response == 'ack':
                    logger.debug(f'Received ACK for transaction {tx_id}')
                    if tx_id in self._transactions:
                        tx_in = self._transactions[tx_id]
                        if tx_in['request'] == 'keepalive':
                            tx = tx_in['tx']
                            tx.set_result(data)
                            del self._transactions[tx_id]
                            logger.debug(f'Closed transaction {tx_id}'
                                         f' with {response}')
                    continue

                # Handle Success / Event / Error
                if response not in {'success', 'error'}:
                    logger.info(f'Janus Event --> {response}')
                if tx_id and tx_id in self._transactions:
                    tx_in = self._transactions[tx_id]
                    tx = tx_in['tx']
                    tx.set_result(data)
                    del self._transactions[tx_id]
                    logger.debug(f'Closed transaction {tx_id}'
                                 f' with {response}')
        except Exception:
            logger.error('WebSocket failure')
        logger.info('Connection closed')

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None
        self._transactions = {}


class JanusPlugin:
    def __init__(self, session, handle_id):
        self._session = session
        self._handle_id = handle_id

    async def sendMessage(self, message):
        logger.info('Sending message to the plugin')
        message.update({'janus': 'message', 'handle_id': self._handle_id})
        response = await self._session.send(message)
        return response


class JanusSession:
    def __init__(self, url='ws://localhost:8188/'):
        self._websocket = None
        self._url = url
        self._handles = {}
        self._session_id = None
        self._ka_interval = 15
        self._ka_task = None

    async def send(self, message):
        message.update({'session_id': self._session_id})
        response = await self._websocket.send(message)
        return response

    async def create(self):
        logger.info('Creating session')
        self._websocket = await WebSocketClient(self._url).connect()
        message = {'janus': 'create'}
        response = await self.send(message)
        assert response['janus'] == 'success'
        session_id = response['data']['id']
        self._session_id = session_id
        self._ka_task = asyncio.ensure_future(self._keepalive())
        logger.info('Session created')

    async def attach(self, plugin):
        logger.info('Attaching handle')
        message = {'janus': 'attach', 'plugin': plugin}
        response = await self.send(message)
        assert response['janus'] == 'success'
        handle_id = response['data']['id']
        handle = JanusPlugin(self, handle_id)
        self._handles[handle_id] = handle
        logger.info('Handle attached')
        return handle

    async def destroy(self):
        logger.info('Destroying session')
        if self._session_id:
            message = {'janus': 'destroy'}
            await self.send(message)
            self._session_id = None
        if self._ka_task:
            self._ka_task.cancel()
            try:
                await self._ka_task
            except asyncio.CancelledError:
                pass
            self._ka_task = None
        self._handles = {}
        logger.info('Session destroyed')

        logger.info('Closing WebSocket')
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def _keepalive(self):
        while True:
            logger.info('Sending keepalive')
            message = {'janus': 'keepalive'}
            await self.send(message)
            logger.info('Keepalive OK')
            await asyncio.sleep(self._ka_interval)


async def run(pc, player, session, display:str, bitrate=512000, record=False ):
    @pc.on('track')
    def on_track(track):
        logger.info(f'Track {track.kind} received')
        @track.on('ended')
        def on_ended():
            print(f'Track {track.kind} ended')

    @pc.on('iceconnectionstatechange')
    def on_ice_state_change():
        # logger.info(f'ICE state changed to {pc.iceConnectionState}')
        pass

    await session.create()

    # configure media

    media = {'audio': False, 'video': False}
    if player:
        if player.audio:
            media['audio'] = True
            pc.addTrack(player.audio)
        else:
            pc.addTrack(AudioStreamTrack())
        if player.video:
            media['video'] = True
            pc.addTrack(player.video)
        else:
            pc.addTrack(VideoStreamTrack())
    else:
        #media['audio'] = True
        #pc.addTrack(PassthroughMediaStreamTrack("packets_audio.rtp"))
        media['video'] = True
        pc.addTrack(PassthroughMediaStreamTrack("packets_video.rtp"))


    # attach to echotest plugin
    plugin = await session.attach('janus.plugin.videoroom')

    # send offer
    await pc.setLocalDescription(await pc.createOffer())
    request = {'record': record, 'bitrate': bitrate,
                "ptype": "publisher",
                "request": "join",
                "display": display,
                "room": 1234,
                }
    request.update(media)

    print (pc.localDescription.sdp)
    response = await plugin.sendMessage(
        {
            'body': request,
            'jsep': {
                'sdp': pc.localDescription.sdp,
                'trickle': False,
                'type': pc.localDescription.type,
            },
        }
    )
   # assert response['plugindata']['data']['result'] == 'ok'

    # apply answer
    answer = RTCSessionDescription(
        sdp=response['jsep']['sdp'],
        type=response['jsep']['type']
    )
    await pc.setRemoteDescription(answer)

    logger.info('Running for a while...')
    await asyncio.sleep(500)

    # Check WebSocket status
    assert session._websocket.connection.open

    # Get RTC stats and check the status
    rtcstats = await pc.getStats()
    rtp = {'audio': {'in': 0, 'out': 0}, 'video': {'in': 0, 'out': 0}}
    dtls_state = None
    for stat in rtcstats.values():
        if stat.type == 'inbound-rtp':
            rtp[stat.kind]['in'] = stat.packetsReceived
        elif stat.type == 'outbound-rtp':
            rtp[stat.kind]['out'] = stat.packetsSent
        elif stat.type == 'transport':
            dtls_state = stat.dtlsState
    # ICE succeded
    assert pc.iceConnectionState == 'completed'
    # DTLS succeded
    assert dtls_state == 'connected'
    # Janus echoed the sent packets
    assert rtp['audio']['out'] >= rtp['audio']['in'] > 0
    assert rtp['video']['out'] >= rtp['video']['in'] > 0

    logger.info('Ending the test now')


async def single_test(url, file, name):
    # create signaling and peer connection
    session = JanusSession(url)
    pc = RTCPeerConnection()
    player = None

    if False and file:
        player = MediaPlayer(file)

    await run(pc, player, session, display=name)
    pc.close()
    session.destroy()



async def test(url, playfrom, count):
    tests_tasks = []
    for i in range(count):
        tests_tasks.append(asyncio.Task(single_test(url, playfrom,f"user-{i}")))
    await asyncio.gather(*tests_tasks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Janus')
    parser.add_argument('url',
                        help='Janus root URL, e.g. ws://localhost:8188/')
    parser.add_argument('--play-from',
                        help='Read the media from a file and sent it.'),
    parser.add_argument('--verbose', '-v', action='count')
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(test(args.url, args.play_from, 1))
        logger.info('Test Passed')
        sys.exit(0)
    except Exception:
        logger.exception('Test Failed')
        sys.exit(1)
    finally:
        sys.exit(0)