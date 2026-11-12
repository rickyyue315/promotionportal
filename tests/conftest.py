"""Test environment setup.

Must run before any `app.*` import, because DATA_DIR and the passwords are read
from the environment at module import time.
"""

import os
import shutil
from pathlib import Path

DATA = Path(__file__).parent / "_data"
if DATA.exists():
    shutil.rmtree(DATA)
DATA.mkdir(parents=True, exist_ok=True)

os.environ["DATA_DIR"] = str(DATA)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_PASSWORD"] = "adminpw"
os.environ["USER_PASSWORD"] = "userpw"
os.environ["COOKIE_SECURE"] = "0"
