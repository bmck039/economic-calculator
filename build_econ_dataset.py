#!/usr/bin/env python3
"""Build the dataset the simulator reads (econ_data/). See econdata/build.py for the
options, or run `python build_econ_dataset.py --help`."""

import sys

from econdata.build import main

if __name__ == "__main__":
    sys.exit(main())
