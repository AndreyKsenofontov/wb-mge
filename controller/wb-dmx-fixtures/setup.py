#!/usr/bin/env python3

from setuptools import find_namespace_packages, setup

setup(
    name="wb-dmx-fixtures",
    version="0.1.0",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    description="DMX fixtures on a Wiren Board controller via a WB-MGU DMX gateway",
    license="MIT",
    packages=find_namespace_packages(include=["wb.*"]),
)
