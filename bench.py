import time
import threading
from unittest.mock import patch, MagicMock

with patch('tdjson.td_create_client_id', return_value=1), \
     patch('tdjson.td_receive') as mock_receive, \
     patch('tdjson.td_execute', return_value=b'{}'), \
     patch('tdjson.td_send'):

    from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient

    class DummyUI:
        def warning(self, msg):
            pass
        def register_client(self, client):
            pass
        def info(self, msg):
            pass

    # We want to measure the impact of time.sleep(1) in the exception loop
    # Let's say td_receive raises exceptions repeatedly.
    def raising_receive(timeout):
        raise ValueError("test exception")

    mock_receive.side_effect = raising_receive

    start = time.time()

    client = TDJsonClient(DummyUI(), "TestDevice")

    # let it run for a bit
    time.sleep(3.1)

    client.stop_event.set()
    client.receiver_thread.join()

    end = time.time()

    print(f"Loop ran for {end - start:.2f}s")
    print(f"Exception count: {mock_receive.call_count}")
