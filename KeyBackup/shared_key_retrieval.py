#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from binascii import unhexlify

from Auth.token_cache import get_cached_value_or_set
from KeyBackup.shared_key_flow import request_shared_key_flow


def _retrieve_shared_key(driver=None):
    print("""[SharedKeyRetrieval] Google may require approval to access end-to-end encrypted keys used to decrypt location reports.
> Continuing in the authenticated Google Chrome session.
> Make that you allow Python (or PyCharm) to control Chrome (macOS only).
    """)

    if driver is None:
        input("[SharedKeyRetrieval] Press 'Enter' to continue...")

    shared_key = request_shared_key_flow(driver)
    if not shared_key:
        raise RuntimeError("Google encrypted-key approval did not return a shared key.")

    return shared_key


def get_shared_key(driver=None) -> bytes:
    return unhexlify(get_cached_value_or_set('shared_key', lambda: _retrieve_shared_key(driver)))


if __name__ == '__main__':
    print(get_shared_key())
