.PHONY: setup test audit smoke benchmark-help

setup:
	./install.sh

test:
	python -m pytest

audit:
	python scripts/public_surface_check.py

smoke:
	python parad0x_media_engine.py --help >/dev/null
	python media_benchmark.py --help >/dev/null

benchmark-help:
	python media_benchmark.py --help
