#!/usr/bin/env python3
"""Print how many windows still need work (pending, claimed, or failed and not yet given up on), for the workflow's restart step."""
import sys

from scrape_hub_client import list_queue

MAX_ATTEMPTS = 40   # keep in step with the hub's RETRY_MAX_ATTEMPTS


def remaining(project):
    failed = [i for i in list_queue(project, "failed") if i.get("attempts", 0) < MAX_ATTEMPTS]
    return len(list_queue(project, "pending")) + len(list_queue(project, "claimed")) + len(failed)


if __name__ == "__main__":
    print(remaining(sys.argv[1] if len(sys.argv) > 1 else "eprocure-tenders"))
