# TODO figure out what this value could be
MAX_RESPONSE_LENGTH = 2048

# the port number that our client will be listening on
# necessary to be able to download a file from other instances of our client
CLIENT_PORT_NUM = 6881

BLOCK_SIZE = pow(2, 14)

# timeouts are in seconds
HANDSHAKE_TIMEOUT = 10.0
# SHOULD be 120 seconds, worry about it tomorrow
# keep-alive?
DEFAULT_TIMEOUT = 5.0

PIECE_DIR = "./pieces"
