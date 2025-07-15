import logging
import os
import re
import signal
import subprocess
import sys
import socket
import getpass
from datetime import datetime, timezone
from time import sleep

from aw_client import ActivityWatchClient
from aw_core.log import setup_logging
from aw_core.models import Event

from .config import parse_args
from .exceptions import FatalError
from .lib import get_current_window
from .macos_permissions import background_ensure_permissions

import requests

logger = logging.getLogger(__name__)

# run with LOG_LEVEL=DEBUG
log_level = os.environ.get("LOG_LEVEL")
if log_level:
    logger.setLevel(logging.__getattribute__(log_level.upper()))


def get_unique_identifier():
    """
    Generate a unique identifier that includes IP address, username, and hostname
    to avoid conflicts when multiple users run ActivityWatch on the same device.
    """
    try:
        # Get local IP address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "unknown-ip"
    
    # Get current username
    try:
        username = getpass.getuser()
    except:
        username = "unknown-user"
    
    # Get hostname
    try:
        hostname = socket.gethostname()
    except:
        hostname = "unknown-hostname"
    
    # Create unique identifier: ip_username_hostname
    unique_id = f"{local_ip}_{username}_{hostname}"
    
    # Replace dots and other special characters that might cause issues
    unique_id = unique_id.replace(".", "-")
    
    return unique_id


def kill_process(pid):
    logger.info("Killing process {}".format(pid))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        logger.info("Process {} already dead".format(pid))


def try_compile_title_regex(title):
    try:
        return re.compile(title, re.IGNORECASE)
    except re.error:
        logger.error(f"Invalid regex pattern: {title}")
        exit(1)


def main():
    args = parse_args()

    if sys.platform.startswith("linux") and (
        "DISPLAY" not in os.environ or not os.environ["DISPLAY"]
    ):
        raise Exception("DISPLAY environment variable not set")

    setup_logging(
        name="aw-watcher-window",
        testing=args.testing,
        verbose=args.verbose,
        log_stderr=True,
        log_file=True,
    )

    if sys.platform == "darwin":
        background_ensure_permissions()

    # Create a custom client with unique hostname
    unique_id = get_unique_identifier()
    client = ActivityWatchClient(
        "aw-watcher-window", host=args.host, port=args.port, testing=args.testing
    )
    client.client_hostname = unique_id

    # Use unique bucket name that includes IP, username, and hostname only once
    bucket_id = f"{client.client_name}_{unique_id}"
    event_type = "currentwindow"

    try:
        client.create_bucket(bucket_id, event_type, queued=True)
    except Exception as e:
        import requests
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            status = e.response.status_code
            msg = e.response.text
            if status in (409, 304) or (status == 400 and "already exists" in msg.lower()):
                logger.info(f"Bucket already exists: {bucket_id}, continuing to send heartbeats.")
            elif status == 400:
                if "schema" in msg.lower():
                    logger.error(f"Bucket schema mismatch for {bucket_id}: {msg}\nPlease delete the bucket on the server and restart the watcher.")
                else:
                    logger.error(f"Bad request when creating bucket {bucket_id}: {msg}")
                raise
            else:
                logger.error(f"Failed to create bucket: {e} (status {status})")
                raise
        else:
            logger.error(f"Failed to create bucket: {e}")
            raise

    logger.info("aw-watcher-window started")
    client.wait_for_start()

    with client:
        if sys.platform == "darwin" and args.strategy == "swift":
            logger.info("Using swift strategy, calling out to swift binary")
            binpath = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "aw-watcher-window-macos"
            )

            try:
                p = subprocess.Popen(
                    [
                        binpath,
                        client.server_address,
                        bucket_id,
                        client.client_hostname,
                        client.client_name,
                    ]
                )
                # terminate swift process when this process dies
                signal.signal(signal.SIGTERM, lambda *_: kill_process(p.pid))
                p.wait()
            except KeyboardInterrupt:
                print("KeyboardInterrupt")
                kill_process(p.pid)
        else:
            heartbeat_loop(
                client,
                bucket_id,
                poll_time=args.poll_time,
                strategy=args.strategy,
                exclude_title=args.exclude_title,
                exclude_titles=[
                    try_compile_title_regex(title)
                    for title in args.exclude_titles
                    if title is not None
                ],
            )


def heartbeat_loop(
    client, bucket_id, poll_time, strategy, exclude_title=False, exclude_titles=[]
):
    while True:
        if os.getppid() == 1:
            logger.info("window-watcher stopped because parent process died")
            break

        current_window = None
        try:
            current_window = get_current_window(strategy)
            logger.debug(current_window)
        except (FatalError, OSError):
            # Fatal exceptions should quit the program
            try:
                logger.exception("Fatal error, stopping")
            except OSError:
                pass
            break
        except Exception:
            # Non-fatal exceptions should be logged
            try:
                logger.exception("Exception thrown while trying to get active window")
            except OSError:
                break

        if current_window is None:
            logger.debug("Unable to fetch window, trying again on next poll")
        else:
            for pattern in exclude_titles:
                if pattern.search(current_window["title"]):
                    current_window["title"] = "excluded"

            if exclude_title:
                current_window["title"] = "excluded"

            now = datetime.now(timezone.utc)
            current_window_event = Event(timestamp=now, data=current_window)

            event_json = current_window_event.to_json_dict()
            # Remove 'id' if None
            if event_json.get('id') is None:
                event_json.pop('id')
            logger.info(f"Sending heartbeat event (json): {event_json}")

            # Fix: client.server_address may already include http://
            base_url = client.server_address
            if not base_url.startswith("http://") and not base_url.startswith("https://"):
                base_url = f"http://{base_url}"
            url = f"{base_url}/api/0/buckets/{bucket_id}/heartbeat?pulsetime={poll_time + 1.0}"
            try:
                resp = requests.post(url, json=event_json, headers={"Content-Type": "application/json"})
                if resp.status_code >= 400:
                    logger.error(f"Raw heartbeat failed: {resp.status_code} {resp.reason} - {resp.text}")
                else:
                    logger.info(f"Raw heartbeat success: {resp.status_code} {resp.reason}")
            except Exception as e:
                logger.error(f"Raw heartbeat exception: {e}")

        sleep(poll_time)
