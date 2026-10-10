import time
import sys

# A tighter benchmark focused purely on the receiver loop method logic
# to show how replacing sleep(1) with something more robust (like wait(1))
# improves responsiveness to stop events.

from unittest.mock import patch, MagicMock
from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient

class DummyUI:
    def warning(self, msg):
        pass
    def register_client(self, client):
        pass

def run():
    with patch('tdjson.td_create_client_id', return_value=1), \
         patch('tdjson.td_receive', side_effect=Exception("Test Error")), \
         patch('tdjson.td_execute', return_value=b'{}'), \
         patch('tdjson.td_send'):

        client = TDJsonClient(DummyUI(), "TestDevice")

        # Let the thread hit the exception block and sleep
        time.sleep(0.1)

        start = time.time()
        client.stop_event.set()

        # In current implementation, if the loop hits exception, it sleeps for 1 sec.
        # This means join() will block for up to 1 second.
        client.receiver_thread.join()
        end = time.time()

        print(f"Join took {end - start:.4f}s")
        return end - start

if __name__ == '__main__':
    run()
