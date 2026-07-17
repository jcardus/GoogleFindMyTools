#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import base64
from urllib.parse import urlencode

from NovaApi.util import generate_random_uuid
from ProtoDecoders import DeviceUpdate_pb2

def get_security_domain_request_url():
    encryption_unlock_request_extras = DeviceUpdate_pb2.EncryptionUnlockRequestExtras()
    encryption_unlock_request_extras.operation = 1
    encryption_unlock_request_extras.securityDomain.name = "finder_hw"
    encryption_unlock_request_extras.securityDomain.unknown = 0
    encryption_unlock_request_extras.sessionId = generate_random_uuid()

    # Encode without the trailing newline produced by binascii.b2a_base64 and
    # URL-escape Base64 padding/special characters as a query parameter.
    serialized = encryption_unlock_request_extras.SerializeToString()
    kdi = base64.b64encode(serialized).decode('ascii')
    scope = "https://accounts.google.com/encryption/unlock/android"
    return f"{scope}?{urlencode({'kdi': kdi})}"


if __name__ == '__main__':
    print(get_security_domain_request_url())
