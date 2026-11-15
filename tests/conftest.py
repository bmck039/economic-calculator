"""The app keeps settings in the browser (localStorage); headless app tests have no
browser, so they start from the defaults straight away."""
import os

os.environ.setdefault("LIFEPLAN_NO_BROWSER_STORE", "1")
