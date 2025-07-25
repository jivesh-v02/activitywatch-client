import logging
import os
import platform
import socket
import getpass
from datetime import datetime, timedelta, timezone
from time import sleep

from aw_client import ActivityWatchClient
from aw_core.models import Event

from .config import load_config

system = platform.system()

if system == "Windows":
    # noreorder
    from .windows import seconds_since_last_input  # fmt: skip
elif system == "Darwin":
    # noreorder
    from .macos import seconds_since_last_input  # fmt: skip
elif system == "Linux":
    # noreorder
    from .unix import seconds_since_last_input  # fmt: skip
else:
    raise Exception(f"Unsupported platform: {system}")


logger = logging.getLogger(__name__)
td1ms = timedelta(milliseconds=1)


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


class Settings:
    def __init__(self, config_section, timeout=None, poll_time=None):
        # Time without input before we're considering the user as AFK
        self.timeout = timeout or config_section["timeout"]
        # How often we should poll for input activity
        self.poll_time = poll_time or config_section["poll_time"]

        assert self.timeout >= self.poll_time


class AFKWatcher:
    def __init__(self, args, testing=False):
        # Read settings from config
        self.settings = Settings(
            load_config(testing), timeout=args.timeout, poll_time=args.poll_time
        )

        # Create a custom client with unique hostname
        unique_id = get_unique_identifier()
        self.client = ActivityWatchClient(
            "aw-watcher-afk", host=args.host, port=args.port, testing=testing
        )
        self.client.client_hostname = unique_id
        # Use unique bucket name that includes IP, username, and hostname only once
        self.bucketname = f"{self.client.client_name}_{unique_id}"

    def ping(self, afk: bool, timestamp: datetime, duration: float = 0):
        data = {"status": "afk" if afk else "not-afk"}
        e = Event(timestamp=timestamp, duration=duration, data=data)
        pulsetime = self.settings.timeout + self.settings.poll_time
        self.client.heartbeat(self.bucketname, e, pulsetime=pulsetime, queued=True)

    def run(self):
        logger.info("aw-watcher-afk started")

        # Initialization
        self.client.wait_for_start()

        eventtype = "afkstatus"
        try:
            self.client.create_bucket(self.bucketname, eventtype)
        except Exception as e:
            import requests
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                status = e.response.status_code
                msg = e.response.text
                if status in (409, 304) or (status == 400 and "already exists" in msg.lower()):
                    logger.info(f"Bucket already exists: {self.bucketname}, continuing to send heartbeats.")
                elif status == 400:
                    if "schema" in msg.lower():
                        logger.error(f"Bucket schema mismatch for {self.bucketname}: {msg}\nPlease delete the bucket on the server and restart the watcher.")
                    else:
                        logger.error(f"Bad request when creating bucket {self.bucketname}: {msg}")
                    raise
                else:
                    logger.error(f"Failed to create bucket: {e} (status {status})")
                    raise
            else:
                logger.error(f"Failed to create bucket: {e}")
                raise

        # Start afk checking loop
        with self.client:
            self.heartbeat_loop()

    def heartbeat_loop(self):
        afk = False
        while True:
            try:
                if system in ["Darwin", "Linux"] and os.getppid() == 1:
                    # TODO: This won't work with PyInstaller which starts a bootloader process which will become the parent.
                    #       There is a solution however.
                    #       See: https://github.com/ActivityWatch/aw-qt/issues/19#issuecomment-316741125
                    logger.info("afkwatcher stopped because parent process died")
                    break

                now = datetime.now(timezone.utc)
                seconds_since_input = seconds_since_last_input()
                last_input = now - timedelta(seconds=seconds_since_input)
                logger.debug(f"Seconds since last input: {seconds_since_input}")

                # If no longer AFK
                if afk and seconds_since_input < self.settings.timeout:
                    logger.info("No longer AFK")
                    self.ping(afk, timestamp=last_input)
                    afk = False
                    # ping with timestamp+1ms with the next event (to ensure the latest event gets retrieved by get_event)
                    self.ping(afk, timestamp=last_input + td1ms)
                # If becomes AFK
                elif not afk and seconds_since_input >= self.settings.timeout:
                    logger.info("Became AFK")
                    self.ping(afk, timestamp=last_input)
                    afk = True
                    # ping with timestamp+1ms with the next event (to ensure the latest event gets retrieved by get_event)
                    self.ping(
                        afk, timestamp=last_input + td1ms, duration=seconds_since_input
                    )
                # Send a heartbeat if no state change was made
                else:
                    if afk:
                        # we need the +1ms here too, to make sure we don't "miss" the last heartbeat
                        # (if last_input hasn't changed)
                        self.ping(
                            afk,
                            timestamp=last_input + td1ms,
                            duration=seconds_since_input,
                        )
                    else:
                        self.ping(afk, timestamp=last_input)

                sleep(self.settings.poll_time)

            except KeyboardInterrupt:
                logger.info("aw-watcher-afk stopped by keyboard interrupt")
                break
