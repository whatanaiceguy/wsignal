import time

from wsignal.parsing.page_worker import process_page


def slow_process_page(raw: str):
    time.sleep(30)
    return process_page(raw)
