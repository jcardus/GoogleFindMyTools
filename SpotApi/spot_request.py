#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import grpc
import os

from Auth.spot_token_retrieval import get_spot_token
from Auth.username_provider import get_username


def spot_request(api_scope: str, payload: bytes) -> bytes:
    spot_oauth_token = get_spot_token(get_username())
    timeout = float(os.getenv('SPOT_REQUEST_TIMEOUT', '30.0'))
    method = f'/google.internal.spot.v1.SpotService/{api_scope}'
    options = (
        (
            'grpc.primary_user_agent',
            'com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT',
        ),
    )
    metadata = (
        ('authorization', f'Bearer {spot_oauth_token}'),
        ('grpc-accept-encoding', 'gzip'),
    )

    with grpc.secure_channel(
        'spot-pa.googleapis.com:443',
        grpc.ssl_channel_credentials(),
        options=options,
    ) as channel:
        call = channel.unary_unary(
            method,
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            return call(payload, timeout=timeout, metadata=metadata)
        except grpc.RpcError as error:
            code = error.code()
            code_name = code.name if code is not None else 'UNKNOWN'
            detail = error.details() or 'no details'
            raise RuntimeError(
                f'Spot API {api_scope} failed with gRPC {code_name}: {detail}'
            ) from error
