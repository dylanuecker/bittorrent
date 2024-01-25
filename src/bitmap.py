import threading
from enum import Enum, auto
import random
from debug import *

class _BitMapStatus(Enum):
    DONT_HAVE = 0
    IN_PROGRESS = 1
    HAVE = auto()

class _BitMapEntry:
    def __init__(self, piece, index_, status=_BitMapStatus.DONT_HAVE):
        self.value = piece
        self.index = index_
        self.status = status
        self.lock = threading.Lock()

    def __bool__(self):
        with self.lock:
            return self.status is _BitMapStatus.HAVE

    def set_status_have(self):
        return self._set_status(_BitMapStatus.HAVE)

    def set_status_dont_have(self):
        return self._set_status(_BitMapStatus.DONT_HAVE)

    def set_status_in_progress(self):
        return self._set_status(_BitMapStatus.IN_PROGRESS)

    def _set_status(self, status):
        with self.lock:
            if self.status is status:
                return False

            self.status = status

            return True

    def is_status_have(self):
        with self.lock:
            if self.status is _BitMapStatus.HAVE:
                return True
            return False

class BitMap:
    def __init__(self, pieces, is_full=False):
        self.map = []
        self.num_have = 0
        self.have_counter_lock = threading.Lock()

        init_status = _BitMapStatus.HAVE if is_full else _BitMapStatus.DONT_HAVE
        for idx, piece in enumerate(pieces):
            self.map.append(_BitMapEntry(piece, idx, init_status))

    def update_from_bytes(self, bytes_):
        bin_ = format(int.from_bytes(bytes_, "big"), 'b')
        updated = False
        for idx, bit in enumerate(bin_):
            if int(bit):
                set_have = self.map[idx].set_status_have()

                if set_have:
                    with self.have_counter_lock:
                        self.num_have += 1
                    updated = set_have

        return updated

    def to_bytes(self):
        _bytes = b''
        i = 0
        b = 0
        for entry in self.map:
            i += 1
            b = b << 1

            if entry:
                b = b | 1
            else:
                b = b | 0

            if i == 8:
                i = 0
                _bytes += b.to_bytes(1, byteorder='big')
                b = 0

        if i != 0:
            b = b << (8 - i) # bits might not evenly divide 8
            _bytes += b.to_bytes(1, byteorder='big')

        return _bytes

    def get_piece_to_download(self, ref_bitmap):
        def entries_to_process():
            ret = [[], []]
            for entry in self.map:
                we_dont_have_entry = entry.status is not _BitMapStatus.HAVE
                peer_has_entry = ref_bitmap.map[entry.index].status is _BitMapStatus.HAVE

                if we_dont_have_entry and peer_has_entry:
                    ret[entry.status.value].append(entry)

            return ret

        dont_have, in_progress = entries_to_process()
        if len(dont_have):
            piece = random.choice(dont_have)
            piece.set_status_in_progress()
        elif len(in_progress):
            piece = random.choice(in_progress)
        else:
            return None

        return piece

    def all_pieces_received(self):
        with self.have_counter_lock:
            return self.num_have == len(self.map)

    def update_piece_have(self, piece_idx):
        set_have = self.map[piece_idx].set_status_have()

        if set_have:
            with self.have_counter_lock:
                self.num_have += 1

        return set_have

    def update_piece_in_progress(self, piece_idx):
        return self.map[piece_idx].set_status_in_progress()

    def update_piece_dont_have(self, piece_idx):
        piece = self.map[piece_idx]

        if piece.status is _BitMapStatus.HAVE:
            with self.have_counter_lock:
                self.num_have -= 1

        return piece.set_status_dont_have()

    def percent_complete(self):
        with self.have_counter_lock:
            return int((self.num_have / len(self.map)) * 100)

    def __str__(self):
        return ''.join([str(e.status.value) for e in self.map])
