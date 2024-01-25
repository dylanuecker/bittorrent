from enum import Enum, auto
from socket import socket, AF_INET, SOCK_STREAM, MSG_WAITALL
from constants import *
from bitmap import BitMap
import math
import hashlib
from debug import *
from abc import abstractmethod

class IncorrectResponse(Exception):
    pass

class ZeroBytesRecieved(IncorrectResponse):
    pass

# keep-alive message does not have a message id
class PeerMessage(Enum):
    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE =  4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8
    PORT = 9
    HANDSHAKE = 19
    KEEP_ALIVE = auto()

    @property
    def length(self):
        if self is PeerMessage.KEEP_ALIVE:
            return 0
        elif self is PeerMessage.CHOKE:
            return 1
        elif self is PeerMessage.UNCHOKE:
            return 1
        elif self is PeerMessage.INTERESTED:
            return 1
        elif self is PeerMessage.NOT_INTERESTED:
            return 1
        elif self is PeerMessage.HAVE:
            return 5
        elif self is PeerMessage.BITFIELD:
            return 1
        elif self is PeerMessage.REQUEST:
            return 13
        elif self is PeerMessage.PIECE:
            return 1
        elif self is PeerMessage.CANCEL:
            return 13
        elif self is PeerMessage.PORT:
            return 3
        elif self is PeerMessage.HANDSHAKE:
            return 68
        else:
            PeerMessage.print_invalid_error()

    def __str__(self):
        if self is PeerMessage.KEEP_ALIVE:
            return "Keep Alive"
        elif self is PeerMessage.CHOKE:
            return "Choke"
        elif self is PeerMessage.UNCHOKE:
            return "Unchoke"
        elif self is PeerMessage.INTERESTED:
            return "Interested"
        elif self is PeerMessage.NOT_INTERESTED:
            return "Not Interested"
        elif self is PeerMessage.HAVE:
            return "Have"
        elif self is PeerMessage.BITFIELD:
            return "Bitfield"
        elif self is PeerMessage.REQUEST:
            return "Request"
        elif self is PeerMessage.PIECE:
            return "Piece"
        elif self is PeerMessage.CANCEL:
            return "Cancel"
        elif self is PeerMessage.PORT:
            return "Ports"
        elif self is PeerMessage.HANDSHAKE:
            return "Handshake"
        else:
            PeerMessage.print_invalid_error()

    @staticmethod
    def print_invalid_error():
        error_msg = "Trying to create a message with an invalid message id"
        if (DEBUG):
            print(error_msg)

class BasePeer:
    def __init__(self, peer_id, ip, port, tracker, this_bitmap, sock: socket):
        self.id = peer_id
        self.ip = ip
        self.port = port
        self.tracker = tracker
        self.this_bitmap: BitMap = this_bitmap # this client's bitmap
        self.sock = sock

        # Should be using these two for downloading (from this peer)
        self.am_interested = False   # this client is interested in the peer
        self.peer_choking = True     # peer is choking this client

        # Should be using these two for uploading (to this peer)
        self.peer_interested = False # peer is interested in this client
        self.am_choking = True       # this client is choking the peer

    @abstractmethod
    def do_handshake(self, tracker):
        pass

    def send_message(self, msg_type: PeerMessage, payload=None) -> bytes:
        """
        * Messages are of the form <length prefix><message ID><payload>
        * The length prefix is a four byte big-endian value
        * The message id is a single decimal byte (so endianness doesn't matter)
        * The payload is message dependent
        * look https://wiki.theory.org/BitTorrentSpecification here for specifics
            on the payload for each message
        """

        len_ = msg_type.length
        if msg_type in [PeerMessage.BITFIELD,  PeerMessage.PIECE] and payload:
            len_ += len(bytes(payload))

        msg = len_.to_bytes(4, 'big')
        msg += msg_type.value.to_bytes(1, 'big')

        if payload:
            msg += payload

        self.sock.sendall(msg)
        if (DEBUG):
            print(f"Sent {msg_type} message: {msg}")

        return 0

    def send_piece(self, piece_index: int, block_index: int, block: bytes):
        lst = [piece_index.to_bytes(4, byteorder='big'),
               block_index.to_bytes(4, byteorder='big'),
               block]
        payload = b''.join(lst)
        self.send_message(PeerMessage.PIECE, payload)

    def send_interested(self):
        self.send_message(PeerMessage.INTERESTED)
        self.am_interested = True

    def send_not_interested(self):
        self.send_message(PeerMessage.NOT_INTERESTED)
        self.am_interested = False

    def recv_all(self, num_bytes_to_receive):
        buf = b''
        while (num_bytes_to_receive != 0):
            tmp = self.sock.recv(num_bytes_to_receive, MSG_WAITALL)
            num_recv = len(tmp)
            if (num_recv == 0):
                raise ZeroBytesRecieved("Recieved 0 bytes")
            num_bytes_to_receive -= num_recv
            buf += tmp

        return buf

    def disconnect(self, print_=DEBUG):
        if not self.sock:
            return

        self.sock.close()
        self.sock = None
        self.num_times_disconnected += 1

        if (print_):
            print("Disconnected from peer")

    def __eq__(self, p1):
        return self.ip == p1.ip and self.port == p1.port

    def __str__(self):
        return f"{self.ip}:{self.port}"

class PeerSeed(BasePeer):
    def __init__(self, ip, port, this_bitmap, sock):
        super().__init__(None, ip, port, None, this_bitmap, sock)
        self.num_times_disconnected = 0

    def do_handshake(self, tracker):
        response = self.recv_all(68) # expecting to receive peer handshake response
        if(response[1:20] != b"BitTorrent protocol"):
            self.disconnect()
            return None

        self.id = response[48:68]
        payload = response[:48] + tracker.peer_id
        self.sock.sendall(payload)
        return response

class PeerDownload(BasePeer):
    def __init__(self, ip, port, tracker, bitmap):
        sock = socket(AF_INET, SOCK_STREAM)
        super().__init__(None, ip, port, tracker, bitmap, sock)
        self.peer_bitmap = BitMap(self.tracker.pieces) # bitmap current peer

        self.num_times_disconnected = 0

        self.busy = False # whether or not we are currently "downloading" a piece
        self.blocks = [b'' for _ in range(0, math.ceil(self.tracker.piece_length / BLOCK_SIZE))]
        self.received_bytes = 0
        self.curr_piece_idx = 0

    """
    using a 2 minute timeout on our recv calls, if we received a keep alive
    message this timeout then resets

    IMPORTANT: for send calls, need to close connections (and then kill that
               peer) on errors/failures so not wasting 2 minutes waiting
    """
    def recv_bytes(self):
        # TODO when does this loop end?
        while True:
            # receive length of message
            len_ = self.recv_all(4)
            len_ = int.from_bytes(len_, "big")

            # keep alive message
            if len_ == 0:
                continue

            # receive type of message
            msg_type = self.recv_all(1)
            msg_type = PeerMessage(int.from_bytes(msg_type, "big"))
            payload = self.recv_all(len_ - 1)

            if (DEBUG):
                print(f"received {msg_type} message")

            if (msg_type is PeerMessage.CHOKE):
                self.peer_choking = True
            elif (msg_type is PeerMessage.UNCHOKE):
                self.peer_choking = False
                # should this then trigger something else to happen, or
                # check for when peer_choking is false and go from there?
            elif (msg_type is PeerMessage.INTERESTED):
                self.peer_interested = True
            elif (msg_type is PeerMessage.NOT_INTERESTED):
                self.peer_interested = False
            elif (msg_type is PeerMessage.HAVE):
                piece_idx = int.from_bytes(payload, "big")
                # XXX check if piece_idx is converted to an int properly
                self.peer_bitmap.update_piece_have(piece_idx)
            elif (msg_type is PeerMessage.BITFIELD):
                self.peer_bitmap.update_from_bytes(payload)
                if BITFIELD_DEBUG:
                    print("peers bitfield")
                    print(str(self.peer_bitmap))
                    print("our bitfield")
                    print(str(self.this_bitmap))
                    print(f"length of peer bitmap {len(self.peer_bitmap.map)}")
                    print(f"length of our bitmap {len(self.this_bitmap.map)}")
                    print(f"length of payload {len(payload)} payload {payload}")
                    print("received bitfield")
                    print(payload)
            elif (msg_type is PeerMessage.REQUEST):
                # NOTE: don't have to implement for downloading (from this peer)
                # BUT will need to for uploading (to this peer)
                print(f"received request from connected peer")
                piece_idx = int.from_bytes(payload[0:4], "big")
                block_idx = int.from_bytes(payload[4:8], "big")
                length = int.from_bytes(payload[8:12], "big")
                if self.this_bitmap.map[piece_idx].is_status_have():
                  try:
                    with open(f"{PIECE_DIR}/{piece_idx}", mode="rb") as f :
                      f.seek(block_idx)
                      data = f.read(length)
                      self.send_piece(piece_idx,block_idx,data)

                  except EnvironmentError: # parent of IOError, OSError *and* WindowsError where available
                    print(f"could not open file {piece_idx}")
                else:
                    print("REQUEST for block that bitmap does not show as HAVE")

            elif (msg_type is PeerMessage.PIECE):
                if (DEBUG):
                    print(f"received a block payload len is {len(payload)}")

                assert(len_ == 9 + BLOCK_SIZE or
                       (len_ == 9 + self.tracker.last_block_of_last_piece_length
                        and self.curr_piece_idx == self.tracker.last_piece_idx))
                piece_idx = int.from_bytes(payload[0:4], "big")
                assert(piece_idx == self.curr_piece_idx)
                block_idx = int.from_bytes(payload[4:8], "big")

                if (DEBUG):
                    print(f"    block index is {block_idx // BLOCK_SIZE}")

                self.blocks[block_idx // BLOCK_SIZE] = payload[8:]
                self.received_bytes += len_ - 9

                if self.received_bytes == self.tracker.piece_length or \
                        (self.received_bytes == self.tracker.last_piece_length
                         and self.curr_piece_idx == self.tracker.last_piece_idx):
                    # pieces is a continous string containing the individual
                    # hashes of each piece concatenated together, length must
                    # a multiple of 20 (since sha1 hashes are 20 bytes long)
                    correct_hash = self.tracker.pieces[self.curr_piece_idx]

                    # will need to test via wireshark, this is probably not correct
                    piece = b''.join(self.blocks)
                    hash_to_check = hashlib.sha1(piece).digest()

                    if correct_hash != hash_to_check:
                        # uh oh ... hashes didn't match up
                        self.this_bitmap.update_piece_dont_have(
                                self.curr_piece_idx)
                        if (DEBUG):
                            print(f"\n\n     !!!Hashes don't match!!!")
                            print(f"What the hash should be: {correct_hash}")
                            print(f"What we got: {hash_to_check}\n")
                    else: # hashes match!
                        # update_piece_have() will return true/false indicating if
                        # the piece was actually updated to have. This handles the
                        # case where two peers happened to grab the same piece at
                        # the same time by stopping a peer from writing to disk
                        # if the either the piece wasn't updated when the peer
                        # tried to update it, or if the file already exists.
                        if self.this_bitmap.update_piece_have(self.curr_piece_idx):
                            if (DEBUG):
                                print(f"**** WRITING TO FILE {self.curr_piece_idx}****")

                            try:
                                with open(f"{PIECE_DIR}/{piece_idx}", 'wb') as file:
                                    for block in self.blocks:
                                        file.write(block)
                                        file.flush()

                                pad = str(self.curr_piece_idx).rjust(6, " ")
                                completion = self.this_bitmap.percent_complete()
                                print(f"Downloaded piece {pad} ({completion}%)\r", end="")

                                self.tracker.justDownloadedAmt(self.received_bytes)
                            except FileExistsError:
                                if (DEBUG):
                                    print(f"another peer already completed piece {piece_idx}!")

                    self.busy = False # so can move onto next piece later

            elif (msg_type is PeerMessage.CANCEL):
                pass # don't have to implement
            elif (msg_type is PeerMessage.PORT):
                pass # don't have to implement
            else:
                print(f"{msg_type} is not a valid message type")
                break

            if not (self.peer_choking or self.busy) and self.am_interested:
                piece = self.this_bitmap.get_piece_to_download(self.peer_bitmap)
                if piece is None:
                    if DEBUG:
                        print("None was returned for piece")
                    continue # TODO: figure out what we want to do

                self.busy = True
                # don't need to reset self.blocks, will just get overwritten
                self.received_bytes = 0 # reset
                self.curr_piece_idx = piece.index

                # RESET god what a bug
                self.blocks = [b'' for _ in range(0, math.ceil(self.tracker.piece_length / BLOCK_SIZE))]

                if self.curr_piece_idx == self.tracker.last_piece_idx:
                    size = self.tracker.last_piece_length
                else:
                    size = self.tracker.piece_length

                for block_idx in range(0, size, BLOCK_SIZE):
                    if block_idx + BLOCK_SIZE > size:
                        self.send_request(piece.index, block_idx,
                                          self.tracker.last_block_of_last_piece_length)
                    else:
                        self.send_request(piece.index, block_idx, BLOCK_SIZE)

            if self.this_bitmap.all_pieces_received():
                self.disconnect()
                return

    def do_handshake(self, tracker) -> None:
        pstr_len = b'\x13'
        pstr = b'BitTorrent protocol'
        reserved_bytes = b'\x00\x00\x00\x00\x00\x00\x00\x00'
        payload = pstr_len + pstr + reserved_bytes + tracker.info_hash + tracker.peer_id

        self.sock.connect((self.ip, self.port)) # connect to peer
        self.sock.sendall(payload) # send handshake to peer
        handshake_resp = self.recv_all(68) # expecting to receive peer handshake response
        recv_msg_type = int.from_bytes(handshake_resp[0:1], "big")
        recv_protocol = handshake_resp[1:20].decode()
        recv_reservedf_bits = handshake_resp[20:28]
        recv_info_hash = handshake_resp[28:48]
        recv_peer_id = handshake_resp[48:]

        if (tracker.info_hash != b'' and tracker.info_hash != recv_info_hash):
            err_msg =  f"received incorrect info hash: expected {tracker.info_hash} got {recv_info_hash}"
            raise IncorrectResponse(err_msg)

        if (DEBUG):
            print(f"received {PeerMessage(recv_msg_type)}")
            # print(f"protocol: {recv_protocol}") #this should be BitTorent protocol
            # print(f"reserved: {recv_reservedf_bits}") #this should be reserved
            # print(f"info hash: {recv_info_hash}") # check that matches info hash
            # print(f"peer id: {recv_peer_id}\n") # peer id of the new peer

        # looks like the handshake response is the same 68 bytes that we send out. The difference is the
        # peer ID of the response will be one generated by the peer, and probably(?) is not the one we sent.
        # don't know if we need to store peer ID

    def send_request(self, piece_index: int, block_index: int, length: int):
        lst = [piece_index.to_bytes(4, byteorder='big'),
               block_index.to_bytes(4, byteorder='big'),
               length.to_bytes(4, byteorder='big')]
        payload = b''.join(lst)
        self.send_message(PeerMessage.REQUEST, payload)
