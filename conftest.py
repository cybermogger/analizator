"""Repository-wide safety defaults applied before test modules are imported."""
import os

os.environ["ANALYZER_DISABLE_DOTENV"] = "1"
