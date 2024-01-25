import sys
from tracker import Tracker
from peer import PeerDownload, PeerSeed, PeerMessage, IncorrectResponse
import concurrent.futures
from socket import socket, AF_INET, SOCK_STREAM, MSG_WAITALL, timeout
from constants import *
from debug import *
import os
import glob
import shutil
from datetime import datetime
import re
import signal

usage = "Usage: python client.py <path of .torrent file> --already-downloaded"

if len(sys.argv) != 2 and len(sys.argv) != 3:
    sys.exit(usage)

already_downloaded = len(sys.argv) == 3 and sys.argv[2] == "--already-downloaded"

this_tracker = Tracker(sys.argv[1], already_downloaded)
bitmap = this_tracker.bitmap

# ctrl-c need to send cleanup message to tracker
signal.signal(signal.SIGINT, this_tracker.close_tracker)

def peer_procedure(peer: PeerDownload):
    # we want a shorter timout for the handshake
    while not bitmap.all_pieces_received():
        try:
            peer.sock.settimeout(HANDSHAKE_TIMEOUT)
            peer.do_handshake(this_tracker)
            peer.send_interested()
            peer.sock.settimeout(DEFAULT_TIMEOUT)
            peer.recv_bytes()
        except (IncorrectResponse, timeout, ConnectionError, OSError):
            # restart connection if any above errors occur
            peer.disconnect()
            if peer.num_times_disconnected >= 3:
                return
            peer.sock = socket(AF_INET, SOCK_STREAM)
        except Exception as e:
            # if any other exceptions occur, then something bad happened.
            # print it out, but try restarting connection with peer
            print(f"ERROR: {e}")
            peer.disconnect()
            if peer.num_times_disconnected >= 3:
                return
            peer.sock = socket(AF_INET, SOCK_STREAM)

def compile(filenames):
    # sort list of filenames in ascending order, based on the name of the file
    # converted to an integer.
    key = lambda filen: int(re.search(r'\/(\d+)', filen).group(1))
    filenames.sort(key=key)
    with open(f'./{this_tracker.name.decode()}', 'wb') as outfile:
        for fname in filenames:
            with open(fname, 'rb') as infile:
                for line in infile:
                    outfile.write(line)

def download():
    try:
        os.mkdir(PIECE_DIR)
    except FileExistsError:
        shutil.rmtree(PIECE_DIR)
        os.mkdir(PIECE_DIR)

    if os.path.exists(this_tracker.name):
        os.remove(this_tracker.name)

    print("Downloading...")
    t_start = datetime.now()

    if SINGLE_PEER_DEBUG_MODE:
        peer_procedure(this_tracker.peers[9])
        print("Terminating...")
        exit(1)

    # create a threadpool that does peer_procedure() for each Peer in this_tracker.peers
    completed_peers = []
    with concurrent.futures.ThreadPoolExecutor(thread_name_prefix="peer_") as exe_:
        for peer in this_tracker.peers:
            completed_peers.append(exe_.submit(peer_procedure, peer))

        concurrent.futures.wait(completed_peers, return_when="ALL_COMPLETED")

    t_diff = datetime.now() - t_start
    filenames = glob.glob(f"{PIECE_DIR}/*")
    print(f"Downloaded {len(filenames)}/{len(this_tracker.pieces)} pieces in {t_diff.seconds} seconds")

    if not bitmap.all_pieces_received() or this_tracker.left != 0:
        print("Something went wrong and the peers think they are done")
        print(bitmap)
        exit(1)

    print("Beginning compiliation...")
    t_start = datetime.now() # want to get compilation time too
    compile(filenames)
    t_diff = datetime.now() - t_start
    print(f"Compilation complete ({t_diff.seconds}s)")
    print(f"Successfully downloaded {this_tracker.name.decode()}")

    shutil.rmtree(PIECE_DIR)

    this_tracker.completed_tracker() # tell the tracker we are done downloading
    print(f"Sent completed event to tracker")
    print(f"Acting as a seeder to other peers now")

# respond to messages sent by a peer
def client_handler(peer: PeerSeed):
    print(f"client accecpted a new connection {peer}")

    if not peer.do_handshake(this_tracker):
        print("Handshake failed, client dropped")
        return

    peer.send_message(PeerMessage.BITFIELD, bitmap.to_bytes())
    peer.send_message(PeerMessage.UNCHOKE)

    while True:
        try:
            len_ = peer.sock.recv(4, MSG_WAITALL)
        except ConnectionResetError:
            print("client disconnected")
            exit(0)

        len_ = int.from_bytes(len_, "big")

        # keep alive message
        if len_ == 0:
            continue

        # receive type of message
        msg_type = peer.sock.recv(1, MSG_WAITALL)
        msg_type = PeerMessage(int.from_bytes(msg_type, "big"))
        payload = peer.sock.recv(len_ - 1, MSG_WAITALL)
        if (msg_type is PeerMessage.CHOKE):
            peer.peer_choking = True
        elif (msg_type is PeerMessage.UNCHOKE):
            peer.peer_choking = False
            # should this then trigger something else to happen, or
            # check for when peer_choking is false and go from there?
        elif (msg_type is PeerMessage.INTERESTED):
            peer.peer_interested = True
        elif (msg_type is PeerMessage.NOT_INTERESTED):
            peer.peer_interested = False
        elif (msg_type is PeerMessage.HAVE):
            pass
        elif (msg_type is PeerMessage.BITFIELD):
            pass
        elif (msg_type is PeerMessage.REQUEST):
            # NOTE: don't have to implement for downloading (from this peer)
            # BUT will need to for uploading (to this peer)
            piece_idx = int.from_bytes(payload[0:4], "big")
            block_idx = int.from_bytes(payload[4:8], "big")
            length = int.from_bytes(payload[8:12], "big")

            try:
                with open(f'./{this_tracker.name.decode()}', mode="rb") as f :
                    f.seek(piece_idx * this_tracker.piece_length + block_idx)
                    data = f.read(length)
                    peer.send_piece(piece_idx,block_idx,data)
            except OSError:
                print(f"could not open file {this_tracker.name.decode()}")
                exit(1)
        elif (msg_type is PeerMessage.PIECE):
            pass
        elif (msg_type is PeerMessage.CANCEL):
            pass # don't have to implement
        elif (msg_type is PeerMessage.PORT):
            pass # don't have to implement
        else:
            print(f"{msg_type} is not a valid message type")
            break

def main():
    # import here to avoid circular import errors
    import threading
    if not already_downloaded:
        t0 = threading.Thread(target=download, name="download")
        t0.start()
        t0.join()

        if not bitmap.all_pieces_received():
            exit(0)
    else:
        print(f"Already downloaded file; will only seed")

    with socket(AF_INET, SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", CLIENT_PORT_NUM))
        sock.listen()
        print("Client is listening")

        active_threads = []
        while True:
            peer_sock, addrinfo = sock.accept()
            ip, port = addrinfo
            peer = PeerSeed(ip, port, bitmap, peer_sock)

            kwargs = {"target": client_handler,
                      "args": (peer,),
                      "name": f"seed_{port}"
                    }

            t = threading.Thread(**kwargs)
            t.start()
            active_threads.append(t)

if __name__ == '__main__':
    main()
