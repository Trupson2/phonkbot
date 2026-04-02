"""
Centralized logging for PhonkBot.
Writes to console AND logs/phonkbot.log with rotation.
"""

import os
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, 'phonkbot.log')

_logger = logging.getLogger('phonkbot')
_logger.setLevel(logging.DEBUG)

if not _logger.handlers:
    _formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s',
                                   datefmt='%Y-%m-%d %H:%M:%S')

    _console = logging.StreamHandler()
    _console.setLevel(logging.INFO)
    _console.setFormatter(_formatter)
    _logger.addHandler(_console)

    _file = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5,
                                encoding='utf-8')
    _file.setLevel(logging.DEBUG)
    _file.setFormatter(_formatter)
    _logger.addHandler(_file)


def log(msg):
    _logger.info(msg)

def log_error(msg):
    _logger.error(msg)

def log_warning(msg):
    _logger.warning(msg)

def log_debug(msg):
    _logger.debug(msg)
