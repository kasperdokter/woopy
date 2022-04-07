import asyncio
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Iterable

import requests
import websockets.client
import websockets.exceptions

logger = logging.getLogger(__name__)

def _get_signature(time_ms, woo_secret, **sorted_params):
    query_string = '&'.join(f'{key}={value}' for key, value in sorted_params.items())
    msg = f"{query_string}|{time_ms}"
    bytes_key = bytes(woo_secret, "utf-8")
    bytes_msg = bytes(msg, "utf-8")
    return hmac.new(bytes_key, msg=bytes_msg, digestmod=hashlib.sha256).hexdigest().upper()

def _get_headers(woo_key, woo_secret, **params) -> Dict[str, str]:
    sorted_params = {key: value for key, value in sorted(params.items())}
    time_ms = int(time.time() * 1000)
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "x-api-key": woo_key,
        "x-api-signature": _get_signature(time_ms, woo_secret, **sorted_params),
        "x-api-timestamp": str(time_ms),
    }

def _get_auth_message(woo_key, woo_secret):
    time_ms = int(time.time() * 1000)
    return json.dumps({
        'event': 'auth',
        'params': {
            "apikey": woo_key,
            "sign": _get_signature(time_ms, woo_secret),
            "timestamp": str(time_ms)
        }
    })

def _headers(url, woo_key=None, woo_secret=None, **params):
    if 'public' in url:
        return {}
    if woo_key is None:
        raise ValueError(f'The API Key is required for the private endpoint {url}.')
    if woo_secret is None:
        raise ValueError(f'The API Secret is required for the private endpoint {url}.')
    return _get_headers(woo_key, woo_secret, **params)

def get(url, woo_key=None, woo_secret=None, **params):
    """ Send an authenticated GET request to the given url. """
    return requests.get(url, params=params, headers=_headers(url, woo_key, woo_secret, **params))

def post(url, woo_key, woo_secret, **params):
    """ Send an authenticated POST request to the given url. """
    return requests.post(url, params=params, headers=_headers(url, woo_key, woo_secret, **params))

def delete(url, woo_key, woo_secret, **params):
    """ Send an authenticated DELETE request to the given url. """
    return requests.delete(url, params=params, headers=_headers(url, woo_key, woo_secret, **params))

async def _listener(url, topics: Iterable[str], msg_queue: queue.Queue, woo_key=None, woo_secret=None):
    while True:
        try:
            async for websocket in websockets.client.connect(url, close_timeout=0.001):
                try:
                    if 'private' in url:
                        if woo_key is None:
                            raise ValueError(f'The API Key is required for the private endpoint {url}.')
                        if woo_secret is None:
                            raise ValueError(f'The API Secret is required for the private endpoint {url}.')
                        await websocket.send(_get_auth_message(woo_key, woo_secret))
                    for topic in topics:
                        await websocket.send(json.dumps({'topic': topic, 'event': 'subscribe'}))
                    while True:
                        msg = await websocket.recv()
                        obj = json.loads(msg)
                        if isinstance(obj, dict) and obj.get('event') == 'ping':
                            await websocket.send(json.dumps({'event': 'pong'}))
                        else:
                            msg_queue.put(obj)
                except websockets.exceptions.ConnectionClosed as cc:
                    logger.warning(f'Connection at {url} closed: {cc}')
                    continue
        except Exception:
            logger.warning(f'Restarting after unexpected exception:', exc_info=True)

async def _all_listeners(topics_by_url: Dict[str, Iterable[str]], msg_queue, woo_key=None, woo_secret=None):
    tasks = [_listener(url, topics, msg_queue, woo_key, woo_secret) for url, topics in topics_by_url.items()]
    await asyncio.gather(*tasks)
            
def receive(topics_by_url: Dict[str, Iterable[str]], woo_key=None, woo_secret=None) -> Iterable[Dict[str, Any]]:
    """ Iterates over all incoming message on the registered topics. This method
    starts a worker thread that runs an event loop that executes one listener
    task per url. This worker automatically restarts in case of an exception. """
    msg_queue = queue.Queue(-1)
    task = _all_listeners(topics_by_url, msg_queue, woo_key, woo_secret)
    worker = threading.Thread(target=asyncio.run, args=(task,), daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            yield msg_queue.get(timeout=1)
        except queue.Empty:
            continue
