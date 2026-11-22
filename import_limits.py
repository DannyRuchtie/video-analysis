"""Admission and resource limits for web imports behind a trusted proxy."""

from contextlib import closing
from hmac import compare_digest
from ipaddress import ip_address, ip_network
from math import ceil
from pathlib import Path
from threading import BoundedSemaphore
from time import time
import sqlite3

FREE_IMPORTS = 2
WINDOW_SECONDS = 24 * 60 * 60
MAX_PUBLIC_BYTES = 250 * 1024 ** 2
MAX_PUBLIC_DURATION = 10 * 60
JOB_SECONDS = 3 * 60
PUBLIC_QUEUE_SIZE = 3


class AdmissionError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def client_bucket(value):
    try:
        address = ip_address(value or "")
    except ValueError as error:
        raise AdmissionError(403, "The analyzer could not verify your network address.") from error
    if address.version == 6:
        return str(address.ipv4_mapped or ip_network(f"{address}/64", strict=False))
    return str(address)


class ImportLimits:
    def __init__(self, database: Path, access_code: str):
        self.database = database
        self.access_code = access_code.encode("ascii")
        self.slots = BoundedSemaphore(PUBLIC_QUEUE_SIZE)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS imports ("
                "job TEXT PRIMARY KEY, client TEXT NOT NULL, submitted REAL NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS imports_window ON imports (client, submitted)")

    def reserve(self, job, client, code):
        bucket = client_bucket(client)
        privileged = False
        if code:
            if not compare_digest(code.encode("utf-8"), self.access_code):
                raise AdmissionError(403, "The access code is incorrect.")
            privileged = True

        if not self.slots.acquire(blocking=False):
            raise AdmissionError(503, "The analyzer is busy. Try again in a moment.", retry_after=15)

        try:
            if not privileged:
                self.claim(job, bucket)
        except BaseException:
            self.slots.release()
            raise

    def claim(self, job, bucket):
        now = time()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            # Take the write lock before counting to make check-and-claim atomic.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM imports WHERE submitted <= ?", (now - WINDOW_SECONDS,))
            count, oldest = connection.execute(
                "SELECT COUNT(*), MIN(submitted) FROM imports WHERE client = ?", (bucket,)
            ).fetchone()
            if count >= FREE_IMPORTS:
                retry = max(1, ceil(oldest + WINDOW_SECONDS - now))
                raise AdmissionError(
                    429, "You've used both free analyses. Try again later or enter an access code.", retry
                )
            connection.execute("INSERT INTO imports VALUES (?, ?, ?)", (job, bucket, now))

    def cancel(self, job):
        try:
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("DELETE FROM imports WHERE job = ?", (job,))
        finally:
            self.slots.release()
