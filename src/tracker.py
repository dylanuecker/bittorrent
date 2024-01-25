import re
import hashlib
from socket import socket, AF_INET, SOCK_STREAM, MSG_WAITALL, inet_ntoa
from constants import CLIENT_PORT_NUM, MAX_RESPONSE_LENGTH
from bitmap import BitMap
from constants import *
import threading

import urllib.parse # only used to urlencode the GET request
import bencoder # store things as raw bytes
import random
import string

from peer import PeerDownload
from enum import Enum
from debug import *

class TrackerResponse(Enum):
    FAILURE = 0
    WARNING = 1
    INTERVAL = 2
    MIN_INTERVAL = 3
    TRACKER_ID = 4
    COMPLETE = 5
    INCOMPLETE = 6
    PEERS_DICT = 7
    PEERS_BIN = 8

class InvalidURL(Exception):
    pass

class Tracker():
    def __init__(self, filename, already_downloaded):
        with open(filename, "rb") as file:
            data = bencoder.decode(file.read())

        # these are now byte objets, not strings
        self.announce = data.get(b'announce', None)
        self.comment = data.get(b'comment', None)
        self.name = data[b'info'][b'name']
        self.file_length = data[b'info'][b'length']
        self.piece_length = data[b'info'][b'piece length']

        self.pieces = data[b'info'][b'pieces']
        self.number_of_pieces = len(self.pieces) // 20
        self.pieces = [self.pieces[(i * 20):((i * 20) + 20)] for i in range(self.number_of_pieces)]

        self.last_piece_idx = self.number_of_pieces - 1
        self.last_piece_length = self.file_length - (self.piece_length * (self.number_of_pieces - 1))
        self.last_block_of_last_piece_length = self.last_piece_length % BLOCK_SIZE

        self.peer_urls = data.get(b'url-list', None)
        self.domainname = None
        self.port = 0
        self.rest_url = None
        self.bitmap = BitMap(self.pieces, already_downloaded)

        # separate the url and port from the annouce string
        match_ = re.search(r"^http:\/\/(\S+):(\d+)(.*)", self.announce.decode())
        if not match_:
            raise InvalidURL()

        self.domainname = match_.group(1)
        self.port = int(match_.group(2))
        self.rest_url = match_.group(3)

        self.peer_id = "S1njd-".encode()
        self.peer_id += ''.join(random.choice(string.digits) for i in range(20 - len(self.peer_id))).encode()

        self.uploaded = 0
        if already_downloaded:
            self.downloaded = self.file_length
            self.left = 0
        else:
            self.downloaded = 0
            # assuming we are always in single file mode
            #   ==> note in write up at end
            self.left = self.file_length

        # use this lock when updating uploaded, downloaded, or left
        self.lock = threading.Lock()

        # hash is the value of the info key (it must still be bencoded)
        m = hashlib.sha1(bencoder.encode(data[b'info']))
        self.hex_info_hash = m.hexdigest() # this is prolly what you want
        self.info_hash = m.digest()
        self.peers = self._get_peers()

    def justDownloadedAmt(self, amt):
        with self.lock:
            self.downloaded += amt
            self.left -= amt

    def justUploadedAmt(self, amt):
        with self.lock:
            self.uploaded += amt

    def cleanup_peers(self):
        if (DEBUG):
            print("Cleaning up peers...")

        for peer in self.peers:
            peer.disconnect(False)

        if (DEBUG):
            print("Peers have all been disconnected")

    def extractPeersFromCompactResponse(self, plaintext):
        response = bencoder.decode(plaintext)

        # TODO need to handle other responses like failure reason, etc.

        peers = []

        raw_peers = response[b'peers']
        raw_peers_len = len(raw_peers)
        assert(raw_peers_len % 6 == 0)

        # using compact flag, so peers in response are multiples of 6 bytes
        #
        # First 4 bytes are the IP address and last 2 bytes are the port number.
        # All in network (big endian) notation.
        for i in range(0, raw_peers_len // 6):
            ip = inet_ntoa(raw_peers[(i * 6) : (i * 6) + 4])
            port = int.from_bytes(raw_peers[(i * 6) + 4: (i * 6) + 6], "big")
            peers.append(PeerDownload(ip, port, self, self.bitmap))

        return peers

    def extractPeersFromNoncompactResponse(self, plaintext):
        response = bencoder.decode(plaintext)

        # TODO need to handle other responses like failure reason, etc.

        peers = []

        # noncompact, so peers are a list of dictionaries
        for peer in response[b'peers']:
            # storing peer_id too although don't use it anywhere because compact
            # responses do not provide the peer_id
            peers.append(PeerDownload(peer[b'ip'], peer[b'port'], self, self.bitmap))

        return peers

    """
    Raw format of http get request:

    GET /?param1=value1&param2=value2
    Host: trackerUrl
    User-Agent: Transmission/4.0.3
    Accept: */*
    Accept-Encoding: deflate, gzip, br, zstd

    NOTE that line endings must be a carriage return followed by a new line

    ip, numwant, key, and trackerid fields are optional fields

    str(1) means that field is true

    assuming we are always in single file mode and using compact always
    read that compact is much faster and the standard nowadays

    EXAMPLE for provided debian .torrent:
    GET /announce?info_hash=mG%95%DE%E7%0A%EB%88%E0%3ES6%CA%7C%9F%CF%0A%1E%20m&peer_id=-TR4030-hjnr9icsinxp&port=51413&uploaded=0&downloaded=0&left=406847488&numwant=80&key=852532994&compact=1&supportcrypto=1&event=started HTTP/1.1
    Host: bttracker.debian.org:6969
    User-Agent: Transmission/4.0.3
    Accept: */*
    Accept-Encoding: deflate, gzip, br, zstd
    """
    def send_get_and_return_response(self, event, compact=True):
        get_request = "GET " + self.rest_url

        # add our GET request parameters
        get_request += "?info_hash="  + str(urllib.parse.quote(self.info_hash))
        get_request += "&peer_id="    + self.peer_id.decode()
        get_request += "&port="       + str(CLIENT_PORT_NUM)
        get_request += "&uploaded="   + str(self.uploaded)
        get_request += "&downloaded=" + str(self.downloaded)
        get_request += "&left="       + str(self.left)
        get_request += "&compact="
        if compact:
            get_request += str(1)
        else:
            get_request += str(0)
        get_request += "&no_peer_id=" + str(1)
        get_request += "&event="      + event
        get_request += " HTTP/1.1\r\n"

        get_request += "Host: " + self.domainname + ":" + str(self.port) + "\r\n"
        get_request += "User-Agent: njd group\r\n"
        get_request += "Accept: */*\r\n"
        get_request += "Accept-Encoding: deflate, gzip, br, zstd\r\n"
        get_request += "\r\n"

        with socket(AF_INET, SOCK_STREAM) as sock:
            sock.connect((self.domainname, self.port)) # connect to our tracker
            sock.sendall(get_request.encode()) # send get_request to tracker
            response = sock.recv(MAX_RESPONSE_LENGTH)
            return response

    def _get_peers(self):
        # default to first asking for a compact response
        response = self.send_get_and_return_response("started")

        if response == None or len(response) == 0: # fallback to noncompact response
            response = self.send_get_and_return_response("started", False)
            flag = 1
        else:
            flag = 0

        # don't care about other http headers (for now)
        # grab the bencoded dictionary containg the useful information
        plaintext = response.split("\r\n\r\n".encode())

        if flag == 0:
            return self.extractPeersFromCompactResponse(plaintext[1])
        else:
            return self.extractPeersFromNoncompactResponse(plaintext[1])

    def update_tracker(self):
        self.send_get_and_return_response("")

    def completed_tracker(self):
        self.send_get_and_return_response("completed")

    def close_tracker(self, signum=0, frame=0):
        self.send_get_and_return_response("stopped")
        self.cleanup_peers()
        exit(0)

    def __str__(self):
        return (
                f"tracker url: {self.url}\n"
                f"tracker port: {self.port}\n"
                f"announce: {self.announce}\n"
                f"comment: {self.comment}\n"
                f"file length: {self.file_length} bytes\n"
                f"piece length: {self.piece_length} bytes\n"
                f"urls ({len(self.peer_urls)}): {self.peer_urls}\n"
                f"pieces ({len(self.pieces)}): {self.pieces}"
               )
